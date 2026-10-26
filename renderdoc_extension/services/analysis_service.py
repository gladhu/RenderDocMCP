"""
Performance analysis service for RenderDoc.

Everything here runs through the replay controller, so when the capture is
opened with remote replay (e.g. an Android device) the overlays, histograms
and pixel reads are executed by the device GPU, not the local PC.
"""

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
