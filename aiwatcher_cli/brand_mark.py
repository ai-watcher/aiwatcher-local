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
from typing import NamedTuple, Optional

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


# --- layout -----------------------------------------------------------------
#
# Everything the Companion's bubble draws is derived from the ring geometry
# above, because the alternative is what this module replaced: the mark was
# placed in artwork units and then nudged by a hand-picked pixel offset, while
# the face, the eyes and the three rim circles were typed as canvas pixels
# that happened to line up once, at one mark size, in one person's eye. They
# did not line up -- the face panel sat 5.5px high, covering the front ring's
# whole top stroke -- and nothing could catch it, because the rule "the face
# belongs inside the front ring" lived in a comment rather than in the code.
#
# So positions are computed here, from one source, and the relationships are
# assertable. Changing the mark's size moves everything with it.


class Rect(NamedTuple):
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0

    def inset(self, dx: float, dy: Optional[float] = None) -> "Rect":
        dy = dx if dy is None else dy
        return Rect(self.x0 + dx, self.y0 + dy, self.x1 - dx, self.y1 - dy)

    def scaled(self, factor: float) -> "Rect":
        return Rect(self.x0 * factor, self.y0 * factor, self.x1 * factor, self.y1 * factor)

    def moved(self, dx: float, dy: float) -> "Rect":
        return Rect(self.x0 + dx, self.y0 + dy, self.x1 + dx, self.y1 + dy)

    def contains(self, other: "Rect", tolerance: float = 1e-9) -> bool:
        return (
            other.x0 >= self.x0 - tolerance
            and other.y0 >= self.y0 - tolerance
            and other.x1 <= self.x1 + tolerance
            and other.y1 <= self.y1 + tolerance
        )


# Fractions, not pixels: the face is a proportion of the opening it sits in, so
# it stays inside that opening at every mark size. The inset leaves the front
# ring's stroke visible all the way round -- without it the panel and the ring
# are the same colour and merge into one solid shape.
FACE_INSET_FRACTION = 0.16
EYE_WIDTH_FRACTION = 0.26
EYE_CENTRE_FRACTIONS = (0.30, 0.70)
EYE_Y_FRACTION = 0.44

# The rim circles (orbit, attention, focus) ride just inside the canvas.
RIM_MARGIN = 2.0
FOCUS_RIM_INSET = 3.0

# The badge sits in the canvas's top-right corner. It is here for the same
# reason as everything else: it was typed as (26, 2, 42, 18) once, which ran
# past the 36-wide canvas and rendered cropped, and was then re-typed as
# (19, 2, 35, 18). Anchoring it to the corner makes that unrepresentable.
BADGE_DIAMETER_FRACTION = 0.44
BADGE_MARGIN = (1.0, 2.0)


class BubbleLayout(NamedTuple):
    canvas: Rect
    rim: Rect
    focus_rim: Rect
    mark_height: float
    mark: Rect
    back_ring: Rect
    front_ring: Rect
    front_interior: Rect
    face: Rect
    eyes: tuple[Rect, Rect]
    badge: Rect


def _ring_rects(index: int, scale: float) -> tuple[Rect, Rect]:
    """The outer edge and the inner opening of one ring, in scaled units."""
    x, y, w, h, _radius, stroke = RINGS[index]
    outer = Rect(x - stroke / 2, y - stroke / 2, x + w + stroke / 2, y + h + stroke / 2)
    interior = Rect(x + stroke / 2, y + stroke / 2, x + w - stroke / 2, y + h - stroke / 2)
    return outer.scaled(scale), interior.scaled(scale)


def fitted_mark_height(width: float) -> float:
    """The mark height whose width is `width`, keeping the artwork's ratio."""
    return width * VIEW_H / VIEW_W


def bubble_layout(
    canvas_w: float,
    canvas_h: float,
    *,
    mark_height: Optional[float] = None,
) -> BubbleLayout:
    """Every region the collapsed bubble draws into, derived from the artwork.

    `mark_height` defaults to the largest mark that spans the rim circle, which
    is what makes the strokes thick enough to read: at the old hand-placed 18px
    the stroke was 2.06px, which no amount of anti-aliasing turns into a
    rounded corner.
    """
    canvas = Rect(0.0, 0.0, float(canvas_w), float(canvas_h))
    side = min(canvas.width, canvas.height) - 2 * RIM_MARGIN
    rim = Rect(
        (canvas.width - side) / 2.0,
        (canvas.height - side) / 2.0,
        (canvas.width + side) / 2.0,
        (canvas.height + side) / 2.0,
    )
    if mark_height is None:
        mark_height = fitted_mark_height(rim.width)
    scale = mark_height / VIEW_H

    width, height = mark_size(mark_height)
    origin_x = (canvas.width - width) / 2.0
    origin_y = (canvas.height - height) / 2.0
    mark = Rect(origin_x, origin_y, origin_x + width, origin_y + height)

    back_outer, _back_interior = _ring_rects(0, scale)
    front_outer, front_interior = _ring_rects(1, scale)
    back_ring = back_outer.moved(origin_x, origin_y)
    front_ring = front_outer.moved(origin_x, origin_y)
    front_interior = front_interior.moved(origin_x, origin_y)

    face = front_interior.inset(
        front_interior.width * FACE_INSET_FRACTION,
        front_interior.height * FACE_INSET_FRACTION,
    )
    eye_half = face.width * EYE_WIDTH_FRACTION / 2.0
    eye_y = face.y0 + face.height * EYE_Y_FRACTION
    eyes = tuple(
        Rect(face.x0 + face.width * centre - eye_half, eye_y,
             face.x0 + face.width * centre + eye_half, eye_y)
        for centre in EYE_CENTRE_FRACTIONS
    )

    diameter = min(canvas.width, canvas.height) * BADGE_DIAMETER_FRACTION
    margin_x, margin_y = BADGE_MARGIN
    badge = Rect(
        canvas.x1 - margin_x - diameter,
        canvas.y0 + margin_y,
        canvas.x1 - margin_x,
        canvas.y0 + margin_y + diameter,
    )

    return BubbleLayout(
        canvas=canvas,
        rim=rim,
        focus_rim=rim.inset(FOCUS_RIM_INSET),
        mark_height=mark_height,
        mark=mark,
        back_ring=back_ring,
        front_ring=front_ring,
        front_interior=front_interior,
        face=face,
        eyes=(eyes[0], eyes[1]),
        badge=badge,
    )
