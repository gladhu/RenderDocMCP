"""bandwidth_service: format sizes, GL tile pass reconstruction, load/store evaluation, issues, summary."""

import unittest

from tests._stubs import load_service

bw = load_service("bandwidth_service")

GL_FRAMEBUFFER = 36160
GL_COLOR_ATTACHMENT0 = 36064
GL_DEPTH_ATTACHMENT = 36096
GL_STENCIL_ATTACHMENT = 36128
GL_DEPTH_BUFFER_BIT = 0x100
GL_COLOR_DEPTH_STENCIL_BITS = 0x4500


class _Stream:
    """Builds a synthetic GL call stream with increasing event ids."""

    def __init__(self):
        self.eid = 0

    def ev(self, name, params=None, action=None, marker="m"):
        self.eid += 1
        return {"eid": self.eid, "name": name, "params": params or {}, "marker": marker, "action": action}

    def draw(self, outputs, depth=None):
        return self.ev("glDrawElements", action={"flags": 2, "outputs": outputs + [None] * (8 - len(outputs)),
                                                 "depth": depth})

    def bind(self, fb):
        return self.ev("glBindFramebuffer", {"target": GL_FRAMEBUFFER, "framebuffer": fb})

    def clear(self, mask):
        return self.ev("glClear", {"mask": mask}, action={"flags": 1, "outputs": [None] * 8, "depth": None})

    def invalidate(self, fb, attachments):
        return self.ev("glInvalidateFramebuffer", {"framebuffer": fb, "attachments": attachments},
                       action={"flags": 1, "outputs": [None] * 8, "depth": None})


TEXTURES = {
    "C": dict(name="Cam", width=1000, height=1000, format="R8G8B8A8_UNORM", samples=1, bpp=4, swapchain=False),
    "D": dict(name="CamDepth", width=1000, height=1000, format="D24_UNORM_S8_UINT", samples=1, bpp=4, swapchain=False),
    "S": dict(name="Shadow", width=1024, height=1024, format="D16_UNORM", samples=1, bpp=2, swapchain=False),
    "B": dict(name="Back", width=1000, height=1000, format="R8G8B8A8_UNORM", samples=1, bpp=4, swapchain=True),
}


def _frame_stream():
    s = _Stream()
    return [
        # pass 0: shadow map, depth cleared, sampled later -> store needed
        s.bind("F1"), s.clear(GL_DEPTH_BUFFER_BIT), s.draw([], "S"), s.draw([], "S"),
        # pass 1: camera, invalidate + clear at start, glFlush between draws, depth invalidated at the end
        s.bind("F2"), s.invalidate("F2", [GL_COLOR_ATTACHMENT0, GL_DEPTH_ATTACHMENT, GL_STENCIL_ATTACHMENT]),
        s.clear(GL_COLOR_DEPTH_STENCIL_BITS), s.draw(["C"], "D"), s.ev("glFlush"), s.draw(["C"], "D"),
        s.invalidate("F2", [GL_DEPTH_ATTACHMENT, GL_STENCIL_ATTACHMENT]),
        # pass 2: another target; pass 3: F2 reopened without clear, depth never read afterwards
        s.bind("F3"), s.draw(["B"]),
        s.bind("F2"), s.draw(["C"], "D"),
        # readback of F2 mid frame, then sync calls
        s.ev("glBlitFramebuffer"),
        s.ev("glFinish", action=None), s.ev("glFlush"),
    ]


class FormatBytesPerPixelTest(unittest.TestCase):
    def test_known_formats(self):
        cases = {
            "R8G8B8A8_SRGB": 4, "D24_UNORM_S8_UINT": 4, "D16_UNORM": 2, "R11G11B10_FLOAT": 4,
            "R16G16B16A16_FLOAT": 8, "R10G10B10A2_UNORM": 4, "D32_FLOAT_S8_UINT": 8, "R5G6B5_UNORM": 2,
        }
        for fmt, expected in cases.items():
            with self.subTest(fmt=fmt):
                self.assertEqual(bw.format_bytes_per_pixel(fmt), expected)


class GlPassAnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stream = _frame_stream()
        cls.passes, cls.syncs = bw.build_passes(stream)
        reads = {"S": [stream[2]["eid"] + 999], "C": [10 ** 6], "D": [], "B": []}
        bw.evaluate_passes(cls.passes, TEXTURES, reads)
        cls.issues = bw.find_issues(cls.passes, cls.syncs)
        cls.summary = bw.summarize(cls.passes, cls.issues, 30, "index")

    def _attachments(self, index):
        return {a["role"]: a for a in self.passes[index]["attachments"]}

    def _issue_types(self):
        return [i["type"] for i in self.issues]

    def test_passes_split_by_framebuffer_binding(self):
        self.assertEqual([p["fbo"] for p in self.passes], ["F1", "F2", "F3", "F2"])

    def test_invalidate_and_clear_skip_loads(self):
        a = self._attachments(1)
        self.assertFalse(a["color0"]["load"]["needed"])
        self.assertFalse(a["depth"]["load"]["needed"])

    def test_invalidated_depth_not_stored_but_color_read_later_is(self):
        a = self._attachments(1)
        self.assertFalse(a["depth"]["store"]["needed"])
        self.assertTrue(a["color0"]["store"]["needed"])
        self.assertTrue(a["color0"]["store"]["read_later"])

    def test_reopened_target_loads_color(self):
        load = self._attachments(3)["color0"]["load"]
        self.assertTrue(load["needed"])
        self.assertTrue(load["reopened"])

    def test_reopened_target_depth_stored_but_never_read(self):
        store = self._attachments(3)["depth"]["store"]
        self.assertTrue(store["needed"])
        self.assertFalse(store["read_later"])

    def test_expected_issue_types_found(self):
        types = self._issue_types()
        for t in ("render_target_reopened", "wasted_store", "flush_by_readback", "glFinish"):
            with self.subTest(issue=t):
                self.assertIn(t, types)

    def test_flush_inside_pass_attributed_to_camera_pass(self):
        self.assertTrue(any(i["type"] == "flush_inside_pass" and i["pass_index"] == 1 for i in self.issues),
                        self._issue_types())

    def test_reopened_issue_points_at_pass_3(self):
        reopened = [i for i in self.issues if i["type"] == "render_target_reopened"]
        self.assertEqual(reopened[0]["pass_index"], 3)

    def test_summary_counts_every_pass_and_issue(self):
        self.assertEqual(self.summary["pass_count"], 4)
        self.assertEqual(self.summary["issue_count"], len(self.issues))
        self.assertEqual(sum(v["count"] for v in self.summary["issues_by_type"].values()), len(self.issues))


def _issue(kind, nbytes, index):
    return {"type": kind, "severity": "high", "bytes": nbytes, "pass_index": index, "event_id": index,
            "marker": "m", "detail": "d", "suggestion": "s"}


class SummarizeIssueTotalsTest(unittest.TestCase):
    """issues_by_type / avoidable_mb are totals over every issue; the issues list itself is capped."""

    MB = int(bw._MB)

    def setUp(self):
        self.issues = ([_issue("wasted_store", self.MB, i) for i in range(40)]
                       + [_issue("render_target_reopened", 2 * self.MB, 40 + i) for i in range(20)]
                       + [_issue("flush_inside_pass", 4 * self.MB, 60 + i) for i in range(10)]
                       + [_issue("glFinish", 0, 70)])

    def test_issue_list_capped_at_50_for_small_top_n(self):
        out = bw.summarize([], self.issues, 10, "index")
        self.assertEqual(out["issue_count"], 71)
        self.assertEqual(len(out["issues"]), 50)

    def test_issue_list_cap_follows_larger_top_n(self):
        out = bw.summarize([], self.issues, 60, "index")
        self.assertEqual(len(out["issues"]), 60)

    def test_issues_by_type_counts_every_issue(self):
        out = bw.summarize([], self.issues, 10, "index")
        self.assertEqual(out["issues_by_type"], {
            "wasted_store": {"count": 40, "mb": 40.0},
            "render_target_reopened": {"count": 20, "mb": 40.0},
            "flush_inside_pass": {"count": 10, "mb": 40.0},
            "glFinish": {"count": 1, "mb": 0.0},
        })

    def test_avoidable_mb_covers_issues_beyond_the_list(self):
        out = bw.summarize([], self.issues, 10, "index")
        # only wasted stores, loads without clear and reopened targets count; flushes do not
        self.assertEqual(out["avoidable_mb"], 80.0)
        listed = sum(i["mb"] for i in out["issues"] if i["type"] in ("wasted_store", "render_target_reopened"))
        self.assertLess(listed, out["avoidable_mb"])

    def test_snapshot_and_report_use_the_totals(self):
        from mcp_server import compare, report
        out = bw.summarize([], self.issues, 10, "index")
        out["total"]["traffic_mb"] = 400.0
        snap = compare.build_snapshot(
            "s", {"filename": "s.rdc", "api": "OpenGL"}, {"statistics": {"draw_calls": 1}, "resource_counts": {}},
            {"pass_count": 0}, {"overall": {}}, {"change_counts": {}, "batching_opportunities": []},
            {"total_textures": 0, "total_size_mb": 0}, {"risk_count": 0, "risks": [], "main_resolution": "1x1"},
            out, None)
        self.assertEqual(snap["bandwidth"]["issues_by_type"],
                         {"wasted_store": 40, "render_target_reopened": 20, "flush_inside_pass": 10, "glFinish": 1})
        self.assertEqual(snap["bandwidth"]["issue_count"], 71)
        self.assertEqual(snap["bandwidth"]["avoidable_mb"], 80.0)
        card = [s for s in report.build_report(snap)["scorecard"]
                if s["metric"] == "Avoidable share of attachment traffic"][0]
        self.assertEqual(card["value"], 20.0)


if __name__ == "__main__":
    unittest.main()
