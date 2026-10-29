"""gl_state: per-draw GL state tracked from the call stream, and state-change summaries."""

import unittest

from tests._stubs import load_service

g = load_service("gl_state")

GL_DEPTH_TEST = 2929
GL_BLEND = 3042
GL_CULL_FACE = 2884
GL_BACK = 1028
GL_LEQUAL = 515
GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA = 770, 771
GL_ONE, GL_ZERO = 1, 0


class _Calls:
    def __init__(self):
        self.eid = 0

    def call(self, name, *args):
        self.eid += 1
        return {"eid": self.eid, "name": name, "args": list(args), "action": None, "path": ["P"], "marker": "P"}

    def draw(self, out="rt1"):
        self.eid += 1
        return {"eid": self.eid, "name": "glDrawElements", "args": [], "path": ["P"], "marker": "P",
                "action": {"flags": 2, "outputs": [out], "depth": "d"}}


class TrackDrawsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        c = _Calls()
        stream = [
            c.call("glUseProgram", "ResourceId::1"), c.call("glEnable", GL_DEPTH_TEST), c.draw(), c.draw(),
            c.call("glUseProgram", "ResourceId::2"), c.draw(),                                      # shader change
            c.call("glEnable", GL_BLEND), c.call("glBlendFuncSeparate", 770, 771, 1, 771), c.draw(),  # blend change
            c.call("glEnable", GL_CULL_FACE), c.call("glCullFace", GL_BACK), c.draw(),               # cull change
            c.call("glDepthFunc", GL_LEQUAL), c.draw(), c.draw("rt2"),                               # depth + RT change
            c.call("glEnablei", GL_BLEND, 1), c.draw(),                                              # other RT: ignored
        ]
        cls.draws = g.track_draws(stream)

    def test_one_entry_per_draw(self):
        self.assertEqual(len(self.draws), 8)

    def test_change_counts_per_category(self):
        r = g.summarize_state_changes(self.draws, ["shader", "blend", "depth", "cull", "render_target"])
        self.assertEqual(r["change_counts"], {"shader": 1, "blend": 1, "depth": 1, "cull": 1, "render_target": 2})

    def test_first_run_of_identical_state_has_two_draws(self):
        r = g.summarize_state_changes(self.draws, ["shader", "blend", "depth", "cull", "render_target"])
        self.assertEqual(r["runs"][0]["count"], 2)

    def test_blend_and_cull_state_tracked(self):
        self.assertTrue(self.draws[4]["blend_on"])
        self.assertFalse(self.draws[0]["blend_on"])
        self.assertEqual(self.draws[5]["cull"], GL_BACK)
        self.assertEqual(self.draws[0]["cull"], "None")

    def test_marker_filter_without_match_yields_no_draws(self):
        self.assertEqual(g.summarize_state_changes(self.draws, ["shader"], marker_filter="nope")["total_draws"], 0)

    def test_max_draws_limits_analysis(self):
        self.assertEqual(g.summarize_state_changes(self.draws, ["shader"], max_draws=3)["analyzed_draws"], 3)


class DrawStateTest(unittest.TestCase):
    def test_alpha_blend_is_not_opaque_and_depth_write_off(self):
        c = _Calls()
        draws = g.track_draws([c.call("glDepthMask", False), c.call("glEnable", GL_BLEND),
                               c.call("glBlendFunc", GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA), c.draw(),
                               c.call("glBlendFunc", GL_ONE, GL_ZERO), c.draw()])
        a = g.draw_state(draws[0])
        self.assertTrue(a["blend_enabled"])
        self.assertIs(a["blend_opaque"], False)
        self.assertIs(a["depth_write"], False)
        self.assertEqual(a["blend_color"], "770/771")
        self.assertIs(g.draw_state(draws[1])["blend_opaque"], True)   # ONE/ZERO blending is opaque

    def test_blend_disabled_has_unknown_opacity(self):
        self.assertIsNone(g.draw_state(g.track_draws([_Calls().draw()])[0])["blend_opaque"])


if __name__ == "__main__":
    unittest.main()
