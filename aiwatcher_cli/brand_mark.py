"""Anti-aliased rasteriser for the two-ring mark, for the Tk Companion.

Tk's canvas draws without anti-aliasing and has no rounded-rectangle
primitive, so `native_overlay._draw_brand_mark` rebuilds each ring from four
lines and four arcs. At the sizes the Companion uses that is a ~2px stroke
meeting a ~3.4px corner arc with nothing to blend the join, and the sixteen
pieces visibly fail to meet. macOS never had the problem: it gets CALayer
corner radii on a Retina backing store.

Rasterising the mark here gives the Tk bar the same anti-aliased edges, and
collapses those sixteen canvas items into one image -- which matters twice
over, because the running arc forces every item under it to be re-rendered on
each frame.

The geometry and the coverage method are logo/render_icons.py's, which draws
the same artwork for the browser extension: both rings 300 wide with a 40
stroke and an 85 outer corner radius in a 429x349 box, the ink ring in front.
This one differs only in taking its colours from the caller and filling a
non-square box, because the Companion recolours the outer ring to carry state.
The two are kept in step by hand; neither is generated from the other.
"""
from __future__ import annotations

import math
import struct
import zlib

# The SVG's own viewBox, unpadded: the Companion positions the mark itself.
VIEW_W = 429.0
VIEW_H = 349.0

# x, y, w, h, corner radius, stroke -- on the stroke centerline, back to front,
# exactly as logo/aiwatcher-mark.svg states them.
RINGS = (
    (20.0, 20.0, 260.0, 220.0, 65.0, 40.0),
    (149.0, 137.0, 260.0, 192.0, 65.0, 40.0),
)

SUPERSAMPLE = 4


def _rgb(colour: str) -> tuple[int, int, int]:
    value = colour.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        raise ValueError("expected #rgb or #rrggbb, got %r" % (colour,))
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _on_ring(px, py, x, y, w, h, r, stroke):
    """Whether a sample lands on the rounded rectangle's stroke band."""
    cx, cy = x + w / 2, y + h / 2
    ex, ey = w / 2 - r, h / 2 - r
    qx, qy = abs(px - cx) - ex, abs(py - cy) - ey
    d = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r
    return abs(d) <= stroke / 2


def mark_size(height: float) -> tuple[int, int]:
    """Pixel size of the mark drawn at `height`, keeping the artwork's ratio."""
    return (
        max(1, int(round(VIEW_W * height / VIEW_H))),
        max(1, int(round(height))),
    )


def render_mark_png(height: float, blue: str, ink: str, supersample: int = SUPERSAMPLE) -> bytes:
    """Return the mark as RGBA PNG bytes, `height` pixels tall.

    Coverage is supersampled `supersample` times per axis, so corners and
    stroke edges anti-alias instead of stair-stepping.
    """
    width, pixel_height = mark_size(height)
    colours = (_rgb(blue), _rgb(ink))
    scale = VIEW_H / float(pixel_height)
    samples = float(supersample * supersample)
    rows = []
    for iy in range(pixel_height):
        row = bytearray(b"\x00")  # PNG filter byte: none
        for ix in range(width):
            red = green = blue_total = alpha = 0.0
            for sy in range(supersample):
                py = (iy + (sy + 0.5) / supersample) * scale
                for sx in range(supersample):
                    px = (ix + (sx + 0.5) / supersample) * scale
                    hit = None
                    for index, geometry in enumerate(RINGS):
                        if _on_ring(px, py, *geometry):
                            hit = colours[index]  # later rings paint over
                    if hit is not None:
                        red += hit[0]
                        green += hit[1]
                        blue_total += hit[2]
                        alpha += 255.0
            if alpha <= 0.0:
                row += b"\x00\x00\x00\x00"
            else:
                # Un-premultiply: the colour is the average over covered
                # samples, while the alpha is the average over all of them.
                covered = alpha / 255.0
                row += bytes((
                    round(red / covered),
                    round(green / covered),
                    round(blue_total / covered),
                    round(alpha / samples),
                ))
        rows.append(bytes(row))
    return _png(width, pixel_height, b"".join(rows))


def _png(width: int, height: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
