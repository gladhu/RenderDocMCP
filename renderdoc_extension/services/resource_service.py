"""
Resource information service for RenderDoc.
"""

import base64

import renderdoc as rd

from ..utils import Parsers, Helpers


class ResourceService:
    """Resource information service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def _find_texture_by_id(self, controller, resource_id):
        """Find texture by resource ID"""
        target_id = Parsers.extract_numeric_id(resource_id)
        for tex in controller.GetTextures():
            tex_id_str = str(tex.resourceId)
            tex_id = Parsers.extract_numeric_id(tex_id_str)
            if tex_id == target_id:
                return tex
        return None

    def get_buffer_contents(self, resource_id, offset=0, length=0):
        """Get buffer data"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            # Parse resource ID
            try:
                rid = Parsers.parse_resource_id(resource_id)
            except Exception:
                result["error"] = "Invalid resource ID: %s" % resource_id
                return

            # Find buffer
            buf_desc = None
            for buf in controller.GetBuffers():
                if buf.resourceId == rid:
                    buf_desc = buf
                    break

            if not buf_desc:
                result["error"] = "Buffer not found: %s" % resource_id
                return

            # Get data
            actual_length = length if length > 0 else buf_desc.length
            data = controller.GetBufferData(rid, offset, actual_length)

            result["data"] = {
                "resource_id": resource_id,
                "length": len(data),
                "total_size": buf_desc.length,
                "offset": offset,
                "content_base64": base64.b64encode(data).decode("ascii"),
            }

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def get_texture_info(self, resource_id):
        """Get texture metadata"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"texture": None, "error": None}

        def callback(controller):
            try:
                tex_desc = self._find_texture_by_id(controller, resource_id)

                if not tex_desc:
                    result["error"] = "Texture not found: %s" % resource_id
                    return

                result["texture"] = {
                    "resource_id": resource_id,
                    "width": tex_desc.width,
                    "height": tex_desc.height,
                    "depth": tex_desc.depth,
                    "array_size": tex_desc.arraysize,
                    "mip_levels": tex_desc.mips,
                    "format": str(tex_desc.format.Name()),
                    "dimension": Helpers.enum_name("TextureType", tex_desc.type),
                    "msaa_samples": tex_desc.msSamp,
                    "byte_size": tex_desc.byteSize,
                }
            except Exception as e:
                import traceback
                result["error"] = "Error: %s\n%s" % (str(e), traceback.format_exc())

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["texture"]

    def get_texture_data(self, resource_id, mip=0, slice=0, sample=0, depth_slice=None):
        """Get texture pixel data."""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            tex_desc = self._find_texture_by_id(controller, resource_id)

            if not tex_desc:
                result["error"] = "Texture not found: %s" % resource_id
                return

            # Validate mip level
            if mip < 0 or mip >= tex_desc.mips:
                result["error"] = "Invalid mip level %d (texture has %d mips)" % (
                    mip,
                    tex_desc.mips,
                )
                return

            # Validate slice for array/cube textures
            max_slices = tex_desc.arraysize
            if tex_desc.cubemap:
                max_slices = tex_desc.arraysize * 6
            if slice < 0 or (max_slices > 1 and slice >= max_slices):
                result["error"] = "Invalid slice %d (texture has %d slices)" % (
                    slice,
                    max_slices,
                )
                return

            # Validate sample for MSAA
            if sample < 0 or (tex_desc.msSamp > 1 and sample >= tex_desc.msSamp):
                result["error"] = "Invalid sample %d (texture has %d samples)" % (
                    sample,
                    tex_desc.msSamp,
                )
                return

            # Calculate dimensions at this mip level
            mip_width = max(1, tex_desc.width >> mip)
            mip_height = max(1, tex_desc.height >> mip)
            mip_depth = max(1, tex_desc.depth >> mip)

            # Validate depth_slice for 3D textures
            is_3d = tex_desc.depth > 1
            if depth_slice is not None:
                if not is_3d:
                    result["error"] = "depth_slice can only be used with 3D textures"
                    return
                if depth_slice < 0 or depth_slice >= mip_depth:
                    result["error"] = "Invalid depth_slice %d (texture has %d depth at mip %d)" % (
                        depth_slice,
                        mip_depth,
                        mip,
                    )
                    return

            # Create subresource specification
            sub = rd.Subresource()
            sub.mip = mip
            sub.slice = slice
            sub.sample = sample

            # Get texture data
            try:
                data = controller.GetTextureData(tex_desc.resourceId, sub)
            except Exception as e:
                result["error"] = "Failed to get texture data: %s" % str(e)
                return

            # Extract depth slice for 3D textures if requested
            output_depth = mip_depth
            if is_3d and depth_slice is not None:
                total_size = len(data)
                bytes_per_slice = total_size // mip_depth
                slice_start = depth_slice * bytes_per_slice
                slice_end = slice_start + bytes_per_slice
                data = data[slice_start:slice_end]
                output_depth = 1

            result["data"] = {
                "resource_id": resource_id,
                "width": mip_width,
                "height": mip_height,
                "depth": output_depth,
                "mip": mip,
                "slice": slice,
                "sample": sample,
                "depth_slice": depth_slice,
                "format": str(tex_desc.format.Name()),
                "dimension": Helpers.enum_name("TextureType", tex_desc.type),
                "is_3d": is_3d,
                "total_depth": mip_depth if is_3d else 1,
                "data_length": len(data),
                "content_base64": base64.b64encode(data).decode("ascii"),
            }

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def list_frame_textures(self, sort_by="size", top_n=50, filter_fmt=None, min_width=None, rt_only=False):
        """Frame-level texture inventory with size and usage counts"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        result = {"data": None}

        def callback(controller):
            rt_ids = set()
            for action in Helpers.flatten_actions(controller.GetRootActions()):
                if action.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.Clear):
                    for o in action.outputs:
                        if o != rd.ResourceId.Null():
                            rt_ids.add(str(o))
                    if action.depthOut != rd.ResourceId.Null():
                        rt_ids.add(str(action.depthOut))

            rows = []
            for tex in controller.GetTextures():
                rid = str(tex.resourceId)
                fmt = str(tex.format.Name())
                if filter_fmt and filter_fmt.upper() not in fmt.upper():
                    continue
                if min_width is not None and tex.width < int(min_width):
                    continue
                is_rt = rid in rt_ids
                if rt_only and not is_rt:
                    continue
                usage = _usage_counts(controller, tex.resourceId)
                size = int(getattr(tex, "byteSize", 0) or 0) or Helpers.estimate_texture_bytes(tex)
                rows.append({
                    "resource_id": rid,
                    "name": self.ctx.GetResourceName(tex.resourceId),
                    "width": tex.width,
                    "height": tex.height,
                    "depth": tex.depth,
                    "array_size": tex.arraysize,
                    "mips": tex.mips,
                    "format": fmt,
                    "dimension": Helpers.enum_name("TextureType", tex.type),
                    "size_bytes": size,
                    "size_mb": round(size / (1024.0 * 1024.0), 3),
                    "is_render_target": is_rt,
                    "read_count": usage["read"],
                    "write_count": usage["write"],
                    "clear_count": usage["clear"],
                    "total_usage_count": usage["read"] + usage["write"] + usage["clear"],
                    "sample_event_ids": usage["eids"][:8],
                })

            key_fn = {
                "size": lambda e: -e["size_bytes"],
                "usage": lambda e: -e["total_usage_count"],
                "name": lambda e: e["name"] or e["resource_id"],
            }.get(sort_by, lambda e: -e["size_bytes"])
            rows.sort(key=key_fn)
            total = sum(r["size_bytes"] for r in rows)
            result["data"] = {
                "total_textures": len(rows),
                "total_size_mb": round(total / (1024.0 * 1024.0), 2),
                "sort_by": sort_by,
                "textures": rows[: max(1, int(top_n))],
            }

        self._invoke(callback)
        return result["data"]

    def get_texture_detail(self, resource_id):
        """Single texture metadata plus full usage event list"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        info = self.get_texture_info(resource_id)
        result = {"data": None, "error": None}

        def callback(controller):
            tex = self._find_texture_by_id(controller, resource_id)
            if not tex:
                result["error"] = "Texture not found: %s" % resource_id
                return
            usages = []
            try:
                for u in controller.GetUsage(tex.resourceId):
                    usages.append({
                        "event_id": int(u.eventId),
                        "usage": Helpers.enum_name("ResourceUsage", u.usage),
                    })
            except Exception:
                pass
            detail = dict(info)
            detail["name"] = self.ctx.GetResourceName(tex.resourceId)
            detail["usages"] = usages
            detail["usage_count"] = len(usages)
            result["data"] = detail

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def get_resource_usage(self, resource_id):
        """Usage history of any resource (texture or buffer)"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        result = {"data": None, "error": None}

        def callback(controller):
            rid = _live_resource_id(controller, resource_id)
            if rid is None:
                result["error"] = "Resource not found: %s" % resource_id
                return
            usages = []
            try:
                for u in controller.GetUsage(rid):
                    name = Helpers.enum_name("ResourceUsage", u.usage)
                    usages.append({
                        "event_id": int(u.eventId),
                        "usage": name,
                        "kind": _classify_usage(name),
                    })
            except Exception as e:
                result["error"] = "GetUsage failed: %s" % str(e)
                return
            result["data"] = {
                "resource_id": str(rid),
                "name": self.ctx.GetResourceName(rid),
                "count": len(usages),
                "usages": usages,
            }

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]


def _live_resource_id(controller, resource_id):
    """Find the replay's ResourceId. Reconstructed ids do not compare equal."""
    try:
        target = Parsers.extract_numeric_id(resource_id)
    except Exception:
        return None
    for collection in (controller.GetTextures(), controller.GetBuffers(), controller.GetResources()):
        for item in collection:
            if Parsers.extract_numeric_id(str(item.resourceId)) == target:
                return item.resourceId
    return None


def _usage_counts(controller, rid):
    counts = {"read": 0, "write": 0, "clear": 0, "eids": []}
    try:
        for u in controller.GetUsage(rid):
            kind = _classify_usage(Helpers.enum_name("ResourceUsage", u.usage))
            if kind in counts:
                counts[kind] += 1
            counts["eids"].append(int(u.eventId))
    except Exception:
        pass
    return counts


def _classify_usage(usage_str):
    s = usage_str
    if "Clear" in s:
        return "clear"
    if any(tok in s for tok in ("ColorTarget", "DepthStencil", "RWResource", "CopyDst", "ResolveDst")):
        return "write"
    if any(tok in s for tok in ("Resource", "CopySrc", "ResolveSrc", "InputTarget", "VertexBuffer", "IndexBuffer")):
        return "read"
    return "other"
