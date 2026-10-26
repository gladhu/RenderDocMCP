"""
Common helper functions for RenderDoc operations.
"""

import re

import renderdoc as rd


class Helpers:
    """Common helper functions (static methods)"""

    @staticmethod
    def flatten_actions(actions):
        """Flatten hierarchical actions to a list"""
        flat = []
        for action in actions:
            flat.append(action)
            if action.children:
                flat.extend(Helpers.flatten_actions(action.children))
        return flat

    @staticmethod
    def count_children(action):
        """Count total number of children recursively"""
        count = 0
        if action.children:
            for child in action.children:
                count += 1
                count += Helpers.count_children(child)
        return count

    @staticmethod
    def get_all_shader_stages():
        """Get list of all shader stages"""
        return [
            rd.ShaderStage.Vertex,
            rd.ShaderStage.Hull,
            rd.ShaderStage.Domain,
            rd.ShaderStage.Geometry,
            rd.ShaderStage.Pixel,
            rd.ShaderStage.Compute,
        ]

    @staticmethod
    def gpu_family(api_name="", vendor_name=""):
        """Map RenderDoc GPUVendor / API strings onto a mobile family name.

        vendor is typically GPUVendor.Qualcomm / ARM / Imagination, not the
        marketing name Adreno / Mali / PowerVR.
        """
        blob = " ".join([api_name or "", vendor_name or ""]).upper()
        if "QUALCOMM" in blob or "ADRENO" in blob:
            return "adreno"
        if "POWERVR" in blob or "IMAGINATION" in blob:
            return "powervr"
        if "APPLE" in blob:
            return "apple"
        if "MALI" in blob or re.search(r"\bARM\b", blob):
            return "mali"
        return ""

    @staticmethod
    def gpu_quirks(api_name, vendor_name="", driver_name=""):
        """Known GPU/API caveats for mobile and tile-based hardware.

        driver_name is ignored. Structured-file versions are not GPU drivers.
        """
        family = Helpers.gpu_family(api_name, vendor_name)
        api = (api_name or "").upper()
        quirks = []
        if family == "adreno":
            quirks.extend([
                "Adreno: mediump 实际精度可能低于 spec，法线/反射建议 highp",
                "Adreno: R11G11B10_FLOAT 没有符号位，写入负值会被 clamp 或未定义",
            ])
        elif family == "mali":
            quirks.extend([
                "Mali: 大量 discard 会导致 early-Z 失效，增加 overdraw",
                "Mali: R11G11B10_FLOAT 存储负值行为未定义",
                "Mali: 逐 DC 计时会因 tile flush 偏大，请用 estimate_shading_cost",
            ])
        elif family == "powervr":
            quirks.extend([
                "PowerVR: TBDR，避免不必要的 framebuffer load/store 和 RT 切换",
                "PowerVR: 避免绑定过多采样器",
            ])
        elif family == "apple":
            quirks.append("Apple GPU: TBDR，注意 tile memory 带宽；float16 后处理通常更划算")
        if "OPENGL ES" in api or re.search(r"\bGLES\b", api) or "OPENGLES" in api:
            quirks.extend([
                "OpenGL ES: 注意 highp/mediump/lowp，错误精度会在真机上才显现",
                "OpenGL ES: 扩展依赖需按真机 GL_EXTENSIONS 核对",
            ])
        return quirks

    @staticmethod
    def estimate_texture_bytes(tex):
        """Rough uncompressed size including mips / array slices"""
        fmt = str(tex.format.Name()).upper()
        bpp = 4
        if "R32G32B32A32" in fmt:
            bpp = 16
        elif "R16G16B16A16" in fmt:
            bpp = 8
        elif "R11G11B10" in fmt or "R10G10B10" in fmt:
            bpp = 4
        elif "R8G8B8A8" in fmt or "B8G8R8A8" in fmt:
            bpp = 4
        elif "D24" in fmt or "D32" in fmt:
            bpp = 4
        elif "D16" in fmt or "R16" in fmt:
            bpp = 2
        elif "BC" in fmt or "ETC" in fmt or "ASTC" in fmt or "EAC" in fmt:
            bpp = 1
        elif "R8" in fmt and "R8G8" not in fmt:
            bpp = 1
        w = max(1, int(tex.width))
        h = max(1, int(tex.height))
        d = max(1, int(getattr(tex, "depth", 1) or 1))
        arr = max(1, int(getattr(tex, "arraysize", 1) or 1))
        mips = max(1, int(getattr(tex, "mips", 1) or 1))
        mip_factor = 1.333 if mips > 1 else 1.0
        return int(w * h * d * arr * bpp * mip_factor)

    @staticmethod
    def action_rt_key(action):
        """Stable key for the color+depth targets of an action"""
        colors = [str(o) for o in action.outputs if o != rd.ResourceId.Null()]
        depth = str(action.depthOut) if action.depthOut != rd.ResourceId.Null() else ""
        if not colors and not depth:
            return None
        return "|".join(colors) + "#d:" + depth
