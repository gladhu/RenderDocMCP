"""
Frame performance report built from a metrics snapshot (see compare.build_snapshot).

Turns the raw numbers into a scorecard per area and a ranked list of concrete actions,
each with its evidence (events, markers) and an impact figure.

Impact percentages are relative to the area they belong to (share of attachment traffic,
of fragment cycles, of draw calls), so they rank actions inside an area reliably but are
not comparable across areas. Nothing here converts to milliseconds: offline-compiler cycles
and attachment bytes are relative costs, and the thresholds in the scorecard are rules of
thumb for mobile, not measurements.
"""

_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

# (watch, problem) thresholds; rules of thumb for a mid/high-end mobile GPU
_DRAW_CALLS = (500, 1000)
_AVOIDABLE_TRAFFIC = (0.10, 0.25)      # share of attachment traffic that could be removed
_OCCLUDED_PIXELS = (0.10, 0.25)        # share of shaded pixels that fail the depth test


def _assess(value, limits):
    watch, problem = limits
    if value is None:
        return "n/a"
    return "problem" if value >= problem else ("watch" if value >= watch else "ok")


def _priority_by_pct(pct, high=15.0, medium=5.0):
    return "high" if pct >= high else ("medium" if pct >= medium else "low")


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else 0.0


def _where(marker, event_id=None, pass_index=None):
    out = {}
    if marker:
        out["marker"] = marker
    if event_id is not None:
        out["event_id"] = event_id
    if pass_index is not None:
        out["tile_pass"] = pass_index
    return out


# ==================== areas ====================


def _bandwidth(snap, actions, scorecard):
    bw = snap.get("bandwidth") or {}
    traffic = bw.get("traffic_mb") or 0
    issues = bw.get("issues", [])
    avoidable = 0.0
    for i in issues:
        mb = i.get("mb", 0)
        if i["type"] in ("wasted_store", "load_without_clear", "render_target_reopened"):
            avoidable += mb
        pct = _pct(mb, traffic)
        if not mb and i["type"] != "glFinish":
            continue
        actions.append({
            "area": "bandwidth", "kind": i["type"],
            "priority": _priority_by_pct(pct) if mb else "medium",
            "title": "%s: %s" % (i["type"], i.get("target") or i.get("detail", "")[:70]),
            "impact": "~%.2f MB/frame (%.1f%% of attachment traffic)" % (mb, pct) if mb else "pipeline stall",
            "impact_pct": pct,
            "where": _where(i.get("marker"), i.get("event_id"), i.get("pass_index")),
            "evidence": i.get("detail", ""),
            "how": i.get("suggestion", ""),
        })
    if bw.get("avoidable_mb") is not None:      # over all issues, not only the 50 stored ones
        avoidable = bw["avoidable_mb"]
    avoidable = min(avoidable, traffic)
    share = avoidable / traffic if traffic else None
    scorecard.append({
        "area": "bandwidth", "metric": "Attachment traffic (MB/frame)", "value": traffic,
        "assessment": "n/a",
        "note": "%.2f GB/s at 60 fps; upper bound, whole attachments, no framebuffer compression" % (bw.get("gb_per_s_at_60fps") or 0),
    })
    scorecard.append({
        "area": "bandwidth", "metric": "Avoidable share of attachment traffic",
        "value": None if share is None else round(100 * share, 1),
        "assessment": _assess(share, _AVOIDABLE_TRAFFIC),
        "note": "%.2f MB of %.2f MB from wasted stores, loads without clear and reopened targets (issues may overlap)"
                % (avoidable, traffic),
    })
    return avoidable


def _shading(snap, actions, scorecard):
    sh = snap.get("shading")
    if not sh:
        scorecard.append({"area": "shading", "metric": "Fragment shading cost", "value": None, "assessment": "n/a",
                          "note": "no offline-compiler data in this snapshot (needs malioc / aoc)"})
        return
    total = sh.get("total_cycles") or 0
    passed = sh.get("total_pixels") or 0
    occluded = sh.get("total_occluded_pixels") or 0
    occ_share = occluded / float(passed + occluded) if passed + occluded else None
    scorecard.append({
        "area": "shading", "metric": "Fragment cycles (sum pixels x cycles/pixel)", "value": total, "assessment": "n/a",
        "note": "%s on %s; relative cost, not wall time" % (sh.get("compiler"), sh.get("gpu")),
    })
    scorecard.append({
        "area": "shading", "metric": "Pixels shaded then rejected by depth",
        "value": None if occ_share is None else round(100 * occ_share, 1),
        "assessment": _assess(occ_share, _OCCLUDED_PIXELS),
        "note": "%d of %d shaded pixels; on tile GPUs hidden-surface removal may already skip some of them" % (occluded, passed + occluded),
    })
    shaders = sh.get("shaders", [])
    for s in shaders[:8]:
        share = _pct(s["cycles"], total)
        if share < 3.0:
            continue
        hints = []
        if s.get("late_zs_draws"):
            hints.append("%d draw(s) use late-Z (discard / depth write in shader): early-Z is lost, occluded pixels still get shaded"
                         % s["late_zs_draws"])
        hints.append("check ALU vs texture bound in analyze_shader_cost(event_ids=%s); trim features, use mediump, "
                     "cut texture fetches" % s.get("sample_event_ids", [])[:2])
        actions.append({
            "area": "shading", "priority": _priority_by_pct(share, high=15.0, medium=7.0),
            "title": "Heavy fragment shader: %s" % (
                ", ".join(s.get("textures", [])[:3]) or "event %s" % (s.get("sample_event_ids") or ["?"])[0]),
            "impact": "%.1f%% of fragment cycles (%.2f cycles/pixel x %d px over %d draws)"
                      % (share, s["cycles_per_pixel"], s["pixels"], s["draw_count"]),
            "impact_pct": share,
            "where": {"event_ids": s.get("sample_event_ids", [])[:5]},
            "evidence": "cycles=%d pixels=%d occluded=%d work_registers=%s"
                        % (s["cycles"], s["pixels"], s["occluded_pixels"], s.get("work_registers")),
            "how": "; ".join(hints),
        })
    total_occ = float(occluded) or 1.0
    detailed = snap.get("overdraw") is not None      # analyze_overdraw findings replace this coarse per-shader view
    for s in ([] if detailed else sorted(shaders, key=lambda s: -s["occluded_pixels"])[:3]):
        denom = s["pixels"] + s["occluded_pixels"]
        if denom and s["occluded_pixels"] / float(denom) >= 0.25 and s["occluded_pixels"] / total_occ >= 0.10:
            pct = _pct(s["occluded_pixels"], total_occ)
            actions.append({
                "area": "overdraw", "priority": _priority_by_pct(pct, high=30.0, medium=15.0),
                "title": "Shaded but hidden: %s" % (
                    ", ".join(s.get("textures", [])[:3]) or "event %s" % (s.get("sample_event_ids") or ["?"])[0]),
                "impact": "%.1f%% of all rejected pixels; %.0f%% of this shader's pixels fail the depth test"
                          % (pct, 100.0 * s["occluded_pixels"] / denom),
                "impact_pct": pct,
                "where": {"event_ids": s.get("sample_event_ids", [])[:5]},
                "evidence": "pixels=%d occluded=%d draws=%d" % (s["pixels"], s["occluded_pixels"], s["draw_count"]),
                "how": "Draw opaque geometry front-to-back, add a depth pre-pass for this material, or cull what is "
                       "behind other geometry.",
            })


def _draw_analysis(snap, actions, scorecard):
    geo, over = snap.get("geometry_cost"), snap.get("overdraw")
    cycles = (snap.get("shading") or {}).get("total_cycles") or 0
    if geo:
        t = geo["totals"]
        scorecard.append({
            "area": "geometry", "metric": "Triangles submitted", "value": t["triangles"], "assessment": "n/a",
            "note": "%d vertices, vertex cycles %d (relative, not comparable with fragment cycles)" % (t["vertices"], t["vs_cycles"]),
        })
        micro = next((f for f in geo["findings"] if f["type"] == "micro_triangles"), None)
        scorecard.append({
            "area": "geometry", "metric": "Micro-triangle share of triangles (%)",
            "value": micro["impact_pct"] if micro else 0.0,
            "assessment": "watch" if micro else "ok",
            "note": "triangles rasterising to < 4 px (includes back-facing ones, so an upper bound)",
        })
        for f in geo["findings"]:
            actions.append({
                "area": "geometry", "priority": f["severity"], "title": "%s: %s" % (f["type"], f["detail"][:80]),
                "impact": f["impact"], "impact_pct": f["impact_pct"], "where": f.get("where", {}),
                "evidence": f["detail"], "how": f["suggestion"],
            })
    if over:
        t = over["totals"]
        opaque_all = t["opaque_pixels"] + t["opaque_rejected"]
        ratio = t["opaque_rejected"] / float(opaque_all) if opaque_all else None
        scorecard.append({
            "area": "overdraw", "metric": "Opaque pixels hidden by other opaque geometry (%)",
            "value": None if ratio is None else round(100 * ratio, 1), "assessment": _assess(ratio, _OCCLUDED_PIXELS),
            "note": "%d hidden of %d; transparent pixels %d" % (t["opaque_rejected"], opaque_all, t["transparent_pixels"]),
        })
        # shading cycles count passed pixels only; the wasted ones come on top
        all_cycles = cycles + (t.get("late_z_wasted_cycles") or 0)
        for f in over["findings"]:
            impact_pct = f["impact_pct"]
            if f["type"] == "late_z_waste" and cycles:
                impact_pct = _pct(f["wasted_cycles"], all_cycles)
                impact = "%s (%.1f%% of fragment cycles)" % (f["impact"], impact_pct)
            else:
                impact = f["impact"]
            actions.append({
                "area": "overdraw", "priority": f["severity"], "title": "%s: %s" % (f["type"], f["where"]),
                "impact": impact, "impact_pct": impact_pct,
                "where": {"render_target": f["where"], "event_ids": f.get("event_ids", [])},
                "evidence": f["detail"], "how": f["suggestion"],
            })


def _textures(snap, actions, scorecard):
    tb = snap.get("texture_bandwidth")
    if not tb:
        return
    t = tb["totals"]
    scorecard.append({
        "area": "textures", "metric": "Texture sampling footprint (MB/frame)", "value": t["footprint_mb"], "assessment": "n/a",
        "note": "upper bound; %.2f GB/s at 60 fps; %.1f MB of it is render targets read as textures"
                % (t["gb_per_s_at_60fps"], t["render_target_footprint_mb"]),
    })
    base = t["footprint_mb"] or 0
    for f in tb["findings"]:
        pct = _pct(f["mb"], base)
        actions.append({
            "area": "textures", "priority": f["severity"], "title": "%s: %s" % (f["type"], f["name"]),
            "impact": "~%.2f MB (%.1f%% of texture footprint)" % (f["mb"], pct), "impact_pct": pct,
            "where": {"event_ids": f.get("events", [])}, "evidence": f["detail"], "how": f["suggestion"],
        })


def _submission(snap, actions, scorecard):
    frame, state, geom = snap.get("frame", {}), snap.get("state", {}), snap.get("geometry", {})
    draws = frame.get("draw_calls") or 0
    scorecard.append({
        "area": "submission", "metric": "Draw calls", "value": draws, "assessment": _assess(draws, _DRAW_CALLS),
        "note": "rule of thumb for mobile: watch from %d, problem from %d" % _DRAW_CALLS,
    })
    scorecard.append({
        "area": "submission", "metric": "Instanced draws", "value": geom.get("instanced_draws"), "assessment": "n/a",
        "note": "%.1f%% of draws" % (100 * (geom.get("instancing_ratio") or 0)),
    })
    for run in state.get("batching", [])[:5]:
        pct = _pct(run.get("count", 0), draws)
        if pct < 1.0:
            continue
        actions.append({
            "area": "submission", "priority": _priority_by_pct(pct, high=8.0, medium=3.0),
            "title": "%d consecutive draws share identical state" % run.get("count", 0),
            "impact": "up to %d fewer draw calls (%.1f%% of the frame)" % (run.get("count", 0) - 1, pct),
            "impact_pct": pct,
            "where": {"event_id_range": [run.get("start_event"), run.get("end_event")]},
            "evidence": "same shader / blend / depth / cull / render target across events %s-%s"
                        % (run.get("start_event"), run.get("end_event")),
            "how": "Check why they were not merged: compare_pipeline_states on two of them, then enable SRP Batcher / "
                   "GPU instancing or share the material / mesh.",
        })


def _risks(snap, actions, scorecard):
    risks = snap.get("risks") or {}
    scorecard.append({
        "area": "risks", "metric": "Mobile risks (high / medium)", "value": "%s / %s" % (risks.get("high", 0), risks.get("medium", 0)),
        "assessment": "problem" if risks.get("high") else ("watch" if risks.get("medium") else "ok"),
        "note": "from diagnose_mobile_risks",
    })
    for r in risks.get("items", []):
        # gpu_specific entries are standing notes about the GPU family, not something to fix in this frame
        if r.get("severity") == "low" or r.get("category") == "gpu_specific":
            continue
        actions.append({
            "area": "risks", "priority": r["severity"] if r["severity"] in _PRIORITY_RANK else "medium",
            "title": r.get("title", ""), "impact": "%s risk (%s)" % (r["severity"], r.get("category")),
            "impact_pct": 0.0, "where": {}, "evidence": r.get("detail", ""), "how": r.get("fix", ""),
        })


# ==================== entry point ====================

# Bandwidth issues kept as separate actions per issue type; the rest of that type become one action
_PER_KIND = 3


def _fold_bandwidth(actions):
    """Keep the heaviest few bandwidth actions of each issue type and fold the rest into one per type.

    A frame with many small render targets produces one wasted_store per target; listed one by one
    they push everything else off the action list. Returns the number of actions folded away.
    """
    by_kind, rest = {}, []
    for a in actions:
        (by_kind.setdefault(a["kind"], []) if a.get("kind") else rest).append(a)
    folded = 0
    for kind, items in by_kind.items():
        items.sort(key=lambda a: -a["impact_pct"])
        rest.extend(items[:_PER_KIND])
        tail = items[_PER_KIND:]
        if len(tail) < 2:
            rest.extend(tail)
            continue
        folded += len(tail) - 1
        pct = round(sum(a["impact_pct"] for a in tail), 1)
        passes = sorted(set(a["where"]["tile_pass"] for a in tail if "tile_pass" in a["where"]))
        rest.append({
            "area": "bandwidth", "kind": kind, "priority": _priority_by_pct(pct),
            "title": "%d more %s issues" % (len(tail), kind),
            "impact": "%.1f%% of attachment traffic together" % pct, "impact_pct": pct,
            "where": {"tile_passes": passes[:20]} if passes else {},
            "evidence": "; ".join(a["title"].split(": ", 1)[-1] for a in tail[:8]) + (" ..." if len(tail) > 8 else ""),
            "how": tail[0]["how"],
        })
    actions[:] = rest
    return folded


def build_report(snap, top_actions=15):
    actions, scorecard = [], []
    avoidable = _bandwidth(snap, actions, scorecard)
    _shading(snap, actions, scorecard)
    _draw_analysis(snap, actions, scorecard)
    _textures(snap, actions, scorecard)
    _submission(snap, actions, scorecard)
    _risks(snap, actions, scorecard)
    folded = _fold_bandwidth(actions)

    actions.sort(key=lambda a: (_PRIORITY_RANK.get(a["priority"], 9), -a["impact_pct"]))
    for n, a in enumerate(actions, 1):
        a["rank"] = n

    problems = [c for c in scorecard if c["assessment"] == "problem"]
    watch = [c for c in scorecard if c["assessment"] == "watch"]
    summary = []
    for c in problems:
        summary.append("PROBLEM - %s: %s (%s)" % (c["metric"], c["value"], c["note"]))
    for c in watch:
        summary.append("watch - %s: %s (%s)" % (c["metric"], c["value"], c["note"]))
    for a in actions[:3]:
        summary.append("top action #%d [%s] %s - %s" % (a["rank"], a["area"], a["title"], a["impact"]))

    cap = snap.get("capture", {})
    return {
        "capture": cap,
        "snapshot": {"name": snap.get("name"), "taken_at": snap.get("taken_at")},
        "summary": summary,
        "scorecard": scorecard,
        "actions": actions[:top_actions],
        "actions_total": len(actions),
        "actions_folded": folded,
        "details": {
            "frame": snap.get("frame"),
            "gpu_notes": [r.get("detail") for r in (snap.get("risks") or {}).get("items", []) if r.get("category") == "gpu_specific"],
            "avoidable_attachment_traffic_mb": round(avoidable, 2),
            "tile_passes": sorted((snap.get("bandwidth") or {}).get("passes", []),
                                  key=lambda p: -(p["load_mb"] + p["store_mb"]))[:6],
            "top_shaders": [{
                "textures": s.get("textures", [])[:3], "share_of_cycles": _pct(s["cycles"], (snap.get("shading") or {}).get("total_cycles") or 0),
                "cycles_per_pixel": s["cycles_per_pixel"], "pixels": s["pixels"], "draws": s["draw_count"],
            } for s in ((snap.get("shading") or {}).get("shaders") or [])[:8]],
        },
        "caveats": [
            "Percentages are relative to their own area (attachment traffic, fragment cycles, draw calls); "
            "do not compare them across areas.",
            "Cycles come from an offline compiler and bytes from whole attachments: use them to rank, "
            "not as milliseconds. Confirm with a profiler (Streamline / Snapdragon Profiler) before and after.",
            "Geometry and overdraw sections appear only when the snapshot was taken with include_draw_analysis. "
            "Texture footprint assumes one texel per pixel and ignores caches and filtering.",
        ],
    }
