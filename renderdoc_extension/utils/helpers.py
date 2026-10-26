"""
Common helper functions for RenderDoc operations.
"""

import re

import renderdoc as rd


# Integer values for this RenderDoc build. Do not reflect SWIG enums here:
# getattr/dir on them inside qrenderdoc crashes the process.
_ENUM_TABLES = {
    "GraphicsAPI": {0: "D3D11", 1: "D3D12", 2: "OpenGL", 3: "Vulkan"},
    "GPUVendor": {
        0: "Unknown", 1: "ARM", 2: "AMD", 3: "Broadcom", 4: "Imagination",
        5: "Intel", 6: "nVidia", 7: "Qualcomm", 8: "Verisilicon", 9: "Software", 10: "Samsung",
    },
    "VarType": {
        0: "Float", 1: "Double", 2: "Half", 3: "SInt", 4: "UInt", 5: "SShort",
        6: "UShort", 7: "SLong", 8: "ULong", 9: "SByte", 10: "UByte", 11: "Bool",
        12: "Enum", 13: "Struct", 14: "GPUPointer", 15: "ConstantBlock",
        16: "ReadOnlyResource", 17: "ReadWriteResource", 18: "Sampler", 255: "Unknown",
    },
    "ResourceUsage": {
        0: "Unused", 1: "VertexBuffer", 2: "IndexBuffer", 3: "VS_Constants",
        4: "HS_Constants", 5: "DS_Constants", 6: "GS_Constants", 7: "PS_Constants",
        8: "CS_Constants", 9: "TS_Constants", 10: "MS_Constants", 11: "All_Constants",
        12: "StreamOut", 13: "VS_Resource", 14: "HS_Resource", 15: "DS_Resource",
        16: "GS_Resource", 17: "PS_Resource", 18: "CS_Resource", 19: "TS_Resource",
        20: "MS_Resource", 21: "All_Resource", 22: "VS_RWResource", 23: "HS_RWResource",
        24: "DS_RWResource", 25: "GS_RWResource", 26: "PS_RWResource", 27: "CS_RWResource",
        28: "TS_RWResource", 29: "MS_RWResource", 30: "All_RWResource", 31: "InputTarget",
        32: "ColorTarget", 33: "DepthStencilTarget", 34: "Indirect", 35: "Clear",
        36: "Discard", 37: "GenMips", 38: "Resolve", 39: "ResolveSrc", 40: "ResolveDst",
        41: "Copy", 42: "CopySrc", 43: "CopyDst", 44: "Barrier", 45: "CPUWrite",
    },
    "Topology": {
        0: "Unknown", 1: "PointList", 2: "LineList", 3: "LineStrip", 4: "LineLoop",
        5: "TriangleList", 6: "TriangleStrip", 7: "TriangleFan", 8: "LineList_Adj",
        9: "LineStrip_Adj", 10: "TriangleList_Adj", 11: "TriangleStrip_Adj", 12: "PatchList",
    },
    "TextureType": {
        0: "Unknown", 1: "Buffer", 2: "Texture1D", 3: "Texture1DArray", 4: "Texture2D",
        5: "TextureRect", 6: "Texture2DArray", 7: "Texture2DMS", 8: "Texture2DMSArray",
        9: "Texture3D", 10: "TextureCube", 11: "TextureCubeArray",
    },
}


class Helpers:
    """Common helper functions (static methods)"""

    @staticmethod
    def enum_name(enum_key, value):
        """SWIG enums in this RenderDoc build stringify as their integer.

        Float is ``0``, ColorTarget is ``32``, ARM is ``1``. ``enum_key`` is the
        table name, not the SWIG type: reflecting those types crashes qrenderdoc.
        """
        try:
            if isinstance(value, int):
                iv = int(value)
                return _ENUM_TABLES.get(enum_key, {}).get(iv, str(iv))
        except Exception:
            pass
        text = str(value)
        if "." in text:
            text = text.rsplit(".", 1)[-1]
        if not text.lstrip("-").isdigit():
            return text
        try:
            iv = int(text)
        except Exception:
            return text
        return _ENUM_TABLES.get(enum_key, {}).get(iv, text)

    @staticmethod
    def recorded_gpu(capture_path):
        """Renderer string stored in the capture header, such as Mali-G610 MC6.

        Local replay reports the desktop GPU. Mobile checks need the device
        that originally recorded the frame.
        """
        if not capture_path:
            return ""
        try:
            with open(capture_path, "rb") as handle:
                data = handle.read(2 * 1024 * 1024)
        except Exception:
            return ""
        for token in (b"Mali", b"Adreno", b"PowerVR", b"Apple"):
            index = data.find(token)
            if index < 0:
                continue
            start = index
            while start > 0 and 32 <= data[start - 1] < 127:
                start -= 1
            if index >= 4:
                length = int.from_bytes(data[index - 4:index], "little")
                blob = data[index:index + length]
                if 1 <= length <= 64 and blob.startswith(token) and all(32 <= b < 127 for b in blob):
                    return blob.decode("ascii")
            end = index
            while end < len(data) and 32 <= data[end] < 127:
                end += 1
            return data[start:end].decode("ascii", "replace")
        return ""

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
    def gpu_family(api_name="", vendor_name="", recorded=""):
        """Map RenderDoc GPUVendor / API strings onto a mobile family name.

        vendor is typically GPUVendor.Qualcomm / ARM / Imagination, not the
        marketing name Adreno / Mali / PowerVR. recorded is the renderer
        string from the capture header.
        """
        blob = " ".join([api_name or "", vendor_name or "", recorded or ""]).upper()
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

        driver_name is the capture's recorded renderer string, for example
        ``Mali-G610 MC6``. Local replay still reports the desktop vendor.
        """
        family = Helpers.gpu_family(api_name, vendor_name, driver_name)
        api = (api_name or "").upper()
        recorded = (driver_name or "").upper()
        # Pipeline type OpenGL plus a mobile vendor is an OpenGL ES capture.
        if (family and api == "OPENGL") or "OPENGL ES" in recorded or "GLES" in recorded:
            api = "OPENGL ES"
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
