"""overlay_service / targets: colour counting, overlay statistics and target resolution (pure parts)."""

import math
import struct
import unittest

from tests._stubs import load_service

ov = load_service("overlay_service")
tg = load_service("targets")

G, R = (0.0, 1.0, 0.0, 1.0), (1.0, 0.0, 0.0, 1.0)


def _rgba8(pixels):
    return b"".join(bytes(int(round(c * 255)) for c in p) for p in pixels)


def _rgba16f(pixels):
    return b"".join(struct.pack("<4e", *p) for p in pixels)


def _rgba32f(pixels):
    return b"".join(struct.pack("<4f", *p) for p in pixels)


class ColorCounts(unittest.TestCase):
    def test_rgba8(self):
        counts = ov.color_counts(_rgba8([G, G, G, R]), 4)
        self.assertEqual([c for _, c in counts], [3, 1])
        self.assertEqual(counts[0][0], G)

    def test_rgba16f_decodes_half_floats_and_inf(self):
        px = [(2.0, 2.0, 2.0, 2.0)] * 2 + [(math.inf, math.inf, math.inf, 1.0)]
        counts = ov.color_counts(_rgba16f(px), 3)
        self.assertEqual(counts[0], ((2.0, 2.0, 2.0, 2.0), 2))
        self.assertTrue(math.isinf(counts[1][0][0]))

    def test_rgba32f(self):
        counts = ov.color_counts(_rgba32f([(0.25, 0.5, 0.75, 1.0)] * 3), 3)
        self.assertEqual(counts, [((0.25, 0.5, 0.75, 1.0), 3)])

    def test_bad_sizes_raise(self):
        with self.assertRaises(ValueError):
            ov.color_counts(b"\0" * 10, 4)
        with self.assertRaises(ValueError):
            ov.color_counts(b"\0" * 12, 4)          # 3 bytes per pixel
        with self.assertRaises(ValueError):
            ov.color_counts(b"", 0)


class Legend(unittest.TestCase):
    def test_depth_pass_fail_and_untouched(self):
        untouched = (0.0, 0.0, 0.0, 0.0)
        counts = ov.color_counts(_rgba16f([G] * 3 + [R] * 5 + [untouched] * 2), 10)
        s = ov.summarise("legend", "depth", counts)
        self.assertEqual(s["by_label"]["passed"]["pixels"], 3)
        self.assertEqual(s["by_label"]["failed"]["pixels"], 5)
        self.assertEqual(s["by_label"]["untouched_or_other"]["pixels"], 2)
        self.assertEqual(s["by_label"]["failed"]["pct_of_target"], 50.0)

    def test_transparent_pixels_never_match_even_with_the_legend_rgb(self):
        wire_rgb = (0.784, 1.0, 0.0)
        counts = ov.color_counts(_rgba16f([wire_rgb + (1.0,)] * 2 + [wire_rgb + (0.0,)] * 8), 10)
        s = ov.summarise("legend", "wireframe", counts)
        self.assertEqual(s["by_label"]["wire"]["pixels"], 2)
        self.assertEqual(s["by_label"]["untouched_or_other"]["pixels"], 8)

    def test_half_float_rounding_is_within_tolerance(self):
        counts = ov.color_counts(_rgba16f([(0.8, 0.1, 0.8, 1.0)] * 4), 4)
        self.assertEqual(ov.summarise("legend", "drawcall", counts)["by_label"]["draw"]["pixels"], 4)


class Marked(unittest.TestCase):
    def test_counts_only_marked_pixels(self):
        counts = ov.color_counts(_rgba16f([(1.0, 0.0, 0.0, 1.0)] * 2 + [(0.0, 0.0, 0.0, 0.0)] * 6), 8)
        s = ov.summarise("marked", "nan", counts)
        self.assertEqual((s["marked_pixels"], s["marked_pct"]), (2, 25.0))
        self.assertEqual(len(s["top_colors"]), 1)

    def test_clean_target(self):
        counts = ov.color_counts(_rgba16f([(0.0, 0.0, 0.0, 0.0)] * 4), 4)
        s = ov.summarise("marked", "nan", counts)
        self.assertEqual((s["marked_pixels"], s["top_colors"]), (0, []))


class Overdraw(unittest.TestCase):
    def test_mean_max_and_share(self):
        v = lambda n: (float(n),) * 4
        counts = ov.color_counts(_rgba16f([v(0)] * 4 + [v(1)] * 4 + [v(3)] * 2), 10)
        s = ov.summarise("overdraw", "quad_overdraw_draw", counts)
        self.assertEqual(s["covered_pixels"], 6)
        self.assertEqual(s["shaded_quad_pixels"], 4 + 6)
        self.assertAlmostEqual(s["mean_overdraw"], 10 / 6.0, places=3)
        self.assertEqual(s["max_overdraw"], 3)
        self.assertAlmostEqual(s["pixels_overdrawn_gt1_pct"], 100.0 * 2 / 6, places=2)
        self.assertEqual([h["overdraw"] for h in s["histogram"]], [1, 3])

    def test_nothing_covered(self):
        counts = ov.color_counts(_rgba16f([(0.0,) * 4] * 4), 4)
        s = ov.summarise("overdraw", "quad_overdraw_draw", counts)
        self.assertEqual((s["covered_pixels"], s["mean_overdraw"], s["max_overdraw"]), (0, 0.0, 0))


class TriangleSize(unittest.TestCase):
    def test_buckets_and_inf(self):
        a = lambda x: (x, x, x, 1.0)
        counts = ov.color_counts(_rgba16f([a(2.0)] * 2 + [a(10.0)] * 3 + [a(500.0)] + [a(math.inf)] * 4 + [(0.0,) * 4] * 5), 15)
        s = ov.summarise("tri_size", "triangle_size_draw", counts)["pixels_by_triangle_area"]
        self.assertEqual(s["<4 px"]["pixels"], 2)
        self.assertEqual(s["4-16 px"]["pixels"], 3)
        self.assertEqual(s["256-1024 px"]["pixels"], 1)
        self.assertEqual(s[">=65504 px"]["pixels"], 4)
        self.assertEqual(sum(b["pixels"] for b in s.values()), 10)

    def test_result_is_json_safe(self):
        import json
        counts = ov.color_counts(_rgba16f([(math.inf, math.inf, math.inf, 1.0)] * 4), 4)
        for kind in ("tri_size", "marked"):
            json.dumps(ov.summarise(kind, "x", counts), allow_nan=False)
        json.dumps(ov.summarise_legend(counts, {"a": (0, 1, 0)}), allow_nan=False)


class Warnings(unittest.TestCase):
    def _stats(self, covered):
        return {"covered_pixels": covered, "mean_overdraw": 0.0}

    def test_gl_overdraw_without_pixels_is_flagged_unsupported(self):
        s = ov.add_warnings("overdraw", self._stats(0), "OpenGL")
        self.assertIs(s["supported"], False)
        self.assertIn("analyze_overdraw", s["warning"])

    def test_vulkan_zero_overdraw_is_a_real_result(self):
        self.assertNotIn("warning", ov.add_warnings("overdraw", self._stats(0), "Vulkan"))

    def test_gl_overdraw_with_pixels_and_other_kinds_are_untouched(self):
        self.assertNotIn("warning", ov.add_warnings("overdraw", self._stats(10), "OpenGL"))
        self.assertNotIn("warning", ov.add_warnings("tri_size", {"covered_pixels": 0}, "OpenGL"))


class Catalogue(unittest.TestCase):
    def test_every_legend_overlay_has_a_legend(self):
        for name, (_, kind, _) in ov.OVERLAYS.items():
            if kind == "legend":
                self.assertIn(name, ov.LEGENDS)

    def test_overlays_without_data_are_not_offered(self):
        self.assertNotIn("clear_before_pass", ov.OVERLAYS)
        self.assertNotIn("clear_before_draw", ov.OVERLAYS)


class Targets(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(tg.parse_target("color0"), ("color", 0))
        self.assertEqual(tg.parse_target("COLOR3"), ("color", 3))
        self.assertEqual(tg.parse_target("depth"), ("depth", 0))
        self.assertEqual(tg.parse_target(None), ("color", 0))
        with self.assertRaises(ValueError):
            tg.parse_target("stencil")

    def test_action_outputs_win_over_the_render_pass_model(self):
        fb = (["c9"], "d9")
        self.assertEqual(tg.pick_target_id("color1", ["a", "b"], "d", fb), "b")
        self.assertEqual(tg.pick_target_id("depth", ["a"], "d", fb), "d")

    def test_vulkan_style_fallback(self):
        fb = (["c1", "c2"], "d1")
        self.assertEqual(tg.pick_target_id("color1", [], None, fb), "c2")
        self.assertEqual(tg.pick_target_id("depth", [], None, fb), "d1")

    def test_missing_targets(self):
        self.assertIsNone(tg.pick_target_id("color2", ["a"], None, None))
        self.assertIsNone(tg.pick_target_id("color2", [], None, (["c1"], None)))
        self.assertIsNone(tg.pick_target_id("depth", [], None, (["c1"], None)))
        self.assertIsNone(tg.pick_target_id("color0", [], None, None))


if __name__ == "__main__":
    unittest.main()
