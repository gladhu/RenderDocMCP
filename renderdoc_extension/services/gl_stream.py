"""
Ordered stream of the recorded GL calls a pure-Python analysis needs.

Reads the structured file only (no replay, so no SetFrameEvent per draw): a whole frame takes a
fraction of a second where a per-draw pipeline read takes minutes on a remote device.
"""

import renderdoc as rd

from .action_service import _sd_value

# ActionFlags values of this RenderDoc build (see pass_service)
FLAG_CLEAR = 1
FLAG_DRAWCALL = 2
FLAG_PUSH_MARKER = 64


def collect_stream(controller, relevant):
    """Calls named in `relevant` plus every draw / clear action, sorted by event id.

    Each item: {"eid", "name", "params" (by name), "args" (values in order), "path" (marker names),
                "marker" (last two levels), "action": None | {"flags", "outputs", "depth"}}
    """
    sf = controller.GetStructuredFile()
    chunks = sf.chunks
    seen = set()
    stream = []

    def take(action, path):
        # Only actions obtained from this controller are read (see ActionService.get_api_calls)
        info = None
        if action.flags & (FLAG_DRAWCALL | FLAG_CLEAR):
            info = {
                "flags": int(action.flags),
                "outputs": [str(o) if o != rd.ResourceId.Null() else None for o in action.outputs],
                "depth": str(action.depthOut) if action.depthOut != rd.ResourceId.Null() else None,
            }
        events = action.events
        for i in range(len(events)):
            eid, ci = int(events[i].eventId), int(events[i].chunkIndex)
            if ci == 0xFFFFFFFF or ci >= len(chunks) or (eid, ci) in seen:
                continue
            seen.add((eid, ci))
            name = str(chunks[ci].name)
            is_action = eid == int(action.eventId) and info is not None
            if name not in relevant and not is_action:
                continue
            params, args = {}, []
            if name in relevant:
                chunk = chunks[ci]
                for k in range(chunk.NumChildren()):
                    child = chunk.GetChild(k)
                    if child is not None:
                        value = _sd_value(child)
                        params[str(child.name)] = value
                        args.append(value)
            stream.append({
                "eid": eid, "name": name, "params": params, "args": args,
                "path": path, "marker": " > ".join(path[-2:]),
                "action": info if is_action else None,
            })

    def walk(actions, path):
        for action in actions:
            take(action, path)
            if action.children:
                sub = path
                if action.flags & FLAG_PUSH_MARKER:
                    sub = path + [action.GetName(sf)]
                walk(action.children, sub)

    walk(controller.GetRootActions(), [])
    stream.sort(key=lambda e: e["eid"])
    return stream
