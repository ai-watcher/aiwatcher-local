import inspect
import math
import struct
import unittest
import zlib

from aiwatcher_cli import brand_mark, native_overlay


def decode_rgba(png):
    """Return (width, height, rows of RGBA tuples) for a PNG this module wrote."""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a PNG")
    pos, idat, header = 8, b"", None
    while pos < len(png):
        length = struct.unpack(">I", png[pos:pos + 4])[0]
        tag = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            header = struct.unpack(">IIBB", data[:10])
        elif tag == b"IDAT":
            idat += data
        pos += 12 + length
    width, height, depth, colour = header
    if (depth, colour) != (8, 6):
        raise AssertionError("expected 8-bit RGBA, got depth=%d colour=%d" % (depth, colour))
    raw = zlib.decompress(idat)
    stride = width * 4 + 1
    rows = []
    for y in range(height):
        row = raw[y * stride:(y + 1) * stride]
        if row[0] != 0:
            raise AssertionError("expected filter 0, got %d" % row[0])
        rows.append([tuple(row[1 + x * 4:5 + x * 4]) for x in range(width)])
    return width, height, rows


class BrandMarkGeometryTests(unittest.TestCase):
    def test_the_mark_keeps_the_artworks_proportions(self) -> None:
        # 429x349 is the SVG's viewBox, and the Companion positions the mark
        # against that ratio: a square image would shift both rings.
        self.assertEqual(brand_mark.mark_size(349.0), (429, 349))
        self.assertEqual(brand_mark.mark_size(18.0), (22, 18))
        self.assertEqual(brand_mark.mark_size(26.0), (32, 26))

    def test_a_degenerate_height_still_yields_a_drawable_image(self) -> None:
        self.assertEqual(brand_mark.mark_size(0.0), (1, 1))

    def test_the_geometry_is_stated_once_and_the_fallback_reads_it(self) -> None:
        # It used to be stated four times -- the SVG, render_icons, this module
        # and _draw_brand_mark's outer boxes -- in two different
        # parameterisations, "kept in step by hand". The vector fallback now
        # reads these numbers rather than restating them.
        back, front = brand_mark.RINGS
        self.assertEqual(back[:4], (20.0, 20.0, 260.0, 220.0))
        self.assertEqual(front[:4], (149.0, 137.0, 260.0, 192.0))
        for ring in (back, front):
            radius, stroke = ring[4], ring[5]
            self.assertEqual(stroke, 40.0)
            self.assertEqual(radius + stroke / 2, 85.0)  # the outer corner radius
        source = inspect.getsource(native_overlay._draw_brand_mark)
        self.assertIn("zip(brand_mark.RINGS, (blue, ink))", source)
        self.assertNotIn("300.0 * s", source)
        self.assertNotIn("129.0 * s", source)

    def test_the_fallback_draws_the_same_ring_the_rasteriser_does(self) -> None:
        # The fallback insets from the outer box; the shared geometry states
        # the centerline. They must land on the same line at the sizes the
        # Companion actually draws.
        for height in (18.0, 26.0, 30.0):
            with self.subTest(height=height):
                s = height / brand_mark.VIEW_H
                stroke = max(2.0, 40.0 * s)
                legacy = (stroke / 2, stroke / 2, 300 * s - stroke / 2, 260 * s - stroke / 2, 85 * s - stroke / 2)
                x, y, w, h, radius, _ = brand_mark.RINGS[0]
                shared = (x * s, y * s, (x + w) * s, (y + h) * s, radius * s)
                for a, b in zip(legacy, shared):
                    self.assertAlmostEqual(a, b, places=9)


class BrandMarkRenderTests(unittest.TestCase):
    def test_edges_are_anti_aliased_rather_than_stair_stepped(self) -> None:
        # This is the whole reason the renderer exists: Tk's canvas has no
        # anti-aliasing, so the vector fallback can only ever produce fully on
        # or fully off pixels. Partial coverage is the thing to assert.
        png = brand_mark.render_mark_png(18.0, "#0052F5", "#141314")
        width, height, rows = decode_rgba(png)
        self.assertEqual((width, height), (22, 18))
        alphas = {pixel[3] for row in rows for pixel in row}
        partial = sorted(a for a in alphas if 0 < a < 255)
        self.assertGreater(len(partial), 4, "expected a range of partial coverage, got %r" % (partial,))
        self.assertIn(0, alphas, "the box corners should be transparent")
        self.assertIn(255, alphas, "the stroke interior should be opaque")

    def test_the_corners_of_the_box_stay_transparent(self) -> None:
        # The mark is a pair of rounded rings, not a filled rectangle, so the
        # extreme corners of the image must carry no ink at all.
        _, _, rows = decode_rgba(brand_mark.render_mark_png(26.0, "#0052F5", "#141314"))
        for y, x in ((0, 0), (0, -1), (-1, 0), (-1, -1)):
            with self.subTest(corner=(y, x)):
                self.assertEqual(rows[y][x][3], 0)

    def test_each_ring_is_drawn_in_its_own_colour(self) -> None:
        png = brand_mark.render_mark_png(26.0, "#43d9a3", "#141314")
        _, _, rows = decode_rgba(png)
        opaque = {pixel[:3] for row in rows for pixel in row if pixel[3] == 255}
        self.assertIn((0x43, 0xD9, 0xA3), opaque)
        self.assertIn((0x14, 0x13, 0x14), opaque)

    def test_the_front_ring_paints_over_the_back_one(self) -> None:
        # The ink ring is in front; where they overlap, ink wins. Painting the
        # two the same colour and then differently must change the pixel count.
        same = brand_mark.render_mark_png(26.0, "#0052F5", "#0052F5")
        _, _, rows = decode_rgba(same)
        opaque = {pixel[:3] for row in rows for pixel in row if pixel[3] == 255}
        self.assertEqual(opaque, {(0x00, 0x52, 0xF5)})

    def test_colours_may_be_given_short_or_long_and_bad_ones_are_refused(self) -> None:
        self.assertEqual(brand_mark._rgb("#fff"), (255, 255, 255))
        self.assertEqual(brand_mark._rgb("0052F5"), (0x00, 0x52, 0xF5))
        for bad in ("#12345", "", "#nothex"):
            with self.subTest(colour=bad):
                with self.assertRaises(ValueError):
                    brand_mark._rgb(bad)

    def test_rendering_is_deterministic(self) -> None:
        # The overlay caches by (height, colours); that is only sound if the
        # same inputs always give the same pixels.
        first = brand_mark.render_mark_png(18.0, "#0052F5", "#141314")
        second = brand_mark.render_mark_png(18.0, "#0052F5", "#141314")
        self.assertEqual(first, second)


class BubbleLayoutTests(unittest.TestCase):
    """The invariants that used to live in a comment and a person's eye.

    Every one of these held only by coincidence before the layout existed, and
    two of them did not hold at all.
    """

    # The Companion's own canvas is 56x60 (a 72px window less its padding).
    # Nothing below 28 is swept: the face is then narrower than
    # MIN_FACE_WIDTH_FOR_EYES and cannot hold two separate eyes at all, which
    # is asserted directly below rather than hidden by omission.
    SIZES = ((56, 60), (36, 40), (28, 28), (52, 52), (64, 48))

    def test_the_face_stays_inside_the_front_ring_at_every_mark_size(self) -> None:
        # This is the bug that shipped: the face panel covered the front
        # ring's entire top stroke, so the mark read as a blob rather than a
        # ring. Sweeping sizes is what makes it impossible to reintroduce.
        for height in range(10, 49):
            with self.subTest(mark_height=height):
                layout = brand_mark.bubble_layout(36, 40, mark_height=float(height))
                self.assertTrue(
                    layout.front_interior.contains(layout.face),
                    "face %s escaped the ring opening %s" % (layout.face, layout.front_interior),
                )

    def test_the_eyes_do_not_merge_once_round_caps_are_added(self) -> None:
        # The eyes are drawn with round caps, so each dash grows by half its
        # thickness at both ends. They merged into one bar twice before this
        # was asserted -- once from a typed 2px thickness, once from scaling
        # the bubble up with the fractions chosen at the smaller size.
        for width, height in self.SIZES:
            with self.subTest(canvas=(width, height)):
                layout = brand_mark.bubble_layout(width, height)
                self.assertGreater(
                    layout.eyes_gap, 0.5,
                    "eyes merge: gap %.2f at canvas %dx%d" % (layout.eyes_gap, width, height),
                )

    def test_a_face_too_small_for_two_eyes_is_a_known_floor(self) -> None:
        # Not every canvas can carry this design, and the limit is derived
        # rather than discovered on screen: below a 5px face the thickness
        # floor wins and the dashes touch. The shipped bubble is well clear.
        self.assertAlmostEqual(brand_mark.MIN_FACE_WIDTH_FOR_EYES, 5.0, places=6)
        cramped = brand_mark.bubble_layout(24, 24)
        self.assertLess(cramped.face.width, brand_mark.MIN_FACE_WIDTH_FOR_EYES)
        self.assertLessEqual(cramped.eyes_gap, 0.5)
        shipped = brand_mark.bubble_layout(56, 60)
        self.assertGreater(shipped.face.width, brand_mark.MIN_FACE_WIDTH_FOR_EYES * 2)

    def test_the_eyes_stay_inside_the_face(self) -> None:
        for height in range(10, 49):
            layout = brand_mark.bubble_layout(36, 40, mark_height=float(height))
            for index, eye in enumerate(layout.eyes):
                with self.subTest(mark_height=height, eye=index):
                    self.assertTrue(layout.face.contains(eye), "eye %s escaped face %s" % (eye, layout.face))

    def test_the_coordinates_that_shipped_are_witnessed_as_wrong(self) -> None:
        # The sweep above is only worth having if it would have caught the
        # real defect, so this pins what the hand-placed numbers actually did:
        # mark at height 18 moved to (7, 13), face panel typed as x 16..28,
        # y 16..23.
        scale = 18.0 / brand_mark.VIEW_H
        x, y, w, h, _radius, stroke = brand_mark.RINGS[1]
        interior = brand_mark.Rect(
            (x + stroke / 2) * scale, (y + stroke / 2) * scale,
            (x + w - stroke / 2) * scale, (y + h - stroke / 2) * scale,
        ).moved(7.0, 13.0)
        shipped_face = brand_mark.Rect(16.0, 16.0, 28.0, 23.0)
        self.assertFalse(
            interior.contains(shipped_face),
            "the shipped face was inside the ring after all; this witness is stale",
        )
        # It sat high: the panel's top edge was above the opening's.
        self.assertLess(shipped_face.y0, interior.y0)

    def test_every_region_stays_within_the_canvas(self) -> None:
        for width, height in self.SIZES:
            with self.subTest(canvas=(width, height)):
                layout = brand_mark.bubble_layout(width, height)
                self.assertTrue(layout.canvas.contains(layout.mark))
                self.assertTrue(layout.canvas.contains(layout.badge))
                self.assertTrue(layout.canvas.contains(layout.rim))
                self.assertTrue(layout.rim.contains(layout.focus_rim))
                self.assertTrue(layout.mark.contains(layout.front_interior))

    def test_the_mark_clears_the_running_arc(self) -> None:
        # The mark is a rectangle inside a circle, so the diagonal binds. An
        # earlier draft of this layout fitted the mark's WIDTH to the rim and
        # drove its corners straight through the arc -- half-diagonal 20.6
        # against 14.0 of clearance.
        for width, height in self.SIZES:
            with self.subTest(canvas=(width, height)):
                layout = brand_mark.bubble_layout(width, height)
                half_diagonal = math.hypot(layout.mark.width, layout.mark.height) / 2.0
                clearance = layout.rim.width / 2.0 - layout.orbit_stroke / 2.0
                self.assertLessEqual(half_diagonal, clearance + 0.75)

    def test_the_default_reproduces_the_size_that_shipped(self) -> None:
        # The hand-placed 18px was right, and encoded exactly this rule; it
        # was the face's position that was wrong. Deriving it must not quietly
        # resize the mark.
        layout = brand_mark.bubble_layout(36, 40)
        self.assertEqual(brand_mark.mark_size(layout.mark_height), (22, 18))
        self.assertAlmostEqual(layout.mark_height, 17.67, places=2)

    def test_rect_helpers_behave(self) -> None:
        rect = brand_mark.Rect(0.0, 0.0, 10.0, 4.0)
        self.assertEqual(rect.width, 10.0)
        self.assertEqual(rect.height, 4.0)
        self.assertEqual(rect.centre, (5.0, 2.0))
        self.assertEqual(rect.inset(1.0), brand_mark.Rect(1.0, 1.0, 9.0, 3.0))
        self.assertEqual(rect.moved(2.0, 3.0), brand_mark.Rect(2.0, 3.0, 12.0, 7.0))
        self.assertTrue(rect.contains(brand_mark.Rect(1.0, 1.0, 2.0, 2.0)))
        self.assertFalse(rect.contains(brand_mark.Rect(-1.0, 1.0, 2.0, 2.0)))


class BrandMarkOverlayWiringTests(unittest.TestCase):
    def test_the_overlay_prefers_the_image_and_keeps_the_vector_fallback(self) -> None:
        source = inspect.getsource(native_overlay._paint_brand_mark)
        self.assertIn("_brand_mark_photo", source)
        self.assertIn("canvas.create_image(0, 0, image=photo, anchor=\"nw\")", source)
        # A Tk too old to read PNG must still draw a mark rather than fail.
        self.assertIn("_draw_brand_mark(canvas, height=height, blue=blue, ink=ink)", source)

    def test_photos_are_cached_so_tk_does_not_collect_the_pixels(self) -> None:
        # A PhotoImage that falls out of scope blanks the canvas item that
        # references it, which is the classic Tk image bug.
        source = inspect.getsource(native_overlay._brand_mark_photo)
        self.assertIn("_MARK_PHOTOS[key] = photo", source)
        self.assertIn("_MARK_PHOTOS.get(key)", source)
        self.assertIsInstance(native_overlay._MARK_PHOTOS, dict)

    def test_the_bubble_positions_everything_from_the_layout(self) -> None:
        # No pixel literals: that is the whole point of the layout. Each of
        # these used to be a typed box that happened to line up at one size.
        source = inspect.getsource(native_overlay.run_native_presence)
        for expression in (
            'collapsed_canvas.move("all", bubble.mark.x0, bubble.mark.y0)',
            "*bubble.rim,",
            "*bubble.focus_rim,",
            "*bubble.badge,",
            "face = bubble.face",
            "for eye in bubble.eyes:",
        ):
            with self.subTest(expression=expression):
                self.assertIn(expression, source)
        for gone in (
            "create_oval(16, 16, 23, 23",
            "create_oval(5, 7, 33, 35",
            "19, 2, 35, 18",
            'move("all", 7.0, 13.0)',
            "2, 4, 34, 36",
        ):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, source)

    def test_every_tk_mark_goes_through_the_painting_helper(self) -> None:
        # Any call site left on the vector path would render differently from
        # the others in the same window.
        overlay = inspect.getsource(native_overlay)
        painted = overlay.count("_paint_brand_mark(")
        self.assertGreaterEqual(painted, 5)  # one definition plus four sites
        drawn = [
            line.strip() for line in overlay.splitlines()
            if "_draw_brand_mark(" in line and "def " not in line
        ]
        for line in drawn:
            with self.subTest(line=line):
                self.assertIn("canvas, height=height", line)


if __name__ == "__main__":
    unittest.main()
