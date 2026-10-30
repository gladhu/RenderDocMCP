"""diff_textures tool: side resolution and orchestration, with a fake bridge that "saves" PNGs."""

import os
import tempfile
import unittest
from unittest import mock

from mcp_server import imagediff as im
from mcp_server import server

from tests.test_imagediff import solid, with_patch

diff = getattr(server.diff_textures, "fn", server.diff_textures)


class FakeBridge:
    """save_texture writes the image registered for (event_id, resource_id, target) and echoes metadata"""

    def __init__(self, images):
        self.images = images
        self.calls = []

    def call(self, method, params, timeout=None):
        assert method == "save_texture", method
        self.calls.append(dict(params))
        key = (params.get("event_id"), params.get("resource_id"), params.get("target"))
        img = self.images[key]
        rows = [img.rgba_row(y) for y in range(img.height)]
        with open(params["output_path"], "wb") as f:
            f.write(im.encode_png(img.width, img.height, rows))
        return {"resource_id": params.get("resource_id") or "ResourceId::7", "name": "RT",
                "width": img.width, "height": img.height, "format": "R8G8B8A8_UNORM"}


def write_png(path, img):
    with open(path, "wb") as f:
        f.write(im.encode_png(img.width, img.height, [img.rgba_row(y) for y in range(img.height)]))


class DiffTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_tool(self, bridge, **kwargs):
        with mock.patch.object(server, "bridge", bridge):
            return diff(**kwargs)

    def test_two_events_of_the_same_target(self):
        a = solid(16, 16, (10, 10, 10, 255))
        b = with_patch(16, 16, (2, 3, 5, 6), (200, 10, 10, 255), base=(10, 10, 10, 255))
        bridge = FakeBridge({(100, None, "color0"): a, (200, None, "color0"): b})
        heat = os.path.join(self.tmp.name, "heat.png")
        r = self.run_tool(bridge, event_id_a=100, event_id_b=200, output_path=heat)
        self.assertEqual(r["differing_pixels"], 4 * 4)
        self.assertEqual(r["bounding_box"], {"x0": 2, "y0": 3, "x1": 5, "y1": 6})
        self.assertEqual(r["heat_map_path"], heat)
        with open(heat, "rb") as f:
            self.assertEqual(im.decode_png(f.read()).width, 16)
        self.assertNotIn("heatmap_png", r)
        self.assertEqual((r["side_a"]["event_id"], r["side_b"]["event_id"]), (100, 200))
        self.assertEqual([c["target"] for c in bridge.calls], ["color0", "color0"])      # B inherits A's target

    def test_side_b_inherits_target_and_resource_from_a(self):
        img = solid(4, 4, (1, 1, 1, 255))
        bridge = FakeBridge({(1, "ResourceId::5", "depth"): img, (2, "ResourceId::5", "depth"): img})
        r = self.run_tool(bridge, event_id_a=1, resource_id_a="ResourceId::5", target_a="depth", event_id_b=2)
        self.assertTrue(r["identical"])
        self.assertEqual([(c["event_id"], c["resource_id"], c["target"]) for c in bridge.calls],
                         [(1, "ResourceId::5", "depth"), (2, "ResourceId::5", "depth")])

    def test_capture_side_against_a_file(self):
        a = solid(8, 8, (50, 60, 70, 255))
        ref = os.path.join(self.tmp.name, "before.png")
        write_png(ref, solid(8, 8, (50, 60, 75, 255)))
        bridge = FakeBridge({(9, None, "color0"): a})
        r = self.run_tool(bridge, event_id_a=9, path_b=ref)
        self.assertEqual((r["differing_pixels"], r["max_delta"]), (64, 5))
        self.assertEqual(r["side_b"], {"source": "file", "path": ref})
        self.assertEqual(len(bridge.calls), 1)                  # a file side asks the capture for nothing
        self.assertEqual(self.run_tool(FakeBridge({(9, None, "color0"): a}), event_id_a=9, path_b=ref, threshold=5)["differing_pixels"], 0)

    def test_two_files_need_no_capture_at_all(self):
        pa, pb = os.path.join(self.tmp.name, "a.png"), os.path.join(self.tmp.name, "b.png")
        write_png(pa, solid(4, 4, (0, 0, 0, 255)))
        write_png(pb, with_patch(4, 4, (0, 0, 0, 0), (255, 0, 0, 255)))
        bridge = FakeBridge({})
        r = self.run_tool(bridge, path_a=pa, path_b=pb)
        self.assertEqual((r["differing_pixels"], bridge.calls), (1, []))

    def test_size_mismatch_is_reported(self):
        pa, pb = os.path.join(self.tmp.name, "a.png"), os.path.join(self.tmp.name, "b.png")
        write_png(pa, solid(4, 4, (0, 0, 0, 255)))
        write_png(pb, solid(6, 4, (0, 0, 0, 255)))
        r = self.run_tool(FakeBridge({}), path_a=pa, path_b=pb)
        self.assertFalse(r["comparable"])
        self.assertNotIn("heat_map_path", r)

    def test_invalid_requests_raise(self):
        with self.assertRaises(ValueError):
            self.run_tool(FakeBridge({}), event_id_a=1)                       # side B empty
        with self.assertRaises(ValueError):
            self.run_tool(FakeBridge({}), event_id_b=1)                       # side A empty (target alone is no source)
        with self.assertRaises(ValueError):
            self.run_tool(FakeBridge({}), path_a=os.path.join(self.tmp.name, "missing.png"), path_b="x")


if __name__ == "__main__":
    unittest.main()
