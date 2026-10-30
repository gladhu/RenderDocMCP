"""
On-device end-to-end checks of the debugging / optimisation tools, through the real MCP layer.

    .venv\\Scripts\\python.exe scripts\\dev_cycle.py all vulkan      # restart RenderDoc, open the capture on the phone
    .venv\\Scripts\\python.exe scripts\\e2e_tools.py vulkan [tool ...]

The tools are called in-process with a FastMCP client (argument validation, the result cache and the bridge
are all in the path). Every run first asserts that the capture is replayed on the phone, not on this PC:
the target platform is mobile, a local replay says nothing about it. Without tool names all checks run.
Exit code 0 = all passed, 1 = a check failed, 2 = not replaying on a phone / bridge lost.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastmcp import Client  # noqa: E402

from mcp_server.server import mcp  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s %-44s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


async def call(client, tool, **args):
    r = await client.call_tool(tool, args, raise_on_error=False)
    if r.is_error:
        text = " ".join(getattr(c, "text", "") for c in r.content)
        raise RuntimeError("%s: %s" % (tool, text[:300]))
    if r.structured_content is not None:
        return r.structured_content
    return json.loads(r.content[0].text)


# ---------------------------------------------------------------- per tool checks

async def t_get_debug_messages(client, mode):
    r = await call(client, "get_debug_messages", max_messages=5, max_groups=5)
    check("debug: counts are consistent", r["total_matched"] == sum(r["by_severity"].values()),
          "in_capture=%s matched=%s %s" % (r["total_in_capture"], r["total_matched"], r["by_severity"]))
    check("debug: caps respected", len(r["messages"]) <= 5 and len(r["groups"]) <= 5)
    check("debug: groups cover the matched messages when untruncated",
          r["groups_truncated"] or sum(g["count"] for g in r["groups"]) == r["total_matched"])
    hi = await call(client, "get_debug_messages", min_severity="high")
    check("debug: min_severity filters", set(hi["by_severity"]) <= {"high"} and hi["total_matched"] <= r["total_matched"],
          "high=%s" % hi["total_matched"])
    none = await call(client, "get_debug_messages", text_filter="\u0001no such text\u0001")
    check("debug: text filter with no hit", none["total_matched"] == 0 and none["groups"] == [])
    for g in r["groups"][:3]:
        print("     [%s/%s x%d] %s" % (g["severity"], g["category"], g["count"], g["description"][:110]))


async def pick_draw(client, min_indices=100):
    """A mid-frame draw call with real geometry (works on every API / capture, also when the draw list is huge)"""
    r = await call(client, "get_draw_calls", only_actions=True, flags_filter=["Drawcall"])
    if "actions" not in r:                                  # too large: the result was cached, ask for windows
        for lo in range(500, 20000, 500):
            r = await call(client, "get_draw_calls", only_actions=True, flags_filter=["Drawcall"],
                           event_id_min=lo, event_id_max=lo + 499)
            if "actions" in r and sum(1 for a in r["actions"] if a.get("num_indices", 0) >= min_indices) >= 5:
                break
    draws = [a for a in r["actions"] if a.get("num_indices", 0) >= min_indices]
    return draws[len(draws) * 2 // 5]["event_id"]


def png_ok(path):
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        return head == b"\x89PNG\r\n\x1a\n" and os.path.getsize(path) > 1000
    except OSError:
        return False


async def t_render_overlay(client, mode):
    eid = await pick_draw(client)
    cov = (await call(client, "get_draw_coverage", event_ids=[eid]))["draws"][0]
    print("     draw event %d: coverage passed=%d failed=%d rt=%s" % (eid, cov["passed"], cov["failed"], cov["rt_size"]))

    d = await call(client, "render_overlay", event_id=eid, overlay="depth")
    by = d["stats"]["by_label"]
    check("overlay depth: passed == coverage", by["passed"]["pixels"] == cov["passed"], "%d vs %d" % (by["passed"]["pixels"], cov["passed"]))
    check("overlay depth: failed == coverage", by["failed"]["pixels"] == cov["failed"], "%d vs %d" % (by["failed"]["pixels"], cov["failed"]))
    check("overlay depth: labels cover the target",
          sum(v["pixels"] for v in by.values()) == d["stats"]["total_pixels"] == cov["rt_size"][0] * cov["rt_size"][1])
    check("overlay depth: PNG written", png_ok(d["image_path"]), d["image_path"])

    qd = (await call(client, "render_overlay", event_id=eid, overlay="quad_overdraw_draw"))["stats"]
    qp = (await call(client, "render_overlay", event_id=eid, overlay="quad_overdraw_pass"))["stats"]
    if mode == "gles":                                      # RenderDoc cannot fill this overlay on GL / GLES
        check("overlay quad_overdraw: flagged unsupported on GLES", qd.get("supported") is False and "analyze_overdraw" in qd["warning"],
              "covered=%d" % qd["covered_pixels"])
    else:
        check("overlay quad_overdraw_draw: covers ~ the passed pixels",
              cov["passed"] == 0 or abs(qd["covered_pixels"] - cov["passed"]) <= max(0.1 * cov["passed"], 64),
              "covered=%d passed=%d" % (qd["covered_pixels"], cov["passed"]))
        check("overlay quad_overdraw_draw: mean >= 1", cov["passed"] == 0 or qd["mean_overdraw"] >= 1.0, "mean=%s max=%s" % (qd["mean_overdraw"], qd["max_overdraw"]))
        check("overlay quad_overdraw_pass covers at least the draw", qp["covered_pixels"] >= qd["covered_pixels"],
              "pass=%d draw=%d mean=%s max=%s" % (qp["covered_pixels"], qd["covered_pixels"], qp["mean_overdraw"], qp["max_overdraw"]))

    ts = (await call(client, "render_overlay", event_id=eid, overlay="triangle_size_draw"))["stats"]
    check("overlay triangle_size_draw: covers ~ the passed pixels",
          cov["passed"] == 0 or abs(ts["covered_pixels"] - cov["passed"]) <= max(0.05 * cov["passed"], 64),
          "covered=%d passed=%d" % (ts["covered_pixels"], cov["passed"]))
    check("overlay triangle_size_draw: buckets sum to covered",
          sum(b["pixels"] for b in ts["pixels_by_triangle_area"].values()) == ts["covered_pixels"])

    bf = (await call(client, "render_overlay", event_id=eid, overlay="backface_cull"))["stats"]
    check("overlay backface_cull: kept pixels", bf["by_label"]["kept"]["pixels"] + bf["by_label"]["culled"]["pixels"] >= cov["passed"] * 0.9,
          str({k: v["pixels"] for k, v in bf["by_label"].items()}))
    wf = await call(client, "render_overlay", event_id=eid, overlay="wireframe", analyze=False)
    check("overlay wireframe: image only, no stats", "stats" not in wf and png_ok(wf["image_path"]))
    vs = await call(client, "render_overlay", event_id=eid, overlay="viewport_scissor")
    check("overlay viewport_scissor: rectangles reported", "viewport" in vs["viewport_scissor"] and "scissor" in vs["viewport_scissor"],
          str(vs["viewport_scissor"].get("viewport")))
    nn = (await call(client, "render_overlay", event_id=eid, overlay="nan"))["stats"]
    check("overlay nan: reports marked pixels", nn["marked_pixels"] >= 0 and nn["total_pixels"] > 0, "marked=%d" % nn["marked_pixels"])

    for bad, args in (("unknown overlay", dict(event_id=eid, overlay="sparkle")),
                      ("event without a draw", dict(event_id=1, overlay="depth"))):
        try:
            await call(client, "render_overlay", **args)
            check("overlay error: " + bad, False, "no error raised")
        except RuntimeError as e:
            check("overlay error: " + bad, True, str(e)[-90:])
    again = await call(client, "render_overlay", event_id=eid, overlay="drawcall")
    check("overlay: bridge still healthy after errors", again["stats"]["by_label"]["draw"]["pixels"] > 0 or cov["passed"] == 0)


async def t_get_gpu_counters(client, mode):
    lst = await call(client, "get_gpu_counters")
    names = [c["name"] for c in lst["available"]]
    check("counters: list has GPU Duration", "GPU Duration" in names, "%d available %s" % (lst["available_count"], lst["by_vendor"]))
    check("counters: list entries are complete", all({"id", "unit", "vendor", "result", "bytes", "category"} <= set(c) for c in lst["available"]))
    if mode == "gles":
        check("counters: Arm HWCPipe counters on GLES", lst["by_vendor"].get("arm", 0) >= 30, str(lst["by_vendor"]))
    else:
        check("counters: Vulkan has Samples Passed", "Samples Passed" in names)

    r = await call(client, "get_gpu_counters", counters=["gpu_duration"], top_n=3)
    c = r["counters"][0]
    check("counters: GPU Duration per event", c["events"] == r["events_in_scope"] > 0 and c["total"] > 0,
          "events=%d total=%.2f ms" % (c["events"], c["total"] * 1000))
    check("counters: top events sorted", [e["value"] for e in c["top_events"]] == sorted((e["value"] for e in c["top_events"]), reverse=True))
    ev = await call(client, "get_gpu_counters", counters=["gpu_duration"], include_events=True, event_id_max=400)
    sub = ev["counters"][0]["total"]                       # same call: the table and the total share one measurement
    check("counters: per-event table sums to the total (same call)", abs(sum(e["GPU Duration"] for e in ev["events"]) - sub) <= 1e-4 * sub,
          "%d events, %.3f ms" % (len(ev["events"]), sub * 1000))
    # GPU time moves with the phone's GPU clock between measurements, so single numbers are compared loosely
    # and repeat is what makes them usable
    rp = await call(client, "get_gpu_counters", counters=["gpu_duration"], repeat=5, top_n=3)
    rc = rp["counters"][0]
    check("counters: repeat=5 reports 5 run totals and a spread", rp["repeat"] == 5 and len(rc["run_totals"]) == 5 and rc["run_spread_pct"] is not None,
          "median total %.2f ms, runs %s ms, spread %.1f%%" % (rc["total"] * 1000, [round(t * 1000, 1) for t in rc["run_totals"]], rc["run_spread_pct"]))
    check("counters: repeat median total lies within the run totals", min(rc["run_totals"]) * 0.7 <= rc["total"] <= max(rc["run_totals"]) * 1.3)
    top = rc["top_events"]
    timings = await call(client, "get_action_timings", event_ids=[e["event_id"] for e in top])
    got = dict((t["event_id"], t["duration_ms"]) for t in timings["timings"])
    ratios = [got[e["event_id"]] / (e["value"] * 1000) for e in top if e["event_id"] in got]
    check("counters: get_action_timings same order of magnitude as the counter", len(ratios) == len(top) and all(0.3 <= x <= 1.5 for x in ratios),
          "timings/counter = %s" % [round(x, 2) for x in ratios])

    if mode == "vulkan":
        sp = await call(client, "get_gpu_counters", counters=["samples_passed"], top_n=5)
        cov = {d["event_id"]: d["passed"] for d in (await call(client, "get_draw_coverage", event_ids=[e["event_id"] for e in sp["counters"][0]["top_events"]]))["draws"]}
        big = [e for e in sp["counters"][0]["top_events"] if cov.get(e["event_id"], 0) >= 100000]
        check("counters: Samples Passed ~ depth-test coverage on big draws",
              bool(big) and all(abs(e["value"] - cov[e["event_id"]]) <= 0.01 * cov[e["event_id"]] for e in big),
              str([(e["event_id"], e["value"], cov[e["event_id"]]) for e in big]))
    else:
        want = ["ExternalMemoryReadBytes", "FragmentCycles", "Tiles", "EarlyZKilled"]
        arm = await call(client, "get_gpu_counters", counters=want, top_n=2)
        check("counters: Arm counters fetched by name", [x["name"].strip() for x in arm["counters"]] == want and arm["unknown_selectors"] == [],
              "values=%d" % arm["values_returned"])
        check("counters: external memory read is non-zero", arm["counters"][0]["total"] > 0, "%.1f MB" % (arm["counters"][0]["total"] / 1e6))
        grp = await call(client, "get_gpu_counters", counters=["@arm"], top_n=1, event_id_min=500, event_id_max=700)
        check("counters: @arm group fetches every Arm counter", len(grp["counters"]) == lst["by_vendor"]["arm"],
              "%d counters, %d events" % (len(grp["counters"]), grp["events_in_scope"]))
    one = r["counters"][0]["top_events"][0]["event_id"]
    part = await call(client, "get_gpu_counters", counters=["gpu_duration"], event_id_min=one, event_id_max=one)
    check("counters: event range selects one event", part["events_in_scope"] == 1 and part["counters"][0]["events"] == 1)
    mix = await call(client, "get_gpu_counters", counters=["gpu_duration", "no_such_counter"])
    check("counters: unknown selector reported, known ones still fetched", mix["unknown_selectors"] == ["no_such_counter"] and len(mix["counters"]) == 1)
    try:
        await call(client, "get_gpu_counters", counters=["no_such_counter"])
        check("counters: only-unknown selectors raise", False)
    except RuntimeError as e:
        check("counters: only-unknown selectors raise", "Available" in str(e), str(e)[-80:])


async def t_event_targets(client, mode):
    """Tools that take event_id + target must find the render target on every API, and must fail loudly"""
    eid = await pick_draw(client)
    pp = await call(client, "pick_pixel", x=10, y=10, event_id=eid)
    check("event targets: pick_pixel returns a pixel (not null)", isinstance(pp, dict) and len(pp["float"]) == 4, "%s %s" % (pp["format"], pp["resource_id"]))
    rid = pp["resource_id"]
    st = await call(client, "read_texture_stats", event_id=eid)
    check("event targets: read_texture_stats resolves the same texture", st["resource_id"] == rid and len(st["max"]) == 4)
    path = os.path.join(os.environ.get("TEMP", "."), "renderdoc_mcp", "e2e_target_%s.png" % mode)
    sv = await call(client, "save_texture", event_id=eid, output_path=path)
    check("event targets: save_texture writes the target", sv["resource_id"] == rid and png_ok(path), path)
    ph = await call(client, "pixel_history", x=10, y=10, event_id=eid)
    check("event targets: pixel_history resolves the same texture", ph["resource_id"] == rid, "%d modifications" % ph["total_count"])
    fp = await call(client, "find_pixels", event_id=eid, filters={"min_r": 0.0}, max_probes=16)
    check("event targets: find_pixels resolves the same texture", fp["resource_id"] == rid)
    by_res = await call(client, "pick_pixel", x=10, y=10, resource_id=rid)
    check("event targets: an existing resource_id is found", by_res["resource_id"] == rid and by_res["format"] == pp["format"], rid)
    dp = await call(client, "pick_pixel", x=10, y=10, event_id=eid, target="depth")
    check("event targets: depth target of the same event", dp["resource_id"] != rid, dp["format"])

    for tool, args in (("pick_pixel", dict(x=1, y=1, event_id=1)), ("save_texture", dict(event_id=1, output_path=path)),
                       ("read_texture_stats", dict(event_id=1)), ("pixel_history", dict(x=1, y=1, event_id=1)),
                       ("sample_pixel_region", dict(event_id=1)), ("pick_pixel", dict(x=1, y=1, resource_id="ResourceId::99999999"))):
        try:
            r = await call(client, tool, **args)
            check("errors propagate: %s %s" % (tool, "resource" if "resource_id" in args else "event 1"), False, "returned %s instead of an error" % str(r)[:60])
        except RuntimeError:
            check("errors propagate: %s %s" % (tool, "resource" if "resource_id" in args else "event 1"), True)


async def t_diff_textures(client, mode):
    eid = await pick_draw(client)
    cov = (await call(client, "get_draw_coverage", event_ids=[eid]))["draws"][0]
    rid = (await call(client, "pick_pixel", x=0, y=0, event_id=eid))["resource_id"]
    print("     draw event %d writes %s: coverage passed=%d failed=%d" % (eid, rid, cov["passed"], cov["failed"]))

    same = await call(client, "diff_textures", event_id_a=eid, event_id_b=eid)
    check("diff: a target against itself is identical", same["identical"] and same["differing_pixels"] == 0,
          "%dx%d" % (same["width"], same["height"]))

    # a pixel only changes if this draw wrote it: differing pixels <= pixels that passed its depth test
    before = await call(client, "diff_textures", resource_id_a=rid, event_id_a=eid - 1, event_id_b=eid, threshold=0)
    check("diff: draw changes at most the pixels it shaded", before["differing_pixels"] <= cov["passed"],
          "differing=%d passed=%d (%.1f%% of the target)" % (before["differing_pixels"], cov["passed"], before["differing_pct"]))
    if cov["passed"] >= 1000:
        check("diff: draw with real coverage changes something", before["differing_pixels"] > 0 and before["bounding_box"] is not None,
              str(before["bounding_box"]))
        bb = before["bounding_box"]
        check("diff: bounding box inside the target", 0 <= bb["x0"] <= bb["x1"] < before["width"] and 0 <= bb["y0"] <= bb["y1"] < before["height"])
        check("diff: regions account for the differing pixels", sum(r["differing_pixels"] for r in before["regions"]) <= before["differing_pixels"] and before["regions"][0]["differing_pixels"] > 0)
    check("diff: heat map is a PNG", png_ok(before["heat_map_path"]), before["heat_map_path"])
    check("diff: sides describe the capture textures", before["side_a"]["resource_id"] == rid == before["side_b"]["resource_id"] and before["side_a"]["format"])

    # the same comparison against a file saved from the capture gives the same numbers
    path = os.path.join(os.environ.get("TEMP", "."), "renderdoc_mcp", "e2e_diff_ref_%s.png" % mode)
    await call(client, "save_texture", resource_id=rid, event_id=eid - 1, output_path=path)
    vs_file = await call(client, "diff_textures", resource_id_a=rid, event_id_a=eid, path_b=path)
    check("diff: capture vs saved file == capture vs capture", vs_file["differing_pixels"] == before["differing_pixels"] and vs_file["max_delta"] == before["max_delta"],
          "%d / %d" % (vs_file["differing_pixels"], before["differing_pixels"]))
    t = await call(client, "diff_textures", resource_id_a=rid, event_id_a=eid - 1, event_id_b=eid, threshold=255)
    check("diff: threshold 255 tolerates everything but is still not 'identical'", t["differing_pixels"] == 0 and t["identical"] is False)

    sizes = {}
    for tex in (await call(client, "list_frame_textures", top_n=50))["textures"]:
        sizes.setdefault((tex["width"], tex["height"]), tex["resource_id"])
    small = [v for k, v in sizes.items() if k != (same["width"], same["height"]) and k[0] > 0]
    if small:
        mism = await call(client, "diff_textures", resource_id_a=rid, event_id_a=eid, resource_id_b=small[0])
        check("diff: different sizes are reported, not raised", mism["comparable"] is False and "size differs" in mism["reason"], mism.get("reason"))
    else:
        print("     (no texture of another size in this capture: size mismatch not exercised on the device)")
    for name, args in (("side B empty", dict(event_id_a=eid)), ("missing file", dict(path_a="Z:/nope.png", path_b="Z:/nope2.png"))):
        try:
            await call(client, "diff_textures", **args)
            check("diff error: " + name, False, "no error raised")
        except RuntimeError:
            check("diff error: " + name, True)


NL = chr(10)


def _mode_edit(mode):
    """edit that makes every pixel of the shader's draws magenta (GLES)"""
    return [{"find": "void main()" + NL + "{", "replace": "void main()" + NL + "{" + NL + "    SV_Target0 = vec4(1.0, 0.0, 1.0, 1.0); return;"}]


async def t_replace_shader(client, mode):
    """Replace a shader on the phone, see the picture change, and get the original back"""
    import base64
    import struct
    # a draw that writes a colour target with real coverage
    if mode == "gles":
        cov = (await call(client, "get_draw_coverage", event_id_min=800, event_id_max=830))["draws"]
        eid = next(d["event_id"] for d in cov if d["passed"] > 5000 and d["rt_size"][0] >= 800)
        want = next(d["passed"] for d in cov if d["event_id"] == eid)
    else:
        eid = 203
        want = (await call(client, "get_draw_coverage", event_ids=[eid]))["draws"][0]["passed"]
    print("     event %d, %d pixels covered" % (eid, want))
    T = os.path.join(os.environ.get("TEMP", "."), "renderdoc_mcp")

    async def shot(name):
        path = os.path.join(T, "e2e_rep_%s_%s.png" % (mode, name))
        await call(client, "save_texture", event_id=eid, output_path=path)
        return path

    async def differ(a, b):
        return (await call(client, "diff_textures", path_a=a, path_b=b))["differing_pixels"]

    base = await shot("base")
    await call(client, "revert_shader")

    ctl = await call(client, "replace_shader", event_id=eid)
    check("replace: unchanged rebuild compiles on the device", ctl["compiled"] and ctl["replaced"] and ctl["compiler_output"] == "",
          "%s %d bytes" % (ctl["encoding"], ctl["bytes"]))
    check("replace: unchanged rebuild leaves the picture identical", await differ(base, await shot("ctl")) == 0)

    if mode == "gles":
        dry = await call(client, "replace_shader", event_id=eid, edits=_mode_edit(mode), dry_run=True)
        check("replace: dry run shows the diff and replaces nothing", dry["dry_run"] and dry["changes"]["changed"] and "replaced" not in dry, str(dry["changes"]["diff"][4:6]))
        bad = await call(client, "replace_shader", event_id=eid, source="this is not glsl")
        check("replace: device compiler error returned, nothing replaced", bad["compiled"] is False and bad["replaced"] is False and bad["compiler_output"] != "",
              bad["compiler_output"][:60])
        check("replace: failed build leaves the previous replacement working", await differ(base, await shot("after_bad")) == 0)
        r = await call(client, "replace_shader", event_id=eid, edits=_mode_edit(mode))
        check("replace: edited shader compiles", r["compiled"] and r["replaced"] and r["edits_applied"] == [1])
        n = await differ(base, await shot("magenta"))
        check("replace: picture changes exactly where the draw shaded", n == want, "%d differing vs %d covered" % (n, want))
        try:
            await call(client, "replace_shader", event_id=eid, edits=[{"find": "text that is not in the shader", "replace": "x"}])
            check("replace: edit with missing text raises", False)
        except RuntimeError as e:
            check("replace: edit with missing text raises", "not found" in str(e))
    else:
        spv_file = os.path.join(T, "e2e_rep_original.spv.b64")
        exported = await call(client, "get_shader_source", event_id=eid, stage="pixel", output_path=spv_file)
        check("replace: shader exported to a file (too big for the conversation)", exported["output_path"] == spv_file and "source" not in exported)
        with open(spv_file, encoding="utf-8") as fh:
            raw = bytearray(base64.b64decode(fh.read()))
        words = struct.unpack("<%dI" % (len(raw) // 4), bytes(raw))
        i, idx = 5, None
        f32 = set()
        while i < len(words):                        # first float constant equal to 1.0 (a plain scale / alpha factor)
            op, n = words[i] & 0xFFFF, words[i] >> 16
            if op == 22 and words[i + 2] == 32:
                f32.add(words[i + 1])
            if op == 43 and words[i + 1] in f32 and struct.unpack("<f", struct.pack("<I", words[i + 3]))[0] == 1.0:
                idx = i + 3
            i += n
        check("replace: found a float constant to edit in the SPIR-V", idx is not None)
        struct.pack_into("<f", raw, idx * 4, 0.0)
        mod_file = os.path.join(T, "e2e_rep_modified.spv")
        with open(mod_file, "wb") as fh:
            fh.write(bytes(raw))                           # raw binary this time
        r = await call(client, "replace_shader", event_id=eid, source_path=mod_file)
        check("replace: modified SPIR-V compiles on the device", r["compiled"] and r["replaced"], "%s %d bytes" % (r["encoding"], r["bytes"]))
        n = await differ(base, await shot("modified"))
        check("replace: modified SPIR-V changes the picture", n > 0, "%d pixels differ (the shader is shared by several draws)" % n)
        try:
            await call(client, "replace_shader", event_id=eid, edits=[{"find": "a", "replace": "b"}])
            check("replace: edits on SPIR-V explain what to do instead", False)
        except RuntimeError as e:
            check("replace: edits on SPIR-V explain what to do instead", "SPIR-V" in str(e))
        for label, blob in (("truncated SPIR-V", bytes(raw[:400])), ("not SPIR-V at all", b"this is not spirv at all!!")):
            try:
                bad_file = os.path.join(T, "e2e_rep_bad.bin")
                with open(bad_file, "wb") as fh:
                    fh.write(blob)
                await call(client, "replace_shader", event_id=eid, source_path=bad_file)
                check("replace: %s is refused before it reaches the device" % label, False)
            except RuntimeError as e:
                check("replace: %s is refused before it reaches the device" % label, "SPIR-V" in str(e), str(e)[-70:])
        check("replace: the replay still works after the refused blobs", await differ(base, await shot("after_refused")) == n)

    active = await call(client, "revert_shader")
    check("revert: everything reverted", active["active_replacements"] == [] and len(active["reverted"]) == 1)
    check("revert: picture is back to the original", await differ(base, await shot("reverted")) == 0)
    try:
        await call(client, "revert_shader", shader_id="ResourceId::1")
        check("revert: unknown shader id raises", False)
    except RuntimeError:
        check("revert: unknown shader id raises", True)
    cov2 = (await call(client, "get_draw_coverage", event_ids=[eid]))["draws"][0]["passed"]
    check("revert: the depth coverage is back too", cov2 == want, "%d vs %d" % (cov2, want))


async def t_debug_shader(client, mode):
    if mode == "gles":
        try:
            await call(client, "debug_shader", event_id=await pick_draw(client), kind="pixel", x=1, y=1)
            check("debug: GLES is refused with an explanation", False, "no error raised")
        except RuntimeError as e:
            check("debug: GLES is refused with an explanation", "not supported on OpenGL" in str(e) and "replace_shader" in str(e), str(e)[-110:])
        return

    eid = await pick_draw(client)
    wf = await call(client, "diff_textures", resource_id_a=(await call(client, "pick_pixel", x=0, y=0, event_id=eid))["resource_id"],
                    event_id_a=eid - 1, event_id_b=eid, grid=4)
    bb = wf["bounding_box"]
    # a pixel this draw really shaded (the history says the draw's fragment passed there)
    pixel = None
    for fy in (0.5, 0.35, 0.65, 0.2, 0.8):
        for fx in (0.5, 0.35, 0.65, 0.2, 0.8):
            px, py = int(bb["x0"] + fx * (bb["x1"] - bb["x0"])), int(bb["y0"] + fy * (bb["y1"] - bb["y0"]))
            h = await call(client, "pixel_history", x=px, y=py, event_id=eid, max_entries=50)
            if any(m["event_id"] == eid and m["passed"] for m in h["modifications"]):
                pixel = (px, py)
                break
        if pixel:
            break
    check("debug: found a pixel the draw shaded", pixel is not None, str(pixel))
    px, py = pixel
    frame = (await call(client, "pick_pixel", x=px, y=py, event_id=eid))["float"]

    t = await call(client, "debug_shader", event_id=eid, kind="pixel", x=px, y=py, max_steps=5)
    check("debug: pixel trace ran to the end on the phone", t["run_complete"] and t["total_steps"] > 50 and t["stage"] == "pixel",
          "%d steps" % t["total_steps"])
    check("debug: no uninitialised-fill noise in the steps", all(c["value"] != -107374176.0 for s in t["steps"] for c in s["changes"]) and
          t["steps"][0].get("uninitialised_declared", 0) > 0, "step 0 declared %s" % t["steps"][0].get("uninitialised_declared"))
    check("debug: summary has inputs and a bounded step list", len(t["inputs"]) > 0 and len(t["steps"]) == 5 and t["steps_truncated"])
    # the emulated shader must have produced the colour the GPU wrote: some variable ends up as the frame's pixel
    match = [f for f in t["final_values"] if isinstance(f["value"], list) and len(f["value"]) >= 3
             and all(isinstance(v, float) and abs(v - frame[i]) <= 0.01 for i, v in enumerate(f["value"][:3]))]
    check("debug: the trace's final colour equals the frame's pixel", bool(match),
          "frame %s, trace %s" % ([round(v, 4) for v in frame[:3]], match[-1]["value"][:3] if match else "no variable matched"))
    if match:
        name = match[-1]["name"]
        f = await call(client, "debug_shader", event_id=eid, kind="pixel", x=px, y=py, only_changes_of=[name], max_steps=100)
        check("debug: only_changes_of follows one variable", 0 < f["total_steps"] < 20 and f["executed_steps"] == t["total_steps"] and
              all(all(name in c["name"] for c in s["changes"]) for s in f["steps"]), "%s: %d writes of %d steps" % (name, f["total_steps"], f["executed_steps"]))

    # a NaN planted in the shader must be found at the instruction that produced it (the debugger runs the replaced shader)
    spv_file = os.path.join(os.environ.get("TEMP", "."), "renderdoc_mcp", "e2e_dbg_original.b64")
    await call(client, "get_shader_source", event_id=eid, stage="pixel", output_path=spv_file)
    import base64
    import struct
    with open(spv_file, encoding="utf-8") as fh:
        spv = bytearray(base64.b64decode(fh.read()))
    words = struct.unpack("<%dI" % (len(spv) // 4), bytes(spv))
    i, f32, consts = 5, set(), []
    while i < len(words):
        op, n = words[i] & 0xFFFF, words[i] >> 16
        if op == 22 and words[i + 2] == 32:
            f32.add(words[i + 1])
        if op == 43 and words[i + 1] in f32 and struct.unpack("<f", struct.pack("<I", words[i + 3]))[0] not in (0.0, 1.0):
            consts.append(i + 3)
        i += n
    planted = None
    for idx in consts[:8]:
        mod = bytearray(spv)
        struct.pack_into("<I", mod, idx * 4, 0x7FC00000)
        nan_file = os.path.join(os.environ.get("TEMP", "."), "renderdoc_mcp", "e2e_dbg_nan.spv")
        with open(nan_file, "wb") as fh:
            fh.write(bytes(mod))
        await call(client, "replace_shader", event_id=eid, source_path=nan_file)
        tn = await call(client, "debug_shader", event_id=eid, kind="pixel", x=px, y=py, max_steps=0)
        if tn["first_nan_or_inf"] is not None:
            planted = tn
            break
    check("debug: a planted NaN is found, with its step and variable", planted is not None and planted["first_nan_or_inf"]["variable"],
          str(planted["first_nan_or_inf"]) if planted else "none of %d constants produced a NaN" % len(consts[:8]))
    if planted:
        check("debug: nan_at_end says whether the NaN reached the end (list of variables)", isinstance(planted["nan_at_end"], list))
    await call(client, "revert_shader")
    clean = await call(client, "debug_shader", event_id=eid, kind="pixel", x=px, y=py, max_steps=0)
    check("debug: after revert the trace is clean again", clean["first_nan_or_inf"] is None and clean["total_steps"] == t["total_steps"])

    v = await call(client, "debug_shader", event_id=eid, kind="vertex", vertex=0, max_steps=3)
    check("debug: vertex trace runs", v["stage"] == "vertex" and v["run_complete"] and v["total_steps"] > 10 and len(v["inputs"]) > 0,
          "%d steps" % v["total_steps"])
    check("debug: no NaN in a healthy shader", t["first_nan_or_inf"] is None and v["first_nan_or_inf"] is None)

    for name, args in (("pixel nobody shaded", dict(event_id=eid, kind="pixel", x=1, y=1)),
                       ("pixel without x", dict(event_id=eid, kind="pixel", y=1)),
                       ("vertex without id", dict(event_id=eid, kind="vertex"))):
        try:
            await call(client, "debug_shader", **args)
            check("debug error: " + name, False, "no error raised")
        except RuntimeError as e:
            check("debug error: " + name, True, str(e)[-80:])
    ok = await call(client, "debug_shader", event_id=eid, kind="pixel", x=px, y=py, max_steps=1)
    check("debug: replay still healthy after errors", ok["total_steps"] == t["total_steps"])


TESTS = {
    "get_debug_messages": t_get_debug_messages,
    "event_targets": t_event_targets,
    "diff_textures": t_diff_textures,
    "replace_shader": t_replace_shader,
    "debug_shader": t_debug_shader,
    "render_overlay": t_render_overlay,
    "get_gpu_counters": t_get_gpu_counters,
}


def on_phone(ctx):
    return bool(ctx.get("remote_connected") and not ctx.get("is_localhost") and ctx.get("capture_loaded")
                and not ctx.get("capture_local", True))


async def main(mode, only):
    async with Client(mcp) as client:
        ctx = await call(client, "get_replay_context")
        if not on_phone(ctx):
            print("not replaying on a phone: %s" % ctx)
            print("run: scripts/dev_cycle.py ensure %s" % mode)
            return 2
        status = await call(client, "get_capture_status")
        print("device: %s (%s)  capture: %s  api: %s" % (
            ctx["remote_friendly_name"], ctx["remote_hostname"], os.path.basename(status["filename"]), status["api"]))
        for name, fn in TESTS.items():
            if only and name not in only:
                continue
            t = time.time()
            try:
                await fn(client, mode)
            except Exception as e:                      # noqa: BLE001
                results.append(False)
                print("FAIL %s raised %s: %s" % (name, type(e).__name__, str(e)[:300]), flush=True)
            print("  tool %-22s %.1fs" % (name, time.time() - t), flush=True)
    print("%d / %d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("gles", "vulkan"):
        sys.exit("usage: e2e_tools.py <gles|vulkan> [tool ...]")
    sys.exit(asyncio.run(main(sys.argv[1], set(sys.argv[2:]))))
