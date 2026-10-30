"""
Resolve "the colour / depth target of event N" to a texture, on every API.

D3D and GL put the targets in ActionDescription.outputs / depthOut. Vulkan leaves them empty: the targets
belong to the render pass, so they come from the call-stream model (draw_targets.event_targets). Resource ids
in that map are strings.
"""

import renderdoc as rd

from .draw_targets import event_targets
from ..utils import Helpers, Parsers


def parse_target(target):
    """"color0".. -> ("color", index); "depth" -> ("depth", 0)"""
    t = (target or "color0").strip().lower()
    if t in ("depth", "d", "ds"):
        return "depth", 0
    if t.startswith("color") and (t[5:] or "0").isdigit():
        return "color", int(t[5:] or 0)
    raise ValueError("Unknown target %r (use color0, color1, ... or depth)" % target)


def pick_target_id(target, outputs, depth_out, fallback):
    """
    Resource id (as str) of the requested target, or None.

    outputs: [str ids] from the action (null ids already removed), depth_out: str or None;
    fallback: ([colour ids], depth id) from the render-pass model, used when the action itself has none.
    """
    kind, idx = parse_target(target)
    if kind == "depth":
        return depth_out or (fallback[1] if fallback else None)
    if idx < len(outputs):
        return outputs[idx]
    colors = fallback[0] if fallback else []
    return colors[idx] if idx < len(colors) else None


def resolve_target(ctx, controller, event_id, resource_id, target, textures=None, targets_cache=None):
    """
    TextureDescription for resource_id, or for the target of event_id (the event must already be set with
    SetFrameEvent when the API reports targets through the pipeline). targets_cache: {} reused across calls
    so the render-pass model is built once.
    """
    textures = list(controller.GetTextures()) if textures is None else textures
    if resource_id:
        # by number, as ResourceService does: a ResourceId built from a number is not == the real object in
        # this build (the old object comparison never found any texture)
        want = Parsers.extract_numeric_id(str(resource_id))
        for tex in textures:
            if Parsers.extract_numeric_id(str(tex.resourceId)) == want:
                return tex
        raise ValueError("Texture not found: %s" % resource_id)
    by_id = dict((str(t.resourceId), t) for t in textures)
    if event_id is None:
        raise ValueError("event_id or resource_id is required")
    action = ctx.GetAction(int(event_id))
    if action is None:
        raise ValueError("Event not found: %d" % int(event_id))
    outputs = [str(o) for o in action.outputs if o != rd.ResourceId.Null()]
    depth = str(action.depthOut) if action.depthOut != rd.ResourceId.Null() else None
    fallback = None
    if not outputs and not depth:
        cache = targets_cache if targets_cache is not None else {}
        if "map" not in cache:
            api = Helpers.enum_name("GraphicsAPI", Helpers.api_props(controller).pipelineType)
            cache["map"] = event_targets(ctx, controller, api)
        fallback = cache["map"].get(int(event_id))
    rid = pick_target_id(target, outputs, depth, fallback)
    if rid is None or rid not in by_id:
        raise ValueError("Event %d has no %s target (is it a draw call?)" % (int(event_id), target))
    return by_id[rid]
