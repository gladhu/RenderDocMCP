"""
Vulkan tile-pass analysis: attachment load / store traffic from render pass loadOp / storeOp.

On a tile-based GPU every render pass is a tile pass: an attachment with loadOp LOAD is read from
memory at the start, one with storeOp STORE is written back at the end; CLEAR and DONT_CARE loads and
DONT_CARE stores cost no memory traffic. Unlike GL there is nothing to infer from clears and
invalidates, the ops are declared in the render pass (or in vkCmdBeginRendering for dynamic rendering).

Everything here is pure data in / data out (see BandwidthService for the RenderDoc side):
    model  = build_model(chunks)        chunks: [(name, params dict)] of the creation calls
    passes = build_vulkan_passes(stream, model, textures)
and the passes then go through bandwidth_service.evaluate_passes / find_issues like the GL ones.
"""

_UNUSED = 0xFFFFFFFF

# VkAttachmentLoadOp / VkAttachmentStoreOp (the *_NONE values come from extensions / Vulkan 1.3)
_LOAD, _CLEAR, _DONT_CARE_LOAD = 0, 1, 2
_STORE, _DONT_CARE_STORE = 0, 1

CREATE_RENDERPASS = ("vkCreateRenderPass", "vkCreateRenderPass2", "vkCreateRenderPass2KHR")
BEGIN_RENDERPASS = ("vkCmdBeginRenderPass", "vkCmdBeginRenderPass2", "vkCmdBeginRenderPass2KHR")
BEGIN_RENDERING = ("vkCmdBeginRendering", "vkCmdBeginRenderingKHR")
END_PASS = ("vkCmdEndRenderPass", "vkCmdEndRenderPass2", "vkCmdEndRenderPass2KHR",
            "vkCmdEndRendering", "vkCmdEndRenderingKHR")
MODEL_CALLS = frozenset(CREATE_RENDERPASS + ("vkCreateFramebuffer", "vkCreateImageView"))
STREAM_CALLS = frozenset(BEGIN_RENDERPASS + BEGIN_RENDERING + END_PASS)


def _as_list(value):
    return value if isinstance(value, list) else []


def _ref_index(ref):
    """attachment index of a VkAttachmentReference dict, or None when unused / absent"""
    if not isinstance(ref, dict):
        return None
    idx = ref.get("attachment")
    return None if idx is None or int(idx) == _UNUSED else int(idx)


def build_model(chunks):
    """Render passes, framebuffers and image views from the creation calls.

    chunks: iterable of (call name, params dict). Returns
      {"renderpasses": {rid: {"attachments": [...], "colors": [idx], "resolves": [idx], "depth": idx|None}},
       "framebuffers": {rid: {"renderpass": rid, "views": [rid], "width": w, "height": h}},
       "views": {rid: {"image": rid, "format": n}}}
    """
    model = {"renderpasses": {}, "framebuffers": {}, "views": {}}
    for name, p in chunks:
        info = p.get("CreateInfo")
        if not isinstance(info, dict):
            continue
        if name in CREATE_RENDERPASS:
            colors, resolves, depth = [], [], None
            for sub in _as_list(info.get("pSubpasses")):
                for ref in _as_list(sub.get("pColorAttachments")):
                    i = _ref_index(ref)
                    if i is not None and i not in colors:
                        colors.append(i)
                for ref in _as_list(sub.get("pResolveAttachments")):
                    i = _ref_index(ref)
                    if i is not None and i not in resolves:
                        resolves.append(i)
                d = _ref_index(sub.get("pDepthStencilAttachment"))
                if d is not None and depth is None:
                    depth = d
            model["renderpasses"][str(p.get("RenderPass"))] = {
                "attachments": _as_list(info.get("pAttachments")), "colors": colors, "resolves": resolves, "depth": depth,
            }
        elif name == "vkCreateFramebuffer":
            model["framebuffers"][str(p.get("Framebuffer"))] = {
                "renderpass": str(info.get("renderPass")), "views": [str(v) for v in _as_list(info.get("pAttachments"))],
                "width": info.get("width"), "height": info.get("height"),
            }
        elif name == "vkCreateImageView":
            model["views"][str(p.get("View"))] = {"image": str(info.get("image")), "format": info.get("format")}
    return model


def _op_load(op, has_stencil_op=None):
    if op == _LOAD:
        return {"needed": True, "skipped_by": None}
    return {"needed": False, "skipped_by": "clear" if op == _CLEAR else "dont_care"}


def _op_store(op):
    if op == _STORE:
        return {"needed": True, "skipped_by": None}
    return {"needed": False, "skipped_by": "dont_care" if op == _DONT_CARE_STORE else "none"}


def _attachment(role, image, tex, area, load, store, samples=None):
    tex = tex or {}
    w, h = tex.get("width", 0), tex.get("height", 0)
    if area:
        w, h = min(w or area[0], area[0]), min(h or area[1], area[1])
    samples = samples or tex.get("samples", 1)
    return {
        "role": role, "resource_id": image, "name": tex.get("name", ""), "width": w, "height": h,
        "format": tex.get("format"), "samples": samples, "swapchain": bool(tex.get("swapchain")),
        "bytes": w * h * tex.get("bpp", 4) * max(1, samples), "load": load, "store": store,
    }


def _stencil_aware(tex, depth_op, stencil_op, decide):
    """Depth and stencil share one image: the pass touches memory if either aspect does"""
    from .bandwidth_service import _has_stencil           # local import: bandwidth_service imports this module
    d = decide(depth_op)
    if tex and _has_stencil(tex.get("format", "")) and stencil_op is not None:
        s = decide(stencil_op)
        if s["needed"] and not d["needed"]:
            return s
    return d


def _render_pass_attachments(rb, model, textures):
    rp = model["renderpasses"].get(str(rb.get("renderPass")))
    fb = model["framebuffers"].get(str(rb.get("framebuffer")))
    if rp is None or fb is None:
        return None, "render pass or framebuffer creation was not found in the capture"
    extent = ((rb.get("renderArea") or {}).get("extent")) or {}
    area = (extent["width"], extent["height"]) if "width" in extent else (fb["width"], fb["height"])
    atts, color_n, resolve_n = [], 0, 0
    for idx, desc in enumerate(rp["attachments"]):
        if idx >= len(fb["views"]):
            continue
        view = model["views"].get(fb["views"][idx])
        if view is None:
            continue
        image = view["image"]
        tex = textures.get(image)
        if idx == rp["depth"]:
            role = "depth"
        elif idx in rp["resolves"]:
            role, resolve_n = "resolve%d" % resolve_n, resolve_n + 1
        elif idx in rp["colors"]:
            role, color_n = "color%d" % color_n, color_n + 1
        else:
            role = "other%d" % idx
        if role == "depth":
            load = _stencil_aware(tex, desc.get("loadOp"), desc.get("stencilLoadOp"), _op_load)
            store = _stencil_aware(tex, desc.get("storeOp"), desc.get("stencilStoreOp"), _op_store)
        else:
            load, store = _op_load(desc.get("loadOp")), _op_store(desc.get("storeOp"))
        atts.append(_attachment(role, image, tex, area, load, store, samples=desc.get("samples")))
    return atts, None


def _rendering_attachments(info, model, textures):
    """vkCmdBeginRendering (dynamic rendering). Field names follow VkRenderingInfo; not yet seen in a real capture."""
    extent = (((info.get("renderArea") or {}).get("extent")) or {})
    area = (extent["width"], extent["height"]) if "width" in extent else None
    atts = []

    def add(role, a, stencil=False):
        if not isinstance(a, dict):
            return
        view = model["views"].get(str(a.get("imageView")))
        if view is None:
            return
        tex = textures.get(view["image"])
        atts.append(_attachment(role, view["image"], tex, area, _op_load(a.get("loadOp")), _op_store(a.get("storeOp"))))
        resolve = model["views"].get(str(a.get("resolveImageView")))
        if resolve is not None and a.get("resolveImageView") not in (None, "ResourceId::0"):
            rtex = textures.get(resolve["image"])
            atts.append(_attachment(role + "_resolve", resolve["image"], rtex, area,
                                    {"needed": False, "skipped_by": "resolve_target"}, {"needed": True, "skipped_by": None}))

    for i, a in enumerate(_as_list(info.get("pColorAttachments"))):
        add("color%d" % i, a)
    add("depth", info.get("pDepthAttachment"))
    if info.get("pStencilAttachment") and not info.get("pDepthAttachment"):
        add("depth", info.get("pStencilAttachment"))
    return atts


def build_vulkan_passes(stream, model, textures):
    """Ordered tile passes with decided load / store per attachment.

    stream: collect_stream items for STREAM_CALLS (+ draw actions); returns (passes, problems)
    """
    passes, problems, cur = [], [], None

    def close(ev):
        if cur is not None:
            cur["end"] = max(cur["end"], ev["eid"])

    for ev in stream:
        name = ev["name"]
        action = ev.get("action")
        if name in BEGIN_RENDERPASS or name in BEGIN_RENDERING:
            close(ev)
            if name in BEGIN_RENDERPASS:
                atts, problem = _render_pass_attachments(ev["params"].get("RenderPassBegin") or {}, model, textures)
                fbo = str((ev["params"].get("RenderPassBegin") or {}).get("framebuffer"))
            else:
                info = ev["params"].get("RenderingInfo") or ev["params"].get("pRenderingInfo") or {}
                atts = _rendering_attachments(info, model, textures)
                problem = None if atts else "vkCmdBeginRendering without readable attachments"
                fbo = "dynamic_rendering"
            if problem:
                problems.append({"event_id": ev["eid"], "problem": problem})
                cur = None
                continue
            cur = {
                "index": len(passes), "fbo": fbo, "sig": None, "begin": ev["eid"], "end": ev["eid"],
                "marker": ev.get("marker", ""), "draws": 0, "ops": [], "outputs": None, "depth": None,
                "flush_causes": [], "attachments": atts,
            }
            passes.append(cur)
        elif name in END_PASS:
            close(ev)
            cur = None
        elif cur is not None:
            cur["end"] = max(cur["end"], ev["eid"])
            if action is not None and action["flags"] & 2:      # Drawcall
                cur["draws"] += 1
    return passes, problems
