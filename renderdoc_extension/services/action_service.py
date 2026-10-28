"""
Draw call / action operations service for RenderDoc.
"""

import renderdoc as rd

from ..utils import Serializers, Helpers


class ActionService:
    """Draw call / action operations service"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def get_draw_calls(
        self,
        include_children=True,
        marker_filter=None,
        exclude_markers=None,
        event_id_min=None,
        event_id_max=None,
        only_actions=False,
        flags_filter=None,
    ):
        """
        Get all draw calls/actions in the capture with optional filtering.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"actions": []}

        def callback(controller):
            root_actions = controller.GetRootActions()
            structured_file = controller.GetStructuredFile()
            result["actions"] = Serializers.serialize_actions(
                root_actions,
                structured_file,
                include_children,
                marker_filter=marker_filter,
                exclude_markers=exclude_markers,
                event_id_min=event_id_min,
                event_id_max=event_id_max,
                only_actions=only_actions,
                flags_filter=flags_filter,
            )

        self._invoke(callback)
        return result

    def get_frame_summary(self):
        """
        Get a summary of the current capture frame.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"summary": None}

        def callback(controller):
            root_actions = controller.GetRootActions()
            structured_file = controller.GetStructuredFile()
            api = Helpers.api_props(controller).pipelineType

            # Statistics counters
            stats = {
                "draw_calls": 0,
                "dispatches": 0,
                "clears": 0,
                "copies": 0,
                "presents": 0,
                "markers": 0,
            }
            total_actions = [0]

            def count_actions(actions):
                for action in actions:
                    total_actions[0] += 1
                    flags = action.flags

                    if flags & rd.ActionFlags.Drawcall:
                        stats["draw_calls"] += 1
                    if flags & rd.ActionFlags.Dispatch:
                        stats["dispatches"] += 1
                    if flags & rd.ActionFlags.Clear:
                        stats["clears"] += 1
                    if flags & rd.ActionFlags.Copy:
                        stats["copies"] += 1
                    if flags & rd.ActionFlags.Present:
                        stats["presents"] += 1
                    if flags & (rd.ActionFlags.PushMarker | rd.ActionFlags.SetMarker):
                        stats["markers"] += 1

                    if action.children:
                        count_actions(action.children)

            count_actions(root_actions)

            # Top-level markers
            top_markers = []
            for action in root_actions:
                if action.flags & rd.ActionFlags.PushMarker:
                    child_count = Helpers.count_children(action)
                    top_markers.append({
                        "name": action.GetName(structured_file),
                        "event_id": action.eventId,
                        "child_count": child_count,
                    })

            # Resource counts
            textures = controller.GetTextures()
            buffers = controller.GetBuffers()

            result["summary"] = {
                "api": Helpers.enum_name("GraphicsAPI", api),
                "total_actions": total_actions[0],
                "statistics": stats,
                "top_level_markers": top_markers,
                "resource_counts": {
                    "textures": len(textures),
                    "buffers": len(buffers),
                },
            }

        self._invoke(callback)
        return result["summary"]

    def get_draw_call_details(self, event_id):
        """Get detailed information about a specific draw call"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"details": None, "error": None}

        def callback(controller):
            # Move to the event
            controller.SetFrameEvent(event_id, True)

            action = self.ctx.GetAction(event_id)
            if not action:
                result["error"] = "No action at event %d" % event_id
                return

            structured_file = controller.GetStructuredFile()

            details = {
                "event_id": action.eventId,
                "action_id": action.actionId,
                "name": action.GetName(structured_file),
                "flags": Serializers.serialize_flags(action.flags),
                "num_indices": action.numIndices,
                "num_instances": action.numInstances,
                "base_vertex": action.baseVertex,
                "vertex_offset": action.vertexOffset,
                "instance_offset": action.instanceOffset,
                "index_offset": action.indexOffset,
            }

            # Output resources
            outputs = []
            for i, output in enumerate(action.outputs):
                if output != rd.ResourceId.Null():
                    outputs.append({"index": i, "resource_id": str(output)})
            details["outputs"] = outputs

            if action.depthOut != rd.ResourceId.Null():
                details["depth_output"] = str(action.depthOut)

            result["details"] = details

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["details"]

    def get_action_timings(
        self,
        event_ids=None,
        marker_filter=None,
        exclude_markers=None,
    ):
        """
        Get GPU timing information for actions.

        Args:
            event_ids: Optional list of specific event IDs to get timings for.
                      If None, returns timings for all actions.
            marker_filter: Only include actions under markers containing this string.
            exclude_markers: Exclude actions under markers containing these strings.

        Returns:
            Dictionary with:
            - available: Whether GPU timing counters are supported
            - unit: Time unit (typically "seconds")
            - timings: List of {event_id, name, duration_seconds, duration_ms}
            - total_duration_ms: Sum of all durations
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None, "error": None}

        def callback(controller):
            # Check if EventGPUDuration counter is available
            counters = controller.EnumerateCounters()
            if rd.GPUCounter.EventGPUDuration not in counters:
                result["data"] = {
                    "available": False,
                    "error": "GPU timing counters not supported on this capture",
                }
                return

            # Get counter description
            counter_desc = controller.DescribeCounter(rd.GPUCounter.EventGPUDuration)

            # Fetch timing data
            counter_results = controller.FetchCounters([rd.GPUCounter.EventGPUDuration])

            # Build event_id to timing map
            timing_map = {}
            target_counter = int(rd.GPUCounter.EventGPUDuration)
            for r in counter_results:
                if r.counter == target_counter:
                    # EventGPUDuration typically returns double
                    # Try to get the value in the most appropriate way
                    val = r.value.d  # double is the standard for duration
                    timing_map[r.eventId] = val

            # Get structured file for action names
            structured_file = controller.GetStructuredFile()
            root_actions = controller.GetRootActions()

            # Collect actions to report timings for
            timings = []
            total_duration = [0.0]

            def collect_timings(actions, parent_markers=None):
                if parent_markers is None:
                    parent_markers = []

                for action in actions:
                    action_name = action.GetName(structured_file)
                    current_markers = parent_markers[:]

                    # Track marker hierarchy
                    is_marker = bool(action.flags & (rd.ActionFlags.PushMarker | rd.ActionFlags.SetMarker))
                    if is_marker:
                        current_markers.append(action_name)

                    # Apply marker filter
                    if marker_filter:
                        marker_path = "/".join(current_markers)
                        if marker_filter.lower() not in marker_path.lower():
                            # Still recurse into children
                            if action.children:
                                collect_timings(action.children, current_markers)
                            continue

                    # Apply exclude filter
                    if exclude_markers:
                        skip = False
                        for exclude in exclude_markers:
                            for m in current_markers:
                                if exclude.lower() in m.lower():
                                    skip = True
                                    break
                            if skip:
                                break
                        if skip:
                            if action.children:
                                collect_timings(action.children, current_markers)
                            continue

                    # Check if we should include this event
                    event_id = action.eventId
                    include = True
                    if event_ids is not None:
                        include = event_id in event_ids

                    if include and event_id in timing_map:
                        duration_sec = timing_map[event_id]
                        duration_ms = duration_sec * 1000.0
                        timings.append({
                            "event_id": event_id,
                            "name": action_name,
                            "duration_seconds": duration_sec,
                            "duration_ms": duration_ms,
                        })
                        total_duration[0] += duration_ms

                    # Recurse into children
                    if action.children:
                        collect_timings(action.children, current_markers)

            collect_timings(root_actions)

            # Sort by event_id
            timings.sort(key=lambda x: x["event_id"])

            result["data"] = {
                "available": True,
                # timings are converted to seconds above; str(unit) is just an integer on this build
                "unit": "seconds",
                "timings": timings,
                "total_duration_ms": total_duration[0],
                "count": len(timings),
            }

        self._invoke(callback)

        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]

    def stat_instancing_efficiency(self, marker_filter="", top_n=10):
        """Instancing statistics over the frame's draw calls.

        Counts how many draws use GPU instancing, the instance count spread, and
        "wasted" instanced draws (2 instances or fewer, which would have cost the
        same as plain draws). Draws are bucketed by their first two marker
        levels (e.g. "Camera/DrawOpaqueObjects"), so the answer is per pass.
        Needs only the action tree, no replay of individual events.

        Args:
            marker_filter: keep draws whose marker path contains this text
            top_n: how many of the highest-instance-count draws to list
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")

        result = {"data": None}
        needle = marker_filter.lower() if marker_filter else ""

        def new_bucket():
            return {"total": 0, "instanced": 0, "wasted": 0, "instance_counts": [], "indices": 0}

        def callback(controller):
            sf = controller.GetStructuredFile()
            overall = new_bucket()
            buckets = {}
            top = []

            def walk(actions, path):
                for action in actions:
                    if action.children:
                        walk(action.children, path + [action.GetName(sf)])
                    if not (action.flags & rd.ActionFlags.Drawcall):
                        continue
                    if needle and not any(needle in m.lower() for m in path):
                        continue
                    instances = int(action.numInstances)
                    indices = int(action.numIndices)
                    instanced = instances > 1
                    wasted = instanced and instances <= 2
                    key = "/".join(path[:2]) or "(no marker)"
                    for bucket in (overall, buckets.setdefault(key, new_bucket())):
                        bucket["total"] += 1
                        bucket["indices"] += indices * max(1, instances)
                        if instanced:
                            bucket["instanced"] += 1
                            bucket["instance_counts"].append(instances)
                        if wasted:
                            bucket["wasted"] += 1
                    if instanced:
                        top.append({"event_id": int(action.eventId), "instances": instances,
                                    "indices": indices, "wasted": wasted, "pass": key})

            walk(controller.GetRootActions(), [])

            def summarize(b):
                counts = b["instance_counts"]
                return {
                    "total_draws": b["total"],
                    "instanced_draws": b["instanced"],
                    "instancing_ratio": round(b["instanced"] / float(b["total"]), 4) if b["total"] else 0,
                    "wasted_instanced_draws": b["wasted"],
                    "avg_instances": round(sum(counts) / float(len(counts)), 2) if counts else 0,
                    "max_instances": max(counts) if counts else 0,
                    "total_indices_submitted": b["indices"],
                }

            by_pass = []
            for key, bucket in buckets.items():
                row = summarize(bucket)
                row["marker"] = key
                by_pass.append(row)
            by_pass.sort(key=lambda r: -r["total_draws"])
            top.sort(key=lambda r: -r["instances"])
            result["data"] = {
                "overall": summarize(overall),
                "by_pass": by_pass,
                "top_instanced_draws": top[:max(0, int(top_n))],
            }

        self._invoke(callback)
        return result["data"]

    def get_api_calls(self, event_id=None, start_event=None, end_event=None,
                      include_params=True, name_filter="", max_calls=200):
        """Raw API calls recorded in the capture, with their parameters.

        The action tree only has draws, clears and markers; the state setup around
        them (glUniform*, glBindBuffer, glBlendFunc, barriers...) is stored as
        extra "events" of each action. Two ways to select calls:
          event_id: the calls that belong to that action (the setup before it)
          start_event / end_event: every call whose event id is in the range
        name_filter is a case-insensitive substring of the API function name.
        """
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        if event_id is None and start_event is None and end_event is None:
            raise ValueError("event_id, or start_event and end_event, is required")
        if event_id is None and (start_event is None or end_event is None):
            raise ValueError("start_event and end_event must be given together")
        if event_id is None and int(start_event) > int(end_event):
            raise ValueError("start_event must be <= end_event")
        limit = max(1, min(int(max_calls), 1000))
        needle = name_filter.lower() if name_filter else ""
        result = {"data": None, "error": None}

        def callback(controller):
            sf = controller.GetStructuredFile()
            collected = []

            def take(action):
                # Copy into plain tuples right away. Only actions obtained from this
                # controller are read: ctx.GetAction() hands back a UI-thread object,
                # and reading its .events array from the replay thread crashed qrenderdoc.
                events = action.events
                for i in range(len(events)):
                    collected.append((int(events[i].eventId), int(events[i].chunkIndex)))

            def find(actions, eid):
                for a in actions:
                    if a.eventId == eid:
                        return a
                    if a.children:
                        hit = find(a.children, eid)
                        if hit is not None:
                            return hit
                return None

            if event_id is not None:
                action = find(controller.GetRootActions(), int(event_id))
                if action is None:
                    result["error"] = "No action at event %d" % int(event_id)
                    return
                take(action)
                scope = {"event_id": int(event_id), "action": action.GetName(sf)}
            else:
                lo, hi = int(start_event), int(end_event)

                def walk(actions):
                    for a in actions:
                        # an action's events precede it, so its range is (previous action .. its own id]
                        if a.eventId >= lo:
                            take(a)
                        if a.children:
                            walk(a.children)
                        if a.eventId > hi:
                            return

                walk(controller.GetRootActions())
                collected[:] = [c for c in collected if lo <= c[0] <= hi]
                scope = {"start_event": lo, "end_event": hi}

            collected.sort()
            seen = set()
            calls = []
            matched = 0
            truncated = False
            for eid, chunk_index in collected:
                if (eid, chunk_index) in seen or chunk_index == 0xFFFFFFFF:
                    continue
                seen.add((eid, chunk_index))
                if chunk_index >= len(sf.chunks):
                    continue
                chunk = sf.chunks[chunk_index]
                name = str(chunk.name)
                if needle and needle not in name.lower():
                    continue
                matched += 1
                if len(calls) >= limit:
                    truncated = True
                    continue
                entry = {"event_id": eid, "name": name}
                try:
                    entry["duration_us"] = int(chunk.metadata.durationMicro)
                except Exception:
                    pass
                if include_params:
                    params = []
                    for i in range(chunk.NumChildren()):
                        child = chunk.GetChild(i)
                        if child is None:
                            continue
                        params.append({"name": str(child.name), "value": _sd_value(child)})
                    entry["params"] = params
                calls.append(entry)

            scope.update({
                "name_filter": name_filter or None,
                "matched": matched,
                "returned": len(calls),
                "truncated": truncated,
                "api_calls": calls,
            })
            result["data"] = scope

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]


# SDBasic values of this build (integers on purpose: reflecting SWIG enums crashes qrenderdoc)
_SD_CHUNK, _SD_STRUCT, _SD_ARRAY, _SD_NULL, _SD_BUFFER, _SD_STRING, _SD_ENUM = 0, 1, 2, 3, 4, 5, 6
_SD_UINT, _SD_SINT, _SD_FLOAT, _SD_BOOL, _SD_CHAR, _SD_RESOURCE, _SD_GPUADDR = 7, 8, 9, 10, 11, 12, 13


def _sd_value(obj, depth=0):
    """Structured-data object -> JSON-friendly value."""
    if obj is None:
        return None
    if depth >= 6:
        return "<max depth>"
    try:
        kind = int(obj.type.basetype)
    except Exception:
        return str(obj)
    if kind == _SD_NULL:
        return None
    if kind == _SD_UINT:
        return int(obj.data.basic.u)
    if kind == _SD_SINT:
        return int(obj.data.basic.i)
    if kind == _SD_FLOAT:
        return float(obj.data.basic.d)
    if kind == _SD_BOOL:
        return bool(obj.data.basic.b)
    if kind == _SD_CHAR:
        return str(obj.data.basic.c)
    if kind == _SD_STRING:
        for attr in ("str", "string"):          # attribute name differs between builds
            try:
                return str(getattr(obj.data, attr))
            except Exception:
                pass
        return "<string>"
    if kind == _SD_ENUM:
        text = ""
        try:
            text = str(obj.data.str)
        except Exception:
            pass
        return text or int(obj.data.basic.u)
    if kind == _SD_RESOURCE:
        return str(obj.data.basic.id)
    if kind == _SD_GPUADDR:
        return "0x%x" % int(obj.data.basic.u)
    if kind == _SD_BUFFER:
        return "<buffer %d bytes>" % int(obj.type.byteSize)
    if kind in (_SD_CHUNK, _SD_STRUCT):
        out = {}
        for i in range(obj.NumChildren()):
            child = obj.GetChild(i)
            if child is not None:
                out[str(child.name) or "[%d]" % i] = _sd_value(child, depth + 1)
        return out
    if kind == _SD_ARRAY:
        return [_sd_value(obj.GetChild(i), depth + 1) for i in range(min(obj.NumChildren(), 64))]
    return str(obj)
