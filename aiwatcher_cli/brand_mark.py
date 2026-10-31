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


def _rasterise(width: int, height: int, shader, supersample: int = SUPERSAMPLE) -> bytes:
    """Supersampled coverage render of `shader`, as RGBA PNG bytes.

    `shader(x, y)` is asked about points in the image's own pixel space and
    returns an (r, g, b) or None. Coverage is sampled `supersample` times per
    axis, which is what anti-aliases the edges -- Tk's canvas cannot, so every
    shape the Companion draws through here stops stair-stepping.
    """
    samples = float(supersample * supersample)
    rows = []
    for iy in range(height):
        row = bytearray(b"\x00")  # PNG filter byte: none
        for ix in range(width):
            red = green = blue_total = alpha = 0.0
            for sy in range(supersample):
                py = iy + (sy + 0.5) / supersample
                for sx in range(supersample):
                    px = ix + (sx + 0.5) / supersample
                    hit = shader(px, py)
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
    return _png(width, height, b"".join(rows))


def _in_capsule(px, py, x0, y0, x1, y1, radius) -> bool:
    """Whether a point is within `radius` of the segment -- a round-capped
    stroke, which is what Tk's capstyle=ROUND draws."""
    dx, dy = x1 - x0, y1 - y0
    length_squared = dx * dx + dy * dy
    if length_squared <= 0.0:
        return math.hypot(px - x0, py - y0) <= radius
    t = ((px - x0) * dx + (py - y0) * dy) / length_squared
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy)) <= radius


def render_mark_png(height: float, blue: str, ink: str, supersample: int = SUPERSAMPLE) -> bytes:
    """Return the mark as RGBA PNG bytes, `height` pixels tall."""
    width, pixel_height = mark_size(height)
    colours = (_rgb(blue), _rgb(ink))
    scale = VIEW_H / float(pixel_height)

    def shader(px, py):
        hit = None
        for index, geometry in enumerate(RINGS):
            if _on_ring(px * scale, py * scale, *geometry):
                hit = colours[index]  # later rings paint over
        return hit

    return _rasterise(width, pixel_height, shader, supersample)


def render_disc_png(diameter: float, colour: str, supersample: int = SUPERSAMPLE) -> bytes:
    """A filled circle -- the count badge, which Tk drew with a jagged edge."""
    size = max(1, int(round(diameter)))
    rgb = _rgb(colour)
    centre = size / 2.0
    radius = size / 2.0

    def shader(px, py):
        return rgb if math.hypot(px - centre, py - centre) <= radius else None

    return _rasterise(size, size, shader, supersample)


def render_ring_png(diameter: float, stroke: float, colour: str, supersample: int = SUPERSAMPLE) -> bytes:
    """A circular outline -- the attention and focus rims.

    Tk strokes an oval centred on its path, so the image is `stroke` wider
    than the bounding box and is drawn half a stroke up and to the left of it.
    """
    size = max(1, int(round(diameter + stroke)))
    rgb = _rgb(colour)
    centre = size / 2.0
    path_radius = diameter / 2.0
    half = stroke / 2.0

    def shader(px, py):
        distance = math.hypot(px - centre, py - centre)
        return rgb if abs(distance - path_radius) <= half else None

    return _rasterise(size, size, shader, supersample)


def render_face_png(
    width: float,
    height: float,
    eyes,
    eye_thickness: float,
    panel: str,
    eye_colour: str,
    supersample: int = SUPERSAMPLE,
) -> bytes:
    """The Companion's face: a stadium panel with two round-capped eyes.

    `eyes` are segments in the face's own pixel space, gaze already applied.
    Drawn as one image so the panel's curve and the eyes anti-alias together;
    drawn as Tk primitives they were the last stair-stepped thing left in the
    bubble once the mark became an image.
    """
    pixel_width = max(1, int(round(width)))
    pixel_height = max(1, int(round(height)))
    panel_rgb, eye_rgb = _rgb(panel), _rgb(eye_colour)
    radius = pixel_height / 2.0
    eye_radius = max(eye_thickness, 0.1) / 2.0

    def shader(px, py):
        for x0, y0, x1, y1 in eyes:
            if _in_capsule(px, py, x0, y0, x1, y1, eye_radius):
                return eye_rgb
        waist = min(max(px, radius), pixel_width - radius)
        if math.hypot(px - waist, py - radius) <= radius:
            return panel_rgb
        return None

    return _rasterise(pixel_width, pixel_height, shader, supersample)


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
EYE_WIDTH_FRACTION = 0.18
EYE_CENTRE_FRACTIONS = (0.25, 0.75)
EYE_Y_FRACTION = 0.46
# Thickness is a proportion of the face too. It was a typed 2px, which at the
# real face height of ~5px was 40% of it: the two eyes met and read as one bar.
#
# The eyes are drawn with round caps, which extend each dash by half its
# thickness at each end -- so thickness eats into the gap between them twice
# over, and these four fractions are only correct together. `eyes_gap` on the
# layout reports what actually survives, and a test holds it above zero at
# every size; picking them by eye at one size is what merged the eyes twice.
EYE_THICKNESS_FRACTION = 0.24
MIN_EYE_THICKNESS = 1.0
MIN_EYES_GAP = 0.6
# Below this the floor on thickness wins and the two dashes touch: the face is
# simply too small for two eyes. Derived, not guessed -- see the test sweep.
MIN_FACE_WIDTH_FOR_EYES = (MIN_EYE_THICKNESS + MIN_EYES_GAP) / (
    EYE_CENTRE_FRACTIONS[1] - EYE_CENTRE_FRACTIONS[0] - EYE_WIDTH_FRACTION
)

# The rim circles (orbit, attention, focus) ride just inside the canvas, and
# the running arc is drawn centred on the rim path, so it reaches ORBIT_STROKE
# / 2 inside it. That inner edge is what the mark has to clear.
RIM_MARGIN = 2.0
FOCUS_RIM_INSET = 3.0
ORBIT_STROKE = 4.0
FOCUS_STROKE_FRACTION = 0.5

# The badge sits in the canvas's top-right corner. It is here for the same
# reason as everything else: it was typed as (26, 2, 42, 18) once, which ran
# past the 36-wide canvas and rendered cropped, and was then re-typed as
# (19, 2, 35, 18). Anchoring it to the corner makes that unrepresentable.
BADGE_DIAMETER_FRACTION = 0.44
BADGE_MARGIN = (1.0, 2.0)
# The count inside the badge has to grow with it, or enlarging the bubble
# leaves an 8pt number rattling around a 25px disc.
BADGE_FONT_FRACTION = 0.5
MIN_BADGE_FONT = 7


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
    orbit_stroke: float
    focus_stroke: float
    eye_thickness: float
    eyes_gap: float
    badge_font_size: int


def _ring_rects(index: int, scale: float) -> tuple[Rect, Rect]:
    """The outer edge and the inner opening of one ring, in scaled units."""
    x, y, w, h, _radius, stroke = RINGS[index]
    outer = Rect(x - stroke / 2, y - stroke / 2, x + w + stroke / 2, y + h + stroke / 2)
    interior = Rect(x + stroke / 2, y + stroke / 2, x + w - stroke / 2, y + h - stroke / 2)
    return outer.scaled(scale), interior.scaled(scale)


def inscribed_mark_height(rim_width: float, stroke: float = ORBIT_STROKE) -> float:
    """Largest mark whose corners clear the inside of the rim's stroke.

    The mark is a rectangle inside a circle, so what binds is the diagonal,
    not the width -- fitting the width instead pushes the corners straight
    through the running arc.

    This is the rule the old hand-placed 18px turned out to encode: at a 32px
    rim with a 4px arc it gives 17.67, which rounds to the 22x18 that shipped.
    The mark size was never the defect; only the face's position was.
    """
    clearance = rim_width / 2.0 - stroke / 2.0
    return 2.0 * clearance / (math.hypot(VIEW_W, VIEW_H) / VIEW_H)


def bubble_layout(
    canvas_w: float,
    canvas_h: float,
    *,
    mark_height: Optional[float] = None,
) -> BubbleLayout:
    """Every region the collapsed bubble draws into, derived from the artwork.

    `mark_height` defaults to the largest mark whose corners clear the running
    arc -- see `inscribed_mark_height`.
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
        mark_height = inscribed_mark_height(rim.width)
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
    # Thickness is capped by the room left between the two dashes, because the
    # round caps spend it twice. Below a certain face width the floor wins and
    # the eyes touch -- see MIN_FACE_WIDTH_FOR_EYES.
    separation = face.width * (EYE_CENTRE_FRACTIONS[1] - EYE_CENTRE_FRACTIONS[0] - EYE_WIDTH_FRACTION)
    eye_thickness = max(MIN_EYE_THICKNESS, min(face.height * EYE_THICKNESS_FRACTION, separation - MIN_EYES_GAP))
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
        orbit_stroke=ORBIT_STROKE,
        focus_stroke=ORBIT_STROKE * FOCUS_STROKE_FRACTION,
        eye_thickness=eye_thickness,
        eyes_gap=(eyes[1].x0 - eye_thickness / 2.0) - (eyes[0].x1 + eye_thickness / 2.0),
        badge_font_size=max(MIN_BADGE_FONT, int(round(badge.height * BADGE_FONT_FRACTION))),
    )
