"""draw_analysis: triangle counts, vertex budget per pass / shader, overdraw per render target."""

import unittest

from tests import _stubs  # noqa: F401  (puts the repo root on sys.path)
from mcp_server import draw_analysis as da


def _draw(eid, marker, indices, instances=1, topology="TriangleList", passed=0, failed=0, **kw):
    return dict(event_id=eid, marker=marker, num_indices=indices, num_instances=instances, topology=topology,
                passed=passed, failed=failed, textures=[], **kw)


def _rt_draw(eid, marker, passed, failed, blend_enabled, blend_opaque=None, rt="r"):
    return _draw(eid, marker, 3, passed=passed, failed=failed, render_target=rt, rt_size=[1000, 1000],
                 blend_enabled=blend_enabled, blend_opaque=blend_opaque)


class TriangleCountTest(unittest.TestCase):
    def test_topologies(self):
        self.assertEqual(da.triangle_count("TriangleList", 300), 100)
        self.assertEqual(da.triangle_count("TriangleStrip", 10), 8)
        self.assertEqual(da.triangle_count("LineList", 10), 0)
        self.assertEqual(da.triangle_count("TriangleStrip", 1), 0)


class GeometryCostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        draws = [
            _draw(1, "Cam/Shadows/Cascade", 30000, passed=5000, failed=0),       # 10000 tris, 0.5 px/tri -> micro
            _draw(2, "Cam/Shadows/Cascade", 30000, instances=4, passed=2000),    # 40000 tris
            _draw(3, "Cam/Opaque", 3000, passed=90000, failed=1000),             # 1000 tris, 91 px/tri
            _draw(4, "Cam/Opaque", 6, passed=100, failed=0, topology="TriangleStrip"),
        ]
        vs = {1: {"cost": 2.0, "hash": "A"}, 2: {"cost": 2.0, "hash": "A"}, 3: {"cost": 4.0, "hash": "B"}}
        cls.g = da.geometry_cost(draws, vs, 5)

    def test_vertex_totals_include_instances(self):
        self.assertEqual(self.g["totals"]["vertices"], 30000 + 120000 + 3000 + 6)
        self.assertEqual(self.g["totals"]["draws_without_vs_cost"], 1)

    def test_shadow_pass_dominates(self):
        top = self.g["by_marker"][0]
        self.assertEqual(top["marker"], "Shadows > Cascade")
        self.assertGreater(top["share_of_vertices"], 90)

    def test_findings(self):
        types = [f["type"] for f in self.g["findings"]]
        self.assertIn("vertex_heavy_pass", types)
        self.assertIn("micro_triangles", types)
        heavy = [f for f in self.g["findings"] if f["type"] == "vertex_heavy_pass"][0]
        self.assertIn("Shadow casters", heavy["suggestion"])

    def test_vertex_shader_cycles(self):
        top = self.g["by_vertex_shader"][0]
        self.assertEqual(top["vertex_shader"], "A")
        self.assertEqual(top["vs_cycles"], 2 * 150000)

    def test_same_leaf_marker_under_different_parents_stays_separate(self):
        g = da.geometry_cost([_draw(40, "A/Shadow/Draw", 3000), _draw(41, "A/Camera/Draw", 3000)], {}, 5)
        self.assertEqual(len(g["by_marker"]), 2)


class OverdrawTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        draws = [
            _rt_draw(10, "Cam/Opaque", 500000, 400000, False),      # 44% rejected
            _rt_draw(11, "Cam/Opaque", 100000, 200000, False),      # late-Z
            _rt_draw(12, "Cam/Particles", 1500000, 0, True),        # 1.5 layers
            _rt_draw(13, "Cam/Opaque", 1000, 0, False),             # opaque after transparent
        ]
        ps = {10: {"cost": 2.0, "late_z": False}, 11: {"cost": 3.0, "late_z": True},
              12: {"cost": 1.0, "late_z": False}, 13: {"cost": 1.0, "late_z": False}}
        cls.r = da.overdraw(draws, ps, 5)

    def test_finding_types(self):
        self.assertEqual(sorted(f["type"] for f in self.r["findings"]),
                         ["draw_order", "late_z_waste", "opaque_overdraw", "transparent_layers"])

    def test_late_z_wasted_cycles(self):
        self.assertEqual(self.r["totals"]["late_z_wasted_cycles"], 600000)
        self.assertEqual(self.r["top_late_z_waste"][0]["event_id"], 11)

    def test_render_target_breakdown(self):
        t = self.r["render_targets"][0]
        self.assertEqual(t["transparent"]["avg_layers"], 1.5)
        self.assertEqual(t["opaque"]["draws"], 2)
        self.assertEqual(t["late_z"]["draws"], 1)

    def test_unknown_state_does_not_crash(self):
        r = da.overdraw([_draw(20, "x", 3, passed=10, failed=5, render_target="r", rt_size=None, blend_enabled=None)],
                        {}, 3)
        self.assertEqual(r["render_targets"][0]["state_unknown_draws"], 1)
        self.assertEqual(r["findings"], [])

    def test_opaque_blend_counts_as_opaque(self):
        draws = [
            _rt_draw(30, "Cam/Opaque", 100000, 0, True, blend_opaque=True),           # ONE/ZERO
            _rt_draw(31, "Cam/Transparent", 100000, 0, True, blend_opaque=False),
            _rt_draw(32, "Cam/Opaque", 100000, 300000, False),
        ]
        r = da.overdraw(draws, {}, 3)
        t = r["render_targets"][0]
        self.assertEqual(t["opaque"]["draws"], 2)
        self.assertEqual(t["transparent"]["draws"], 1)
        self.assertEqual(t["top_hidden_draws"][0]["event_id"], 32)
        self.assertEqual(t["opaque_after_transparent_draws"], 1)
        self.assertTrue(any(f["type"] == "opaque_overdraw" and "event 32" in f["detail"] for f in r["findings"]),
                        r["findings"])

    def test_opaque_overdraw_does_not_blame_transparent_draw(self):
        draws = [
            _rt_draw(50, "Cam/Sky", 0, 900000, True, blend_opaque=False),
            _rt_draw(51, "Cam/Opaque", 500000, 400000, False),
        ]
        finding = [f for f in da.overdraw(draws, {}, 3)["findings"] if f["type"] == "opaque_overdraw"][0]
        self.assertIn("event 51", finding["detail"])
        self.assertNotIn("event 50", finding["detail"])


if __name__ == "__main__":
    unittest.main()
