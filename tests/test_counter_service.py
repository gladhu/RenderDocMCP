"""counter_service: counter selection, value decoding and per-counter aggregation."""

import types
import unittest

from tests._stubs import load_service

cs = load_service("counter_service")

AV = {
    1: dict(name="GPU Duration", unit="s", category="Built-in", vendor="generic"),
    7: dict(name="Samples Passed", unit="", category="Built-in", vendor="generic"),
    5000013: dict(name="ShaderCycles", unit="", category="HWC", vendor="arm"),
    5000016: dict(name="Instructions", unit="", category="HWC", vendor="arm"),
    5000015: dict(name="DivergedInstructions", unit="", category="HWC", vendor="arm"),
    5000026: dict(name="Pixels", unit="", category="HWC", vendor="arm"),
    5000029: dict(name="  ", unit="", category="HWC", vendor="arm"),
    5000040: dict(name="Occupancy", unit="%", category="HWC", vendor="arm"),
}


class Vendor(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual([cs.vendor_of(i) for i in (1, 15, 1000001, 2000000, 3999999, 4000000, 5000001, 6000000, 7000000)],
                         ["generic", "generic", "amd", "intel", "nvidia", "vulkan_ext", "arm", "arm", "other"])


class PickValue(unittest.TestCase):
    v = types.SimpleNamespace(d=2.5, f=1.5, u32=7, u64=2 ** 40)

    def test_by_type_and_width(self):
        self.assertEqual(cs.pick_value(self.v, "float", 8), 2.5)
        self.assertEqual(cs.pick_value(self.v, "float", 4), 1.5)
        self.assertEqual(cs.pick_value(self.v, "uint", 8), 2 ** 40)
        self.assertEqual(cs.pick_value(self.v, "uint", 4), 7)


class Resolve(unittest.TestCase):
    def test_short_names_and_ids(self):
        self.assertEqual(cs.resolve_counters(["gpu_duration", "7"], AV), ([1, 7], []))

    def test_exact_name_beats_substring(self):
        self.assertEqual(cs.resolve_counters(["instructions"], AV), ([5000016], []))
        self.assertEqual(sorted(cs.resolve_counters(["instruction"], AV)[0]), [5000015, 5000016])

    def test_groups(self):
        arm = cs.resolve_counters(["@arm"], AV)[0]
        self.assertEqual(arm, sorted(i for i in AV if i >= 5000000))
        self.assertEqual(cs.resolve_counters(["@generic"], AV)[0], [1, 7])
        self.assertEqual(len(cs.resolve_counters(["@all"], AV)[0]), len(AV))

    def test_unknown_and_unavailable_are_reported_and_deduplicated(self):
        ids, unknown = cs.resolve_counters(["pixels", "Pixels", "no_such", "ps_invocations", "5000026"], AV)
        self.assertEqual(ids, [5000026])
        self.assertEqual(unknown, ["no_such", "ps_invocations"])      # a known name the device does not offer

    def test_blank_named_counter_is_reachable_by_id(self):
        self.assertEqual(cs.resolve_counters(["5000029"], AV)[0], [5000029])


class Aggregate(unittest.TestCase):
    rows = [(10, 1, 0.002), (11, 1, 0.005), (12, 1, 0.001), (10, 5000040, 40.0), (11, 5000040, 80.0), (99, 7, 5)]

    def test_totals_ranking_and_scope(self):
        out = cs.aggregate(self.rows, [1, 5000040], AV, top_n=2, names={11: "draw"})
        t = out[0]
        self.assertEqual((t["name"], t["events"], t["max"], t["min"]), ("GPU Duration", 3, 0.005, 0.001))
        self.assertAlmostEqual(t["total"], 0.008, places=6)
        self.assertEqual([e["event_id"] for e in t["top_events"]], [11, 10])
        self.assertEqual(t["top_events"][0]["name"], "draw")

    def test_percent_counters_have_no_total(self):
        pct = cs.aggregate(self.rows, [5000040], AV)[0]
        self.assertNotIn("total", pct)
        self.assertEqual((pct["mean"], pct["max"]), (60.0, 80.0))

    def test_counter_without_values(self):
        e = cs.aggregate([], [1], AV)[0]
        self.assertEqual((e["events"], e["max"], e["mean"], e["top_events"]), (0, None, None, []))
        self.assertNotIn("total", e)


class Runs(unittest.TestCase):
    def test_median_of_odd_and_even(self):
        self.assertEqual(cs.median([3, 1, 2]), 2)
        self.assertEqual(cs.median([4, 1, 2, 3]), 2.5)

    def test_combine_takes_the_median_per_event_and_keeps_run_totals(self):
        runs = [[(1, 7, 10.0), (2, 7, 1.0)], [(1, 7, 30.0), (2, 7, 1.0)], [(1, 7, 12.0), (2, 7, 3.0)]]
        rows, totals = cs.combine_runs(runs)
        self.assertEqual(sorted(rows), [(1, 7, 12.0), (2, 7, 1.0)])
        self.assertEqual(totals, {7: [11.0, 31.0, 15.0]})

    def test_event_missing_in_some_runs_uses_the_runs_that_have_it(self):
        rows, totals = cs.combine_runs([[(1, 7, 4.0)], [(1, 7, 6.0), (2, 7, 9.0)]])
        self.assertEqual(dict(((e, c), v) for e, c, v in rows), {(1, 7): 5.0, (2, 7): 9.0})
        self.assertEqual(totals[7], [4.0, 15.0])

    def test_spread(self):
        self.assertIsNone(cs.spread_pct([5.0]))
        self.assertEqual(cs.spread_pct([8.0, 10.0, 12.0]), 40.0)
        self.assertIsNone(cs.spread_pct([0.0, 0.0]))


class Wiring(unittest.TestCase):
    def test_requires_capture(self):
        ctx = types.SimpleNamespace(IsCaptureLoaded=lambda: False)
        with self.assertRaises(ValueError):
            cs.CounterService(ctx, None).get_gpu_counters()


if __name__ == "__main__":
    unittest.main()
