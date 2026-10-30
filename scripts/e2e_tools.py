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


TESTS = {
    "get_debug_messages": t_get_debug_messages,
    "render_overlay": t_render_overlay,
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
