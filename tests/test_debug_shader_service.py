"""debug_shader_service: value decoding (incl. the uninitialised fill), NaN/Inf detection and trace summaries."""

import math
import struct
import types
import unittest

from tests._stubs import load_service

ds = load_service("debug_shader_service")

UNINIT = 0xCCCCCCCC


def value(floats=(), ints=(), uints=None):
    """A ShaderValue stand-in: f32v / u32v / s32v views of the same 32-bit lanes"""
    lanes = list(floats) if floats else list(ints)
    if floats:
        u = [struct.unpack("<I", struct.pack("<f", f))[0] if f == f and not isinstance(f, int) else UNINIT for f in lanes]
    else:
        u = [i & 0xFFFFFFFF for i in lanes]
    if uints is not None:
        u = list(uints)
    f = [struct.unpack("<f", struct.pack("<I", w))[0] for w in u]
    s = [struct.unpack("<i", struct.pack("<I", w))[0] for w in u]
    return types.SimpleNamespace(f32v=tuple(f), u32v=tuple(u), s32v=tuple(s), f64v=(), s64v=(), u64v=())


def var(name, v, rows=1, cols=1):
    return types.SimpleNamespace(name=name, value=v, rows=rows, columns=cols)


class Values(unittest.TestCase):
    def test_floats(self):
        self.assertEqual(ds.component_values(value(floats=[0.5, 2.0, -1.0]), "Float", 3), [0.5, 2.0, -1.0])

    def test_uninitialised_components_are_marked_not_reported_as_numbers(self):
        v = value(uints=[UNINIT, UNINIT, UNINIT, struct.unpack("<I", struct.pack("<f", 1.0))[0]])
        self.assertEqual(ds.component_values(v, "Float", 4), ["uninit", "uninit", "uninit", 1.0])

    def test_ints_and_bools(self):
        v = value(ints=[-3, 7])
        self.assertEqual(ds.component_values(v, "SInt", 2), [-3, 7])
        self.assertEqual(ds.component_values(value(uints=[1, 0]), "Bool", 2), [True, False])
        self.assertEqual(ds.component_values(value(uints=[UNINIT]), "UInt", 1), ["uninit"])

    def test_nan_and_inf_become_strings(self):
        v = value(uints=[0x7FC00000, 0x7F800000, 0xFF800000])
        self.assertEqual(ds.component_values(v, "Float", 3), ["nan", "inf", "-inf"])

    def test_count_limits_components(self):
        self.assertEqual(len(ds.component_values(value(floats=[1.0, 2.0, 3.0, 4.0]), "Float", 2)), 2)


class Describe(unittest.TestCase):
    def test_scalar_vector_matrix_shapes(self):
        self.assertEqual(ds.describe_variable(var("a", value(floats=[2.5])), "Float")["value"], 2.5)
        self.assertEqual(ds.describe_variable(var("v", value(floats=[1.0, 2.0, 3.0]), cols=3), "Float")["value"], [1.0, 2.0, 3.0])
        m = ds.describe_variable(var("m", value(floats=[1.0, 2.0, 3.0, 4.0]), rows=2, cols=2), "Float")
        self.assertEqual((m["rows"], m["columns"], m["value"]), (2, 2, [[1.0, 2.0], [3.0, 4.0]]))

    def test_uninitialised_detection(self):
        self.assertTrue(ds.is_uninitialised("uninit"))
        self.assertTrue(ds.is_uninitialised(["uninit", "uninit"]))
        self.assertFalse(ds.is_uninitialised(["uninit", 1.0]))
        self.assertFalse(ds.is_uninitialised(0.0))
        self.assertFalse(ds.is_uninitialised([]))

    def test_bad_number_detection_ignores_uninit(self):
        self.assertTrue(ds.contains_bad_number(["nan", 1.0]))
        self.assertTrue(ds.contains_bad_number([[0.0, "-inf"]]))
        self.assertFalse(ds.contains_bad_number(["uninit", 1.0]))
        self.assertFalse(ds.contains_bad_number(3.0))


class Flags(unittest.TestCase):
    def test_names(self):
        self.assertEqual(ds.event_flag_names(0), [])
        self.assertEqual(ds.event_flag_names(2), ["generated_nan_or_inf"])
        self.assertEqual(ds.event_flag_names(1 | 4), ["sample_load_gather", "debug_break"])


def step(i, line, changes=(), flags=()):
    return {"step": i, "instruction": i, "line": line, "flags": list(flags),
            "changes": [{"name": n, "value": v} for n, v in changes]}


class Summary(unittest.TestCase):
    def test_first_nan_is_found_by_value_and_final_values_are_the_last_writes(self):
        steps = [step(0, 1, [("a", 1.0)]), step(1, 2, [("b", [0.0, "nan"])]), step(2, 2, [("a", 5.0)]), step(3, 3, [("c", "inf")])]
        s = ds.summarise_trace(steps, max_steps=2)
        self.assertEqual(s["first_nan_or_inf"], {"step": 1, "line": 2, "variable": "b", "value": [0.0, "nan"]})
        self.assertEqual((s["total_steps"], s["steps_shown"], s["steps_truncated"]), (4, 2, True))
        finals = dict((f["name"], f) for f in s["final_values"])
        self.assertEqual((finals["a"]["value"], finals["a"]["step"]), (5.0, 2))
        self.assertEqual(s["hot_lines"][0], {"line": 2, "steps": 2})

    def test_first_nan_by_flag_when_no_value_shows_it(self):
        s = ds.summarise_trace([step(0, None), step(1, None, flags=["generated_nan_or_inf"])])
        self.assertEqual(s["first_nan_or_inf"]["step"], 1)
        self.assertIsNone(s["first_nan_or_inf"]["variable"])

    def test_nan_that_was_absorbed_is_not_at_the_end(self):
        """a NaN in the middle that a max/clamp later replaces: first_nan_or_inf finds it, nan_at_end is empty"""
        steps = [step(0, None, [("t", "nan")]), step(1, None, [("t", 0.0078)]), step(2, None, [("out", [0.1, 0.2, 0.3, 1.0])])]
        s = ds.summarise_trace(steps)
        self.assertEqual(s["first_nan_or_inf"]["variable"], "t")
        self.assertEqual(s["nan_at_end"], [])

    def test_nan_that_reached_the_output_is_at_the_end(self):
        steps = [step(0, None, [("out", [0.1, "nan", 0.3, 1.0])]), step(1, None, [("t", 2.0)])]
        self.assertEqual(ds.summarise_trace(steps)["nan_at_end"], ["out"])

    def test_clean_trace(self):
        s = ds.summarise_trace([step(0, None, [("a", 1.0)]), step(1, None, [("a", 2.0)])])
        self.assertIsNone(s["first_nan_or_inf"])
        self.assertEqual(s["hot_lines"], [])
        self.assertFalse(s["steps_truncated"])

    def test_empty(self):
        s = ds.summarise_trace([])
        self.assertEqual((s["total_steps"], s["steps"], s["final_values"]), (0, [], []))


class Requests(unittest.TestCase):
    def _svc(self, loaded=True):
        ctx = types.SimpleNamespace(IsCaptureLoaded=lambda: loaded)
        return ds.DebugShaderService(ctx, lambda cb: None)

    def test_validation(self):
        with self.assertRaises(ValueError):
            self._svc(False).debug_shader(1)
        with self.assertRaises(ValueError):
            self._svc().debug_shader(1, kind="geometry")
        with self.assertRaises(ValueError):
            self._svc().debug_shader(1, kind="pixel", x=3)
        with self.assertRaises(ValueError):
            self._svc().debug_shader(1, kind="vertex")


if __name__ == "__main__":
    unittest.main()
