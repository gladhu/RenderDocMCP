"""
Pixel diff of two images, with a PNG codec that needs nothing but zlib.

Used by diff_textures: the two sides (render targets of two events, textures, or PNG files saved from other
captures) are brought to 8-bit PNGs, decoded here and compared. Decoding is the cost: row filters None / Up /
Sub are done in C speed (bytes slicing, a big-integer byte-wise add, itertools.accumulate), Average and Paeth
in a Python loop.
"""

import array
import itertools
import math
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}          # PNG colour type -> channels (8-bit, no palette)


class ImageError(ValueError):
    pass


class Image(object):
    """8-bit image: rows of width*channels bytes"""

    def __init__(self, width, height, channels, rows):
        self.width, self.height, self.channels, self.rows = width, height, channels, rows

    def rgba_row(self, y):
        """Row y as RGBA bytes (gray and RGB are expanded, alpha 255)"""
        row, c = self.rows[y], self.channels
        if c == 4:
            return row
        out = bytearray(b"\xff" * (self.width * 4))
        if c == 3:
            out[0::4], out[1::4], out[2::4] = row[0::3], row[1::3], row[2::3]
        elif c == 1:
            out[0::4] = out[1::4] = out[2::4] = row
        else:                                                   # gray + alpha
            out[0::4] = out[1::4] = out[2::4] = row[0::2]
            out[3::4] = row[1::2]
        return bytes(out)


# ---------------------------------------------------------------- PNG decode

def _byte_add(a, b):
    """(a + b) mod 256 for every byte of two equally long byte strings, in one pass over big integers"""
    n = len(a)
    x, y = int.from_bytes(a, "little"), int.from_bytes(b, "little")
    low = int.from_bytes(b"\x7f" * n, "little")
    high = int.from_bytes(b"\x80" * n, "little")
    return (((x & low) + (y & low)) ^ ((x ^ y) & high)).to_bytes(n, "little")


def _unfilter_sub(row, bpp):
    out = bytearray(len(row))
    for c in range(bpp):
        out[c::bpp] = bytes(v & 255 for v in itertools.accumulate(row[c::bpp]))
    return bytes(out)


def _unfilter_average(row, prior, bpp):
    out = bytearray(row)
    for i in range(len(row)):
        left = out[i - bpp] if i >= bpp else 0
        out[i] = (row[i] + ((left + prior[i]) >> 1)) & 255
    return bytes(out)


def _unfilter_paeth(row, prior, bpp):
    out = bytearray(row)
    for i in range(len(row)):
        if i >= bpp:
            a, c = out[i - bpp], prior[i - bpp]
        else:
            a = c = 0
        b = prior[i]
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
        out[i] = (row[i] + pred) & 255
    return bytes(out)


def decode_png(data):
    """bytes of a PNG file -> Image. 8-bit gray / gray+alpha / RGB / RGBA, not interlaced."""
    if data[:8] != PNG_SIGNATURE:
        raise ImageError("not a PNG file")
    pos, idat, header = 8, [], None
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body[:13])
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if header is None or not idat:
        raise ImageError("PNG without header or image data")
    width, height, depth, ctype, _, _, interlace = header
    if depth != 8 or ctype not in CHANNELS or interlace:
        raise ImageError("unsupported PNG (bit depth %d, colour type %d, interlace %d): need 8-bit gray / RGB / "
                         "RGBA, not interlaced" % (depth, ctype, interlace))
    bpp = CHANNELS[ctype]
    stride = width * bpp
    raw = zlib.decompress(b"".join(idat))
    if len(raw) < height * (stride + 1):
        raise ImageError("PNG image data is truncated")
    rows, prior = [], bytes(stride)
    for y in range(height):
        base = y * (stride + 1)
        ftype, row = raw[base], raw[base + 1:base + 1 + stride]
        if ftype == 0:
            cur = row
        elif ftype == 1:
            cur = _unfilter_sub(row, bpp)
        elif ftype == 2:
            cur = _byte_add(row, prior)
        elif ftype == 3:
            cur = _unfilter_average(row, prior, bpp)
        elif ftype == 4:
            cur = _unfilter_paeth(row, prior, bpp)
        else:
            raise ImageError("bad PNG filter type %d" % ftype)
        rows.append(cur)
        prior = cur
    return Image(width, height, bpp, rows)


def encode_png(width, height, rows, channels=4):
    """rows of width*channels bytes -> PNG bytes (filter None)"""
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    body = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    return (PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, ctype, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(body, 6)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- diff

def _heat(delta):
    """0 .. 255 -> RGB: dark red for small differences up to bright yellow-white for large ones"""
    t = delta / 255.0
    return (255, int(255 * min(1.0, max(0.0, (t - 0.25) / 0.5))), int(255 * max(0.0, (t - 0.75) / 0.25)))


def diff_images(a, b, threshold=0, ignore_alpha=True, grid=8, heatmap=True, top_regions=5):
    """
    Compare two Images of equal size. A pixel differs when any compared channel differs by more than threshold
    (0-255). Returns the statistics dict and, when heatmap, the PNG bytes of the difference image under "heatmap_png"
    (differing pixels coloured by size on a dimmed copy of image A).
    """
    if (a.width, a.height) != (b.width, b.height):
        return {"comparable": False, "reason": "size differs: %dx%d vs %dx%d" % (a.width, a.height, b.width, b.height)}
    w, h = a.width, a.height
    threshold = max(0, min(255, int(threshold)))
    gridn = max(1, int(grid))
    cell_w, cell_h = max(1, math.ceil(w / gridn)), max(1, math.ceil(h / gridn))
    cells = {}
    diff_count = 0
    sum_sq = 0
    sum_abs = 0
    max_delta = 0
    chan_max = [0, 0, 0, 0]
    bbox = None
    out_rows = [] if heatmap else None
    compared = 3 if ignore_alpha else 4
    for y in range(h):
        ra, rb = a.rgba_row(y), b.rgba_row(y)
        same_row = ra == rb
        if same_row:
            if heatmap:
                out_rows.append(_dim_row(ra))
            continue
        row_out = bytearray(_dim_row(ra)) if heatmap else None
        pa, pb = array.array("I", ra), array.array("I", rb)
        for x in range(w):
            if pa[x] == pb[x]:
                continue
            i = x * 4
            deltas = [abs(ra[i + c] - rb[i + c]) for c in range(compared)]
            d = max(deltas)
            sum_sq += sum(v * v for v in deltas)
            sum_abs += sum(deltas)
            if d <= threshold:
                continue
            diff_count += 1
            if d > max_delta:
                max_delta = d
            for c in range(compared):
                if deltas[c] > chan_max[c]:
                    chan_max[c] = deltas[c]
            if bbox is None:
                bbox = [x, y, x, y]
            else:
                if x < bbox[0]:
                    bbox[0] = x
                if x > bbox[2]:
                    bbox[2] = x
                bbox[3] = y
            key = (x // cell_w, y // cell_h)
            cells[key] = cells.get(key, 0) + 1
            if heatmap:
                row_out[i:i + 4] = bytes(_heat(d) + (255,))
        if heatmap:
            out_rows.append(bytes(row_out))
    total = w * h
    samples = total * compared
    rmse = math.sqrt(sum_sq / float(samples)) if samples else 0.0
    regions = sorted(cells.items(), key=lambda kv: -kv[1])[:max(1, int(top_regions))]
    result = {
        "comparable": True,
        "width": w, "height": h, "total_pixels": total,
        "identical": diff_count == 0 and sum_abs == 0,
        "threshold": threshold,
        "differing_pixels": diff_count,
        "differing_pct": round(100.0 * diff_count / total, 4) if total else 0.0,
        "max_delta": max_delta,
        "max_delta_by_channel": dict(zip("rgba", chan_max[:compared])),
        "mean_delta_over_differing": round(sum_abs / float(max(diff_count, 1) * compared), 3) if diff_count else 0.0,
        "rmse": round(rmse, 4),
        "psnr_db": None if rmse == 0 else round(20.0 * math.log10(255.0 / rmse), 2),
        "bounding_box": None if bbox is None else {"x0": bbox[0], "y0": bbox[1], "x1": bbox[2], "y1": bbox[3]},
        "regions": [{"x0": cx * cell_w, "y0": cy * cell_h, "x1": min(w, (cx + 1) * cell_w) - 1,
                     "y1": min(h, (cy + 1) * cell_h) - 1, "differing_pixels": n} for (cx, cy), n in regions],
        "alpha_compared": not ignore_alpha,
    }
    if heatmap:
        result["heatmap_png"] = encode_png(w, h, out_rows)
    return result


def _dim_row(rgba_row):
    """Image A as a faded background: halve the colour, opaque"""
    out = bytearray(rgba_row)
    out[0::4] = bytes(v >> 1 for v in out[0::4])
    out[1::4] = bytes(v >> 1 for v in out[1::4])
    out[2::4] = bytes(v >> 1 for v in out[2::4])
    out[3::4] = b"\xff" * (len(out) // 4)
    return bytes(out)
