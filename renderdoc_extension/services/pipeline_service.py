"""
Pipeline state service for RenderDoc.
"""

import re

import renderdoc as rd

from ..utils import Parsers, Serializers, Helpers


class PipelineService:
    """Pipeline state service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def get_shader_info(self, event_id, stage):
        """Get shader information for a specific stage"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"shader": None, "error": None}

        def callback(controller):
            controller.SetFrameEvent(event_id, True)

            pipe = controller.GetPipelineState()
            stage_enum = Parsers.parse_stage(stage)

            shader = pipe.GetShader(stage_enum)
            if shader == rd.ResourceId.Null():
                result["error"] = "No %s shader bound" % stage
                return

            entry = pipe.GetShaderEntryPoint(stage_enum)
            reflection = pipe.GetShaderReflection(stage_enum)

            shader_info = {
                "resource_id": str(shader),
                "entry_point": entry,
                "stage": stage,
            }

            # Get disassembly
            try:
                targets = controller.GetDisassemblyTargets(True)
                if targets:
                    disasm = controller.DisassembleShader(
                        pipe.GetGraphicsPipelineObject(), reflection, targets[0]
                    )
                    shader_info["disassembly"] = disasm
            except Exception as e:
                shader_info["disassembly_error"] = str(e)

            # Get constant buffer info
            if reflection:
                shader_info["constant_buffers"] = self._get_cbuffer_info(
                    controller, pipe, reflection, stage_enum
                )
                shader_info["resources"] = self._get_resource_bindings(reflection)

            result["shader"] = shader_info

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["shader"]

    def get_pipeline_state(self, event_id):
        """Get full pipeline state at an event"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"pipeline": None, "error": None}

        def callback(controller):
            controller.SetFrameEvent(event_id, True)

            pipe = controller.GetPipelineState()
            api = controller.GetAPIProperties().pipelineType

            pipeline_info = {
                "event_id": event_id,
                "api": Helpers.enum_name("GraphicsAPI", api),
            }

            # Shader stages with detailed bindings
            stages = {}
            stage_list = Helpers.get_all_shader_stages()
            for stage in stage_list:
                shader = pipe.GetShader(stage)
                if shader != rd.ResourceId.Null():
                    stage_info = {
                        "resource_id": str(shader),
                        "entry_point": pipe.GetShaderEntryPoint(stage),
                    }

                    reflection = pipe.GetShaderReflection(stage)

                    stage_info["resources"] = self._get_stage_resources(
                        controller, pipe, stage, reflection
                    )
                    stage_info["uavs"] = self._get_stage_uavs(
                        controller, pipe, stage, reflection
                    )
                    stage_info["samplers"] = self._get_stage_samplers(
                        pipe, stage, reflection
                    )
                    stage_info["constant_buffers"] = self._get_stage_cbuffers(
                        controller, pipe, stage, reflection
                    )

                    stages[Helpers.enum_name("ShaderStage", stage)] = stage_info

            pipeline_info["shaders"] = stages

            # Viewport (API-agnostic)
            try:
                viewports = []
                for i in range(16):  # Max viewports
                    try:
                        v = pipe.GetViewport(i)
                        if v.width > 0 and v.height > 0:
                            entry = {
                                "x": v.x,
                                "y": v.y,
                                "width": v.width,
                                "height": v.height,
                                "min_depth": v.minDepth,
                                "max_depth": v.maxDepth,
                            }
                            # GL reports all 16 slots even when only one is used
                            if entry not in viewports:
                                viewports.append(entry)
                        else:
                            break
                    except Exception:
                        break
                if viewports:
                    pipeline_info["viewports"] = viewports
            except Exception:
                pass

            # Render targets (API-agnostic)
            try:
                output_targets = pipe.GetOutputTargets()
                rts = []
                for i, rt in enumerate(output_targets):
                    if rt.resource != rd.ResourceId.Null():
                        rts.append({"index": i, "resource_id": str(rt.resource)})
                if rts:
                    pipeline_info["render_targets"] = rts
            except Exception:
                pass

            # Depth target (API-agnostic)
            try:
                depth = pipe.GetDepthTarget()
                if depth.resource != rd.ResourceId.Null():
                    pipeline_info["depth_target"] = str(depth.resource)
            except Exception:
                pass

            # Input assembly / topology (API-agnostic)
            try:
                topo = pipe.GetPrimitiveTopology()
                pipeline_info["input_assembly"] = {"topology": Helpers.enum_name("Topology", topo)}
            except Exception:
                pass

            result["pipeline"] = pipeline_info

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["pipeline"]

    def _get_stage_resources(self, controller, pipe, stage, reflection):
        """Get shader resource views (SRVs) for a stage"""
        resources = []
        try:
            srvs = pipe.GetReadOnlyResources(stage, False)

            name_map = {}
            if reflection:
                for res in reflection.readOnlyResources:
                    name_map[res.fixedBindNumber] = res.name

            for srv in srvs:
                if srv.descriptor.resource == rd.ResourceId.Null():
                    continue

                slot = srv.access.index
                res_info = {
                    "slot": slot,
                    "name": name_map.get(slot, ""),
                    "resource_id": str(srv.descriptor.resource),
                }

                res_info.update(
                    self._get_resource_details(controller, srv.descriptor.resource)
                )

                res_info["first_mip"] = srv.descriptor.firstMip
                res_info["num_mips"] = srv.descriptor.numMips
                res_info["first_slice"] = srv.descriptor.firstSlice
                res_info["num_slices"] = srv.descriptor.numSlices

                resources.append(res_info)
        except Exception as e:
            resources.append({"error": str(e)})

        return resources

    def _get_stage_uavs(self, controller, pipe, stage, reflection):
        """Get unordered access views (UAVs) for a stage"""
        uavs = []
        try:
            uav_list = pipe.GetReadWriteResources(stage, False)

            name_map = {}
            if reflection:
                for res in reflection.readWriteResources:
                    name_map[res.fixedBindNumber] = res.name

            for uav in uav_list:
                if uav.descriptor.resource == rd.ResourceId.Null():
                    continue

                slot = uav.access.index
                uav_info = {
                    "slot": slot,
                    "name": name_map.get(slot, ""),
                    "resource_id": str(uav.descriptor.resource),
                }

                uav_info.update(
                    self._get_resource_details(controller, uav.descriptor.resource)
                )

                uav_info["first_element"] = uav.descriptor.firstMip
                uav_info["num_elements"] = uav.descriptor.numMips

                uavs.append(uav_info)
        except Exception as e:
            uavs.append({"error": str(e)})

        return uavs

    def _get_stage_samplers(self, pipe, stage, reflection):
        """Get samplers for a stage"""
        samplers = []
        try:
            sampler_list = pipe.GetSamplers(stage, False)

            name_map = {}
            if reflection:
                for samp in reflection.samplers:
                    name_map[samp.fixedBindNumber] = samp.name

            for samp in sampler_list:
                slot = samp.access.index
                samp_info = {
                    "slot": slot,
                    "name": name_map.get(slot, ""),
                }

                desc = samp.descriptor
                try:
                    samp_info["address_u"] = str(desc.addressU)
                    samp_info["address_v"] = str(desc.addressV)
                    samp_info["address_w"] = str(desc.addressW)
                except AttributeError:
                    pass

                try:
                    samp_info["filter"] = str(desc.filter)
                except AttributeError:
                    pass

                try:
                    samp_info["max_anisotropy"] = desc.maxAnisotropy
                except AttributeError:
                    pass

                try:
                    samp_info["min_lod"] = desc.minLOD
                    samp_info["max_lod"] = desc.maxLOD
                    samp_info["mip_lod_bias"] = desc.mipLODBias
                except AttributeError:
                    pass

                try:
                    samp_info["border_color"] = [
                        desc.borderColor[0],
                        desc.borderColor[1],
                        desc.borderColor[2],
                        desc.borderColor[3],
                    ]
                except (AttributeError, TypeError):
                    pass

                try:
                    samp_info["compare_function"] = str(desc.compareFunction)
                except AttributeError:
                    pass

                samplers.append(samp_info)
        except Exception as e:
            samplers.append({"error": str(e)})

        return samplers

    def _get_stage_cbuffers(self, controller, pipe, stage, reflection):
        """Get constant buffers for a stage with actual values"""
        cbuffers = []
        try:
            if not reflection:
                return cbuffers

            for i, cb in enumerate(reflection.constantBlocks):
                slot = cb.bindPoint if hasattr(cb, 'bindPoint') else cb.fixedBindNumber
                cb_info = {
                    "slot": slot,
                    "name": cb.name,
                    "byte_size": cb.byteSize,
                    "variable_count": len(cb.variables) if cb.variables else 0,
                }

                # Use GetConstantBlock (API-agnostic) to get binding info and actual values
                try:
                    used_desc = pipe.GetConstantBlock(stage, i, 0)
                    if used_desc.descriptor.resource != rd.ResourceId.Null():
                        cb_info["resource_id"] = str(used_desc.descriptor.resource)
                        variables = controller.GetCBufferVariableContents(
                            pipe.GetGraphicsPipelineObject(),
                            reflection.resourceId,
                            stage,
                            reflection.entryPoint,
                            i,
                            used_desc.descriptor.resource,
                            used_desc.descriptor.byteOffset,
                            used_desc.descriptor.byteSize,
                        )
                        cb_info["variables"] = Serializers.serialize_variables(variables)
                except Exception as e:
                    cb_info["value_error"] = str(e)
                    # Fallback: provide at least the layout info from reflection
                    if cb.variables:
                        cb_info["variables"] = []
                        for var in cb.variables:
                            cb_info["variables"].append({
                                "name": var.name,
                                "byte_offset": var.byteOffset,
                                "type": str(var.type.name) if var.type else "",
                            })

                cbuffers.append(cb_info)

        except Exception as e:
            cbuffers.append({"error": str(e)})

        return cbuffers

    def _get_resource_details(self, controller, resource_id):
        """Get details about a resource (texture or buffer)"""
        details = {}

        try:
            resource_name = self.ctx.GetResourceName(resource_id)
            if resource_name:
                details["resource_name"] = resource_name
        except Exception:
            pass

        for tex in controller.GetTextures():
            if tex.resourceId == resource_id:
                details["type"] = "texture"
                details["width"] = tex.width
                details["height"] = tex.height
                details["depth"] = tex.depth
                details["array_size"] = tex.arraysize
                details["mip_levels"] = tex.mips
                details["format"] = str(tex.format.Name())
                details["dimension"] = Helpers.enum_name("TextureType", tex.type)
                details["msaa_samples"] = tex.msSamp
                return details

        for buf in controller.GetBuffers():
            if buf.resourceId == resource_id:
                details["type"] = "buffer"
                details["length"] = buf.length
                return details

        return details

    def _get_cbuffer_info(self, controller, pipe, reflection, stage):
        """Get constant buffer information and values"""
        cbuffers = []

        for i, cb in enumerate(reflection.constantBlocks):
            cb_info = {
                "name": cb.name,
                "slot": i,
                "size": cb.byteSize,
                "variables": [],
            }

            try:
                # Use GetConstantBlock (API-agnostic) instead of GetConstantBuffer (D3D-only)
                # Returns UsedDescriptor with .descriptor.resource, etc.
                used_desc = pipe.GetConstantBlock(stage, i, 0)
                if used_desc.descriptor.resource != rd.ResourceId.Null():
                    cb_info["resource_id"] = str(used_desc.descriptor.resource)
                    variables = controller.GetCBufferVariableContents(
                        pipe.GetGraphicsPipelineObject(),
                        reflection.resourceId,
                        stage,
                        reflection.entryPoint,
                        i,
                        used_desc.descriptor.resource,
                        used_desc.descriptor.byteOffset,
                        used_desc.descriptor.byteSize,
                    )
                    cb_info["variables"] = Serializers.serialize_variables(variables)
            except Exception as e:
                cb_info["error"] = str(e)

            cbuffers.append(cb_info)

        return cbuffers

    def _get_resource_bindings(self, reflection):
        """Get shader resource bindings"""
        resources = []

        try:
            for res in reflection.readOnlyResources:
                resources.append(
                    {
                        "name": res.name,
                        "type": str(res.resType),
                        "binding": res.fixedBindNumber,
                        "access": "ReadOnly",
                    }
                )
        except Exception:
            pass

        try:
            for res in reflection.readWriteResources:
                resources.append(
                    {
                        "name": res.name,
                        "type": str(res.resType),
                        "binding": res.fixedBindNumber,
                        "access": "ReadWrite",
                    }
                )
        except Exception:
            pass

        return resources

    # ==================== Targeted constant buffer read ====================

    @staticmethod
    def _resolve_cbuffer_name(blocks, name):
        """Resolve a cbuffer name to an index in reflection.constantBlocks.

        Match order: exact name, case-insensitive exact, unique substring, then
        a trailing number behind a cb/pb/b/buffer/ubo style prefix ("cb5",
        "cbuffer5", "buffer_05") taken as the index itself.

        Returns None when nothing matches or a substring is ambiguous; the
        caller reports the available names.
        """
        if not name:
            return None
        for i in range(len(blocks)):
            if blocks[i].name == name:
                return i
        low = name.lower()
        for i in range(len(blocks)):
            if blocks[i].name.lower() == low:
                return i
        matches = [i for i in range(len(blocks)) if low in blocks[i].name.lower()]
        if len(matches) == 1:
            return matches[0]
        m = re.match(r"^([a-z_]*?)(\d+)$", low)
        if m:
            prefix = m.group(1)
            if prefix == "b" or any(k in prefix for k in ("cb", "pb", "buffer", "ubo")):
                idx = int(m.group(2))
                if 0 <= idx < len(blocks):
                    return idx
        return None

    def read_constant_buffer(self, event_id, stage, slot=None, array_index=0):
        """Read one constant buffer of one stage at an event.

        slot is an index into reflection.constantBlocks, not an API bind point:
        on GLES captures every block reports bind point 0, so the index is what
        tells them apart. It may also be a cbuffer name (exact, substring or
        "cb5" style). With no slot the stage's blocks are listed with their
        index, name and size, which is the index <-> name table needed to pick one.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            controller.SetFrameEvent(event_id, True)
            pipe = controller.GetPipelineState()
            stage_enum = Parsers.parse_stage(stage)

            if pipe.GetShader(stage_enum) == rd.ResourceId.Null():
                result["error"] = "No %s shader bound at event %d" % (stage, event_id)
                return
            reflection = pipe.GetShaderReflection(stage_enum)
            if not reflection:
                result["error"] = "No shader reflection available"
                return
            blocks = reflection.constantBlocks

            slot_idx = None
            if slot is not None and not (isinstance(slot, str) and not slot.strip()):
                if isinstance(slot, bool):
                    result["error"] = "slot must be an index or a cbuffer name"
                    return
                if isinstance(slot, (int, float)):
                    slot_idx = int(slot)
                else:
                    text = str(slot).strip()
                    if text.lstrip("-").isdigit():
                        slot_idx = int(text)
                    else:
                        slot_idx = self._resolve_cbuffer_name(blocks, text)
                        if slot_idx is None:
                            result["error"] = "No cbuffer named %r. Available: %s" % (
                                text,
                                "; ".join("%d=%s" % (i, blocks[i].name) for i in range(len(blocks))),
                            )
                            return

            if slot_idx is None:
                result["data"] = {
                    "event_id": event_id,
                    "stage": stage,
                    "constant_buffers": [
                        {
                            "index": i,
                            "name": blocks[i].name,
                            "byte_size": blocks[i].byteSize,
                            "variable_count": len(blocks[i].variables) if blocks[i].variables else 0,
                        }
                        for i in range(len(blocks))
                    ],
                    "hint": "Pass slot=<index> or a cbuffer name to read its values. The index is "
                            "the position in this list, not the API bind point.",
                }
                return

            if slot_idx < 0 or slot_idx >= len(blocks):
                result["error"] = "Slot %d out of range (0..%d)" % (slot_idx, len(blocks) - 1)
                return

            cb = blocks[slot_idx]
            data = {
                "event_id": event_id,
                "stage": stage,
                "slot": slot_idx,
                "name": cb.name,
                "byte_size": cb.byteSize,
                "variable_count": len(cb.variables) if cb.variables else 0,
            }
            try:
                used = pipe.GetConstantBlock(stage_enum, slot_idx, array_index)
                desc = used.descriptor
                data["bound_resource_id"] = str(desc.resource)
                data["byte_offset"] = desc.byteOffset
                data["bound_byte_size"] = desc.byteSize
                if desc.resource == rd.ResourceId.Null():
                    data["status"] = "No Resource"
                    data["variables"] = []
                else:
                    variables = controller.GetCBufferVariableContents(
                        pipe.GetGraphicsPipelineObject(),
                        reflection.resourceId,
                        stage_enum,
                        reflection.entryPoint,
                        slot_idx,
                        desc.resource,
                        desc.byteOffset,
                        desc.byteSize,
                    )
                    data["variables"] = Serializers.serialize_variables(variables)
            except Exception as e:
                data["error"] = str(e)
            result["data"] = data

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        if result["data"] is None:
            # BlockInvoke swallows exceptions raised inside the callback, leaving
            # the result untouched; surface that instead of returning null.
            raise ValueError(
                "read_constant_buffer did not complete (an exception was probably swallowed "
                "by BlockInvoke, see the qrenderdoc output); check the stage name"
            )
        return result["data"]
