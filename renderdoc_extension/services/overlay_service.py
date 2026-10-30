"""
Debug overlays (wireframe, depth/stencil test, back-face culling, viewport/scissor, NaN, quad overdraw,
triangle size, ...) rendered by the replay on the device, saved as an image and summarised numerically.

What the overlay texture holds (measured on a Mali-G610 Vulkan replay, not the heat map the RenderDoc UI
shows): pass/fail style overlays are flat colours (green / red / purple ...) with alpha 0 where the draw
did not touch the pixel; the quad overdraw overlays hold the overdraw COUNT per pixel (0 = not covered, and
only fragments that passed the depth test count); the triangle size overlays hold the area in pixels of the
triangle covering the pixel, stored as half float so anything >= 65504 reads as inf.
The pixels are read back once; colours are counted on the raw bytes with collections.Counter (C speed) and
only the distinct values are decoded.
"""

import collections
import math
import os
import struct
import tempfile

import renderdoc as rd

from .targets import resolve_target
from ..utils import Helpers

# name -> (DebugOverlay member, kind, what it shows)
OVERLAYS = {
    "drawcall": ("Drawcall", "legend", "the draw call's own pixels highlighted"),
    "wireframe": ("Wireframe", "legend", "the draw's triangle edges"),
    "depth": ("Depth", "legend", "depth test: passed (green) / failed (red) pixels of this draw"),
    "stencil": ("Stencil", "legend", "stencil test: passed (green) / failed (red) pixels of this draw"),
    "backface_cull": ("BackfaceCull", "legend", "face culling: kept (green) / culled (red) pixels of this draw"),
    "viewport_scissor": ("ViewportScissor", "marked", "viewport and scissor rectangles of the draw"),
    "nan": ("NaN", "marked", "pixels of the target holding NaN / Inf / negative values"),
    "clipping": ("Clipping", "marked", "pixels clipped by the depth range or the viewport"),
    "quad_overdraw_pass": ("QuadOverdrawPass", "overdraw",
                           "quad overdraw of every draw of the pass up to this one (count per pixel)"),
    "quad_overdraw_draw": ("QuadOverdrawDraw", "overdraw", "quad overdraw of this draw only (count per pixel)"),
    "triangle_size_pass": ("TriangleSizePass", "tri_size",
                           "area in pixels of the triangle covering each pixel, whole pass up to this draw"),
    "triangle_size_draw": ("TriangleSizeDraw", "tri_size", "area in pixels of the triangle covering each pixel"),
}

# overlay -> {label: (r, g, b)}: the flat colours of the "legend" overlays
LEGENDS = {
    "depth": {"passed": (0.0, 1.0, 0.0), "failed": (1.0, 0.0, 0.0)},
    "stencil": {"passed": (0.0, 1.0, 0.0), "failed": (1.0, 0.0, 0.0)},
    "backface_cull": {"kept": (0.0, 1.0, 0.0), "culled": (1.0, 0.0, 0.0)},
    "drawcall": {"draw": (0.8, 0.1, 0.8)},
    "wireframe": {"wire": (0.784, 1.0, 0.0)},
}
TOLERANCE = 0.12
HALF_MAX = 65504.0
TRI_BUCKETS = [(4, "<4 px"), (16, "4-16 px"), (64, "16-64 px"), (256, "64-256 px"), (1024, "256-1024 px"),
               (HALF_MAX, "1024-65504 px"), (float("inf"), ">=65504 px")]


# ---------------------------------------------------------------- pure helpers

def color_counts(data, pixel_count):
    """
    [((r, g, b, a), count)] sorted by count, from raw RGBA8 / RGBA16F / RGBA32F bytes (bytes per pixel is
    inferred from the length). Values are floats in the texture's own range (0..1 for RGBA8).
    """
    if pixel_count <= 0 or len(data) < pixel_count * 4:
        raise ValueError("overlay data is %d bytes for %d pixels" % (len(data), pixel_count))
    bpp = len(data) // pixel_count
    mv = memoryview(bytes(data)[: pixel_count * bpp])
    if bpp == 4:
        raw = collections.Counter(mv.cast("I"))
        decode = lambda v: tuple(b / 255.0 for b in struct.pack("<I", v))
    elif bpp == 8:
        raw = collections.Counter(mv.cast("Q"))
        decode = lambda v: struct.unpack("<4e", struct.pack("<Q", v))
    elif bpp == 16:
        raw = collections.Counter(struct.iter_unpack("<4f", mv))
        decode = lambda v: v
    else:
        raise ValueError("unsupported overlay pixel size: %d bytes" % bpp)
    return sorted(((tuple(decode(v)), c) for v, c in raw.items()), key=lambda kv: -kv[1])


def _num(x):
    """JSON-safe number: inf / nan become strings"""
    if isinstance(x, float) and (math.isinf(x) or math.isnan(x)):
        return "nan" if math.isnan(x) else ("inf" if x > 0 else "-inf")
    return round(x, 3) if isinstance(x, float) else x


def _top(counts, total, n):
    return [{"rgba": [_num(x) for x in col], "pixels": c, "pct": round(100.0 * c / total, 2)}
            for col, c in counts[: max(1, int(n))]]


def summarise_legend(counts, legend, tolerance=TOLERANCE, top_n=6):
    """Pixels per legend label; everything else (alpha < 0.5 or another colour) is "untouched_or_other" """
    total = sum(c for _, c in counts) or 1
    labels = collections.OrderedDict((k, 0) for k in legend)
    other = 0
    for (r, g, b, a), c in counts:
        best, best_d = None, tolerance
        if a >= 0.5:
            for name, (lr, lg, lb) in legend.items():
                d = max(abs(r - lr), abs(g - lg), abs(b - lb))
                if d <= best_d:
                    best, best_d = name, d
        if best is None:
            other += c
        else:
            labels[best] += c
    labels["untouched_or_other"] = other
    return {
        "total_pixels": total,
        "by_label": dict((k, {"pixels": v, "pct_of_target": round(100.0 * v / total, 3)}) for k, v in labels.items()),
        "top_colors": _top(counts, total, top_n),
    }


def summarise_marked(counts, top_n=6):
    """Pixels the overlay marked (alpha > 0) and their colours"""
    total = sum(c for _, c in counts) or 1
    marked = [(col, c) for col, c in counts if col[3] > 0.01]
    n = sum(c for _, c in marked)
    return {
        "total_pixels": total,
        "marked_pixels": n,
        "marked_pct": round(100.0 * n / total, 3),
        "top_colors": _top(marked, total, top_n),
    }


def summarise_overdraw(counts, top_n=8):
    """Overdraw count per covered pixel (value 0 = not covered)"""
    total = sum(c for _, c in counts) or 1
    by_value = collections.Counter()
    for (r, g, b, a), c in counts:
        by_value[int(round(r)) if not math.isnan(r) and not math.isinf(r) else -1] += c
    covered = sum(c for v, c in by_value.items() if v > 0)
    shaded = sum(v * c for v, c in by_value.items() if v > 0)
    hist = sorted(((v, c) for v, c in by_value.items() if v > 0), key=lambda kv: -kv[1])
    return {
        "total_pixels": total,
        "covered_pixels": covered,
        "covered_pct": round(100.0 * covered / total, 3),
        "mean_overdraw": round(shaded / float(covered), 3) if covered else 0.0,
        "max_overdraw": max((v for v, _ in hist), default=0),
        "pixels_overdrawn_gt1_pct": round(100.0 * sum(c for v, c in hist if v > 1) / covered, 3) if covered else 0.0,
        "shaded_quad_pixels": shaded,
        "histogram": [{"overdraw": v, "pixels": c, "pct_of_covered": round(100.0 * c / covered, 3)}
                      for v, c in hist[: max(1, int(top_n))]],
    }


def summarise_tri_size(counts):
    """Pixels by the area of the triangle covering them (pixel-weighted: micro triangles cover few pixels)"""
    total = sum(c for _, c in counts) or 1
    buckets = collections.OrderedDict((label, 0) for _, label in TRI_BUCKETS)
    covered = 0
    for (r, g, b, a), c in counts:
        if a < 0.5 or r <= 0 or math.isnan(r):
            continue
        covered += c
        for limit, label in TRI_BUCKETS:
            if r < limit or math.isinf(limit):
                buckets[label] += c
                break
    return {
        "total_pixels": total,
        "covered_pixels": covered,
        "covered_pct": round(100.0 * covered / total, 3),
        "pixels_by_triangle_area": dict((k, {"pixels": v, "pct_of_covered": round(100.0 * v / covered, 3) if covered else 0.0})
                                        for k, v in buckets.items()),
        "note": "area is stored as half float: values >= 65504 px read as inf",
    }


def summarise(kind, overlay, counts):
    if kind == "legend":
        return summarise_legend(counts, LEGENDS[overlay])
    if kind == "marked":
        return summarise_marked(counts)
    if kind == "overdraw":
        return summarise_overdraw(counts)
    if kind == "tri_size":
        return summarise_tri_size(counts)
    raise ValueError("unknown overlay kind %r" % kind)


def add_warnings(kind, stats, api):
    """
    Say so when an overlay returned nothing because the API cannot produce it, instead of letting zeros read as
    "no overdraw". Measured: the quad overdraw overlay is all zero on OpenGL ES (Mali-G610) even for a full-screen
    draw, while Vulkan fills it in.
    """
    if kind == "overdraw" and stats.get("covered_pixels", 1) == 0 and api == "OpenGL":
        stats["supported"] = False
        stats["warning"] = ("the quad overdraw overlay is not supported on OpenGL / OpenGL ES (it counted no pixel even "
                            "for a full-screen draw); use analyze_overdraw (depth-test based) instead")
    return stats


def overlay_names():
    return sorted(OVERLAYS)


# ---------------------------------------------------------------- service

class OverlayService:
    """Render debug overlays on the replay device"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def render_overlay(self, event_id, overlay="depth", target="color0", resource_id=None,
                       output_path=None, analyze=True):
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        key = (overlay or "").lower()
        if key not in OVERLAYS:
            raise ValueError("Unknown overlay %r (use one of: %s)" % (overlay, ", ".join(overlay_names())))
        member, kind, meaning = OVERLAYS[key]
        enum = getattr(rd.DebugOverlay, member)
        if not output_path:
            output_path = os.path.join(tempfile.gettempdir(), "renderdoc_mcp", "overlays",
                                       "overlay_e%d_%s.png" % (int(event_id), key))
        folder = os.path.dirname(os.path.abspath(output_path))
        if not os.path.isdir(folder):
            os.makedirs(folder)

        result = {"data": None, "error": None}

        def callback(controller):
            try:
                run(controller)
            except Exception as e:                              # BlockInvoke swallows exceptions
                result["error"] = "%s: %s" % (type(e).__name__, e)

        def run(controller):
            controller.SetFrameEvent(int(event_id), True)
            textures = list(controller.GetTextures())
            tex = resolve_target(self.ctx, controller, event_id, resource_id, target, textures)
            output = controller.CreateOutput(rd.CreateHeadlessWindowingData(64, 64), rd.ReplayOutputType.Texture)
            try:
                disp = rd.TextureDisplay()
                disp.resourceId = tex.resourceId
                disp.overlay = enum
                output.SetTextureDisplay(disp)
                output.Display()
                overlay_id = output.GetDebugOverlayTexID()
                if overlay_id == rd.ResourceId.Null():
                    raise ValueError("overlay %r is not available at event %d (not a draw call, or the "
                                     "device/API does not support it)" % (key, int(event_id)))
                save = rd.TextureSave()
                save.resourceId = overlay_id
                save.destType = rd.FileType.PNG
                save.mip = 0
                save.alpha = rd.AlphaMapping.Discard
                ok = controller.SaveTexture(save, output_path)
                if (not ok.OK()) if hasattr(ok, "OK") else (not ok):
                    raise ValueError("SaveTexture failed: %s" % str(ok))
                data = {
                    "event_id": int(event_id),
                    "overlay": key,
                    "meaning": meaning,
                    "target": {"resource_id": str(tex.resourceId), "name": self.ctx.GetResourceName(tex.resourceId),
                               "width": int(tex.width), "height": int(tex.height), "format": str(tex.format.Name())},
                    "image_path": output_path,
                }
                if key == "viewport_scissor":
                    data["viewport_scissor"] = _viewport_scissor(controller.GetPipelineState())
                if analyze:
                    raw = controller.GetTextureData(overlay_id, rd.Subresource(0, 0, 0))
                    stats = summarise(kind, key, color_counts(raw, int(tex.width) * int(tex.height)))
                    api = Helpers.enum_name("GraphicsAPI", Helpers.api_props(controller).pipelineType)
                    data["stats"] = add_warnings(kind, stats, api)
                result["data"] = data
            finally:
                output.Shutdown()

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]


def _viewport_scissor(pipe):
    out = {}
    try:
        vp = pipe.GetViewport(0)
        out["viewport"] = {"x": vp.x, "y": vp.y, "width": vp.width, "height": vp.height,
                           "min_depth": vp.minDepth, "max_depth": vp.maxDepth}
    except Exception:
        pass
    try:
        sc = pipe.GetScissor(0)
        out["scissor"] = {"x": sc.x, "y": sc.y, "width": sc.width, "height": sc.height,
                          "enabled": bool(sc.enabled)}
    except Exception:
        pass
    return out
