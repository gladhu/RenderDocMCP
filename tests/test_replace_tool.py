"""replace_shader MCP tool: reading the replacement file (GLSL / raw SPIR-V / base64 SPIR-V) and what is sent."""

import base64
import os
import struct
import tempfile
import unittest
from unittest import mock

from mcp_server import server

replace = getattr(server.replace_shader, "fn", server.replace_shader)

SPV = struct.pack("<7I", 0x07230203, 0x10300, 0, 8, 0, (2 << 16) | 17, 1)


class RecordingBridge:
    def __init__(self):
        self.calls = []

    def call(self, method, params, timeout=None):
        self.calls.append((method, dict(params)))
        return {"compiled": True}


class ReadReplacement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_raw_spirv_becomes_base64(self):
        got = server._read_replacement(self.write("a.spv", SPV), "auto")
        self.assertEqual(base64.b64decode(got), SPV)

    def test_base64_text_even_wrapped_in_lines(self):
        b64 = base64.b64encode(SPV).decode()
        wrapped = "\n".join(b64[i:i + 8] for i in range(0, len(b64), 8)) + "\n"
        self.assertEqual(server._read_replacement(self.write("a.b64", wrapped.encode()), "auto"), b64)

    def test_glsl_text_is_passed_through(self):
        text = "#version 320 es\nvoid main() {}\n"
        self.assertEqual(server._read_replacement(self.write("a.frag", text.encode()), "auto"), text.strip())
        self.assertEqual(server._read_replacement(self.write("b.frag", text.encode()), "glsl"), text)

    def test_missing_file(self):
        with self.assertRaises(ValueError):
            server._read_replacement(os.path.join(self.tmp.name, "nope"), "auto")


class ToolCall(unittest.TestCase):
    def test_source_path_is_read_and_sent_as_source(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.spv")
            with open(path, "wb") as f:
                f.write(SPV)
            bridge = RecordingBridge()
            with mock.patch.object(server, "bridge", bridge):
                replace(event_id=5, source_path=path)
        method, params = bridge.calls[0]
        self.assertEqual(method, "replace_shader")
        self.assertEqual(base64.b64decode(params["source"]), SPV)
        self.assertNotIn("source_path", params)

    def test_both_sources_is_an_error_and_edits_are_forwarded(self):
        bridge = RecordingBridge()
        with mock.patch.object(server, "bridge", bridge):
            with self.assertRaises(ValueError):
                replace(event_id=5, source="a", source_path="b")
            replace(event_id=5, edits=[{"find": "a", "replace": "b"}], dry_run=True)
        self.assertEqual(bridge.calls[0][1]["edits"], [{"find": "a", "replace": "b"}])
        self.assertTrue(bridge.calls[0][1]["dry_run"])


if __name__ == "__main__":
    unittest.main()
