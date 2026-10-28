"""
GL / GLES fixed-function and program state per draw, rebuilt from the recorded call stream.

Replaying every draw to read its pipeline state costs ~100 ms per draw on a remote device (a
SetFrameEvent re-executes the commands up to that draw). The same state can be followed through
the calls themselves (glEnable, glBlendFunc, glUseProgram, ...), which needs no replay at all.

Pure functions over plain data (see gl_stream.collect_stream), so they are testable without RenderDoc.
Tracked state starts from the GL defaults; a capture whose first draws rely on state set before
the frame will therefore see at most a few spurious changes at the very start.
"""

_BLEND, _DEPTH_TEST, _CULL_FACE = 3042, 2929, 2884

# calls the tracker reads
STATE_CALLS = frozenset([
    "glEnable", "glDisable", "glEnablei", "glDisablei",
    "glBlendFunc", "glBlendFuncSeparate", "glBlendEquation", "glBlendEquationSeparate",
    "glColorMask", "glDepthFunc", "glDepthMask", "glCullFace", "glUseProgram",
])

_ONE, _ZERO, _FUNC_ADD, _LESS, _BACK = 1, 0, 32774, 513, 1029


def _initial():
    return {
        "blend": False, "depth_test": False, "cull_face": False,
        "src_rgb": _ONE, "dst_rgb": _ZERO, "src_a": _ONE, "dst_a": _ZERO,
        "eq_rgb": _FUNC_ADD, "eq_a": _FUNC_ADD, "mask": (True, True, True, True),
        "depth_func": _LESS, "depth_mask": True, "cull_mode": _BACK, "program": None,
    }


def _apply(state, name, args):
    """Update `state` for one call; unknown or malformed calls are ignored"""
    try:
        if name in ("glEnable", "glDisable"):
            on = name == "glEnable"
            cap = int(args[0])
        elif name in ("glEnablei", "glDisablei"):
            # indexed variants: only the first render target is followed
            on = name == "glEnablei"
            cap = int(args[0])
            if int(args[1]) != 0:
                return
        elif name == "glBlendFunc":
            state["src_rgb"] = state["src_a"] = int(args[0])
            state["dst_rgb"] = state["dst_a"] = int(args[1])
            return
        elif name == "glBlendFuncSeparate":
            state["src_rgb"], state["dst_rgb"], state["src_a"], state["dst_a"] = (int(a) for a in args[:4])
            return
        elif name == "glBlendEquation":
            state["eq_rgb"] = state["eq_a"] = int(args[0])
            return
        elif name == "glBlendEquationSeparate":
            state["eq_rgb"], state["eq_a"] = int(args[0]), int(args[1])
            return
        elif name == "glColorMask":
            state["mask"] = tuple(bool(a) for a in args[:4])
            return
        elif name == "glDepthFunc":
            state["depth_func"] = int(args[0])
            return
        elif name == "glDepthMask":
            state["depth_mask"] = bool(args[0])
            return
        elif name == "glCullFace":
            state["cull_mode"] = int(args[0])
            return
        elif name == "glUseProgram":
            state["program"] = str(args[0])
            return
        else:
            return
    except (IndexError, TypeError, ValueError):
        return
    if cap == _BLEND:
        state["blend"] = on
    elif cap == _DEPTH_TEST:
        state["depth_test"] = on
    elif cap == _CULL_FACE:
        state["cull_face"] = on


def track_draws(stream):
    """One entry per draw call: its program and the blend / depth / cull state in force, plus
    the render-target key and marker path. Values are hashable so they compare with ==."""
    state = _initial()
    draws = []
    for ev in stream:
        action = ev.get("action")
        if action is not None and action["flags"] & 2:           # Drawcall
            colors = [o for o in action["outputs"] if o]
            draws.append({
                "eid": ev["eid"], "path": ev.get("path", []), "marker": ev.get("marker", ""),
                "program": state["program"],
                "blend": (state["blend"], state["src_rgb"], state["dst_rgb"], state["eq_rgb"],
                          state["src_a"], state["dst_a"], state["mask"]),
                "depth": (state["depth_test"], state["depth_func"]),
                "cull": state["cull_mode"] if state["cull_face"] else "None",
                "render_target": ("|".join(colors) + "#d:" + (action["depth"] or "")) if colors or action["depth"] else None,
                "blend_on": state["blend"],
                "depth_write": state["depth_mask"],
                "src_rgb": state["src_rgb"], "dst_rgb": state["dst_rgb"],
            })
        elif ev["name"] in STATE_CALLS:
            _apply(state, ev["name"], ev.get("args", []))
    return draws


def draw_state(d):
    """blend / depth fields in the shape get_draw_coverage(with_state=True) reports"""
    return {
        "blend_enabled": d["blend_on"],
        # Unity writes opaque materials as "Blend One Zero": blend is on but the colour is simply overwritten
        "blend_opaque": (d["src_rgb"] == _ONE and d["dst_rgb"] == _ZERO) if d["blend_on"] else None,
        "blend_color": "%d/%d" % (d["src_rgb"], d["dst_rgb"]) if d["blend_on"] else None,
        "depth_test": d["depth"][0],
        "depth_write": d["depth_write"],
    }


def summarize_state_changes(draws, track, marker_filter=None, max_draws=200):
    """Count changes between consecutive draws and find runs of identical state.

    Same result as DiagnosisService.analyze_state_changes computes by replaying every draw.
    """
    if marker_filter:
        draws = [d for d in draws if any(marker_filter in p for p in d["path"])]
    field_of = {"shader": "program", "blend": "blend", "depth": "depth", "cull": "cull", "render_target": "render_target"}
    change_counts = dict((t, 0) for t in track)
    saw_value = dict((t, False) for t in track)
    prev = None
    runs, current = [], None
    analyzed = 0
    for d in draws[:max(1, int(max_draws))]:
        analyzed += 1
        cur = dict((t, d[field_of[t]]) for t in track)
        for t, v in cur.items():
            if v is not None:
                saw_value[t] = True
        changed = [t for t in track if prev is not None and cur[t] != prev[t]]
        for t in changed:
            change_counts[t] += 1
        if prev is None or not changed:
            if current is None:
                current = {"start_event": d["eid"], "end_event": d["eid"], "count": 1}
            else:
                current["end_event"] = d["eid"]
                current["count"] += 1
        else:
            if current and current["count"] >= 2:
                runs.append(current)
            current = {"start_event": d["eid"], "end_event": d["eid"], "count": 1}
        prev = cur
    if current and current["count"] >= 2:
        runs.append(current)
    runs.sort(key=lambda r: -r["count"])
    return {
        "analyzed_draws": analyzed, "total_draws": len(draws), "change_counts": change_counts,
        "unavailable": [t for t in ("shader", "blend", "depth", "cull") if t in track and not saw_value.get(t)],
        "runs": runs,
    }
