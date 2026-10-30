"""replace_service: edit application, diffs, encoding choice and replacement bookkeeping."""

import types
import unittest

from tests._stubs import load_service

rp = load_service("replace_service")

SRC = "#version 320 es\nvoid main()\n{\n    a = b;\n    c = b;\n}\n"


class ApplyEdits(unittest.TestCase):
    def test_every_occurrence_is_replaced_and_counted(self):
        out, counts = rp.apply_edits(SRC, [{"find": "= b", "replace": "= d"}])
        self.assertEqual(out.count("= d"), 2)
        self.assertEqual(counts, [2])

    def test_edits_apply_in_order_on_the_result_of_the_previous(self):
        out, counts = rp.apply_edits(SRC, [{"find": "a = b", "replace": "a = x"}, {"find": "a = x", "replace": "a = y"}])
        self.assertIn("a = y;", out)
        self.assertEqual(counts, [1, 1])

    def test_text_that_is_not_there_is_an_error_naming_the_edit(self):
        with self.assertRaises(ValueError) as e:
            rp.apply_edits(SRC, [{"find": "a = b", "replace": "a = c"}, {"find": "nope", "replace": "x"}])
        self.assertIn("edit 1", str(e.exception))
        self.assertIn("nope", str(e.exception))

    def test_malformed_edits(self):
        for bad in ([{"find": "a"}], [{"replace": "a"}], ["a"], [{"find": "", "replace": "x"}]):
            with self.assertRaises(ValueError):
                rp.apply_edits(SRC, bad)

    def test_no_edits_is_a_noop(self):
        self.assertEqual(rp.apply_edits(SRC, None), (SRC, []))
        self.assertEqual(rp.apply_edits(SRC, []), (SRC, []))

    def test_multiline_find(self):
        out, _ = rp.apply_edits(SRC, [{"find": "void main()\n{", "replace": "void main()\n{\n    discard;"}])
        self.assertIn("{\n    discard;\n    a = b;", out)


class Summary(unittest.TestCase):
    def test_diff_shows_the_changed_line(self):
        new, _ = rp.apply_edits(SRC, [{"find": "c = b", "replace": "c = 0.0"}])
        s = rp.change_summary(SRC, new)
        self.assertTrue(s["changed"])
        self.assertIn("-    c = b;", s["diff"])
        self.assertIn("+    c = 0.0;", s["diff"])

    def test_unchanged_and_truncation(self):
        self.assertEqual(rp.change_summary(SRC, SRC), {"changed": False, "diff": [], "diff_truncated": False, "diff_lines": 0})
        big = rp.change_summary("a\n" * 200, "b\n" * 200, max_lines=10)
        self.assertEqual((len(big["diff"]), big["diff_truncated"]), (10, True))
        self.assertGreater(big["diff_lines"], 10)


class Encoding(unittest.TestCase):
    def test_auto_follows_the_original(self):
        self.assertEqual(rp.pick_encoding("auto", "spirv"), "spirv")
        self.assertEqual(rp.pick_encoding(None, "glsl"), "glsl")

    def test_explicit_wins_and_junk_is_rejected(self):
        self.assertEqual(rp.pick_encoding("glsl", "spirv"), "glsl")
        self.assertEqual(rp.pick_encoding("SPIRV", "glsl"), "spirv")
        with self.assertRaises(ValueError):
            rp.pick_encoding("hlsl", "glsl")

    def test_hash_is_stable_for_text_and_bytes(self):
        self.assertEqual(rp.short_hash("abc"), rp.short_hash(b"abc"))
        self.assertNotEqual(rp.short_hash("abc"), rp.short_hash("abd"))
        self.assertEqual(len(rp.short_hash("x")), 12)


class Spirv(unittest.TestCase):
    @staticmethod
    def module(*instrs, bound=10):
        """header + instructions; each instr is (opcode, [operand words])"""
        import struct
        words = [0x07230203, 0x00010300, 0, bound, 0]
        for op, operands in instrs:
            words.append(((len(operands) + 1) << 16) | op)
            words.extend(operands)
        return struct.pack("<%dI" % len(words), *words)

    def test_a_well_formed_module_passes(self):
        data = self.module((17, [1]), (14, [0, 1]), (54, [2, 3, 0, 4]))
        self.assertEqual(rp.validate_spirv(data), 5 + 2 + 3 + 5)

    def test_header_only_module_passes(self):
        self.assertEqual(rp.validate_spirv(self.module()), 5)

    def test_rejects_garbage_text_and_short_data(self):
        for bad in (b"this is not spirv at all!!", b"", b"#"):
            with self.assertRaises(ValueError):
                rp.validate_spirv(bad)

    def test_rejects_wrong_magic_and_big_endian(self):
        import struct
        data = bytearray(self.module((17, [1])))
        data[0:4] = struct.pack(">I", 0x07230203)
        with self.assertRaises(ValueError) as e:
            rp.validate_spirv(bytes(data))
        self.assertIn("magic", str(e.exception))

    def test_rejects_a_truncated_module(self):
        data = self.module((17, [1]), (54, [2, 3, 0, 4]), (54, [2, 3, 0, 4]))
        for cut in (4, 8, 12):                       # cut inside the last instruction, still word aligned
            with self.assertRaises(ValueError) as e:
                rp.validate_spirv(data[:-cut])
            self.assertIn("truncated", str(e.exception))
        with self.assertRaises(ValueError):
            rp.validate_spirv(data[:-1])             # not whole words

    def test_rejects_zero_length_instruction_and_bad_bound(self):
        import struct
        zero = bytearray(self.module((17, [1])))
        zero[20:24] = struct.pack("<I", 0)           # first instruction word: length 0
        with self.assertRaises(ValueError):
            rp.validate_spirv(bytes(zero))
        with self.assertRaises(ValueError):
            rp.validate_spirv(self.module((17, [1]), bound=0))

    def test_real_shader_from_the_phone_if_present(self):
        import base64, json, os
        path = os.path.join(os.environ.get("TEMP", ""), "renderdoc_mcp", "vk_spv.json")
        if not os.path.isfile(path):
            self.skipTest("no SPIR-V fixture saved from a device")
        with open(path, encoding="utf-8") as f:
            data = base64.b64decode(json.load(f)["b64"])
        self.assertGreater(rp.validate_spirv(data), 1000)
        with self.assertRaises(ValueError):
            rp.validate_spirv(data[:400])             # what poisoned the replay on the phone


class DecodeSource(unittest.TestCase):
    BLOB = bytes([3, 2, 35, 7]) + b"abcd"

    def test_valid_base64(self):
        import base64
        self.assertEqual(rp.decode_spirv_source(base64.b64encode(self.BLOB).decode()), self.BLOB)

    def test_not_base64_says_what_was_expected(self):
        glsl = "#version 320 es" + chr(10) + "void main(){}"
        for bad in ("this is not spirv at all!!", "abc", glsl):
            with self.assertRaises(ValueError) as e:
                rp.decode_spirv_source(bad)
            self.assertIn("base64", str(e.exception))
            self.assertNotIn("padding", str(e.exception))


class Bookkeeping(unittest.TestCase):
    def _svc(self, name):
        ctx = types.SimpleNamespace(IsCaptureLoaded=lambda: name[0] is not None, GetCaptureFilename=lambda: name[0])
        return rp.ReplaceService(ctx, None)

    def test_replacements_are_forgotten_when_another_capture_is_loaded(self):
        name = ["a.rdc"]
        svc = self._svc(name)
        svc._sync_capture()
        svc._active["ResourceId::1"] = {"x": 1}
        svc._sync_capture()
        self.assertEqual(len(svc._active), 1)
        name[0] = "b.rdc"
        svc._sync_capture()
        self.assertEqual(svc._active, {})

    def test_requires_a_capture(self):
        svc = self._svc([None])
        with self.assertRaises(ValueError):
            svc.replace_shader(1)
        self.assertEqual(svc.revert_shader(), {"reverted": [], "active_replacements": []})

    def test_source_and_edits_together_are_rejected_and_stage_is_checked(self):
        svc = self._svc(["a.rdc"])
        with self.assertRaises(ValueError):
            svc.replace_shader(1, source="x", edits=[{"find": "a", "replace": "b"}])
        with self.assertRaises(ValueError):
            svc.replace_shader(1, stage="mesh")

    def test_listing_is_sorted_and_plain(self):
        svc = self._svc(["a.rdc"])
        svc._active = {"ResourceId::9": {"new": "ResourceId::90", "stage": "pixel", "event_id": 5, "new_hash": "h"},
                       "ResourceId::2": {"new": "ResourceId::20", "stage": "vertex", "event_id": 3, "new_hash": "g"}}
        self.assertEqual([x["shader_id"] for x in svc._listing()], ["ResourceId::2", "ResourceId::9"])


if __name__ == "__main__":
    unittest.main()
