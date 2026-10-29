"""compare (snapshots + diff) and report (scorecard + ranked actions)."""

import copy
import json
import tempfile
import unittest

from tests import _stubs  # noqa: F401  (puts the repo root on sys.path)
from mcp_server import compare as c
from mcp_server import draw_analysis as da
from mcp_server import report
from mcp_server import texture_analysis as ta


def _snapshot(name, draws, load, store, wasted, cycles, with_reopen):
    issues = [{"type": "wasted_store", "severity": "high", "bytes": 4608000, "mb": wasted,
               "target": "depth:_CameraDepth 1600x720 D24S8", "marker": "RenderLoop.Draw", "detail": "x"}]
    if with_reopen:
        issues.append({"type": "render_target_reopened", "severity": "high", "bytes": 1, "mb": 17.58,
                       "target": "color0:Cam", "marker": "RenderLoop.Draw", "detail": "y"})
    passes = [{"marker": "A > Main", "draw_count": 100, "load_mb": load, "store_mb": store, "wasted_store_mb": wasted,
               "event_id_begin": 5, "attachments": [{"role": "color0", "size": "1600x720", "format": "R11G11B10_FLOAT"}]}]
    return c.build_snapshot(
        name, {"filename": name + ".rdc", "api": "OpenGL", "recorded_gpu": "Mali-G610", "vendor": "ARM"},
        {"statistics": {"draw_calls": draws, "markers": 5}, "resource_counts": {"textures": 3, "buffers": 4}},
        {"pass_count": 10}, {"overall": {"total_indices_submitted": 1000, "instanced_draws": 2}},
        {"analyzed_draws": draws, "change_counts": {"shader": 50, "render_target": 4, "blend": 1},
         "batching_opportunities": [{"count": 10}]},
        {"total_textures": 3, "total_size_mb": 87.4},
        {"risk_count": 1, "main_resolution": "1600x720", "risks": [{"severity": "high", "title": "R11G11B10 no sign"}]},
        {"pass_count": 9, "total": {"load_mb": load, "store_mb": store, "traffic_mb": load + store,
                                    "wasted_store_mb": wasted, "gb_per_s_at_60fps": 1.0},
         "issues": issues, "passes": passes},
        {"compiler": "mali", "gpu": "Mali-G610", "total_pixels": 1000, "total_occluded_pixels": 10, "total_cycles": cycles,
         "shaders": [{"pixel_shader": "Toon", "draw_count": 5, "pixels": 500, "occluded_pixels": 1, "cycles": cycles,
                      "cycles_per_pixel": cycles / 500.0, "textures": []}]},
    )


def _minimal_snapshot(name, shading=None, geometry=None, overdraw=None, texture_bandwidth=None, state=None):
    return c.build_snapshot(
        name, {"filename": name, "api": "OpenGL"}, {"statistics": {"draw_calls": 2}, "resource_counts": {}},
        {"pass_count": 1}, {"overall": {}}, state or {"change_counts": {}, "batching_opportunities": []},
        {"total_textures": 1, "total_size_mb": 1}, {"risk_count": 0, "risks": [], "main_resolution": "1x1"},
        {"pass_count": 0, "total": {}, "issues": [], "passes": []}, shading, geometry, overdraw, texture_bandwidth)


class DiffSnapshotsTest(unittest.TestCase):
    def setUp(self):
        self.before = _snapshot("before", 800, 28.0, 52.0, 7.7, 9000, True)
        self.after = _snapshot("after", 700, 10.0, 40.0, 0.0, 6000, False)
        self.after["bandwidth"]["issues"] = []
        self.diff = c.diff_snapshots(self.before, self.after)
        self.rows = {r["metric"]: r for r in self.diff["metrics"]}

    def test_metric_verdicts(self):
        self.assertEqual(self.rows["Draw calls"]["verdict"], "better")
        self.assertEqual(self.rows["Draw calls"]["pct"], -12.5)
        self.assertEqual(self.rows["Attachment load (MB)"]["verdict"], "better")
        self.assertEqual(self.rows["Instanced draws"]["verdict"], "same")

    def test_bandwidth_issues_resolved(self):
        self.assertEqual(len(self.diff["bandwidth_issues"]["resolved"]), 2)
        self.assertFalse(self.diff["bandwidth_issues"]["new"])

    def test_pass_traffic_delta(self):
        self.assertEqual(self.diff["bandwidth_passes"]["changed"][0]["delta_mb"], -30.0)

    def test_shader_cost_delta(self):
        self.assertEqual(self.diff["shading"]["cheaper"][0]["shader_cost_delta"], -3000)

    def test_no_warning_for_same_resolution(self):
        self.assertFalse(self.diff["warnings"])

    def test_resolution_change_warns(self):
        other = copy.deepcopy(self.after)
        other["capture"]["main_resolution"] = "2400x1080"
        self.assertTrue(c.diff_snapshots(self.before, other)["warnings"])

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            c.save_snapshot(self.before, d)
            self.assertEqual(c.load_snapshot("before", d)["name"], "before")


def _shader(name, pixels, cycles_per_pixel, costed_pixels=None, shader_hash="h1"):
    s = {"pixel_shader": name, "shader_hash": shader_hash, "draw_count": 1, "pixels": pixels,
         "occluded_pixels": 0, "cycles_per_pixel": cycles_per_pixel, "textures": ["t"],
         "cycles": round((costed_pixels if costed_pixels is not None else pixels) * (cycles_per_pixel or 0))}
    if costed_pixels is not None:
        s["costed_pixels"] = costed_pixels
    return s


def _shading(*shaders):
    return {"shading": {"shaders": list(shaders)}}


class DiffShadingTest(unittest.TestCase):
    def _only_change(self, before, after):
        d = c._diff_shading(_shading(before), _shading(after))
        changed = d["cheaper"] + d["costlier"] + [x for x in d["coverage_changed"]
                                                 if x not in d["cheaper"] + d["costlier"]]
        self.assertEqual(len(changed), 1, d)
        return d, changed[0]

    def test_shader_appearing_from_zero_pixels_is_all_coverage(self):
        d, ch = self._only_change(_shader("S", 0, None), _shader("S", 500, 12.0))
        self.assertEqual(ch["delta_cycles"], 6000)
        self.assertEqual(ch["shader_cost_delta"], 0)
        self.assertEqual(ch["coverage_delta"], 6000)
        self.assertEqual(d["total_coverage_delta"], 6000)
        self.assertEqual(d["total_shader_cost_delta"], 0)

    def test_shader_dropping_to_zero_pixels_is_all_coverage(self):
        _, ch = self._only_change(_shader("S", 500, 12.0), _shader("S", 0, None))
        self.assertEqual(ch["shader_cost_delta"], 0)
        self.assertEqual(ch["coverage_delta"], -6000)
        self.assertEqual(ch["cycles_per_pixel"], [12.0, None])

    def test_both_sides_without_cycles_per_pixel_are_skipped(self):
        before, after = _shader("S", 0, None), _shader("S", 0, None)
        after["cycles"] = 5                       # inconsistent, but must not raise
        d = c._diff_shading(_shading(before), _shading(after))
        self.assertEqual(d["total_shader_cost_delta"], 0)
        self.assertEqual(d["coverage_changed"], [])

    def test_costed_pixels_used_for_split(self):
        # 1000 pixels shaded, only 500 of them costed by malioc; cycles/pixel 10 -> 8
        _, ch = self._only_change(_shader("S", 1000, 10.0, costed_pixels=500),
                                  _shader("S", 1000, 8.0, costed_pixels=500))
        self.assertEqual(ch["delta_cycles"], -1000)
        self.assertEqual(ch["shader_cost_delta"], -1000)
        self.assertEqual(ch["coverage_delta"], 0)

    def test_snapshot_keeps_costed_pixels(self):
        snap = _minimal_snapshot("s", shading={"shaders": [_shader("S", 1000, 2.0, costed_pixels=400)]})
        self.assertEqual(snap["shading"]["shaders"][0]["costed_pixels"], 400)
        legacy = _minimal_snapshot("s", shading={"shaders": [_shader("S", 1000, 2.0)]})
        self.assertEqual(legacy["shading"]["shaders"][0]["costed_pixels"], 1000)

    def test_diff_snapshots_with_zero_pixel_side(self):
        a = _minimal_snapshot("a", shading={"shaders": [_shader("S", 0, None)]})
        b = _minimal_snapshot("b", shading={"shaders": [_shader("S", 300, 4.0)]})
        d = c.diff_snapshots(a, b)
        self.assertEqual(d["shading"]["total_coverage_delta"], 1200)
        json.dumps(d)


class SnapshotBatchingTest(unittest.TestCase):
    def test_batching_totals_prefer_full_counts(self):
        state = {"change_counts": {}, "batching_opportunities": [{"count": 9}, {"count": 4}],
                 "batching_run_count": 12, "batchable_draws": 40}
        snap = _minimal_snapshot("s", state=state)
        self.assertEqual(snap["state"]["batching_opportunities"], 12)
        self.assertEqual(snap["state"]["batchable_draws"], 40)

    def test_batching_totals_fall_back_to_listed_runs(self):
        state = {"change_counts": {}, "batching_opportunities": [{"count": 9}, {"count": 4}]}
        snap = _minimal_snapshot("s", state=state)
        self.assertEqual(snap["state"]["batching_opportunities"], 2)
        self.assertEqual(snap["state"]["batchable_draws"], 13)


class BuildReportTest(unittest.TestCase):
    def setUp(self):
        snap = _snapshot("before", 800, 28.0, 52.0, 7.7, 9000, True)
        snap["risks"]["items"] = [{"category": "precision", "severity": "high", "title": "R11G11B10 no sign",
                                   "detail": "d", "fix": "clamp"}]
        snap["state"]["batching"] = [{"start_event": 10, "end_event": 50, "count": 40}]
        snap["shading"]["shaders"][0]["sample_event_ids"] = [7]
        snap["shading"]["shaders"][0]["late_zs_draws"] = 2
        self.report = report.build_report(snap)

    def test_actions_cover_all_areas(self):
        areas = [x["area"] for x in self.report["actions"]]
        for area in ("bandwidth", "shading", "submission", "risks"):
            with self.subTest(area=area):
                self.assertIn(area, areas)

    def test_actions_ranked_and_sorted_by_priority(self):
        actions = self.report["actions"]
        self.assertEqual([x["rank"] for x in actions], list(range(1, len(actions) + 1)))
        order = [{"high": 0, "medium": 1, "low": 2}[x["priority"]] for x in actions]
        self.assertEqual(order, sorted(order))

    def test_shading_action_mentions_late_z_and_sample_event(self):
        sh = [x for x in self.report["actions"] if x["area"] == "shading"][0]
        self.assertIn("late-Z", sh["how"])
        self.assertIn("event 7", sh["title"])


class GeometryOverdrawSectionsTest(unittest.TestCase):
    def setUp(self):
        cov = [dict(event_id=1, marker="A/B/C", num_indices=30000, num_instances=1, topology="TriangleList",
                    passed=1000, failed=0, textures=["t"], render_target="r", rt_size=[1000, 1000],
                    blend_enabled=False, blend_opaque=None),
               dict(event_id=2, marker="A/B/D", num_indices=300, num_instances=1, topology="TriangleList",
                    passed=500000, failed=400000, textures=["u"], render_target="r", rt_size=[1000, 1000],
                    blend_enabled=False, blend_opaque=None)]
        geo = da.geometry_cost(cov, {1: {"cost": 3.0, "hash": "x"}}, 5)
        ov = da.overdraw(cov, {1: {"cost": 1.0, "late_z": False}, 2: {"cost": 2.0, "late_z": True}}, 5)
        self.full = _snapshot("before", 800, 28.0, 52.0, 7.7, 9000, True)
        self.a = _minimal_snapshot("a", self.full["shading"], geo, ov)
        self.b = _minimal_snapshot("b", self.full["shading"], geo, ov)

    def test_sections_stored(self):
        self.assertTrue(self.a["geometry_cost"]["by_marker"][0]["path"])
        self.assertTrue(self.a["overdraw"]["render_targets"])

    def test_vertex_metric_only_when_both_sides_have_it(self):
        metrics = [m["metric"] for m in c.diff_snapshots(self.a, self.b)["metrics"]]
        self.assertIn("Vertices submitted", metrics)
        metrics = [m["metric"] for m in c.diff_snapshots(self.a, self.full)["metrics"]]
        self.assertNotIn("Vertices submitted", metrics)

    def test_report_uses_geometry_and_overdraw(self):
        rep = report.build_report(self.a)
        self.assertLessEqual({"geometry", "overdraw"}, set(x["area"] for x in rep["actions"]))
        # the coarse "shaded but hidden" view is replaced by the overdraw section
        self.assertFalse(any(x["title"].startswith("Shaded but hidden") for x in rep["actions"]))
        self.assertTrue(any(s["area"] == "overdraw" for s in rep["scorecard"]))
        self.assertTrue(any(s["area"] == "geometry" for s in rep["scorecard"]))
        json.dumps(rep)


class TextureSectionTest(unittest.TestCase):
    def setUp(self):
        tx = [dict(resource_id="T1", name="Big_D", width=2048, height=2048, depth=1, array_size=1, mips=1,
                   format="R8G8B8A8_SRGB", size_bytes=2048 * 2048 * 4, is_render_target=False)]
        tb = ta.texture_footprint(tx, [dict(event_id=1, passed=5000000, sampled_textures=["T1"], marker="m")], 5)
        self.snap = _minimal_snapshot("c", texture_bandwidth=tb)

    def test_texture_metric_in_diff(self):
        metrics = [m["metric"] for m in c.diff_snapshots(self.snap, self.snap)["metrics"]]
        self.assertIn("Texture sampling footprint (MB)", metrics)

    def test_report_flags_uncompressed_texture(self):
        rep = report.build_report(self.snap)
        self.assertTrue(any(x["area"] == "textures" and "uncompressed_texture" in x["title"] for x in rep["actions"]),
                        rep["actions"])
        self.assertTrue(any(s["area"] == "textures" for s in rep["scorecard"]))


if __name__ == "__main__":
    unittest.main()
