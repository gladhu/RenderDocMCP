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


TESTS = {
    "get_debug_messages": t_get_debug_messages,
    "event_targets": t_event_targets,
    "render_overlay": t_render_overlay,
    "get_gpu_counters": t_get_gpu_counters,
}


async def main(mode, only):
    async with Client(mcp) as client:
        ctx = await call(client, "get_replay_context")
        if not (ctx.get("remote_connected") and not ctx.get("is_localhost") and ctx.get("capture_loaded")
                and not ctx.get("capture_local", True)):
            print("not replaying on a phone: %s" % ctx)
            print("run: scripts/dev_cycle.py all %s" % mode)
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
