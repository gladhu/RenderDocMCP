"""report.build_report: folding of repeated bandwidth issues."""

import unittest

from mcp_server import report


def _snap(issues, traffic=100.0):
    return {
        "capture": {"api": "OpenGL"},
        "bandwidth": {"traffic_mb": traffic, "gb_per_s_at_60fps": 6.0, "issues": issues, "passes": []},
        "frame": {"draw_calls": 10},
    }


def _issue(kind, mb, n):
    return {"type": kind, "severity": "low", "mb": mb, "target": "depth:Temp %d" % n, "marker": "M",
            "detail": "", "event_id": n, "pass_index": n, "suggestion": "invalidate it"}


class FoldBandwidthTest(unittest.TestCase):
    def test_many_issues_of_one_type_fold_into_one_action(self):
        issues = [_issue("wasted_store", 10.0 - i, i) for i in range(10)]
        r = report.build_report(_snap(issues), top_actions=50)
        bw = [a for a in r["actions"] if a["area"] == "bandwidth"]
        self.assertEqual(len(bw), 4)                       # 3 heaviest + one folded
        folded = [a for a in bw if a["title"].startswith("7 more wasted_store")]
        self.assertEqual(len(folded), 1)
        self.assertAlmostEqual(folded[0]["impact_pct"], sum(10.0 - i for i in range(3, 10)), places=1)
        self.assertEqual(folded[0]["where"]["tile_passes"], list(range(3, 10)))
        self.assertEqual(r["actions_folded"], 6)
        kept = sorted(a["impact_pct"] for a in bw if a is not folded[0])
        self.assertEqual(kept, [8.0, 9.0, 10.0])

    def test_one_leftover_is_kept_as_is(self):
        issues = [_issue("wasted_store", 5.0, i) for i in range(4)]
        r = report.build_report(_snap(issues), top_actions=50)
        bw = [a for a in r["actions"] if a["area"] == "bandwidth"]
        self.assertEqual(len(bw), 4)
        self.assertFalse(any("more" in a["title"] for a in bw))
        self.assertEqual(r["actions_folded"], 0)

    def test_types_are_folded_separately(self):
        issues = [_issue("wasted_store", 1.0, i) for i in range(5)] + [_issue("load_without_clear", 2.0, 10 + i) for i in range(2)]
        r = report.build_report(_snap(issues), top_actions=50)
        titles = [a["title"] for a in r["actions"] if a["area"] == "bandwidth"]
        self.assertEqual(sum(t.startswith("load_without_clear") for t in titles), 2)
        self.assertIn("2 more wasted_store issues", titles)


if __name__ == "__main__":
    unittest.main()
