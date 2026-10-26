"""
Heuristic diagnostics for mobile / tile-based GPUs.

Uses only official ReplayController APIs (textures, actions, optional GLSL scan).
"""

import renderdoc as rd

from ..utils import Helpers


_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


class DiagnosisService:
    """Mobile-risk and precision heuristics"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def diagnose_mobile_risks(self, check_categories=None, severity_filter="all"):
        """Scan the capture for precision / performance / compatibility / GPU risks"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        valid = set(["precision", "performance", "compatibility", "gpu_specific"])
        if check_categories:
            cats = set(check_categories)
            unknown = cats - valid
            if unknown:
                raise ValueError("Unknown categories: %s" % sorted(unknown))
        else:
            cats = valid

        result = {"data": None}

        def callback(controller):
            props = controller.GetAPIProperties()
            api = str(props.pipelineType)
            vendor = str(getattr(props, "vendor", ""))
            driver = ""
            try:
                driver = str(controller.GetStructuredFile().version or "")
            except Exception:
                pass
            blob = " ".join([api, vendor, driver]).upper()

            textures = list(controller.GetTextures())
            name_of = {}
            for tex in textures:
                name_of[str(tex.resourceId)] = self.ctx.GetResourceName(tex.resourceId)

            actions = Helpers.flatten_actions(controller.GetRootActions())
            rt_counts = {}
            rt_clears = {}
            discard_draws = 0
            sampled_shaders = set()

            for action in actions:
                if action.flags & rd.ActionFlags.Drawcall:
                    for o in action.outputs:
                        if o != rd.ResourceId.Null():
                            td = _tex_by_id(textures, o)
                            if td is not None:
                                key = (td.width, td.height)
                                rt_counts[key] = rt_counts.get(key, 0) + 1
                    if _shader_has_discard(controller, action, sampled_shaders):
                        discard_draws += 1
                if action.flags & rd.ActionFlags.Clear:
                    for o in action.outputs:
                        if o != rd.ResourceId.Null():
                            td = _tex_by_id(textures, o)
                            if td is not None:
                                key = (td.width, td.height)
                                rt_clears[key] = rt_clears.get(key, 0) + 1

            main_res = (0, 0)
            if rt_counts:
                main_res = max(rt_counts, key=lambda k: rt_counts[k])

            risks = []
            if "precision" in cats:
                risks.extend(_precision_risks(textures, name_of, actions))
            if "performance" in cats:
                main_clears = rt_clears.get(main_res, 0)
                if main_clears > 6:
                    risks.append({
                        "category": "performance",
                        "severity": "medium",
                        "title": "全屏 pass / Clear 过多: %d 次" % main_clears,
                        "detail": "主 RT %sx%s 被 Clear %d 次，分块 GPU 上每次都可能触发 tile store/load"
                                  % (main_res[0], main_res[1], main_clears),
                        "fix": "合并后处理 pass，减少 RT 切换和全屏 Clear",
                    })
                if discard_draws > 0:
                    risks.append({
                        "category": "performance",
                        "severity": "medium" if ("MALI" in blob or "ARM" in blob) else "low",
                        "title": "检测到 discard: %d 个 DC 的像素着色器含 discard" % discard_draws,
                        "detail": "discard 会关掉 early-Z，Mali/部分 TBR 上 overdraw 会升高",
                        "fix": "改用 alpha-to-coverage、pre-Z，或把测试推到 alpha clip 之外",
                    })
            if "compatibility" in cats:
                for tex in textures:
                    if tex.width > 4096 or tex.height > 4096:
                        rid = str(tex.resourceId)
                        risks.append({
                            "category": "compatibility",
                            "severity": "medium",
                            "title": "超大纹理: %s (%sx%s)" % (name_of.get(rid) or rid, tex.width, tex.height),
                            "detail": "部分移动设备最大纹理尺寸为 4096",
                            "fix": "降低分辨率或使用纹理流式加载",
                        })
            if "gpu_specific" in cats:
                for q in Helpers.gpu_quirks(api, vendor, driver):
                    sev = "medium" if ("mediump" in q or "discard" in q or "R11G11B10" in q) else "low"
                    risks.append({
                        "category": "gpu_specific",
                        "severity": sev,
                        "title": q.split(":")[0] if ":" in q else "GPU quirk",
                        "detail": q,
                        "fix": "按该 GPU 的注意事项检查 shader 精度、RT 格式和 discard",
                    })

            min_rank = _SEVERITY_RANK.get(severity_filter, 0)
            if severity_filter != "all":
                risks = [r for r in risks if _SEVERITY_RANK.get(r["severity"], 0) >= min_rank]

            rank = {"high": 0, "medium": 1, "low": 2}
            risks.sort(key=lambda r: (rank.get(r["severity"], 9), r["category"], r["title"]))
            result["data"] = {
                "api": api,
                "vendor": vendor,
                "main_resolution": "%sx%s" % (main_res[0], main_res[1]) if main_res[0] else "unknown",
                "risk_count": len(risks),
                "risks": risks,
            }

        self._invoke(callback)
        return result["data"]


def _tex_by_id(textures, rid):
    for tex in textures:
        if tex.resourceId == rid:
            return tex
    return None


def _precision_risks(textures, name_of, actions):
    risks = []
    written = set()
    for action in actions:
        if action.flags & rd.ActionFlags.Drawcall:
            for o in action.outputs:
                if o != rd.ResourceId.Null():
                    written.add(str(o))
            if action.depthOut != rd.ResourceId.Null():
                written.add(str(action.depthOut))

    for tex in textures:
        rid = str(tex.resourceId)
        fmt = str(tex.format.Name()).upper()
        name = name_of.get(rid) or rid
        if "R11G11B10" in fmt and rid in written:
            risks.append({
                "category": "precision",
                "severity": "high",
                "title": "R11G11B10_FLOAT 无符号位: %s" % name,
                "detail": "%s 被 DrawCall 写入。该格式没有符号位，负值会被丢掉" % rid,
                "fix": "写入前 clamp 到 [0,+inf]，或改用 R16G16B16A16_FLOAT",
            })
        if fmt.startswith("D16"):
            if rid in written:
                risks.append({
                    "category": "precision",
                    "severity": "high",
                    "title": "D16 深度精度不足: %s" % name,
                    "detail": "16-bit 深度在大 far/near 比下容易 z-fighting",
                    "fix": "改用 D24 / D32，并考虑 reversed-Z",
                })
    return risks


def _shader_has_discard(controller, action, sampled):
    """True if the pixel shader GLSL contains a discard (best-effort, GLES only)"""
    try:
        controller.SetFrameEvent(action.eventId, False)
        pipe = controller.GetPipelineState()
        shader = pipe.GetShader(rd.ShaderStage.Pixel)
        if shader == rd.ResourceId.Null():
            return False
        key = str(shader)
        if key in sampled:
            return False
        sampled.add(key)
        refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
        files = refl.debugInfo.files
        if files and files[0].contents and "discard" in str(files[0].contents):
            return True
    except Exception:
        return False
    return False
