"""
Render-pass enumeration using official RenderDoc APIs.

Resolution order:
  1. ActionFlags.PassBoundary + BeginPass (Vulkan / D3D12)
  2. Marker groups. A marker is its own pass only for draws that are its
     direct children; nested markers are separate passes. Draws outside any
     marker are reported as one unmarked group.
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
                "passes": [_public_pass(p) for p in passes],
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
            wanted = target.get("_event_ids")
            sf = controller.GetStructuredFile()
            draws = []
            for action in Helpers.flatten_actions(controller.GetRootActions()):
                if not (action.flags & rd.ActionFlags.Drawcall):
                    continue
                eid = action.eventId
                if wanted is not None:
                    if eid not in wanted:
                        continue
                elif eid < target["event_id_begin"] or eid > target["event_id_end"]:
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
            detail = _public_pass(target)
            detail["draws"] = draws
            result["data"] = detail

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]


# ActionFlags values for this RenderDoc build. getattr() on the SWIG enum crashes qrenderdoc.
_ACTION_FLAGS = {
    "Clear": 1,
    "Drawcall": 2,
    "Dispatch": 4,
    "PushMarker": 64,
    "PopMarker": 128,
    "SetMarker": 32,
    "Present": 256,
    "PassBoundary": 8192,
    "BeginPass": 4194304,
    "EndPass": 8388608,
}


def _flag(name, default=0):
    return _ACTION_FLAGS.get(name, default)


def _public_pass(p):
    return dict((k, v) for k, v in p.items() if not k.startswith("_"))


def _draw_actions(actions):
    return [a for a in actions if a.flags & rd.ActionFlags.Drawcall]


def _enumerate_passes(controller, ctx):
    sf = controller.GetStructuredFile()
    actions = Helpers.flatten_actions(controller.GetRootActions())
    ranges = _from_pass_flags(actions, sf)
    for rng in ranges:
        rng["source"] = "pass_boundary"
    if not ranges:
        ranges = _from_markers(controller.GetRootActions(), sf)
        for rng in ranges:
            rng["source"] = "marker"
        covered = set()
        for rng in ranges:
            covered.update(rng.get("_event_ids") or [])
        unmarked = [a for a in _draw_actions(actions) if a.eventId not in covered]
        if unmarked and ranges:
            ids = [int(a.eventId) for a in unmarked]
            ranges.append({
                "name": "Unmarked",
                "begin": ids[0],
                "end": ids[-1],
                "_event_ids": set(ids),
                "synthetic": True,
                "source": "unmarked",
            })
    if not ranges:
        ranges = _from_rt_changes(actions, sf)
        for rng in ranges:
            rng["source"] = "rt_change"

    passes = []
    for rng in ranges:
        if rng.get("_event_ids") is not None:
            draws = [a for a in _draw_actions(actions) if a.eventId in rng["_event_ids"]]
        else:
            draws = [a for a in _draw_actions(actions)
                     if rng["begin"] <= a.eventId <= rng["end"]]
        if not draws and rng.get("source") != "pass_boundary":
            continue
        first = draws[0] if draws else None
        colors, depth = _attachments(ctx, controller, first)
        passes.append({
            "index": 0,
            "name": rng["name"],
            "synthetic": rng.get("synthetic", False),
            "source": rng.get("source"),
            "_event_ids": rng.get("_event_ids"),
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
    """One pass per marker, counting only its direct draw children.

    Nested markers are walked separately so an outer Camera marker does not
    swallow DrawOpaque / DrawTransparent.
    """
    ranges = []

    def walk(nodes):
        for action in nodes:
            if action.children and (action.flags & rd.ActionFlags.PushMarker):
                direct = [c for c in action.children if c.flags & rd.ActionFlags.Drawcall]
                if direct:
                    ids = [int(c.eventId) for c in direct]
                    ranges.append({
                        "name": action.GetName(sf),
                        "begin": ids[0],
                        "end": ids[-1],
                        "_event_ids": set(ids),
                        "synthetic": False,
                    })
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
