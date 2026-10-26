"""
Render-pass enumeration using official RenderDoc APIs.

Resolution order:
  1. ActionFlags.PassBoundary + BeginPass (Vulkan / D3D12)
  2. Marker groups that contain draws
  3. Synthetic split on color/depth target changes (plus Clear)
"""

import renderdoc as rd

from ..utils import Helpers


class PassService:
    """Frame pass list and per-pass detail"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def get_renderpass_list(self, sort_by="index"):
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        result = {"data": None}

        def callback(controller):
            passes = _enumerate_passes(controller, self.ctx)
            if sort_by == "draw_count":
                passes.sort(key=lambda p: -p["draw_count"])
            elif sort_by == "event_id":
                passes.sort(key=lambda p: p["event_id_begin"])
            result["data"] = {
                "pass_count": len(passes),
                "sort_by": sort_by,
                "passes": passes,
            }

        self._invoke(callback)
        return result["data"]

    def get_pass_detail(self, pass_index):
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        result = {"data": None, "error": None}

        def callback(controller):
            passes = _enumerate_passes(controller, self.ctx)
            if pass_index < 0 or pass_index >= len(passes):
                result["error"] = "pass_index %d out of range (0-%d)" % (
                    pass_index, max(0, len(passes) - 1)
                )
                return
            target = passes[pass_index]
            sf = controller.GetStructuredFile()
            draws = []
            for action in Helpers.flatten_actions(controller.GetRootActions()):
                if not (action.flags & rd.ActionFlags.Drawcall):
                    continue
                eid = action.eventId
                if eid < target["event_id_begin"] or eid > target["event_id_end"]:
                    continue
                controller.SetFrameEvent(eid, False)
                pipe = controller.GetPipelineState()
                ps = pipe.GetShader(rd.ShaderStage.Pixel)
                draws.append({
                    "event_id": eid,
                    "name": action.GetName(sf),
                    "num_indices": int(action.numIndices),
                    "num_instances": int(action.numInstances),
                    "pixel_shader": str(ps) if ps != rd.ResourceId.Null() else None,
                })
            target["draws"] = draws
            result["data"] = target

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]


def _flag(name, default=0):
    return int(getattr(rd.ActionFlags, name, default) or 0)


def _enumerate_passes(controller, ctx):
    sf = controller.GetStructuredFile()
    actions = Helpers.flatten_actions(controller.GetRootActions())
    ranges = _from_pass_flags(actions, sf)
    source = "pass_boundary"
    if not ranges:
        ranges = _from_markers(controller.GetRootActions(), sf)
        source = "marker"
    if not ranges:
        ranges = _from_rt_changes(actions, sf)
        source = "rt_change"

    passes = []
    for i, rng in enumerate(ranges):
        draws = [a for a in actions
                 if (a.flags & rd.ActionFlags.Drawcall)
                 and rng["begin"] <= a.eventId <= rng["end"]]
        if not draws and source != "pass_boundary":
            continue
        first = draws[0] if draws else None
        colors, depth = _attachments(ctx, controller, first)
        passes.append({
            "index": i,
            "name": rng["name"],
            "synthetic": rng.get("synthetic", False),
            "source": source,
            "event_id_begin": rng["begin"],
            "event_id_end": rng["end"],
            "first_draw_event_id": first.eventId if first else None,
            "draw_count": len(draws),
            "color_targets": colors,
            "depth_target": depth,
        })
    for i, p in enumerate(passes):
        p["index"] = i
    return passes


def _from_pass_flags(actions, sf):
    begin_flag = _flag("PassBoundary") | _flag("BeginPass")
    if not _flag("PassBoundary") or not _flag("BeginPass"):
        begin_flag = 0
    starts = []
    for action in actions:
        flags = int(action.flags)
        if begin_flag and (flags & _flag("PassBoundary")) and (flags & _flag("BeginPass")):
            starts.append(action)
    if not starts:
        return []
    ranges = []
    for i, action in enumerate(starts):
        end = starts[i + 1].eventId - 1 if i + 1 < len(starts) else actions[-1].eventId
        ranges.append({
            "name": action.GetName(sf),
            "begin": action.eventId,
            "end": end,
            "synthetic": False,
        })
    return ranges


def _from_markers(roots, sf):
    ranges = []

    def walk(nodes):
        for action in nodes:
            if action.children and (action.flags & rd.ActionFlags.PushMarker):
                draws = [c for c in Helpers.flatten_actions(action.children)
                         if c.flags & rd.ActionFlags.Drawcall]
                if draws:
                    ranges.append({
                        "name": action.GetName(sf),
                        "begin": action.eventId,
                        "end": draws[-1].eventId,
                        "synthetic": False,
                    })
                else:
                    walk(action.children)
            elif action.children:
                walk(action.children)

    walk(roots)
    return ranges


def _from_rt_changes(actions, sf):
    ranges = []
    current = None
    last_key = None
    for action in actions:
        if not (action.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.Clear)):
            continue
        key = Helpers.action_rt_key(action)
        if key is None:
            continue
        new_pass = (action.flags & rd.ActionFlags.Clear) or (key != last_key)
        if new_pass:
            if current is not None:
                if action.eventId > current["begin"]:
                    current["end"] = action.eventId - 1
                ranges.append(current)
            current = {
                "name": action.GetName(sf) or ("Clear" if action.flags & rd.ActionFlags.Clear else "Pass"),
                "begin": action.eventId,
                "end": action.eventId,
                "synthetic": True,
            }
            last_key = key
        elif current is not None:
            current["end"] = action.eventId
    if current is not None:
        ranges.append(current)
    return ranges


def _attachments(ctx, controller, action):
    if action is None:
        return [], None
    colors = []
    for o in action.outputs:
        if o == rd.ResourceId.Null():
            continue
        colors.append(_tex_info(ctx, controller, o))
    depth = None
    if action.depthOut != rd.ResourceId.Null():
        depth = _tex_info(ctx, controller, action.depthOut)
    return colors, depth


def _tex_info(ctx, controller, rid):
    info = {"resource_id": str(rid), "name": ctx.GetResourceName(rid)}
    for tex in controller.GetTextures():
        if tex.resourceId == rid:
            info["width"] = tex.width
            info["height"] = tex.height
            info["format"] = str(tex.format.Name())
            break
    return info
