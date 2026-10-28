"""
Capture metric snapshots and before/after comparison.

A snapshot condenses one capture into a small JSON document (frame size, state
changes, tile-pass bandwidth, shading cost, texture memory, risks). Two snapshots
can be diffed to answer "did the optimisation work, and what got worse".

Everything here is pure data in / data out; the MCP tools that talk to RenderDoc
live in server.py.
"""

import json
import os
import re
import tempfile
import time

SNAPSHOT_VERSION = 1

# (section, key, label, better direction). "lower": smaller is better; None: informational
_METRICS = [
    ("frame", "draw_calls", "Draw calls", "lower"),
    ("frame", "logical_passes", "Marker passes (informational)", None),
    ("geometry", "indices_submitted", "Indices submitted", "lower"),
    ("geometry", "instanced_draws", "Instanced draws", "higher"),
    ("state", "shader_changes", "Shader changes (sampled draws)", "lower"),
    ("state", "render_target_changes", "Render target changes", "lower"),
    ("state", "blend_changes", "Blend changes", "lower"),
    ("state", "batchable_draws", "Draws in batchable runs", "lower"),
    ("memory", "texture_total_mb", "Texture memory (MB)", "lower"),
    ("bandwidth", "tile_passes", "Tile passes", "lower"),
    ("bandwidth", "load_mb", "Attachment load (MB)", "lower"),
    ("bandwidth", "store_mb", "Attachment store (MB)", "lower"),
    ("bandwidth", "traffic_mb", "Attachment traffic (MB)", "lower"),
    ("bandwidth", "wasted_store_mb", "Wasted store (MB)", "lower"),
    ("bandwidth", "gb_per_s_at_60fps", "Attachment GB/s @60fps", "lower"),
    ("shading", "total_pixels", "Pixels shaded", "lower"),
    ("shading", "total_occluded_pixels", "Occluded pixels (shaded then rejected)", "lower"),
    ("shading", "total_cycles", "Fragment cycles (sum pixels x cycles)", "lower"),
    ("geometry_cost", "vertices", "Vertices submitted", "lower"),
    ("geometry_cost", "triangles", "Triangles submitted", "lower"),
    ("geometry_cost", "vs_cycles", "Vertex cycles (sum vertices x cycles)", "lower"),
    ("overdraw", "opaque_rejected", "Opaque pixels hidden", "lower"),
    ("overdraw", "late_z_wasted_cycles", "Late-Z wasted cycles", "lower"),
    ("overdraw", "transparent_pixels", "Transparent pixels", "lower"),
    ("texture_bandwidth", "footprint_mb", "Texture sampling footprint (MB)", "lower"),
    ("risks", "high", "Risks: high", "lower"),
    ("risks", "medium", "Risks: medium", "lower"),
]

_SAME_REL = 0.005   # changes under 0.5% count as "same"


# ==================== snapshot ====================


def default_dir():
    return os.path.join(tempfile.gettempdir(), "renderdoc_mcp", "snapshots")


def _tail(marker):
    return (marker or "").split(" > ")[-1]


def _pass_signature(p):
    parts = sorted("%s:%s:%s" % (a["role"], a["size"], a["format"]) for a in p["attachments"])
    return "%s|%s" % (_tail(p["marker"]), ",".join(parts))


def build_snapshot(name, status, summary, pass_list, instancing, state, textures, risks, bandwidth, shading,
                   geometry_cost=None, overdraw=None, texture_bandwidth=None):
    """Condense the raw tool results of one capture. shading / geometry_cost / overdraw may be None."""
    stats = summary.get("statistics", {})
    counts = summary.get("resource_counts", {})
    overall = instancing.get("overall", {})
    changes = state.get("change_counts", {})
    sev = {"high": 0, "medium": 0, "low": 0}
    for r in risks.get("risks", []):
        sev[r.get("severity", "low")] = sev.get(r.get("severity", "low"), 0) + 1

    snap = {
        "version": SNAPSHOT_VERSION,
        "name": name,
        "taken_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "capture": {
            "filename": status.get("filename"),
            "api": status.get("api") or summary.get("api"),
            "vendor": status.get("vendor"),
            "recorded_gpu": status.get("recorded_gpu"),
            "main_resolution": risks.get("main_resolution"),
        },
        "frame": {
            "draw_calls": stats.get("draw_calls"),
            "dispatches": stats.get("dispatches"),
            "clears": stats.get("clears"),
            "markers": stats.get("markers"),
            "textures": counts.get("textures"),
            "buffers": counts.get("buffers"),
            "logical_passes": pass_list.get("pass_count"),
        },
        "geometry": {
            "indices_submitted": overall.get("total_indices_submitted"),
            "instanced_draws": overall.get("instanced_draws"),
            "instancing_ratio": overall.get("instancing_ratio"),
        },
        "state": {
            "analyzed_draws": state.get("analyzed_draws"),
            "shader_changes": changes.get("shader"),
            "render_target_changes": changes.get("render_target"),
            "blend_changes": changes.get("blend"),
            "depth_changes": changes.get("depth"),
            "cull_changes": changes.get("cull"),
            "batching_opportunities": len(state.get("batching_opportunities", [])),
            "batchable_draws": sum(o.get("count", 0) for o in state.get("batching_opportunities", [])),
            "batching": sorted(state.get("batching_opportunities", []), key=lambda o: -o.get("count", 0))[:10],
        },
        "memory": {
            "texture_count": textures.get("total_textures"),
            "texture_total_mb": textures.get("total_size_mb"),
        },
        "risks": {
            "total": risks.get("risk_count"), "high": sev["high"], "medium": sev["medium"], "low": sev["low"],
            "titles": sorted(set(r.get("title", "") for r in risks.get("risks", []))),
            "items": [dict((k, r.get(k)) for k in ("category", "severity", "title", "detail", "fix")) for r in risks.get("risks", [])],
        },
    }

    total = bandwidth.get("total", {})
    by_type = {}
    for i in bandwidth.get("issues", []):
        by_type[i["type"]] = by_type.get(i["type"], 0) + 1
    snap["bandwidth"] = {
        "tile_passes": bandwidth.get("pass_count"),
        "load_mb": total.get("load_mb"), "store_mb": total.get("store_mb"),
        "traffic_mb": total.get("traffic_mb"), "wasted_store_mb": total.get("wasted_store_mb"),
        "gb_per_s_at_60fps": total.get("gb_per_s_at_60fps"),
        "issues_by_type": by_type,
        "issues": [{
            "type": i["type"], "severity": i["severity"], "mb": i.get("mb", 0),
            "target": i.get("target", ""), "marker": _tail(i.get("marker", "")), "detail": i.get("detail", ""),
            "event_id": i.get("event_id"), "pass_index": i.get("pass_index"), "suggestion": i.get("suggestion", ""),
        } for i in bandwidth.get("issues", [])],
        "passes": [{
            "signature": _pass_signature(p), "marker": _tail(p["marker"]), "draws": p["draw_count"],
            "load_mb": p["load_mb"], "store_mb": p["store_mb"], "wasted_store_mb": p["wasted_store_mb"],
            "event_id_begin": p["event_id_begin"],
        } for p in bandwidth.get("passes", [])],
    }

    if shading is not None:
        snap["shading"] = {
            "compiler": shading.get("compiler"), "gpu": shading.get("gpu"),
            "total_pixels": shading.get("total_pixels"),
            "total_occluded_pixels": shading.get("total_occluded_pixels"),
            "total_cycles": shading.get("total_cycles"),
            "shaders": [{
                "pixel_shader": s["pixel_shader"], "shader_hash": s.get("shader_hash"),
                "draw_count": s["draw_count"], "pixels": s["pixels"],
                "occluded_pixels": s["occluded_pixels"], "cycles": s["cycles"],
                "cycles_per_pixel": s["cycles_per_pixel"], "textures": s.get("textures", []),
                "late_zs_draws": s.get("late_zs_draws", 0), "sample_event_ids": s.get("sample_event_ids", []),
                "work_registers": s.get("work_registers", []),
            } for s in shading.get("shaders", [])],
        }
    else:
        snap["shading"] = None

    if geometry_cost is not None:
        snap["geometry_cost"] = {
            "totals": geometry_cost["totals"], "findings": geometry_cost["findings"],
            "by_marker": [dict((k, m[k]) for k in ("marker", "path", "draws", "vertices", "triangles", "vs_cycles",
                                                  "share_of_vertices", "px_per_triangle")) for m in geometry_cost["by_marker"][:6]],
            "top_draws": [dict((k, r[k]) for k in ("event_id", "marker", "vertices", "triangles", "px_per_triangle", "textures"))
                          for r in geometry_cost["top_draws_by_vertices"][:5]],
        }
    if overdraw is not None:
        snap["overdraw"] = {
            "totals": overdraw["totals"], "findings": overdraw["findings"],
            "render_targets": [dict((k, t[k]) for k in ("render_target", "size", "marker", "draws", "opaque", "late_z",
                                                        "transparent", "top_hidden_draws")) for t in overdraw["render_targets"][:4]],
        }
    if texture_bandwidth is not None:
        snap["texture_bandwidth"] = {
            "totals": texture_bandwidth["totals"], "findings": texture_bandwidth["findings"],
            "top_textures": [dict((k, t[k]) for k in ("name", "size", "format", "mips", "size_mb", "footprint_mb", "draws", "is_render_target"))
                             for t in texture_bandwidth["top_textures"][:8]],
        }
    return snap


def save_snapshot(snap, save_dir=None):
    directory = save_dir or default_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, _safe_name(snap["name"]) + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    return path


def load_snapshot(ref, save_dir=None):
    """ref: a snapshot name (looked up in the snapshot directory) or a path to a .json file"""
    candidates = [ref, os.path.join(save_dir or default_dir(), _safe_name(ref) + ".json")]
    for path in candidates:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    raise ValueError("Snapshot not found: %s (looked in %s)" % (ref, ", ".join(candidates)))


def _safe_name(name):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "snapshot"


# ==================== diff ====================


def _row(label, before, after, direction):
    row = {"metric": label, "before": before, "after": after}
    if before is None or after is None:
        row["verdict"] = "n/a"
        return row
    delta = after - before
    row["delta"] = round(delta, 3)
    if before:
        row["pct"] = round(100.0 * delta / abs(before), 1)
    if abs(delta) <= _SAME_REL * max(abs(before), abs(after), 1e-9):
        row["verdict"] = "same"
    elif direction is None:
        row["verdict"] = "changed"
    else:
        improved = delta < 0 if direction == "lower" else delta > 0
        row["verdict"] = "better" if improved else "worse"
    return row


def _match_by_key(before, after, key_fn):
    """Pair items with equal keys in order of occurrence; return (pairs, only_before, only_after)"""
    pool = {}
    for item in after:
        pool.setdefault(key_fn(item), []).append(item)
    pairs, only_before = [], []
    for item in before:
        bucket = pool.get(key_fn(item))
        if bucket:
            pairs.append((item, bucket.pop(0)))
        else:
            only_before.append(item)
    only_after = [item for bucket in pool.values() for item in bucket]
    return pairs, only_before, only_after


def _diff_passes(a, b):
    pairs, gone, new = _match_by_key(a["bandwidth"]["passes"], b["bandwidth"]["passes"], lambda p: p["signature"])
    changed = []
    for x, y in pairs:
        d_load = round(y["load_mb"] - x["load_mb"], 2)
        d_store = round(y["store_mb"] - x["store_mb"], 2)
        if d_load or d_store:
            changed.append({
                "pass": x["signature"], "before_event": x["event_id_begin"], "after_event": y["event_id_begin"],
                "load_mb": [x["load_mb"], y["load_mb"]], "store_mb": [x["store_mb"], y["store_mb"]],
                "delta_mb": round(d_load + d_store, 2),
            })
    changed.sort(key=lambda r: r["delta_mb"])
    cut = lambda p: {"pass": p["signature"], "event": p["event_id_begin"], "load_mb": p["load_mb"], "store_mb": p["store_mb"]}
    return {
        "changed": changed,
        "removed": [cut(p) for p in gone],
        "added": [cut(p) for p in new],
    }


_ID_NAME = re.compile(r"\b(Texture|Renderbuffer|Buffer|Framebuffer) \d+")


def _norm_name(text):
    """GL default object names embed the object id ("Texture 702"), which differs between captures"""
    return _ID_NAME.sub(r"\1", text or "")


def _issue_key(i):
    return (i["type"], _norm_name(i.get("target", "")), i.get("marker", ""))


def _diff_issues(a, b):
    pairs, resolved, new = _match_by_key(a["bandwidth"]["issues"], b["bandwidth"]["issues"], _issue_key)
    show = lambda i: {"type": i["type"], "severity": i["severity"], "mb": i["mb"], "target": i["target"], "marker": i["marker"]}
    return {
        "resolved": [show(i) for i in resolved],
        "new": [show(i) for i in new],
        "persisting": [dict(show(y), mb_before=x["mb"]) for x, y in pairs],
    }


def _texture_set(s):
    return set(_norm_name(t) for t in s.get("textures", []))


def _jaccard(x, y):
    union = x | y
    return len(x & y) / float(len(union)) if union else 1.0   # neither samples a texture: same material


def _match_shaders(before, after):
    """Pair the same shader across two captures; returns (pairs, only_before, only_after).

    ResourceIds are not comparable between captures, so shaders are first paired by the hash of
    their source. What is left (older snapshots without a hash, or a shader whose code really
    changed) is paired greedily by overlap of the textures it samples, heaviest first: a
    re-optimised shader keeps its material. Each pair is tagged with how it was matched.
    """
    pairs, left_a, left_b = _match_by_key(
        [s for s in before if s.get("shader_hash")], [s for s in after if s.get("shader_hash")],
        lambda s: s["shader_hash"],
    )
    pairs = [(x, y, "hash") for x, y in pairs]
    rest_a = sorted(left_a + [s for s in before if not s.get("shader_hash")], key=lambda s: -s["cycles"])
    rest_b = left_b + [s for s in after if not s.get("shader_hash")]
    gone = []
    for x in rest_a:
        tx = _texture_set(x)
        best, best_score = None, 0.5 - 1e-9
        for y in rest_b:
            score = _jaccard(tx, _texture_set(y))
            if score > best_score or (best is not None and score == best_score
                                      and abs(y["cycles"] - x["cycles"]) < abs(best["cycles"] - x["cycles"])):
                best, best_score = y, score
        if best is None:
            gone.append(x)
        else:
            rest_b.remove(best)
            pairs.append((x, best, "textures"))
    return pairs, gone, rest_b


def _diff_shading(a, b):
    sa, sb = a.get("shading"), b.get("shading")
    if not sa or not sb:
        return None
    pairs, gone, new = _match_shaders(sa["shaders"], sb["shaders"])
    changed = []
    for x, y, how in pairs:
        if y["cycles"] == x["cycles"]:
            continue
        # cycles = pixels x cycles/pixel, so the change splits exactly into
        #   shader_cost_delta: the shader itself got cheaper / dearer (at the "after" pixel count)
        #   coverage_delta:    it simply covers more / fewer pixels in this view
        changed.append({
            "pixel_shader": x["pixel_shader"], "textures": x.get("textures", []), "matched_by": how,
            "cycles": [x["cycles"], y["cycles"]], "delta_cycles": y["cycles"] - x["cycles"],
            "shader_cost_delta": round((y["cycles_per_pixel"] - x["cycles_per_pixel"]) * y["pixels"]),
            "coverage_delta": round((y["pixels"] - x["pixels"]) * x["cycles_per_pixel"]),
            "cycles_per_pixel": [x["cycles_per_pixel"], y["cycles_per_pixel"]],
            "pixels": [x["pixels"], y["pixels"]],
            "draws": [x["draw_count"], y["draw_count"]],
        })
    cut = lambda s: {"pixel_shader": s["pixel_shader"], "textures": s.get("textures", []), "cycles": s["cycles"],
                     "pixels": s["pixels"], "cycles_per_pixel": s["cycles_per_pixel"], "draws": s["draw_count"]}
    cost_down = sorted((c for c in changed if c["shader_cost_delta"] < 0), key=lambda c: c["shader_cost_delta"])
    cost_up = sorted((c for c in changed if c["shader_cost_delta"] > 0), key=lambda c: -c["shader_cost_delta"])
    view = sorted(changed, key=lambda c: -abs(c["coverage_delta"]))
    return {
        "note": "Shaders are matched by source hash (textures as a fallback for older snapshots). "
                "shader_cost_delta isolates real shader changes (cycles/pixel x pixels); coverage_delta is just "
                "the camera / scene showing more or fewer pixels of it. Only the top shaders of each snapshot "
                "are stored, so an added / removed one may have crossed that cut-off.",
        "total_shader_cost_delta": sum(c["shader_cost_delta"] for c in changed),
        "total_coverage_delta": sum(c["coverage_delta"] for c in changed),
        "cheaper": cost_down[:15],
        "costlier": cost_up[:15],
        "coverage_changed": view[:8],
        "removed": [cut(s) for s in sorted(gone, key=lambda s: -s["cycles"])[:10]],
        "added": [cut(s) for s in sorted(new, key=lambda s: -s["cycles"])[:10]],
    }


_DIGITS = re.compile(r"\d+")


def _diff_risks(a, b):
    """Risk titles carry counts ("396 draws use discard"); compare them with the numbers blanked out"""
    ka = dict((_DIGITS.sub("#", _norm_name(t)), t) for t in a["risks"]["titles"])
    kb = dict((_DIGITS.sub("#", _norm_name(t)), t) for t in b["risks"]["titles"])
    return {
        "resolved": [ka[k] for k in sorted(set(ka) - set(kb))],
        "new": [kb[k] for k in sorted(set(kb) - set(ka))],
        "changed": [{"before": ka[k], "after": kb[k]} for k in sorted(set(ka) & set(kb)) if ka[k] != kb[k]],
    }


def diff_snapshots(a, b):
    rows = []
    for section, key, label, direction in _METRICS:
        sa, sb = a.get(section) or {}, b.get(section) or {}
        if section in ("shading", "geometry_cost", "overdraw", "texture_bandwidth") and (not a.get(section) or not b.get(section)):
            continue
        if section in ("geometry_cost", "overdraw", "texture_bandwidth"):
            sa, sb = sa.get("totals", {}), sb.get("totals", {})
        rows.append(_row(label, sa.get(key), sb.get(key), direction))

    warnings = []
    ca, cb = a["capture"], b["capture"]
    for key, what in (("api", "graphics API"), ("recorded_gpu", "capture GPU"), ("main_resolution", "main resolution")):
        if ca.get(key) != cb.get(key):
            warnings.append("%s differs (%s vs %s): absolute pixel / bandwidth numbers are not directly comparable"
                            % (what, ca.get(key), cb.get(key)))
    if bool(a.get("shading")) != bool(b.get("shading")):
        warnings.append("shading cost is present in only one snapshot and was skipped")

    better = [r for r in rows if r["verdict"] == "better"]
    worse = [r for r in rows if r["verdict"] == "worse"]
    by_pct = lambda r: -abs(r.get("pct", 0))
    headline = []
    for r in sorted(better, key=by_pct)[:5]:
        headline.append("better: %s %s -> %s (%s%%)" % (r["metric"], r["before"], r["after"], r.get("pct")))
    for r in sorted(worse, key=by_pct)[:5]:
        headline.append("WORSE: %s %s -> %s (%s%%)" % (r["metric"], r["before"], r["after"], r.get("pct")))

    return {
        "before": {"name": a["name"], "capture": ca, "taken_at": a["taken_at"]},
        "after": {"name": b["name"], "capture": cb, "taken_at": b["taken_at"]},
        "warnings": warnings,
        "headline": headline,
        "metrics": rows,
        "bandwidth_passes": _diff_passes(a, b),
        "bandwidth_issues": _diff_issues(a, b),
        "shading": _diff_shading(a, b),
        "risks": _diff_risks(a, b),
    }
