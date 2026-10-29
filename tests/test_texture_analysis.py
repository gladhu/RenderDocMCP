"""texture_analysis: sampling footprint per texture and texture findings."""

import unittest

from tests import _stubs  # noqa: F401  (puts the repo root on sys.path)
from mcp_server import texture_analysis as ta

MB = 1048576


def _tex(rid, name, w, h, mips, fmt, size, rt=False):
    return dict(resource_id=rid, name=name, width=w, height=h, depth=1, array_size=1, mips=mips, format=fmt,
                size_bytes=size, is_render_target=rt)


def _draw(eid, pixels, ids, marker="Cam/Opaque"):
    return dict(event_id=eid, passed=pixels, sampled_textures=ids, marker=marker)


class TextureFootprintTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        textures = [
            _tex("T1", "Big_D", 2048, 2048, 1, "R8G8B8A8_SRGB", 2048 * 2048 * 4),   # uncompressed 16 MB
            _tex("T2", "Hero_D", 2048, 2048, 1, "ASTC_UNORM", 1864000),             # ASTC 6x6, ~3.56 bpp
            _tex("T3", "CamColor", 1600, 720, 1, "R11G11B10_FLOAT", 1600 * 720 * 4, rt=True),
            _tex("T4", "Shadow", 2048, 2048, 1, "D16", 2048 * 2048 * 2),
            _tex("T5", "Char_D", 1024, 1024, 1, "ASTC_UNORM", 467856),
        ]
        draws = [_draw(1, 100000, ["T1", "T2"]), _draw(2, 5000000, ["T1"]), _draw(3, 1152000, ["T3"], "Cam/Post"),
                 _draw(4, 500000, ["T4", "T5"]), _draw(5, 10, ["T5", "NOPE"])]
        cls.r = ta.texture_footprint(textures, draws, 10)
        cls.by_id = dict((x["resource_id"], x) for x in cls.r["top_textures"])

    def test_unresolved_and_sampled_counts(self):
        self.assertEqual(self.r["totals"]["unresolved_textures"], 1)
        self.assertEqual(self.r["textures_sampled"], 5)

    def test_footprint_capped_at_texture_size(self):
        self.assertEqual(self.by_id["T1"]["footprint_mb"], 16.0)

    def test_small_footprint_for_rarely_sampled_texture(self):
        t2 = self.by_id["T2"]
        self.assertLess(t2["footprint_mb"], t2["size_mb"] * 0.25)
        self.assertAlmostEqual(t2["bits_per_pixel"], 3.56, delta=0.01)

    def test_finding_types(self):
        self.assertEqual(sorted(f["type"] for f in self.r["findings"]),
                         ["heavy_render_target_read", "low_utilization", "uncompressed_texture"])

    def test_uncompressed_finding(self):
        unc = [f for f in self.r["findings"] if f["type"] == "uncompressed_texture"][0]
        self.assertEqual(unc["name"], "Big_D")
        self.assertTrue(13 < unc["mb"] < 15, unc["mb"])
        self.assertEqual(unc["severity"], "high")

    def test_single_mip_textures_listed(self):
        self.assertEqual([x["name"] for x in self.r["single_mip_textures"]], ["Big_D", "Char_D", "Hero_D"])

    def test_internal_fields_not_exposed(self):
        self.assertTrue(all("_size" not in x for x in self.r["top_textures"]))


class TexelBytesTest(unittest.TestCase):
    def test_mipped_texture_uses_real_size(self):
        m = _tex("M", "Mipped", 1024, 1024, 11, "R8G8B8A8_UNORM", int(1024 * 1024 * 4 * 1.3333))
        self.assertAlmostEqual(ta._texel_bytes(m), 4.0, delta=0.01)


class AtlasTest(unittest.TestCase):
    def test_atlases_not_flagged_for_low_utilization(self):
        textures = [_tex("A1", "NormalFont Atlas", 2048, 2048, 1, "R8_UNORM", 4 * MB),
                    _tex("A2", "sactx-0-2048x1024-ASTC 6x6-X", 2048, 1024, 1, "ASTC_UNORM", 935712)]
        r = ta.texture_footprint(textures, [_draw(1, 1000, ["A1", "A2"])], 5)
        self.assertFalse([f for f in r["findings"] if f["type"] == "low_utilization"], r["findings"])


if __name__ == "__main__":
    unittest.main()
