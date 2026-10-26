"""
Performance analysis service for RenderDoc.

Everything here runs through the replay controller, so when the capture is
opened with remote replay (e.g. an Android device) the overlays, histograms
and pixel reads are executed by the device GPU, not the local PC.
"""

import math
import os
import struct

import renderdoc as rd

from ..utils import Parsers


# Uniform blocks that are folded into #defines by default. Unity puts material
# properties in UnityPerMaterial, which is what decides the runtime branch path.
DEFAULT_FOLD_BLOCKS = ["UnityPerMaterial"]


class AnalysisService:
    """Performance analysis service (coverage, shader sources, texture export)"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    # ==================== Draw selection ====================

    def _select_draws(self, controller, event_ids, marker_filter, event_id_min, event_id_max):
        """Return [(action, marker_path)] for draw calls matching the filters"""
        sf = controller.GetStructuredFile()
        wanted = set(int(e) for e in event_ids) if event_ids else None
        selected = []

        def walk(actions, path):
            for action in actions:
                name = action.GetName(sf)
                if action.children:
                    walk(action.children, path + [name])
                if not (action.flags & rd.ActionFlags.Drawcall):
                    continue
                eid = action.eventId
                if wanted is not None and eid not in wanted:
                    continue
                if event_id_min is not None and eid < event_id_min:
                    continue
                if event_id_max is not None and eid > event_id_max:
                    continue
                if marker_filter and not any(marker_filter in p for p in path):
                    continue
                selected.append((action, "/".join(path)))

        walk(controller.GetRootActions(), [])
        return selected

    def _first_output(self, action):
        for o in action.outputs:
            if o != rd.ResourceId.Null():
                return o
        if action.depthOut != rd.ResourceId.Null():
            return action.depthOut
        return None

    def _texture_names(self, controller, pipe, limit=3):
        """Names of textures bound to the pixel shader (skipping engine defaults)"""
        names = []
        try:
            for used in pipe.GetReadOnlyResources(rd.ShaderStage.Pixel):
                rid = used.descriptor.resource
                if rid == rd.ResourceId.Null():
                    continue
                name = self.ctx.GetResourceName(rid)
                if not name or name.startswith(("Unity", "TempBuffer", "_Camera")):
                    continue
                if name not in names:
                    names.append(name)
                if len(names) >= limit:
                    break
        except Exception:
            pass
        return names

    # ==================== Coverage ====================

    def get_draw_coverage(self, event_ids=None, marker_filter=None, event_id_min=None, event_id_max=None):
        """
        Per-draw pixel coverage measured with RenderDoc's depth-test overlay.
        passed = pixels that passed the depth test (shaded), failed = covered but occluded.
        Counting is done with GetHistogram so no image data is transferred.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            draws = self._select_draws(controller, event_ids, marker_filter, event_id_min, event_id_max)
            if not draws:
                result["error"] = "No draw calls matched the filter"
                return

            output = controller.CreateOutput(
                rd.CreateHeadlessWindowingData(64, 64), rd.ReplayOutputType.Texture
            )
            sub = rd.Subresource(0, 0, 0)
            entries = []
            try:
                for action, path in draws:
                    target = self._first_output(action)
                    if target is None:
                        continue
                    controller.SetFrameEvent(action.eventId, True)
                    pipe = controller.GetPipelineState()

                    disp = rd.TextureDisplay()
                    disp.resourceId = target
                    disp.overlay = rd.DebugOverlay.Depth
                    output.SetTextureDisplay(disp)
                    output.Display()
                    overlay = output.GetDebugOverlayTexID()

                    # Depth overlay: passed = green (0,1,0,1), failed = red (1,0,0,1)
                    green = controller.GetHistogram(overlay, sub, rd.CompType.Typeless, 0.0, 1.0,
                                                    (False, True, False, False))
                    red = controller.GetHistogram(overlay, sub, rd.CompType.Typeless, 0.0, 1.0,
                                                  (True, False, False, False))
                    entries.append({
                        "event_id": action.eventId,
                        "marker": path,
                        "pixel_shader": str(pipe.GetShader(rd.ShaderStage.Pixel)),
                        "render_target": str(target),
                        "passed": int(green[-1]),
                        "failed": int(red[-1]),
                        "textures": self._texture_names(controller, pipe),
                    })
            finally:
                output.Shutdown()

            result["data"] = {
                "draw_count": len(entries),
                "total_passed": sum(e["passed"] for e in entries),
                "total_failed": sum(e["failed"] for e in entries),
                "draws": entries,
            }

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    # ==================== Shader usage ====================

    def get_shader_usage(self, marker_filter=None, event_id_min=None, event_id_max=None):
        """Group draw calls by pixel shader"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None}

        def callback(controller):
            groups = {}
            for action, path in self._select_draws(controller, None, marker_filter, event_id_min, event_id_max):
                controller.SetFrameEvent(action.eventId, False)
                pipe = controller.GetPipelineState()
                ps = str(pipe.GetShader(rd.ShaderStage.Pixel))
                vs = str(pipe.GetShader(rd.ShaderStage.Vertex))
                g = groups.setdefault(ps, {
                    "pixel_shader": ps, "vertex_shader": vs, "draw_count": 0,
                    "total_primitives": 0, "sample_event_ids": [], "textures": {},
                })
                g["draw_count"] += 1
                g["total_primitives"] += (action.numIndices * max(1, action.numInstances)) // 3
                if len(g["sample_event_ids"]) < 5:
                    g["sample_event_ids"].append(action.eventId)
                for name in self._texture_names(controller, pipe):
                    g["textures"][name] = g["textures"].get(name, 0) + 1
            out = []
            for g in sorted(groups.values(), key=lambda x: -x["draw_count"]):
                g["textures"] = [n for n, _ in sorted(g["textures"].items(), key=lambda kv: -kv[1])[:5]]
                out.append(g)
            result["data"] = {"shader_count": len(out), "shaders": out}

        self._invoke(callback)
        return result["data"]

    def list_shaders(self, stage=None, top_n=50):
        """Unique shaders in the frame with draw counts and sample events"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        stages = [stage] if stage else ["vertex", "pixel", "compute"]
        result = {"data": None}

        def callback(controller):
            from ..utils import Helpers
            groups = {}
            sf = controller.GetStructuredFile()
            for action in Helpers.flatten_actions(controller.GetRootActions()):
                if not (action.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.Dispatch)):
                    continue
                controller.SetFrameEvent(action.eventId, False)
                pipe = controller.GetPipelineState()
                path = action.GetName(sf)
                for st in stages:
                    try:
                        enum = Parsers.parse_stage(st)
                    except Exception:
                        continue
                    sid = pipe.GetShader(enum)
                    if sid == rd.ResourceId.Null():
                        continue
                    key = "%s:%s" % (st, sid)
                    g = groups.setdefault(key, {
                        "stage": st,
                        "shader": str(sid),
                        "draw_count": 0,
                        "sample_event_ids": [],
                        "sample_markers": [],
                    })
                    g["draw_count"] += 1
                    if len(g["sample_event_ids"]) < 5:
                        g["sample_event_ids"].append(action.eventId)
                        g["sample_markers"].append(path)
            rows = sorted(groups.values(), key=lambda e: -e["draw_count"])
            result["data"] = {
                "shader_count": len(rows),
                "stages": stages,
                "shaders": rows[: max(1, int(top_n))],
            }

        self._invoke(callback)
        return result["data"]

    # ==================== Shader sources ====================

    def _literal(self, var):
        """GLSL literal for a scalar/vector uniform, or None if not foldable"""
        if var.members or var.rows > 1:
            return None
        n = max(1, var.columns)
        vt = var.type
        if vt in (rd.VarType.Float, getattr(rd.VarType, "Half", None)):
            vals = ["%r" % float(v) for v in var.value.f32v[:n]]
            vals = [v if ("." in v or "e" in v or "inf" in v or "nan" in v) else v + ".0" for v in vals]
            if any("inf" in v or "nan" in v for v in vals):
                return None
            return vals[0] if n == 1 else "vec%d(%s)" % (n, ", ".join(vals))
        if vt in (getattr(rd.VarType, "SInt", None), getattr(rd.VarType, "Int", None)):
            vals = [str(int(v)) for v in var.value.s32v[:n]]
            return "int(%s)" % vals[0] if n == 1 else "ivec%d(%s)" % (n, ", ".join(vals))
        if vt == rd.VarType.UInt:
            vals = ["%du" % int(v) for v in var.value.u32v[:n]]
            return vals[0] if n == 1 else "uvec%d(%s)" % (n, ", ".join(vals))
        if vt == getattr(rd.VarType, "Bool", None):
            vals = ["true" if v else "false" for v in var.value.u32v[:n]]
            return vals[0] if n == 1 else "bvec%d(%s)" % (n, ", ".join(vals))
        return None

    def _fold_defines(self, controller, pipe, refl, stage, fold_blocks):
        defines = {}
        for i, cb in enumerate(refl.constantBlocks):
            if cb.name not in fold_blocks:
                continue
            try:
                used = pipe.GetConstantBlock(stage, i, 0)
                if used.descriptor.resource == rd.ResourceId.Null():
                    continue
                variables = controller.GetCBufferVariableContents(
                    pipe.GetGraphicsPipelineObject(), refl.resourceId, stage, refl.entryPoint, i,
                    used.descriptor.resource, used.descriptor.byteOffset, used.descriptor.byteSize,
                )
            except Exception:
                continue
            for var in variables:
                lit = self._literal(var)
                if lit is not None:
                    defines[var.name] = lit
        return defines

    def _shader_source(self, controller, refl):
        """(kind, text) - GLSL source for GL/GLES, None otherwise"""
        try:
            files = refl.debugInfo.files
            if files and files[0].contents:
                return "glsl", str(files[0].contents)
        except Exception:
            pass
        return None, None

    def get_shader_sources(self, event_ids=None, marker_filter=None, event_id_min=None,
                           event_id_max=None, stage="pixel", fold_uniforms=True,
                           fold_blocks=None, overrides=None):
        """
        Shader sources for the selected draws, de-duplicated.

        With fold_uniforms, every scalar/vector uniform of the fold blocks is
        turned into a #define holding that draw's actual value and injected right
        before main(), so an offline compiler sees the branch path the material
        really takes. 'overrides' ({name: literal}) are applied on top.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        stage_enum = Parsers.parse_stage(stage)
        blocks = fold_blocks or DEFAULT_FOLD_BLOCKS
        result = {"data": None, "error": None}

        def callback(controller):
            draws = self._select_draws(controller, event_ids, marker_filter, event_id_min, event_id_max)
            if not draws:
                result["error"] = "No draw calls matched the filter"
                return
            sources = {}
            entries = []
            raw_cache = {}
            for action, path in draws:
                controller.SetFrameEvent(action.eventId, True)
                pipe = controller.GetPipelineState()
                shader = pipe.GetShader(stage_enum)
                if shader == rd.ResourceId.Null():
                    continue
                refl = pipe.GetShaderReflection(stage_enum)
                key = str(shader)
                if key not in raw_cache:
                    raw_cache[key] = self._shader_source(controller, refl)
                kind, text = raw_cache[key]
                if text is None:
                    entries.append({"event_id": action.eventId, "shader": key, "error": "source unavailable (only GLSL is supported)"})
                    continue

                defines = self._fold_defines(controller, pipe, refl, stage_enum, blocks) if fold_uniforms else {}
                if overrides:
                    defines.update(overrides)
                final = text
                if defines:
                    pos = text.find("void main(")
                    if pos < 0:
                        pos = len(text)
                    block = "".join("#define %s %s\n" % (k, v) for k, v in sorted(defines.items()))
                    final = text[:pos] + block + text[pos:]
                source_id = "%s#%x" % (key, hash(final) & 0xFFFFFFFFFFFF)
                sources[source_id] = final
                entries.append({
                    "event_id": action.eventId,
                    "marker": path,
                    "shader": key,
                    "source_id": source_id,
                    "folded_uniforms": len(defines),
                    "textures": self._texture_names(controller, pipe),
                })
            result["data"] = {
                "stage": stage,
                "language": "glsl",
                "unique_sources": len(sources),
                "sources": sources,
                "draws": entries,
            }

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    # ==================== Texture export / pixel pick ====================

    def _resolve_texture(self, controller, event_id, resource_id, target):
        if resource_id:
            rid = Parsers.parse_resource_id(resource_id)
            for tex in controller.GetTextures():
                if tex.resourceId == rid:
                    return tex
            raise ValueError("Texture not found: %s" % resource_id)
        action = self.ctx.GetAction(event_id)
        if action is None:
            raise ValueError("Event not found: %d" % event_id)
        if target == "depth":
            rid = action.depthOut
        else:
            idx = int(target.replace("color", "") or 0) if target.startswith("color") else 0
            rid = action.outputs[idx] if idx < len(action.outputs) else rd.ResourceId.Null()
        if rid == rd.ResourceId.Null():
            raise ValueError("Event %d has no %s target" % (event_id, target))
        for tex in controller.GetTextures():
            if tex.resourceId == rid:
                return tex
        raise ValueError("Target texture not found")

    def save_texture(self, output_path, event_id=None, resource_id=None, target="color0", file_type="png"):
        """Save a texture (or the current render target of an event) to a local file"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        if event_id is None and resource_id is None:
            raise ValueError("event_id or resource_id is required")

        result = {"data": None, "error": None}

        def callback(controller):
            if event_id is not None:
                controller.SetFrameEvent(int(event_id), True)
            tex = self._resolve_texture(controller, event_id, resource_id, target)
            save = rd.TextureSave()
            save.resourceId = tex.resourceId
            save.destType = Parsers.parse_file_type(file_type)
            save.mip = 0
            if tex.format.compType == rd.CompType.Depth:
                save.channelExtract = 0  # write depth as greyscale
            ok = controller.SaveTexture(save, output_path)
            # Older APIs return bool, newer ones a ResultDetails
            failed = (not ok.OK()) if hasattr(ok, "OK") else (not ok)
            if failed:
                result["error"] = "SaveTexture failed: %s" % str(ok)
                return
            result["data"] = {
                "output_path": output_path,
                "resource_id": str(tex.resourceId),
                "name": self.ctx.GetResourceName(tex.resourceId),
                "width": tex.width,
                "height": tex.height,
                "format": str(tex.format.Name()),
            }

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def pick_pixel(self, x, y, event_id=None, resource_id=None, target="color0"):
        """Read one pixel of a texture (or an event's render target) at the given event"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        if event_id is None and resource_id is None:
            raise ValueError("event_id or resource_id is required")

        result = {"data": None}

        def callback(controller):
            if event_id is not None:
                controller.SetFrameEvent(int(event_id), True)
            tex = self._resolve_texture(controller, event_id, resource_id, target)
            is_depth = tex.format.compType == rd.CompType.Depth
            comp = rd.CompType.Depth if is_depth else rd.CompType.Typeless
            v = controller.PickPixel(tex.resourceId, int(x), int(y), rd.Subresource(0, 0, 0), comp)
            result["data"] = {
                "resource_id": str(tex.resourceId),
                "format": str(tex.format.Name()),
                "x": int(x),
                "y": int(y),
                "float": list(v.floatValue),
                "uint": list(v.uintValue),
            }

        self._invoke(callback)
        return result["data"]

    def pixel_history(self, x, y, event_id=None, resource_id=None, target="color0"):
        """Which draws modified this pixel, with pass/fail reasons"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        if event_id is None and resource_id is None:
            raise ValueError("event_id or resource_id is required")
        result = {"data": None, "error": None}

        def callback(controller):
            if event_id is not None:
                controller.SetFrameEvent(int(event_id), True)
            tex = self._resolve_texture(controller, event_id, resource_id, target)
            try:
                history = controller.PixelHistory(
                    tex.resourceId, int(x), int(y), rd.Subresource(0, 0, 0), rd.CompType.Typeless
                )
            except Exception as e:
                result["error"] = "PixelHistory failed: %s" % str(e)
                return
            mods = []
            for mod in history:
                passed = True
                try:
                    passed = bool(mod.Passed())
                except Exception:
                    passed = True
                entry = {"event_id": int(mod.eventId), "passed": passed}
                if not passed:
                    reasons = []
                    for attr, label in (
                        ("backfaceCulled", "backface_culled"),
                        ("depthTestFailed", "depth_test_failed"),
                        ("stencilTestFailed", "stencil_test_failed"),
                        ("scissorClipped", "scissor_clipped"),
                        ("shaderDiscarded", "shader_discarded"),
                        ("depthClipped", "depth_clipped"),
                    ):
                        try:
                            if getattr(mod, attr, False):
                                reasons.append(label)
                        except Exception:
                            pass
                    if reasons:
                        entry["failure_reasons"] = reasons
                try:
                    pre, post = mod.preMod, mod.postMod
                    entry["pre"] = {
                        "color": list(pre.col.floatValue),
                        "depth": pre.depth,
                        "stencil": pre.stencil,
                    }
                    entry["post"] = {
                        "color": list(post.col.floatValue),
                        "depth": post.depth,
                        "stencil": post.stencil,
                    }
                    entry["pixel_changed"] = list(pre.col.floatValue) != list(post.col.floatValue)
                except Exception:
                    pass
                mods.append(entry)
            result["data"] = {
                "resource_id": str(tex.resourceId),
                "x": int(x),
                "y": int(y),
                "count": len(mods),
                "modifications": mods,
            }

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def read_texture_stats(self, event_id=None, resource_id=None, target="color0"):
        """GPU min/max of a texture, with NaN / Inf / negative flags"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        if event_id is None and resource_id is None:
            raise ValueError("event_id or resource_id is required")
        result = {"data": None, "error": None}

        def callback(controller):
            if event_id is not None:
                controller.SetFrameEvent(int(event_id), True)
            tex = self._resolve_texture(controller, event_id, resource_id, target)
            try:
                mm = controller.GetMinMax(tex.resourceId, rd.Subresource(0, 0, 0), rd.CompType.Typeless)
            except Exception as e:
                result["error"] = "GetMinMax failed: %s" % str(e)
                return
            mn, mx = mm[0], mm[1]
            mins = list(mn.floatValue)
            maxs = list(mx.floatValue)
            flags = {
                "has_negative": any(v < 0 for v in mins[:3]),
                "has_inf": any(math.isinf(v) for v in mins + maxs),
                "has_nan": any(math.isnan(v) for v in mins + maxs),
                "all_zero": all(abs(v) < 1e-8 for v in maxs),
            }
            result["data"] = {
                "resource_id": str(tex.resourceId),
                "name": self.ctx.GetResourceName(tex.resourceId),
                "format": str(tex.format.Name()),
                "width": tex.width,
                "height": tex.height,
                "min": mins,
                "max": maxs,
                "flags": flags,
            }

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def sample_pixel_region(
        self, event_id=None, resource_id=None, target="color0",
        x=0, y=0, width=None, height=None, sample_count=256, anomaly_threshold=10.0,
    ):
        """Grid-sample a region for NaN / Inf / negative / extreme-bright pixels"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        if event_id is None and resource_id is None:
            raise ValueError("event_id or resource_id is required")
        result = {"data": None}

        def callback(controller):
            if event_id is not None:
                controller.SetFrameEvent(int(event_id), True)
            tex = self._resolve_texture(controller, event_id, resource_id, target)
            tw, th = int(tex.width), int(tex.height)
            rx, ry = max(0, int(x)), max(0, int(y))
            rw = min(int(width) if width else tw, tw - rx)
            rh = min(int(height) if height else th, th - ry)
            count = min(max(int(sample_count), 1), 1024)
            cols = max(1, int(math.sqrt(count * rw / float(max(rh, 1)))))
            rows = max(1, count // cols)
            step_x = max(1, rw // cols)
            step_y = max(1, rh // rows)
            points = []
            for r in range(rows):
                for c in range(cols):
                    px = rx + c * step_x + step_x // 2
                    py = ry + r * step_y + step_y // 2
                    if px < rx + rw and py < ry + rh:
                        points.append((px, py))

            nan_c = inf_c = neg_c = bright_c = 0
            hotspots = []
            for px, py in points:
                try:
                    v = controller.PickPixel(tex.resourceId, px, py, rd.Subresource(0, 0, 0), rd.CompType.Typeless)
                    rgb = [v.floatValue[0], v.floatValue[1], v.floatValue[2]]
                except Exception:
                    continue
                kind = None
                if any(math.isnan(n) for n in rgb):
                    nan_c += 1
                    kind = "NaN"
                elif any(math.isinf(n) for n in rgb):
                    inf_c += 1
                    kind = "Inf"
                elif any(n < 0 for n in rgb):
                    neg_c += 1
                    kind = "negative"
                elif max(rgb) > float(anomaly_threshold):
                    bright_c += 1
                    kind = "extreme_bright"
                if kind and len(hotspots) < 20:
                    hotspots.append({"x": px, "y": py, "value": [round(n, 6) for n in rgb], "type": kind})

            total = len(points)
            anomalies = nan_c + inf_c + neg_c + bright_c
            result["data"] = {
                "resource_id": str(tex.resourceId),
                "region": {"x": rx, "y": ry, "width": rw, "height": rh},
                "sample_count": total,
                "anomalies": {
                    "nan": nan_c, "inf": inf_c, "negative": neg_c,
                    "extreme_bright": bright_c, "total": anomalies,
                    "rate": round(anomalies / float(max(total, 1)), 4),
                },
                "hotspots": hotspots,
            }

        self._invoke(callback)
        return result["data"]

    def get_post_vs_data(self, event_id, stage="vsout", max_vertices=100):
        """Post-transform vertices in draw order (index buffer applied)"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        stage_map = {
            "vsin": rd.MeshDataStage.VSIn,
            "vsout": rd.MeshDataStage.VSOut,
            "gsout": getattr(rd.MeshDataStage, "GSOut", rd.MeshDataStage.VSOut),
        }
        if stage not in stage_map:
            raise ValueError("Unknown stage: %s (vsin/vsout/gsout)" % stage)
        result = {"data": None, "error": None}

        def callback(controller):
            controller.SetFrameEvent(int(event_id), True)
            postvs = controller.GetPostVSData(0, 0, stage_map[stage])
            if postvs.vertexResourceId == rd.ResourceId.Null():
                result["error"] = "No post-VS data at event %d" % event_id
                return
            pipe = controller.GetPipelineState()
            refl_stage = rd.ShaderStage.Geometry if stage == "gsout" else rd.ShaderStage.Vertex
            refl = pipe.GetShaderReflection(refl_stage)
            sigs = []
            if refl is not None:
                sigs = refl.inputSignature if stage == "vsin" else refl.outputSignature
            layout = _sig_layout(sigs)
            try:
                indices, indexed = _read_draw_indices(controller, postvs, int(max_vertices))
                raw = _read_vertex_bytes(controller, postvs, indices)
            except ValueError as e:
                result["error"] = str(e)
                return
            stride = int(postvs.vertexByteStride)
            vertices = []
            for idx in indices:
                off = idx * stride
                if off + stride > len(raw):
                    break
                chunk = raw[off:off + stride]
                values = {}
                unsupported = []
                for attr in layout:
                    decoded = _decode_comps(chunk, attr)
                    if decoded is None:
                        unsupported.append(attr["name"])
                    else:
                        values[attr["name"]] = decoded
                vertices.append({"values": values, "unsupported": unsupported})
            result["data"] = {
                "event_id": int(event_id),
                "stage": stage,
                "indexed": indexed,
                "attributes": [_public_attr(a) for a in layout],
                "vertex_stride": stride,
                "total_indices": int(postvs.numIndices),
                "returned_vertices": len(vertices),
                "vertices": vertices,
                "note": "values are in draw order after the index buffer; only float/half are decoded",
            }

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def export_mesh(self, event_id, output_path):
        """Export the indexed post-VS mesh as OBJ (float/half position, normal, uv)"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        if not output_path:
            raise ValueError("output_path is required")
        result = {"data": None, "error": None}

        def callback(controller):
            controller.SetFrameEvent(int(event_id), True)
            postvs = controller.GetPostVSData(0, 0, rd.MeshDataStage.VSOut)
            if postvs.vertexResourceId == rd.ResourceId.Null():
                result["error"] = "No post-VS data at event %d" % event_id
                return
            pipe = controller.GetPipelineState()
            refl = pipe.GetShaderReflection(rd.ShaderStage.Vertex)
            layout = _sig_layout(refl.outputSignature if refl is not None else [])
            pos = _find_attr(layout, "POSITION")
            nrm = _find_attr(layout, "NORMAL")
            uv = _find_attr(layout, "TEXCOORD")
            if pos is None:
                result["error"] = "No POSITION output on the vertex shader"
                return
            for attr in (pos, nrm, uv):
                if attr is not None and attr["kind"] not in ("float", "half"):
                    result["error"] = "Attribute %s is %s; only float/half can be exported" % (
                        attr["name"], attr["var_type"]
                    )
                    return
            try:
                indices, indexed = _read_draw_indices(controller, postvs, None)
                raw = _read_vertex_bytes(controller, postvs, indices)
            except ValueError as e:
                result["error"] = str(e)
                return
            stride = int(postvs.vertexByteStride)
            topo = _topology_name(postvs, pipe)
            runs = _split_restart(indices)
            positions, normals, uvs = [], [], []
            triangles = []
            for run in runs:
                decoded = []
                for idx in run:
                    off = idx * stride
                    if off + stride > len(raw):
                        decoded.append(None)
                        continue
                    chunk = raw[off:off + stride]
                    decoded.append((
                        _decode_comps(chunk, pos),
                        _decode_comps(chunk, nrm) if nrm else None,
                        _decode_comps(chunk, uv) if uv else None,
                    ))
                local = []
                for item in decoded:
                    if item is None or item[0] is None or len(item[0]) < 3:
                        local.append(None)
                        continue
                    local.append(len(positions))
                    positions.append(tuple(item[0][:3]))
                    if item[1] is not None and len(item[1]) >= 3:
                        normals.append(tuple(item[1][:3]))
                    if item[2] is not None and len(item[2]) >= 2:
                        uvs.append(tuple(item[2][:2]))
                triangles.extend(_faces_from_run(local, topo))

            folder = os.path.dirname(output_path)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            lines = [
                "# RenderDoc MCP mesh event %d" % int(event_id),
                "# vertices %d topology %s indexed %s" % (len(positions), topo, indexed),
            ]
            for p in positions:
                lines.append("v %.6f %.6f %.6f" % p)
            for n in normals:
                lines.append("vn %.6f %.6f %.6f" % n)
            for t in uvs:
                lines.append("vt %.6f %.6f" % t)
            has_n = len(normals) == len(positions)
            has_t = len(uvs) == len(positions)
            for a, b, c in triangles:
                i1, i2, i3 = a + 1, b + 1, c + 1
                if has_n and has_t:
                    lines.append("f %d/%d/%d %d/%d/%d %d/%d/%d" % (i1, i1, i1, i2, i2, i2, i3, i3, i3))
                elif has_n:
                    lines.append("f %d//%d %d//%d %d//%d" % (i1, i1, i2, i2, i3, i3))
                elif has_t:
                    lines.append("f %d/%d %d/%d %d/%d" % (i1, i1, i2, i2, i3, i3))
                else:
                    lines.append("f %d %d %d" % (i1, i2, i3))
            with open(output_path, "w") as f:
                f.write("\n".join(lines) + "\n")
            result["data"] = {
                "event_id": int(event_id),
                "output_path": output_path,
                "topology": topo,
                "indexed": indexed,
                "vertices": len(positions),
                "normals": len(normals),
                "uvs": len(uvs),
                "triangles": len(triangles),
            }

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]


def _component_kind(var_type):
    name = str(var_type).upper()
    if "DOUBLE" in name:
        return "double"
    if "HALF" in name or "FLOAT16" in name:
        return "half"
    if "FLOAT" in name:
        return "float"
    return "other"


def _component_size(kind):
    return {"double": 8, "half": 2, "float": 4}.get(kind, 4)


def _sig_layout(sigs):
    layout = []
    offset = 0
    for sig in sigs:
        kind = _component_kind(sig.varType)
        comps = int(sig.compCount)
        name = str(sig.varName or getattr(sig, "semanticName", "") or "")
        layout.append({
            "name": name,
            "semantic": str(getattr(sig, "semanticName", "") or name).upper(),
            "var_type": str(sig.varType),
            "comp_count": comps,
            "byte_offset": offset,
            "kind": kind,
        })
        offset += _component_size(kind) * max(comps, 1)
        if offset % 4:
            offset += 4 - (offset % 4)
    return layout


def _public_attr(attr):
    return {
        "name": attr["name"],
        "var_type": attr["var_type"],
        "comp_count": attr["comp_count"],
        "byte_offset": attr["byte_offset"],
        "decoded": attr["kind"] in ("float", "half", "double"),
    }


def _find_attr(layout, token):
    for attr in layout:
        if token in attr["semantic"] or token in attr["name"].upper():
            return attr
    return None


def _decode_comps(chunk, attr):
    if attr is None or attr["kind"] not in ("float", "half", "double"):
        return None
    fmt = {"float": "f", "half": "e", "double": "d"}[attr["kind"]]
    size = _component_size(attr["kind"])
    values = []
    for i in range(attr["comp_count"]):
        off = attr["byte_offset"] + i * size
        if off + size > len(chunk):
            return None
        values.append(round(struct.unpack_from(fmt, chunk, off)[0], 6))
    return values


def _read_draw_indices(controller, postvs, limit):
    """Draw-order vertex indices. -1 is primitive restart."""
    total = int(postvs.numIndices)
    count = total if limit is None else min(total, max(0, int(limit)))
    idx_id = getattr(postvs, "indexResourceId", rd.ResourceId.Null())
    stride = int(getattr(postvs, "indexByteStride", 0) or 0)
    indexed = idx_id != rd.ResourceId.Null() and stride in (2, 4)
    if not indexed:
        return list(range(count)), False
    raw = bytes(controller.GetBufferData(
        idx_id, int(getattr(postvs, "indexByteOffset", 0) or 0), count * stride
    ))
    fmt = "H" if stride == 2 else "I"
    restart = 0xFFFF if stride == 2 else 0xFFFFFFFF
    base = int(getattr(postvs, "baseVertex", 0) or 0)
    indices = []
    for i in range(count):
        off = i * stride
        if off + stride > len(raw):
            break
        value = struct.unpack_from(fmt, raw, off)[0]
        indices.append(-1 if value == restart else base + value)
    return indices, True


def _read_vertex_bytes(controller, postvs, indices):
    stride = int(postvs.vertexByteStride)
    if stride <= 0:
        raise ValueError("Invalid vertex stride %d" % stride)
    real = [i for i in indices if i >= 0]
    if not real:
        return b""
    max_i = max(real)
    if max_i > 5000000:
        raise ValueError("Vertex index %d is too large to read" % max_i)
    return bytes(controller.GetBufferData(
        postvs.vertexResourceId, int(postvs.vertexByteOffset), (max_i + 1) * stride
    ))


def _split_restart(indices):
    runs = []
    current = []
    for idx in indices:
        if idx < 0:
            if current:
                runs.append(current)
                current = []
            continue
        current.append(idx)
    if current:
        runs.append(current)
    return runs


def _topology_name(postvs, pipe):
    topo = str(getattr(postvs, "topology", "") or "")
    if topo and topo != "None":
        return topo
    try:
        return str(pipe.GetPrimitiveTopology())
    except Exception:
        return "TriangleList"


def _faces_from_run(slots, topo):
    """slots[i] is an OBJ vertex index, or None when that corner failed to decode."""
    faces = []
    if "Strip" in topo:
        for i in range(len(slots) - 2):
            tri = slots[i:i + 3]
            if None in tri:
                continue
            faces.append((tri[0], tri[2], tri[1]) if i % 2 else (tri[0], tri[1], tri[2]))
    elif "Fan" in topo:
        if slots and slots[0] is not None:
            for i in range(1, len(slots) - 1):
                if slots[i] is None or slots[i + 1] is None:
                    continue
                faces.append((slots[0], slots[i], slots[i + 1]))
    else:
        for i in range(0, len(slots) - 2, 3):
            tri = slots[i:i + 3]
            if None in tri:
                continue
            faces.append((tri[0], tri[1], tri[2]))
    return faces
