"""
Geometry cost and overdraw analysis over per-draw data.

Inputs are the entries of the extension's get_draw_coverage (with geometry and, for overdraw,
blend / depth state) plus per-draw offline-compiler results. Everything here is pure data in,
data out so it can be tested without RenderDoc.

Costs are relative (offline-compiler cycles x counts), not milliseconds.
"""

# A draw with at least this many triangles averaging fewer pixels than the limit below is
# "micro-triangle" territory: the GPU shades in 2x2 quads, so tiny triangles waste lanes.
_MICRO_MIN_TRIANGLES = 100
# Rasterised pixels / submitted triangles, with back-facing and off-screen triangles still in the
# divisor: roughly half of a closed mesh is back-facing, so 4 here is about 8 per visible triangle.
_MICRO_PIXELS_PER_TRIANGLE = 4.0

_OPAQUE_FAIL_RATIO = 0.20          # share of opaque pixels rejected by depth worth a finding
_OPAQUE_FAIL_MIN_PIXELS = 100000
_LAYERS_WATCH = 1.5                # average transparent layers per render-target pixel
_SHADOW_WORDS = ("shadow",)


def _tail(marker):
    """Last two marker levels: a bare leaf like "RenderLoop.Draw" repeats under different passes"""
    return " > ".join((marker or "").split("/")[-2:])


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else 0.0


def triangle_count(topology, indices):
    """Triangles produced by `indices` indices of the given topology (0 for points / lines)"""
    t = str(topology)
    if "Point" in t or "Line" in t:
        return 0
    if "Strip" in t or "Fan" in t:
        return max(0, indices - 2)
    return indices // 3


# ==================== geometry ====================


def geometry_cost(draws, vs_info, top_n=10):
    """
    draws:   coverage entries (event_id, marker, num_indices, num_instances, topology, passed, failed)
    vs_info: {event_id: {"cost": cycles per vertex or None, "hash": str or None, "shader": str}}
    Vertex counts are index counts (an upper bound: the post-transform cache reuses indexed vertices).
    """
    rows = []
    for d in draws:
        inst = max(1, d.get("num_instances", 1))
        idx = d.get("num_indices", 0)
        info = vs_info.get(d["event_id"]) or {}
        verts = idx * inst
        tris = triangle_count(d.get("topology", "TriangleList"), idx) * inst
        covered = d.get("passed", 0) + d.get("failed", 0)
        cost = info.get("cost")
        rows.append({
            "event_id": d["event_id"], "marker": _tail(d.get("marker")), "path": d.get("marker", ""),
            "instances": inst, "vertices": verts, "triangles": tris,
            "vs_cycles_per_vertex": cost, "vs_cycles": None if cost is None else verts * cost,
            "vs_hash": info.get("hash") or info.get("shader"),
            "covered_pixels": covered,
            "px_per_triangle": round(covered / float(tris), 2) if tris else None,
            "textures": d.get("textures", [])[:3],
        })

    total_v = sum(r["vertices"] for r in rows)
    total_t = sum(r["triangles"] for r in rows)
    total_c = sum(r["vs_cycles"] for r in rows if r["vs_cycles"] is not None)

    by_marker = {}
    for r in rows:
        # full path: "DrawOpaqueObjects > RenderLoop.Draw" exists under the shadow pass and the main camera
        b = by_marker.setdefault(r["path"], {"marker": r["marker"], "path": r["path"], "draws": 0, "vertices": 0, "triangles": 0,
                                               "vs_cycles": 0.0, "covered_pixels": 0})
        b["draws"] += 1
        b["vertices"] += r["vertices"]
        b["triangles"] += r["triangles"]
        b["vs_cycles"] += r["vs_cycles"] or 0.0
        b["covered_pixels"] += r["covered_pixels"]
    markers = sorted(by_marker.values(), key=lambda b: -b["vertices"])
    for b in markers:
        b["share_of_vertices"] = _pct(b["vertices"], total_v)
        b["vs_cycles"] = round(b["vs_cycles"])
        b["px_per_triangle"] = round(b["covered_pixels"] / float(b["triangles"]), 2) if b["triangles"] else None

    by_vs = {}
    for r in rows:
        if r["vs_hash"] is None:
            continue
        b = by_vs.setdefault(r["vs_hash"], {"vertex_shader": r["vs_hash"], "draws": 0, "vertices": 0, "vs_cycles": 0.0,
                                            "cycles_per_vertex": r["vs_cycles_per_vertex"], "sample_event_ids": []})
        b["draws"] += 1
        b["vertices"] += r["vertices"]
        b["vs_cycles"] += r["vs_cycles"] or 0.0
        if len(b["sample_event_ids"]) < 4:
            b["sample_event_ids"].append(r["event_id"])
    shaders = sorted(by_vs.values(), key=lambda b: -b["vs_cycles"])
    for b in shaders:
        b["share_of_vs_cycles"] = _pct(b["vs_cycles"], total_c)
        b["vs_cycles"] = round(b["vs_cycles"])

    micro = [r for r in rows if r["triangles"] >= _MICRO_MIN_TRIANGLES and r["px_per_triangle"] is not None
             and r["px_per_triangle"] < _MICRO_PIXELS_PER_TRIANGLE]
    micro_tris = sum(r["triangles"] for r in micro)

    findings = []
    for b in markers[:3]:
        if b["share_of_vertices"] >= 20.0:
            shadow = any(w in b["path"].lower() for w in _SHADOW_WORDS)   # the parent marker usually carries the name
            findings.append({
                "type": "vertex_heavy_pass", "severity": "high" if b["share_of_vertices"] >= 35.0 else "medium",
                "impact": "%.1f%% of all vertices" % b["share_of_vertices"], "impact_pct": b["share_of_vertices"],
                "where": {"marker": b["marker"], "path": b["path"]},
                "detail": "%s submits %.1f%% of all vertices (%d draws, %d triangles)"
                          % (b["marker"], b["share_of_vertices"], b["draws"], b["triangles"]),
                "suggestion": ("Shadow casters are drawn once per cascade: use lower-LOD shadow meshes, cull casters "
                               "that cannot reach the visible area, drop small objects from shadows."
                               if shadow else
                               "Reduce triangle count with LODs / mesh simplification, or cull more aggressively."),
            })
    if micro and micro_tris / float(total_t or 1) >= 0.10:
        worst = sorted(micro, key=lambda r: -r["triangles"])[:3]
        findings.append({
            "type": "micro_triangles", "severity": "medium",   # never high: the figure includes back-facing triangles
            "impact": "%.1f%% of triangles" % _pct(micro_tris, total_t), "impact_pct": _pct(micro_tris, total_t),
            "where": {"event_ids": [r["event_id"] for r in worst]},
            "detail": "%.1f%% of triangles (%d, in %d draws) rasterise to fewer than %g pixels each; worst events %s"
                      % (_pct(micro_tris, total_t), micro_tris, len(micro), _MICRO_PIXELS_PER_TRIANGLE,
                         [r["event_id"] for r in worst]),
            "suggestion": "Add LODs / impostors for distant or small meshes. Note: backface-culled triangles also "
                          "lower this figure, so confirm on a mesh you can see.",
        })

    return {
        "draws": len(rows),
        "totals": {"vertices": total_v, "triangles": total_t, "vs_cycles": round(total_c),
                   "draws_without_vs_cost": sum(1 for r in rows if r["vs_cycles"] is None)},
        "findings": findings,
        "by_marker": markers[:top_n],
        "by_vertex_shader": shaders[:top_n],
        "top_draws_by_vertices": sorted(rows, key=lambda r: -r["vertices"])[:top_n],
        "micro_triangle_draws": sorted(micro, key=lambda r: -r["triangles"])[:top_n],
        "note": "vertices = indices x instances (upper bound; indexed vertices may be reused). vs_cycles are offline-"
                "compiler cycles per vertex x vertices, relative cost, not comparable with fragment cycles.",
    }


# ==================== overdraw ====================


def overdraw(draws, ps_info, top_n=10):
    """
    draws:   coverage entries with state (blend_enabled, depth_test, depth_write) and rt_size
    ps_info: {event_id: {"cost": cycles per pixel or None, "late_z": True / False / None}}
    A draw whose blend is on but only ONE/ZERO (blend_opaque) is opaque.
    A pixel counts as 'passed' when it survives the depth test (it is shaded and written) and as
    'failed' when it is covered but hidden by earlier geometry.
    """
    targets = {}
    waste_rows = []
    for d in draws:
        info = ps_info.get(d["event_id"]) or {}
        late_z = info.get("late_z")
        if d.get("blend_enabled") and d.get("blend_opaque") is not True:
            kind = "transparent"
        elif late_z:
            kind = "late_z"
        else:
            kind = "opaque"
        t = targets.setdefault(d["render_target"], {
            "render_target": d["render_target"], "size": d.get("rt_size"), "marker": _tail(d.get("marker")),
            "draws": 0, "events": [],
            "opaque": {"draws": 0, "passed": 0, "failed": 0},
            "late_z": {"draws": 0, "passed": 0, "failed": 0, "wasted_cycles": 0.0},
            "transparent": {"draws": 0, "passed": 0, "failed": 0},
            "state_unknown_draws": 0, "contributors": [],
        })
        t["draws"] += 1
        t["events"].append((d["event_id"], kind))
        t["contributors"].append((d.get("failed", 0), d["event_id"], kind, d.get("textures", [])[:3]))
        b = t[kind]
        b["draws"] += 1
        b["passed"] += d.get("passed", 0)
        b["failed"] += d.get("failed", 0)
        if d.get("blend_enabled") is None:
            t["state_unknown_draws"] += 1
        if kind == "late_z" and d.get("failed", 0):
            cost = info.get("cost")
            cycles = d["failed"] * cost if cost is not None else None
            if cycles is not None:
                b["wasted_cycles"] += cycles
            waste_rows.append({
                "event_id": d["event_id"], "marker": _tail(d.get("marker")), "failed_pixels": d["failed"],
                "cycles_per_pixel": cost, "wasted_cycles": None if cycles is None else round(cycles),
                "textures": d.get("textures", [])[:3],
            })

    findings = []
    out_targets = []
    total_wasted = 0.0
    for t in targets.values():
        w, h = (t["size"] or [0, 0])
        area = w * h
        op, lz, tr = t["opaque"], t["late_z"], t["transparent"]
        total_wasted += lz["wasted_cycles"]
        op_all = op["passed"] + op["failed"]
        fail_ratio = op["failed"] / float(op_all) if op_all else 0.0
        layers = tr["passed"] / float(area) if area else None

        # opaque draws issued after the first transparent draw of the same target
        first_tr = min((e for e, k in t["events"] if k == "transparent"), default=None)
        late_opaque = [e for e, k in t["events"] if k != "transparent" and first_tr is not None and e > first_tr]

        row = {
            "render_target": t["render_target"], "size": "%dx%d" % (w, h) if area else None, "marker": t["marker"],
            "draws": t["draws"],
            "opaque": dict(op, fail_pct=round(100 * fail_ratio, 1)),
            "late_z": dict(lz, wasted_cycles=round(lz["wasted_cycles"])),
            "transparent": dict(tr, avg_layers=None if layers is None else round(layers, 2)),
            "opaque_after_transparent_draws": len(late_opaque),
            "top_hidden_draws": [{"event_id": e, "hidden_pixels": f, "class": k, "textures": tx}
                                 for f, e, k, tx in sorted(t["contributors"], reverse=True)[:5] if f],
            "state_unknown_draws": t["state_unknown_draws"],
        }
        out_targets.append(row)
        where = "%s (%s, %s)" % (t["marker"], row["size"], t["render_target"])

        if fail_ratio >= _OPAQUE_FAIL_RATIO and op["failed"] >= _OPAQUE_FAIL_MIN_PIXELS:
            findings.append({
                "type": "opaque_overdraw", "severity": "high" if fail_ratio >= 0.4 else "medium", "where": where,
                "impact": "%.0f%% of opaque pixels hidden" % (100 * fail_ratio), "impact_pct": round(100 * fail_ratio, 1),
                "event_ids": [e for f, e, k, tx in sorted(t["contributors"], reverse=True)[:5] if f and k != "transparent"],
                "detail": "%.0f%% of opaque pixels (%d) are drawn and then hidden by other opaque geometry; "
                          "biggest contributors: %s" % (100 * fail_ratio, op["failed"], "; ".join(
                              "event %d (%s) %.0f%%" % (e, ",".join(tx[:2]) or k, 100.0 * f / max(1, op["failed"] + lz["failed"]))
                              for f, e, k, tx in sorted((c for c in t["contributors"] if c[2] != "transparent"), reverse=True)[:3] if f)),
                "suggestion": "Sort opaque draws front-to-back, add a depth pre-pass for expensive materials, or cull "
                              "occluded objects. On Mali with hidden-surface removal early-Z failures are cheap; this "
                              "matters most where the shaders below use late-Z.",
            })
        if lz["wasted_cycles"] and lz["failed"] >= 10000:
            findings.append({
                "type": "late_z_waste", "severity": "high" if lz["failed"] >= 100000 else "medium", "where": where,
                "impact": "~%d fragment cycles wasted" % lz["wasted_cycles"], "impact_pct": 0.0,
                "wasted_cycles": round(lz["wasted_cycles"]),
                "event_ids": [e for f, e, k, tx in sorted(t["contributors"], reverse=True)[:5] if f and k == "late_z"],
                "detail": "%d pixels are shaded by draws that use discard / write depth in the shader and are then rejected "
                          "(~%d fragment cycles)" % (lz["failed"], lz["wasted_cycles"]),
                "suggestion": "Late-Z disables early rejection, so hidden pixels still run the shader. Draw these after "
                              "the other opaque geometry, use alpha-to-coverage, or give them a pre-pass.",
            })
        if layers is not None and layers >= _LAYERS_WATCH:
            findings.append({
                "type": "transparent_layers", "severity": "high" if layers >= 3.0 else "medium", "where": where,
                "impact": "%.1f blended layers per pixel" % layers, "impact_pct": 0.0,
                "detail": "transparent draws cover the target %.1f times on average (%d pixels over %d draws)"
                          % (layers, tr["passed"], tr["draws"]),
                "suggestion": "Reduce blended layers: merge or atlas particles / UI, shrink quads to their visible "
                              "area, lower particle resolution, or use alpha-tested cutouts.",
            })
        if late_opaque:
            findings.append({
                "type": "draw_order", "severity": "medium", "where": where,
                "impact": "%d opaque draws after transparent" % len(late_opaque), "impact_pct": 0.0,
                "event_ids": late_opaque[:5],
                "detail": "%d opaque draw(s) come after a transparent draw in the same target (first at event %s)"
                          % (len(late_opaque), late_opaque[0]),
                "suggestion": "Render all opaque geometry before blended geometry; transparent draws that run early "
                              "cannot be depth-sorted correctly against what is drawn later.",
            })

    rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: rank[f["severity"]])
    totals = {
        "draws": len(draws),
        "opaque_pixels": sum(t["opaque"]["passed"] for t in targets.values()),
        "opaque_rejected": sum(t["opaque"]["failed"] for t in targets.values()),
        "late_z_rejected": sum(t["late_z"]["failed"] for t in targets.values()),
        "transparent_pixels": sum(t["transparent"]["passed"] for t in targets.values()),
        "late_z_wasted_cycles": round(total_wasted),
    }
    out_targets.sort(key=lambda r: -(r["opaque"]["failed"] + r["late_z"]["failed"] + r["transparent"]["passed"]))
    return {
        "totals": totals,
        "findings": findings,
        "render_targets": out_targets[:top_n],
        "top_late_z_waste": sorted(waste_rows, key=lambda r: -(r["wasted_cycles"] or 0))[:top_n],
        "note": "Classes come from the draw's blend state (transparent) and the pixel shader's late-Z / coverage flags "
                "(late_z; Mali only, unknown on Adreno so those draws count as opaque). passed / failed come from the "
                "depth-test overlay, which ignores shader discard, so alpha-tested draws are slightly over-counted.",
    }
