"""
End-to-end check of the analysis tools against a capture that is already open in RenderDoc.

Usage (RenderDoc running with the extension, capture loaded, e.g. via open_capture(use_remote=True)):
    .venv\\Scripts\\python.exe scripts\\e2e_check.py gles      # 8.057.001.108 (Unity GLES, Mali-G610)
    .venv\\Scripts\\python.exe scripts\\e2e_check.py vulkan    # test-Vulkan_02 (Unity Vulkan, Mali-G610)

The expected numbers below belong to those two captures (D:\\farm\\renderdocAI); for another capture,
add an entry to EXPECTED. Exit code 0 = all checks passed, 1 = a check failed, 2 = the bridge did not
answer (RenderDoc not running or crashed).
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_server.bridge.client import RenderDocBridge, RenderDocBridgeError  # noqa: E402

EXPECTED = {
    "gles": dict(api="OpenGL", draws=839, passes=9, traffic=80.9, wasted=7.69, shader=225, rt=8, blend=65,
                 depth=7, cull=69, discard=336, instanced=4, indices=683343),
    "vulkan": dict(api="Vulkan", draws=59, passes=2, traffic=28.7, wasted=9.57, shader=22, rt=1, blend=9,
                   depth=1, cull=3, discard=22, instanced=0, indices=92031),
}


class Lost(Exception):
    pass


def main(mode):
    exp = EXPECTED[mode]
    bridge = RenderDocBridge()
    results = []

    def call(method, params=None, timeout=900):
        try:
            return bridge.call(method, params or {}, timeout=timeout)
        except RenderDocBridgeError as e:
            if "timed out" in str(e).lower() or "no renderdoc mcp bridge" in str(e).lower():
                raise Lost(str(e))
            raise

    def check(name, got, want, tol=0):
        ok = (got == want) if not tol else abs(got - want) <= tol
        results.append(ok)
        print("%s %-30s got=%s want=%s" % ("PASS" if ok else "FAIL", name, got, want), flush=True)

    def first_int(text):
        return int(re.findall(r"\d+", text)[0])

    def status():
        check("api", call("get_capture_status")["api"], exp["api"])
        check("draw calls", call("get_frame_summary")["statistics"]["draw_calls"], exp["draws"])

    def instancing():
        o = call("stat_instancing_efficiency", {"top_n": 1})["overall"]
        check("instanced draws", o["instanced_draws"], exp["instanced"])
        check("indices submitted", o["total_indices_submitted"], exp["indices"])

    def state():
        r = call("analyze_state_changes", {"max_draws": exp["draws"]})
        check("state method", r["method"], "api_stream" if mode == "gles" else "replay")
        for key, e in (("shader", "shader"), ("render_target", "rt"), ("blend", "blend"), ("depth", "depth"),
                       ("cull", "cull")):
            check("state." + key, r["change_counts"][key], exp[e])

    def risks():
        rk = call("diagnose_mobile_risks")
        d = [x for x in rk["risks"] if x["category"] == "performance" and ":" in x["title"]
             and "discard" in x["title"].lower()]
        check("discard draws", first_int(d[0]["title"].split(":", 1)[1]) if d else 0, exp["discard"])

    def bandwidth():
        bw = call("analyze_pass_bandwidth", {"top_n": 50, "sort_by": "index"})
        check("tile passes", bw["pass_count"], exp["passes"])
        check("traffic MB", bw["total"]["traffic_mb"], exp["traffic"], 0.05)
        check("wasted store MB", bw["total"]["wasted_store_mb"], exp["wasted"], 0.05)
        check("issue totals cover all issues", sum(v["count"] for v in bw["issues_by_type"].values()),
              bw["issue_count"])

    def passes():
        rp = call("get_renderpass_list")
        check("render passes listed", rp["pass_count"] > 0, True)
        if mode == "vulkan":
            check("vulkan pass source", rp["passes"][0]["source"], "vulkan_render_pass")
            check("pass has a colour target", bool(rp["passes"][0]["color_targets"]), True)
        tx = call("list_frame_textures", {"top_n": 3, "rt_only": True})
        check("render-target textures", tx["total_textures"] > 0, True)

    def coverage():
        cov = call("get_draw_coverage", {"with_state": True, "with_textures": True,
                                         "with_sources": {"stages": ["pixel", "vertex"], "fold_uniforms": True}})
        check("coverage draws", cov["draw_count"], exp["draws"])
        check("coverage has pixels", cov["total_passed"] > 0, True)
        check("sources for both stages", sorted(cov["sources"]), ["pixel", "vertex"])
        check("pixel sources found", cov["sources"]["pixel"]["unique_sources"] > 0, True)
        check("shader language", cov["sources"]["pixel"]["language"], "glsl" if mode == "gles" else "spirv")
        check("state fields present", all("depth_test" in x and "blend_enabled" in x for x in cov["draws"]), True)
        check("topology and rt_size", all(x.get("topology") and x.get("rt_size") for x in cov["draws"]), True)

    for name, step in (("status", status), ("instancing", instancing), ("state", state), ("risks", risks),
                       ("bandwidth", bandwidth), ("passes", passes), ("coverage", coverage)):
        t = time.time()
        try:
            step()
        except Lost as e:
            print("step %s: lost the bridge (%s)" % (name, e), flush=True)
            return 2
        except Exception as e:
            results.append(False)
            print("FAIL step %s raised %s: %s" % (name, type(e).__name__, str(e)[:200]), flush=True)
        print("  step %-10s %.1fs" % (name, time.time() - t), flush=True)

    print("%d / %d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in EXPECTED:
        sys.exit("usage: e2e_check.py <%s>" % "|".join(EXPECTED))
    sys.exit(main(sys.argv[1]))
