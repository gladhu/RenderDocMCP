"""imagediff: PNG decode for every row filter, image diff statistics and the heat map."""

import random
import struct
import unittest
import zlib

from mcp_server import imagediff as im


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)


def _filter_row(ftype, row, prior, bpp):
    out = bytearray()
    for i, v in enumerate(row):
        a = row[i - bpp] if i >= bpp else 0
        b = prior[i]
        c = prior[i - bpp] if i >= bpp else 0
        pred = (0, a, b, (a + b) >> 1, _paeth(a, b, c))[ftype]
        out.append((v - pred) & 255)
    return bytes(out)


def make_png(width, height, rows, channels, filters):
    """PNG bytes with the given per-row filter types (independent of imagediff's own encoder)"""
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    body, prior = b"", bytes(width * channels)
    for y, row in enumerate(rows):
        ft = filters[y % len(filters)]
        body += bytes([ft]) + _filter_row(ft, row, prior, channels)
        prior = row

    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    return (im.PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, ctype, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(body)) + chunk(b"IEND", b""))


def solid(w, h, rgba):
    return im.Image(w, h, 4, [bytes(rgba) * w for _ in range(h)])


def with_patch(w, h, box, rgba, base=(0, 0, 0, 255)):
    rows = [bytearray(bytes(base) * w) for _ in range(h)]
    x0, y0, x1, y1 = box
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            rows[y][x * 4:x * 4 + 4] = bytes(rgba)
    return im.Image(w, h, 4, [bytes(r) for r in rows])


class Decode(unittest.TestCase):
    def test_every_filter_round_trips_for_every_channel_count(self):
        rnd = random.Random(7)
        for channels in (1, 2, 3, 4):
            w, h = 13, 11
            rows = [bytes(rnd.randrange(256) for _ in range(w * channels)) for _ in range(h)]
            for ft in range(5):
                img = im.decode_png(make_png(w, h, rows, channels, [ft]))
                self.assertEqual((img.width, img.height, img.channels), (w, h, channels))
                self.assertEqual(img.rows, rows, "filter %d, %d channels" % (ft, channels))

    def test_mixed_filters_per_row(self):
        rnd = random.Random(3)
        rows = [bytes(rnd.randrange(256) for _ in range(40)) for _ in range(10)]
        self.assertEqual(im.decode_png(make_png(10, 10, rows, 4, [0, 1, 2, 3, 4])).rows, rows)

    def test_byte_add_wraps_per_byte_without_carry(self):
        a, b = bytes([255, 1, 128, 0, 200]), bytes([1, 255, 128, 0, 100])
        self.assertEqual(im._byte_add(a, b), bytes([0, 0, 0, 0, 44]))

    def test_expansion_to_rgba(self):
        gray = im.Image(2, 1, 1, [bytes([10, 20])])
        self.assertEqual(gray.rgba_row(0), bytes([10, 10, 10, 255, 20, 20, 20, 255]))
        rgb = im.Image(2, 1, 3, [bytes([1, 2, 3, 4, 5, 6])])
        self.assertEqual(rgb.rgba_row(0), bytes([1, 2, 3, 255, 4, 5, 6, 255]))
        ga = im.Image(1, 1, 2, [bytes([7, 99])])
        self.assertEqual(ga.rgba_row(0), bytes([7, 7, 7, 99]))

    def test_rejects_what_it_cannot_read(self):
        with self.assertRaises(im.ImageError):
            im.decode_png(b"not a png")
        png16 = bytearray(make_png(2, 2, [bytes(8)] * 2, 4, [0]))
        png16[24] = 16                                           # bit depth byte of IHDR
        with self.assertRaises(im.ImageError):
            im.decode_png(bytes(png16))

    def test_own_encoder_is_readable(self):
        rows = [bytes([i, i + 1, i + 2, 255] * 3) for i in range(4)]
        img = im.decode_png(im.encode_png(3, 4, rows))
        self.assertEqual(img.rows, rows)


class Diff(unittest.TestCase):
    def test_identical(self):
        r = im.diff_images(solid(4, 4, (9, 9, 9, 255)), solid(4, 4, (9, 9, 9, 255)))
        self.assertTrue(r["identical"])
        self.assertEqual((r["differing_pixels"], r["max_delta"], r["rmse"], r["bounding_box"]), (0, 0, 0.0, None))
        self.assertIsNone(r["psnr_db"])

    def test_size_mismatch_is_reported_not_raised(self):
        r = im.diff_images(solid(4, 4, (0, 0, 0, 255)), solid(5, 4, (0, 0, 0, 255)))
        self.assertFalse(r["comparable"])
        self.assertIn("4x4 vs 5x4", r["reason"])

    def test_counts_bounding_box_and_deltas(self):
        a = solid(16, 16, (0, 0, 0, 255))
        b = with_patch(16, 16, (4, 6, 7, 9), (50, 0, 10, 255))
        r = im.diff_images(a, b)
        self.assertEqual(r["differing_pixels"], 4 * 4)
        self.assertEqual(r["bounding_box"], {"x0": 4, "y0": 6, "x1": 7, "y1": 9})
        self.assertEqual((r["max_delta"], r["max_delta_by_channel"]), (50, {"r": 50, "g": 0, "b": 10}))
        self.assertAlmostEqual(r["mean_delta_over_differing"], (50 + 0 + 10) / 3.0, places=3)
        self.assertEqual(r["differing_pct"], round(100.0 * 16 / 256, 4))
        self.assertAlmostEqual(r["rmse"], ((16 * (50 ** 2 + 10 ** 2)) / (256 * 3.0)) ** 0.5, places=3)

    def test_threshold_ignores_small_differences(self):
        a = solid(8, 8, (100, 100, 100, 255))
        b = solid(8, 8, (103, 100, 100, 255))
        self.assertEqual(im.diff_images(a, b, threshold=0)["differing_pixels"], 64)
        r = im.diff_images(a, b, threshold=3)
        self.assertEqual((r["differing_pixels"], r["identical"]), (0, False))     # tolerated, but not identical

    def test_alpha_is_ignored_unless_asked(self):
        a, b = solid(4, 4, (1, 2, 3, 255)), solid(4, 4, (1, 2, 3, 10))
        self.assertEqual(im.diff_images(a, b)["differing_pixels"], 0)
        r = im.diff_images(a, b, ignore_alpha=False)
        self.assertEqual((r["differing_pixels"], r["max_delta"]), (16, 245))

    def test_regions_rank_the_busiest_cells(self):
        a = solid(32, 32, (0, 0, 0, 255))
        rows = [bytearray(r) for r in a.rows]
        for y in range(0, 4):
            for x in range(0, 4):
                rows[y][x * 4] = 200                           # 16 pixels in the top-left cell
        rows[20][20 * 4] = 200                                  # 1 pixel elsewhere
        r = im.diff_images(a, im.Image(32, 32, 4, [bytes(x) for x in rows]), grid=4)
        self.assertEqual(r["regions"][0], {"x0": 0, "y0": 0, "x1": 7, "y1": 7, "differing_pixels": 16})
        self.assertEqual(r["regions"][1]["differing_pixels"], 1)

    def test_heatmap_marks_only_differing_pixels(self):
        a = solid(8, 8, (200, 200, 200, 255))
        b = with_patch(8, 8, (2, 2, 2, 2), (255, 255, 255, 255), base=(200, 200, 200, 255))
        r = im.diff_images(a, b)
        heat = im.decode_png(r["heatmap_png"])
        self.assertEqual((heat.width, heat.height), (8, 8))
        row = heat.rgba_row(2)
        self.assertEqual(row[0:4], bytes([100, 100, 100, 255]))             # untouched pixel: image A dimmed
        self.assertEqual(row[2 * 4:2 * 4 + 4], bytes(im._heat(55) + (255,)))  # differing pixel: heat colour of delta 55
        self.assertNotIn("heatmap_png", im.diff_images(a, b, heatmap=False))

    def test_gray_and_rgb_images_compare_after_expansion(self):
        gray = im.Image(2, 1, 1, [bytes([10, 20])])
        rgb = im.Image(2, 1, 3, [bytes([10, 10, 10, 25, 25, 25])])
        r = im.diff_images(gray, rgb)
        self.assertEqual((r["differing_pixels"], r["max_delta"]), (1, 5))

    def test_heat_scale_is_monotonic_and_bounded(self):
        prev = -1
        for d in (1, 40, 90, 140, 200, 255):
            r, g, b = im._heat(d)
            self.assertTrue(0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255)
            self.assertGreaterEqual(g + b, prev)
            prev = g + b


if __name__ == "__main__":
    unittest.main()
