"""
Heuristic diagnostics for mobile / tile-based GPUs.

Uses only official ReplayController APIs (textures, actions, optional GLSL scan).
"""

import re

import renderdoc as rd

from .gl_state import STATE_CALLS, summarize_state_changes, track_draws
from .gl_stream import collect_stream
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
            props = Helpers.api_props(controller)
            api = Helpers.enum_name("GraphicsAPI", props.pipelineType)
            vendor = Helpers.enum_name("GPUVendor", props.vendor)
            try:
                recorded = Helpers.recorded_gpu(self.ctx.GetCaptureFilename())
            except Exception:
                recorded = ""
            family = Helpers.gpu_family(api, vendor, recorded)

            textures = list(controller.GetTextures())
            name_of = {}
            for tex in textures:
                name_of[str(tex.resourceId)] = self.ctx.GetResourceName(tex.resourceId)

            actions = Helpers.flatten_actions(controller.GetRootActions())
            # GL: the bound program of every draw follows from the recorded calls, so the shader is
            # inspected once per program instead of replaying all draws
            program_of = {}
            if api == "OpenGL":
                program_of = dict((d["eid"], d["program"]) for d in track_draws(collect_stream(controller, STATE_CALLS)))
            program_has_discard = {}
            rt_counts = {}
            rt_clears = {}
            discard_draws = 0
            sampled_shaders = {}

            for action in actions:
                if action.flags & rd.ActionFlags.Drawcall:
                    for o in action.outputs:
                        if o != rd.ResourceId.Null():
                            td = _tex_by_id(textures, o)
                            if td is not None:
                                key = (td.width, td.height)
                                rt_counts[key] = rt_counts.get(key, 0) + 1
                    program = program_of.get(int(action.eventId))
                    if program is not None and program in program_has_discard:
                        has_discard = program_has_discard[program]
                    else:
                        has_discard = _shader_has_discard(controller, action, sampled_shaders)
                        if program is not None:
                            program_has_discard[program] = has_discard
                    if has_discard:
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
                        "severity": "medium" if family == "mali" else "low",
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
                for q in Helpers.gpu_quirks(api, vendor, recorded):
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
                "recorded_gpu": recorded,
                "gpu_family": family,
                "main_resolution": "%sx%s" % (main_res[0], main_res[1]) if main_res[0] else "unknown",
                "risk_count": len(risks),
                "risks": risks,
            }

        self._invoke(callback)
        return result["data"]

    def analyze_state_changes(self, marker_filter=None, change_types=None, max_draws=200):
        """Count PSO / RT / shader changes between consecutive draws; find batchable runs"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        valid = set(["shader", "blend", "depth", "cull", "render_target"])
        if change_types:
            track = set(change_types)
            unknown = track - valid
            if unknown:
                raise ValueError("Unknown change_types: %s" % sorted(unknown))
        else:
            track = valid
        result = {"data": None}

        try:
            api = Helpers.enum_name("GraphicsAPI", self.ctx.APIProps().pipelineType)
        except Exception:
            api = ""

        def callback(controller):
            if api == "OpenGL":
                # State follows from the recorded calls: no per-draw replay (minutes on a remote device)
                draws = track_draws(collect_stream(controller, STATE_CALLS))
                summary = summarize_state_changes(draws, track, marker_filter, max_draws)
                result["data"] = _state_report(
                    summary["analyzed_draws"], summary["total_draws"], summary["change_counts"],
                    summary["unavailable"], summary["runs"], "api_stream")
                return
            sf = controller.GetStructuredFile()
            draws = []

            def walk(actions, path):
                for action in actions:
                    # read before descending (see ActionService.stat_instancing_efficiency)
                    name = action.GetName(sf)
                    children = action.children
                    is_draw = bool(action.flags & rd.ActionFlags.Drawcall)
                    child_path = path + [name] if children else path
                    if children:
                        walk(children, child_path)
                    if not is_draw:
                        continue
                    if marker_filter and not any(marker_filter in p for p in child_path + [name]):
                        continue
                    draws.append(action)

            walk(controller.GetRootActions(), [])
            change_counts = dict((t, 0) for t in track)
            saw_value = dict((t, False) for t in track)
            prev = {}
            runs = []
            current = None
            analyzed = 0
            for action in draws[: max(1, int(max_draws))]:
                analyzed += 1
                try:
                    controller.SetFrameEvent(action.eventId, False)
                    pipe = controller.GetPipelineState()
                except Exception:
                    continue
                cur = {}
                if "shader" in track:
                    try:
                        cur["shader"] = str(pipe.GetShader(rd.ShaderStage.Pixel))
                    except Exception:
                        cur["shader"] = None
                if track & set(["blend", "depth", "cull"]):
                    # PipeState has no GetColorBlend / GetDepthState / GetRasterizer; the
                    # old calls raised and every one of these fields stayed "unavailable"
                    ff = Helpers.fixed_function_state(controller, pipe)
                    values = {
                        "blend": _freeze(ff.get("blend")),
                        "depth": _freeze((ff.get("depth") or {}).get("test"),
                                         (ff.get("depth") or {}).get("func")),
                        "cull": _freeze((ff.get("rasterizer") or {}).get("cull_mode")),
                    }
                    for name in ("blend", "depth", "cull"):
                        if name in track:
                            cur[name] = values[name]
                if "render_target" in track:
                    cur["render_target"] = Helpers.action_rt_key(action)

                for field, value in cur.items():
                    if value is not None:
                        saw_value[field] = True
                changed = []
                if prev:
                    for field in track:
                        if cur.get(field) != prev.get(field):
                            change_counts[field] += 1
                            changed.append(field)
                if not changed:
                    if current is None:
                        current = {"start_event": action.eventId, "end_event": action.eventId, "count": 1}
                    else:
                        current["end_event"] = action.eventId
                        current["count"] += 1
                else:
                    if current and current["count"] >= 2:
                        runs.append(current)
                    current = {"start_event": action.eventId, "end_event": action.eventId, "count": 1}
                prev = cur
            if current and current["count"] >= 2:
                runs.append(current)
            runs.sort(key=lambda r: -r["count"])
            unavailable = [name for name in ("shader", "blend", "depth", "cull")
                           if name in track and not saw_value.get(name)]
            result["data"] = _state_report(analyzed, len(draws), change_counts, unavailable, runs, "replay")

        self._invoke(callback)
        return result["data"]


def _state_report(analyzed, total, change_counts, unavailable, runs, method):
    suggestions = []
    if change_counts.get("shader", 0) > analyzed // 3:
        suggestions.append("Shader 切换 %d 次，考虑按 shader 排序合批" % change_counts["shader"])
    if change_counts.get("render_target", 0) > 5:
        suggestions.append(
            "RT 切换 %d 次，分块 GPU 上每次都可能触发 tile store/load" % change_counts["render_target"]
        )
    if runs:
        best = runs[0]
        suggestions.append(
            "事件 %s–%s 连续 %d 个 DC 状态相同，可考虑 instancing 合并"
            % (best["start_event"], best["end_event"], best["count"])
        )
    return {
        "analyzed_draws": analyzed,
        "total_draws": total,
        "change_counts": change_counts,
        "unavailable": unavailable,
        "batching_opportunities": runs[:5],
        "suggestions": suggestions,
        "method": method,
    }


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
    """True if the pixel shader GLSL contains a discard statement (GLES only)"""
    try:
        controller.SetFrameEvent(action.eventId, False)
        pipe = controller.GetPipelineState()
        shader = pipe.GetShader(rd.ShaderStage.Pixel)
        if shader == rd.ResourceId.Null():
            return False
        key = str(shader)
        if key in sampled:
            return sampled[key]
        refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
        files = refl.debugInfo.files
        has = bool(files and files[0].contents and _glsl_has_discard(str(files[0].contents)))
        sampled[key] = has
        return has
    except Exception:
        return False


_DISCARD_RE = re.compile(r"\bdiscard\b")


def _glsl_has_discard(source):
    """Match the discard keyword, not comments or longer identifiers."""
    text = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    text = re.sub(r"//.*?$", "", text, flags=re.M)
    return _DISCARD_RE.search(text) is not None


def _freeze(*values):
    """Comparable stand-in for a state value; None when nothing could be read."""
    if all(v is None for v in values):
        return None
    if len(values) == 1 and isinstance(values[0], dict):
        return tuple(sorted(values[0].items()))
    return values
