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


TESTS = {
    "get_debug_messages": t_get_debug_messages,
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
