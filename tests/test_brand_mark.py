import inspect
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

    def test_the_geometry_matches_the_svg_the_vector_fallback_draws(self) -> None:
        # Both paths draw the same artwork, and nothing generates one from the
        # other, so the numbers have to be checked against each other by hand.
        # The SVG states centerlines; _draw_brand_mark states outer boxes.
        back, front = brand_mark.RINGS
        self.assertEqual(back[:4], (20.0, 20.0, 260.0, 220.0))
        self.assertEqual(front[:4], (149.0, 137.0, 260.0, 192.0))
        for ring in (back, front):
            radius, stroke = ring[4], ring[5]
            self.assertEqual(stroke, 40.0)
            self.assertEqual(radius + stroke / 2, 85.0)  # the outer corner radius
        source = inspect.getsource(native_overlay._draw_brand_mark)
        self.assertIn("ring(0.0, 0.0, 300.0 * s, 260.0 * s, blue)", source)
        self.assertIn("ring(129.0 * s, 117.0 * s, 300.0 * s, 232.0 * s, ink)", source)


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
