"""
Attachment load/store traffic per tile pass (tile-based mobile GPUs).

A tile GPU renders one framebuffer at a time into on-chip memory. Whenever it
starts rendering to a framebuffer it LOADs the attachments from main memory
(unless they are cleared or invalidated first) and when it moves on it STOREs
them back (unless they are invalidated). Those transfers are the bandwidth
cost of a pass, independent of what the shaders do.

The analysis is driven by the recorded GL calls:
  glBindFramebuffer                       selects the framebuffer being rendered
  glClear / glClearBuffer*                before the first draw: no load needed
  glInvalidateFramebuffer / glDiscard...  before the first draw: no load,
                                          after the last draw: no store
  glBlitFramebuffer / glReadPixels / ...  read a framebuffer: forces it to be stored
  glFlush / glFinish                      inside a pass the driver may have to end the pass early
A store is "wasted" when nothing reads the texture afterwards (and it is not
the swapchain image).

build_passes / evaluate_passes / find_issues are pure functions over plain
data so they can be tested without RenderDoc.
"""

import re


from ..utils import Helpers
from . import vk_passes as vk
from .action_service import _sd_value
from .gl_stream import collect_stream

# ActionFlags values of this RenderDoc build (see pass_service)
_FLAG_CLEAR = 1
_FLAG_DRAWCALL = 2
_FLAG_PUSH_MARKER = 64

# GL enum values
_GL_READ_FRAMEBUFFER = 36008
_GL_DRAW_FRAMEBUFFER = 36009
_GL_FRAMEBUFFER = 36160
_GL_COLOR_ATTACHMENT0 = 36064
_GL_DEPTH_ATTACHMENT = 36096
_GL_STENCIL_ATTACHMENT = 36128
_GL_DEPTH_STENCIL_ATTACHMENT = 33306
_GL_COLOR = 6144
_GL_DEPTH = 6145
_GL_STENCIL = 6146
_GL_DEPTH_STENCIL = 34041
_GL_COLOR_BUFFER_BIT = 0x4000
_GL_DEPTH_BUFFER_BIT = 0x100
_GL_STENCIL_BUFFER_BIT = 0x400

_DEFAULT_FBO = "ResourceId::0"

_CLEAR_CALLS = frozenset(["glClear", "glClearBufferfv", "glClearBufferiv", "glClearBufferuiv", "glClearBufferfi"])
_INVALIDATE_CALLS = frozenset([
    "glInvalidateFramebuffer", "glInvalidateSubFramebuffer", "glDiscardFramebufferEXT",
])
_READBACK_CALLS = frozenset([
    "glReadPixels", "glBlitFramebuffer", "glCopyTexImage2D", "glCopyTexSubImage2D",
    "glCopyTexSubImage3D", "glCopyImageSubData", "glCopyImageSubDataEXT", "glCopyImageSubDataOES",
])
_SYNC_CALLS = frozenset(["glFinish", "glFlush"])

# Calls whose parameters the analysis needs; every other chunk is skipped while collecting
_RELEVANT = frozenset(["glBindFramebuffer"]) | _CLEAR_CALLS | _INVALIDATE_CALLS | _READBACK_CALLS | _SYNC_CALLS

_MB = 1048576.0

# Usage kinds (resource_service._classify_usage) that count as someone consuming the texture
_READ_TOKENS = ("Resource", "CopySrc", "ResolveSrc", "InputTarget")


# ==================== pure analysis ====================


def format_bytes_per_pixel(name):
    """Bytes per pixel of an uncompressed format name such as R8G8B8A8_SRGB or D24_UNORM_S8_UINT"""
    upper = str(name).upper()
    if upper.startswith("D32") and "S8" in upper:
        return 8                                  # D32F + S8 is padded to 8 bytes
    parts = upper.split("_")
    # the packed component list is the first token; D24_UNORM_S8_UINT carries its stencil in a later token
    tokens = [parts[0]] + [t for t in parts[1:] if re.match(r"^[DS]\d+$", t)]
    bits = sum(int(n) for t in tokens for n in re.findall(r"[RGBADSX](\d+)", t))
    return max(1, bits // 8) if bits else 4


def _has_stencil(name):
    upper = str(name).upper()
    return "S8" in upper or upper.startswith("S8")


def _attachment_tags(values):
    """GL attachment enum values (glInvalidateFramebuffer list) -> set of role tags"""
    tags = set()
    for v in values or []:
        v = int(v)
        if _GL_COLOR_ATTACHMENT0 <= v < _GL_COLOR_ATTACHMENT0 + 16:
            tags.add("color%d" % (v - _GL_COLOR_ATTACHMENT0))
        elif v == _GL_COLOR:
            tags.add("color0")                    # default framebuffer spelling
        elif v in (_GL_DEPTH_ATTACHMENT, _GL_DEPTH):
            tags.add("depth")
        elif v in (_GL_STENCIL_ATTACHMENT, _GL_STENCIL):
            tags.add("stencil")
        elif v == _GL_DEPTH_STENCIL_ATTACHMENT:
            tags.update(["depth", "stencil"])
    return tags


def _clear_tags(ev):
    """Role tags covered by a glClear / glClearBuffer* event"""
    name, p = ev["name"], ev["params"]
    if name == "glClear":
        mask = int(p.get("mask") or 0)
        tags = set()
        if mask & _GL_COLOR_BUFFER_BIT:
            tags.update("color%d" % i for i in range(8))
        if mask & _GL_DEPTH_BUFFER_BIT:
            tags.add("depth")
        if mask & _GL_STENCIL_BUFFER_BIT:
            tags.add("stencil")
        return tags
    buffer_kind = int(p.get("buffer") or 0)
    if buffer_kind == _GL_COLOR:
        return set(["color%d" % int(p.get("drawbuffer") or 0)])
    if buffer_kind == _GL_DEPTH:
        return set(["depth"])
    if buffer_kind == _GL_STENCIL:
        return set(["stencil"])
    if buffer_kind == _GL_DEPTH_STENCIL:
        return set(["depth", "stencil"])
    return set()


def _event_kind(ev):
    name = ev["name"]
    action = ev.get("action")
    if action is not None and action["flags"] & _FLAG_DRAWCALL:
        return "draw"
    if name in _CLEAR_CALLS:
        return "clear"
    if name in _INVALIDATE_CALLS:
        return "invalidate"
    if name == "glBlitFramebuffer":
        return "blit"
    if name in _READBACK_CALLS:
        return "readback"
    if name in _SYNC_CALLS:
        return "sync"
    return None


def build_passes(stream):
    """Split the ordered GL event stream into tile passes (runs of work on one framebuffer).

    stream: list of {"eid", "name", "params", "marker", "action": None | {"flags", "outputs", "depth"}}
    Returns (passes, syncs). A framebuffer that is left and bound again later yields a new
    pass, which is exactly the tile-memory round trip the analysis wants to see.
    """
    passes = []
    syncs = []
    bound_draw = _DEFAULT_FBO
    bound_read = _DEFAULT_FBO
    last_of_fbo = {}
    cur = None

    def new_pass(fbo, ev):
        p = {
            "index": len(passes), "fbo": fbo, "sig": None, "begin": ev["eid"], "end": ev["eid"],
            "marker": ev.get("marker", ""), "draws": 0, "ops": [], "outputs": None, "depth": None,
            "flush_causes": [],
        }
        passes.append(p)
        last_of_fbo[fbo] = p
        return p

    for ev in stream:
        name, params = ev["name"], ev["params"]
        if name == "glBindFramebuffer":
            target = int(params.get("target") or 0)
            fb = str(params.get("framebuffer") or _DEFAULT_FBO)
            if target in (_GL_DRAW_FRAMEBUFFER, _GL_FRAMEBUFFER):
                bound_draw = fb
            if target in (_GL_READ_FRAMEBUFFER, _GL_FRAMEBUFFER):
                bound_read = fb
            continue
        kind = _event_kind(ev)
        if kind is None:
            continue
        if kind == "sync":
            syncs.append({"event_id": ev["eid"], "name": name})
            continue
        if kind in ("readback", "blit"):
            reader = last_of_fbo.get(bound_read)
            if reader is not None:
                reader["flush_causes"].append({"event_id": ev["eid"], "name": name})
            if kind == "readback":
                continue

        if kind == "invalidate":
            fbo = str(params.get("framebuffer") or bound_draw)
        else:
            fbo = bound_draw

        sig = None
        if kind == "draw":
            sig = "|".join(str(o) for o in ev["action"]["outputs"] if o) + "#" + str(ev["action"]["depth"] or "")
        if cur is None or cur["fbo"] != fbo or (sig is not None and cur["sig"] not in (None, sig)):
            cur = new_pass(fbo, ev)
        if sig is not None and cur["sig"] is None:
            cur["sig"] = sig
            cur["outputs"] = list(ev["action"]["outputs"])
            cur["depth"] = ev["action"]["depth"]
        cur["end"] = ev["eid"]

        if kind in ("draw", "blit"):
            cur["draws"] += 1
        elif kind == "clear":
            cur["ops"].append({"kind": "clear", "eid": ev["eid"], "draws_before": cur["draws"], "covers": _clear_tags(ev)})
        elif kind == "invalidate":
            cur["ops"].append({
                "kind": "invalidate", "eid": ev["eid"], "draws_before": cur["draws"],
                "covers": _attachment_tags(params.get("attachments")),
            })

    # draw-less passes (clear/invalidate only) borrow attachments seen on the same framebuffer
    attach_of_fbo = {}
    for p in passes:
        if p["outputs"] is not None:
            attach_of_fbo[p["fbo"]] = (p["outputs"], p["depth"])
    for p in passes:
        if p["outputs"] is None and p["fbo"] in attach_of_fbo:
            p["outputs"], p["depth"] = attach_of_fbo[p["fbo"]]
    return passes, syncs


def _covered(ops, predicate, role, stencil):
    """Which op kind covers `role` among ops selected by predicate (None if not covered)"""
    need = set([role])
    if role == "depth" and stencil:
        need.add("stencil")
    got = {}
    for op in ops:
        if predicate(op):
            for tag in op["covers"]:
                got.setdefault(tag, op["kind"])
    if not need.issubset(got):
        return None
    kinds = set(got[t] for t in need)
    return "clear" if "clear" in kinds else "invalidate"


def _gl_attachments(p, textures):
    """Load / store decisions of a GL pass from the clears and invalidates recorded around its draws"""
    slots = []
    for i, rid in enumerate(p["outputs"] or []):
        if rid:
            slots.append(("color%d" % i, rid))
    if p["depth"]:
        slots.append(("depth", p["depth"]))
    atts = []
    for role, rid in slots:
        tex = textures.get(rid) or {}
        stencil = role == "depth" and _has_stencil(tex.get("format", ""))
        size = tex.get("width", 0) * tex.get("height", 0) * tex.get("bpp", 4) * max(1, tex.get("samples", 1))
        how = _covered(p["ops"], lambda op: op["draws_before"] == 0, role, stencil)
        load = {"needed": how is None, "skipped_by": how}
        how = _covered(p["ops"], lambda op: op["draws_before"] == p["draws"], role, stencil)
        store = {"needed": how is None, "skipped_by": how}
        atts.append({
            "role": role, "resource_id": rid, "name": tex.get("name", ""),
            "width": tex.get("width"), "height": tex.get("height"), "format": tex.get("format"),
            "samples": tex.get("samples", 1), "swapchain": bool(tex.get("swapchain")),
            "bytes": size, "load": load, "store": store,
        })
    return atts


def evaluate_passes(passes, textures, reads_after):
    """Total the traffic of tile passes and link them: where each load comes from, who reads each store.

    A pass either carries "attachments" already decided (Vulkan: loadOp / storeOp) or gets them
    derived from its clear / invalidate ops (GL).

    textures:    {rid: {"name", "width", "height", "format", "samples", "bpp", "swapchain"}}
    reads_after: {rid: [event ids where the texture is consumed (sampled / copied)]}
    """
    last_writer = {}
    for p in passes:
        if p.get("attachments") is None:
            p["attachments"] = _gl_attachments(p, textures)
        for a in p["attachments"]:
            load = a["load"]
            if load["needed"]:
                writer = last_writer.get(a["resource_id"])
                if writer is None:
                    load["source"] = "none_in_frame"
                else:
                    load["source"] = "pass %d (event %d)" % (writer["pass"], writer["end"])
                    load["source_pass"] = writer["pass"]
                    load["reopened"] = writer["pass"] < p["index"] - 1
        for a in p["attachments"]:
            last_writer[a["resource_id"]] = {"pass": p["index"], "end": p["end"]}

    # a stored attachment is useful only if something reads it later
    for p in passes:
        for a in p["attachments"]:
            if not a["store"]["needed"]:
                continue
            later = [e for e in reads_after.get(a["resource_id"], []) if e > p["end"]]
            reloaded = None
            for q in passes[p["index"] + 1:]:
                for b in q["attachments"]:
                    if b["resource_id"] == a["resource_id"] and b["load"]["needed"]:
                        reloaded = q["index"]
                        break
                if reloaded is not None:
                    break
            a["store"]["read_later"] = bool(later) or reloaded is not None or a["swapchain"]
            if later:
                a["store"]["first_read_event"] = min(later)
            if reloaded is not None:
                a["store"]["reloaded_by_pass"] = reloaded

    for p in passes:
        p["load_bytes"] = sum(a["bytes"] for a in p["attachments"] if a["load"]["needed"])
        p["store_bytes"] = sum(a["bytes"] for a in p["attachments"] if a["store"]["needed"])
        p["wasted_store_bytes"] = sum(
            a["bytes"] for a in p["attachments"] if a["store"]["needed"] and not a["store"]["read_later"]
        )
    return passes


def _severity(nbytes):
    mb = nbytes / _MB
    return "high" if mb >= 4 else ("medium" if mb >= 1 else "low")


_DONT_CARE_STORE_GL = ("glInvalidateFramebuffer on it at the end of the pass "
                       "(Unity: RenderBufferStoreAction.DontCare / URP store action). "
                       "Ignore if it is a history buffer consumed next frame.")
_DONT_CARE_STORE_VK = ("Set storeOp to DONT_CARE for it (Unity: RenderBufferStoreAction.DontCare / URP store action). "
                       "Ignore if it is a history buffer consumed next frame.")


def _target(a):
    """Stable identity of an attachment across captures (no resource ids)"""
    return "%s:%s %sx%s %s" % (a["role"], a["name"], a["width"], a["height"], a["format"])


def find_issues(passes, syncs, api="gl"):
    issues = []
    reopened = {}
    for p in passes:
        where = {"pass_index": p["index"], "event_id": p["begin"], "marker": p["marker"]}
        for a in p["attachments"]:
            label = "%s (%s %dx%d %s)" % (a["role"], a["name"] or a["resource_id"], a["width"] or 0, a["height"] or 0, a["format"])
            if a["store"]["needed"] and not a["store"]["read_later"]:
                issues.append(dict(where, type="wasted_store", severity=_severity(a["bytes"]), bytes=a["bytes"], target=_target(a),
                                   detail="%s is written back to memory but never read afterwards" % label,
                                   suggestion=(_DONT_CARE_STORE_VK if api == "vulkan" else _DONT_CARE_STORE_GL)))
            if a["load"]["needed"] and a["load"].get("source") == "none_in_frame":
                issues.append(dict(where, type="load_without_clear", severity=_severity(a["bytes"]), bytes=a["bytes"], target=_target(a),
                                   detail="%s is loaded from memory at pass start but nothing in this frame wrote it" % label,
                                   suggestion=("Use loadOp CLEAR or DONT_CARE (Unity: RenderBufferLoadAction.Clear / DontCare) if the old contents "
                                              "are not needed, so the tile load is skipped." if api == "vulkan" else
                                              "Clear or invalidate it before the first draw so the tile load is skipped.")))
            if a["load"]["needed"] and a["load"].get("reopened"):
                reopened.setdefault(p["index"], []).append((a, label))
            if a["store"]["needed"] and a["samples"] > 1:
                issues.append(dict(where, type="msaa_stored", severity=_severity(a["bytes"]), bytes=a["bytes"], target=_target(a),
                                   detail="%s keeps %d samples in memory" % (label, a["samples"]),
                                   suggestion="Resolve in-tile (EXT_multisampled_render_to_texture / resolve attachment) "
                                              "and discard the multisampled buffer."))
        if p["flush_causes"] and p["store_bytes"]:
            first = p["flush_causes"][0]
            issues.append(dict(where, type="flush_by_readback", severity="medium", bytes=p["store_bytes"],
                               detail="%s at event %d reads this framebuffer, forcing it out of tile memory (%d such call(s))"
                                      % (first["name"], first["event_id"], len(p["flush_causes"])),
                               suggestion="Avoid mid-frame blit/copy/readback of the active render target; "
                                          "render into a separate target or read it at a pass boundary."))
    for index, hits in reopened.items():
        p = passes[index]
        total = sum(a["bytes"] for a, _ in hits)
        first_src = min(a["load"]["source_pass"] for a, _ in hits)
        between = ["#%d %s" % (q["index"], (q["marker"].split(" > ")[-1] or q["fbo"])) for q in passes[first_src + 1:index]]
        issues.append({
            "type": "render_target_reopened", "severity": _severity(total * 2), "bytes": total * 2,
            "pass_index": index, "event_id": p["begin"], "marker": p["marker"],
            "target": ", ".join(sorted(_target(a) for a, _ in hits)),
            "detail": "%s rendered again after other pass(es) [%s]; the first pass stored them and this one loads them back (load + store each)"
                      % (", ".join(label for _, label in hits), ", ".join(between)),
            "suggestion": "Avoid breaking the pass: do the work of the intervening pass(es) without leaving this "
                          "framebuffer (e.g. CopyDepth -> use a resolved/copy-free depth, or run it before/after the pass), "
                          "or move the later draws before it.",
        })
    in_pass = {}
    for s_ in syncs:
        if s_["name"] != "glFlush":
            continue
        for p in passes:
            if p["draws"] and p["begin"] < s_["event_id"] < p["end"]:
                in_pass.setdefault(p["index"], []).append(s_["event_id"])
                break
    for index, eids in in_pass.items():
        p = passes[index]
        issues.append({
            "type": "flush_inside_pass", "severity": "low" if p["store_bytes"] < _MB * 4 else "medium",
            "bytes": p["store_bytes"] * len(eids), "pass_index": index, "event_id": eids[0], "marker": p["marker"],
            "detail": "glFlush x%d inside this pass (events %s). Depending on the driver this ends the tile pass early "
                      "and costs an extra store + load of its attachments each time (upper bound counted)"
                      % (len(eids), ", ".join(str(e) for e in eids[:6])),
            "suggestion": "Remove glFlush from inside the pass (often added by native plugins / UI middleware).",
        })
    finish = [s_ for s_ in syncs if s_["name"] == "glFinish"]
    if finish:
        issues.append({
            "type": "glFinish", "severity": "medium", "bytes": 0,
            "event_id": finish[0]["event_id"], "pass_index": None, "marker": "",
            "detail": "glFinish called %d time(s); it stalls the pipeline and flushes all pending tile work" % len(finish),
            "suggestion": "Remove glFinish from the render path.",
        })
    rank = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: (rank[i["severity"]], -i["bytes"]))
    return issues


def _mb(nbytes):
    return round(nbytes / _MB, 2)


def summarize(passes, issues, top_n, sort_by):
    load = sum(p["load_bytes"] for p in passes)
    store = sum(p["store_bytes"] for p in passes)
    wasted = sum(p["wasted_store_bytes"] for p in passes)
    rows = list(passes)
    if sort_by == "bytes":
        rows.sort(key=lambda p: -(p["load_bytes"] + p["store_bytes"]))
    out_passes = []
    for p in rows[:top_n]:
        out_passes.append({
            "pass_index": p["index"], "marker": p["marker"], "framebuffer": p["fbo"],
            "event_id_begin": p["begin"], "event_id_end": p["end"], "draw_count": p["draws"],
            "load_mb": _mb(p["load_bytes"]), "store_mb": _mb(p["store_bytes"]),
            "wasted_store_mb": _mb(p["wasted_store_bytes"]),
            "flush_causes": p["flush_causes"],
            "attachments": [{
                "role": a["role"], "resource_id": a["resource_id"], "name": a["name"],
                "size": "%sx%s" % (a["width"], a["height"]), "format": a["format"], "samples": a["samples"],
                "mb": _mb(a["bytes"]), "load": a["load"], "store": a["store"],
            } for a in p["attachments"]],
        })
    traffic = load + store
    return {
        "pass_count": len(passes),
        "total": {
            "load_mb": _mb(load), "store_mb": _mb(store), "traffic_mb": _mb(traffic),
            "wasted_store_mb": _mb(wasted),
            "gb_per_s_at_60fps": round(traffic * 60 / (1024.0 * _MB), 2),
        },
        "issue_count": len(issues),
        "issues": [dict(i, mb=_mb(i["bytes"])) for i in issues[:50]],
        "passes": out_passes,
        "passes_returned": len(out_passes),
        "note": "Upper bound: whole attachments are counted, not the tiles actually touched, and the GPU's "
                "framebuffer compression is ignored. Use it to rank passes and to compare before/after.",
    }


# ==================== RenderDoc side ====================


class BandwidthService:
    """Tile-pass attachment bandwidth analysis"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def analyze_pass_bandwidth(self, top_n=30, sort_by="bytes"):
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        result = {"data": None, "error": None}

        def callback(controller):
            try:
                result["data"] = self._analyze(controller, int(top_n), sort_by)
            except Exception as e:                # BlockInvoke swallows exceptions
                import traceback
                result["error"] = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc())

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def _analyze(self, controller, top_n, sort_by):
        api = Helpers.enum_name("GraphicsAPI", self.ctx.APIProps().pipelineType)
        return analyze_capture(controller, api, self.ctx.GetResourceName, top_n, sort_by)


def analyze_capture(controller, api, name_of, top_n=30, sort_by="bytes"):
    """Tile-pass bandwidth of the loaded capture. name_of(ResourceId) -> display name."""
    is_vulkan = api == "Vulkan"
    if not is_vulkan and api not in ("OpenGL", "OpenGLES"):
        raise ValueError("analyze_pass_bandwidth supports OpenGL / GLES / Vulkan captures only (this one is %s)" % api)

    textures = {}
    rids = {}
    for tex in controller.GetTextures():
        rid = str(tex.resourceId)
        fmt = str(tex.format.Name())
        name = name_of(tex.resourceId)
        # Not tex.creationFlags: for composite values (Vulkan swapchain images) building the Python enum
        # access-violates qrenderdoc inside enum.py. RenderDoc names the presentable images itself.
        swap = name.startswith(("Swapchain", "Backbuffer"))
        textures[rid] = {
            "name": name, "width": int(tex.width), "height": int(tex.height),
            "format": fmt, "samples": int(tex.msSamp), "bpp": format_bytes_per_pixel(fmt), "swapchain": swap,
        }
        rids[rid] = tex.resourceId

    problems = []
    if is_vulkan:
        sf = controller.GetStructuredFile()
        model_chunks = [(str(c.name), _sd_value(c, limit=12)) for c in sf.chunks if str(c.name) in vk.MODEL_CALLS]
        model = vk.build_model(model_chunks)
        stream = collect_stream(controller, vk.STREAM_CALLS)
        passes, problems = vk.build_vulkan_passes(stream, model, textures)
        syncs = []
    else:
        stream = collect_stream(controller, _RELEVANT)
        passes, syncs = build_passes(stream)

    reads_after = {}
    for p in passes:
        if p.get("attachments") is not None:
            wanted = [a["resource_id"] for a in p["attachments"]]
        else:
            wanted = list(p["outputs"] or []) + [p["depth"]]
        for rid in wanted:
            if rid and rid not in reads_after and rid in rids:
                reads_after[rid] = _read_events(controller, rids[rid])

    evaluate_passes(passes, textures, reads_after)
    issues = find_issues(passes, syncs, api="vulkan" if is_vulkan else "gl")
    data = summarize(passes, issues, top_n, sort_by)
    data["api"] = api
    if problems:
        data["unparsed_passes"] = problems
    return data


def _read_events(controller, rid):
    events = []
    for u in controller.GetUsage(rid):
        name = Helpers.enum_name("ResourceUsage", u.usage)
        if "Clear" in name:
            continue
        if any(tok in name for tok in _READ_TOKENS) and not any(
                tok in name for tok in ("ColorTarget", "DepthStencil", "CopyDst", "ResolveDst", "RWResource")):
            events.append(int(u.eventId))
    return events
