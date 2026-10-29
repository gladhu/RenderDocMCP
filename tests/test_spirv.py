"""diagnosis_service pure helpers: SPIR-V / GLSL discard detection and the state-change report."""

import struct
import unittest

from tests._stubs import load_service

diag = load_service("diagnosis_service")

SPIRV_HEADER = struct.pack("<5I", 0x07230203, 0x10000, 0, 10, 0)


def _op(opcode, *operands):
    return struct.pack("<%dI" % (1 + len(operands)), ((1 + len(operands)) << 16) | opcode, *operands)


OP_CAPABILITY, OP_KILL, OP_UNREACHABLE = 17, 252, 253
OP_TERMINATE_INVOCATION, OP_DEMOTE_TO_HELPER = 4416, 5380


class SpirvDiscardTest(unittest.TestCase):
    def test_kill_after_other_instruction(self):
        self.assertTrue(diag.spirv_has_discard(SPIRV_HEADER + _op(50, 1) + _op(OP_KILL)))

    def test_terminate_and_demote_count_as_discard(self):
        self.assertTrue(diag.spirv_has_discard(SPIRV_HEADER + _op(OP_TERMINATE_INVOCATION)))
        self.assertTrue(diag.spirv_has_discard(SPIRV_HEADER + _op(OP_CAPABILITY, 1) + _op(OP_DEMOTE_TO_HELPER)))

    def test_no_kill_instruction(self):
        self.assertFalse(diag.spirv_has_discard(SPIRV_HEADER + _op(OP_UNREACHABLE)))

    def test_kill_opcode_as_operand_is_ignored(self):
        self.assertFalse(diag.spirv_has_discard(SPIRV_HEADER + _op(OP_CAPABILITY, OP_KILL)))

    def test_invalid_input(self):
        self.assertFalse(diag.spirv_has_discard(b""))
        self.assertFalse(diag.spirv_has_discard(SPIRV_HEADER))
        self.assertFalse(diag.spirv_has_discard(b"\0" * 20 + _op(OP_KILL)))        # wrong magic
        self.assertFalse(diag.spirv_has_discard(SPIRV_HEADER + struct.pack("<I", 0)))  # zero word count


class GlslDiscardTest(unittest.TestCase):
    def test_discard_statement(self):
        self.assertTrue(diag._glsl_has_discard("void main() { if (a < 0.5) discard; }"))

    def test_comments_and_identifiers_ignored(self):
        src = "// discard here\n/* discard\n there */ float discarded = 1.0; int no_discard;"
        self.assertFalse(diag._glsl_has_discard(src))


class StateReportTest(unittest.TestCase):
    def setUp(self):
        self.runs = [{"start_event": 10 * i, "end_event": 10 * i + 5, "count": 8 - i} for i in range(7)]
        self.report = diag._state_report(30, 40, {"shader": 20, "render_target": 6}, [], self.runs, "stream")

    def test_listed_runs_capped_at_five(self):
        self.assertEqual(len(self.report["batching_opportunities"]), 5)

    def test_totals_cover_every_run(self):
        self.assertEqual(self.report["batching_run_count"], 7)
        self.assertEqual(self.report["batchable_draws"], sum(8 - i for i in range(7)))

    def test_suggestions(self):
        self.assertEqual(len(self.report["suggestions"]), 3)
        self.assertIn(" 8 ", self.report["suggestions"][2])   # the longest run (events 0-5, 8 draws) is named

    def test_no_runs(self):
        r = diag._state_report(3, 3, {}, [], [], "replay")
        self.assertEqual((r["batching_run_count"], r["batchable_draws"], r["suggestions"]), (0, 0, []))


if __name__ == "__main__":
    unittest.main()
