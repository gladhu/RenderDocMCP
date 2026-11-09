"""
Texture sampling footprint per frame.

Combines the frame's texture list (format, size in bytes, mips) with per-draw coverage (pixels
shaded and the textures each draw samples).

What is estimated: for every texture, an upper bound of the bytes the frame can pull from memory,
    footprint = min(texture size, pixels that sample it x bytes per texel)
i.e. at best one texel per shaded pixel, never more than the whole texture. Caches and compression
make real traffic lower; texture filtering (bilinear / trilinear / anisotropic) is not modelled, so
this is not a measured bandwidth. It ranks textures and exposes mismatches between what is stored
and what is used. Pure data in, data out.
"""

_MB = 1048576.0
_UNCOMPRESSED_PREFIXES = ("R8G8B8A8", "R8G8B8_", "B8G8R8A8", "R16G16B16A16", "R32G32B32A32", "B4G4R4A4", "R5G6B5", "R10G10B10A2")
# ASTC 6x6 is 3.56 bits per pixel; used only to size the saving of recompressing, never as a measurement
_ASTC_BPP = 3.56


def _mip_factor(mips):
    return sum(0.25 ** i for i in range(max(1, mips)))        # 2D chain: 1 + 1/4 + 1/16 ...


def _texel_bytes(t):
    """Average bytes per texel of one mip level, from the real total size"""
    texels = max(1, t["width"]) * max(1, t["height"]) * max(1, t.get("depth", 1)) * max(1, t.get("array_size", 1))
    return t["size_bytes"] / (texels * _mip_factor(t["mips"]))


def _bits_per_pixel(t):
    return 8.0 * t["size_bytes"] / (max(1, t["width"]) * max(1, t["height"]) * max(1, t.get("depth", 1))
                                   * max(1, t.get("array_size", 1)) * _mip_factor(t["mips"]))


def _is_atlas(name):
    """Atlases (fonts, Unity sprite atlases "sactx-...") are drawn in small parts on purpose"""
    n = str(name).lower()
    return "atlas" in n or n.startswith("sactx")


def _is_depth(fmt):
    return str(fmt).upper().startswith(("D16", "D24", "D32", "S8"))


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else 0.0


def texture_footprint(textures, draws, top_n=15):
    """
    textures: list_frame_textures entries (resource_id, name, width, height, mips, format, size_bytes, is_render_target ...)
    draws:    coverage entries with sampled_textures (ids), passed, marker, event_id
    """
    by_id = dict((t["resource_id"], t) for t in textures)
    use = {}
    unknown_ids = set()
    for d in draws:
        pixels = d.get("passed", 0)
        for rid in d.get("sampled_textures", []):
            t = by_id.get(rid)
            if t is None:
                unknown_ids.add(rid)
                continue
            u = use.setdefault(rid, {"pixels": 0, "draws": 0, "events": [], "markers": {}})
            u["pixels"] += pixels
            u["draws"] += 1
            if len(u["events"]) < 4:
                u["events"].append(d["event_id"])
            m = d.get("marker", "")
            u["markers"][m] = u["markers"].get(m, 0) + 1

    rows = []
    for rid, u in use.items():
        t = by_id[rid]
        bpt = _texel_bytes(t)
        size = t["size_bytes"]
        footprint = min(size, u["pixels"] * bpt)
        rows.append({
            "resource_id": rid, "name": t["name"], "size": "%dx%d" % (t["width"], t["height"]), "mips": t["mips"],
            "format": t["format"], "bits_per_pixel": round(_bits_per_pixel(t), 2), "is_render_target": t["is_render_target"],
            "size_mb": round(size / _MB, 3), "pixels_sampled": u["pixels"], "draws": u["draws"],
            "footprint_mb": round(footprint / _MB, 3), "utilization": round(footprint / size, 3) if size else None,
            "sample_event_ids": u["events"],
            "top_marker": max(u["markers"], key=u["markers"].get) if u["markers"] else "",
            "_size": size, "_footprint": footprint, "_t": t,
        })
    rows.sort(key=lambda r: -r["_footprint"])

    total_fp = sum(r["_footprint"] for r in rows)
    total_size = sum(r["_size"] for r in rows)
    rt_fp = sum(r["_footprint"] for r in rows if r["is_render_target"])

    findings = []
    # 1. big textures of which the frame touches only a small part
    for r in sorted(rows, key=lambda r: -(r["_size"] - r["_footprint"])):
        t = r["_t"]
        if r["is_render_target"] or _is_depth(t["format"]) or _is_atlas(r["name"]):
            continue
        waste = r["_size"] - r["_footprint"]
        if r["_size"] >= 0.5 * _MB and r["utilization"] is not None and r["utilization"] <= 0.25 and waste >= 0.4 * _MB:
            findings.append({
                "type": "low_utilization", "severity": "medium" if waste >= 2 * _MB else "low",
                "name": r["name"], "resource_id": r["resource_id"], "mb": round(waste / _MB, 2),
                "detail": "%s (%s, %s, %.2f MB) is sampled by %d draw(s) covering %d pixels, at most %.0f%% of its texels"
                          % (r["name"], r["size"], r["format"], r["size_mb"], r["draws"], r["pixels_sampled"], 100 * r["utilization"]),
                "suggestion": "Check whether the resolution is higher than needed on screen (lower max size / texture "
                              "streaming budget / mip bias). An atlas drawn in small parts is expected: ignore those.",
                "events": r["sample_event_ids"],
            })
    # 2. large uncompressed colour textures that could be block-compressed
    for r in rows:
        t = r["_t"]
        fmt = str(t["format"]).upper()
        if r["is_render_target"] or not fmt.startswith(_UNCOMPRESSED_PREFIXES):
            continue
        bpp = _bits_per_pixel(t)
        save = r["_size"] * (1.0 - _ASTC_BPP / bpp) if bpp > _ASTC_BPP else 0
        if r["_size"] >= 1 * _MB and save >= 0.5 * _MB:
            findings.append({
                "type": "uncompressed_texture", "severity": "high" if save >= 4 * _MB else "medium",
                "name": r["name"], "resource_id": r["resource_id"], "mb": round(save / _MB, 2),
                "detail": "%s (%s) is stored as %s (%.1f bits/pixel, %.2f MB) and sampled by %d draw(s)"
                          % (r["name"], r["size"], t["format"], bpp, r["size_mb"], r["draws"]),
                "suggestion": "Compress to ASTC (about %.1f bits/pixel at 6x6, saves ~%.1f MB of memory and fetch traffic). "
                              "Keep uncompressed only if it is generated at runtime or needs exact values (data / LUT)."
                              % (_ASTC_BPP, save / _MB),
                "events": r["sample_event_ids"],
            })
    # 3. render targets read back as textures: stored in full, then fetched
    for r in rows:
        if r["is_render_target"] and not _is_depth(r["_t"]["format"]) and r["footprint_mb"] >= 4.0:
            findings.append({
                "type": "heavy_render_target_read", "severity": "medium",
                "name": r["name"], "resource_id": r["resource_id"], "mb": r["footprint_mb"],
                "detail": "render target %s (%s %s) is sampled by %d draw(s), up to %.1f MB read per frame"
                          % (r["name"], r["size"], r["format"], r["draws"], r["footprint_mb"]),
                "suggestion": "Reading a full-size render target costs its store plus this fetch. Lower its resolution, "
                              "use a cheaper format, or merge the passes that read it.",
                "events": r["sample_event_ids"],
            })
    rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (rank[f["severity"]], -f["mb"]))

    # informational: single-mip colour textures that are not obviously UI atlases
    single_mip = [{
        "name": r["name"], "size": r["size"], "format": r["format"], "size_mb": r["size_mb"], "draws": r["draws"],
        "pixels_per_draw": round(r["pixels_sampled"] / float(r["draws"])) if r["draws"] else 0,
    } for r in rows if r["mips"] == 1 and not r["is_render_target"] and not _is_depth(r["_t"]["format"])
        and max(r["_t"]["width"], r["_t"]["height"]) >= 512][:top_n]

    by_marker = {}
    for r in rows:
        by_marker[r["top_marker"]] = by_marker.get(r["top_marker"], 0.0) + r["_footprint"]

    for r in rows:
        for k in ("_size", "_footprint", "_t"):
            del r[k]
    return {
        "textures_sampled": len(rows),
        "totals": {
            "footprint_mb": round(total_fp / _MB, 2), "size_mb": round(total_size / _MB, 2),
            "render_target_footprint_mb": round(rt_fp / _MB, 2),
            "gb_per_s_at_60fps": round(total_fp * 60 / (1024.0 * _MB), 2),
            "unresolved_textures": len(unknown_ids),
        },
        "findings": findings[:30],
        "top_textures": rows[:top_n],
        "by_marker": [{"marker": m, "footprint_mb": round(v / _MB, 2), "share": _pct(v, total_fp)}
                      for m, v in sorted(by_marker.items(), key=lambda kv: -kv[1])[:top_n]],
        "single_mip_textures": single_mip,
        "note": "footprint = min(texture size, pixels sampling it x bytes per texel): an upper bound of memory read per "
                "frame, assuming one texel per pixel. Caches, filtering taps and anisotropy are not modelled, so use it "
                "to rank textures and compare before / after, not as measured GB/s. Each texture's top_marker is only "
                "the pass that uses it most. single_mip_textures is informational: atlases are single-mip on purpose, "
                "but character / scene textures without mips shimmer and thrash the cache.",
    }
