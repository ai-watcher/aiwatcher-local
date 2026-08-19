from __future__ import annotations

import re
import unittest

from aiwatcher_cli import ui


WEB_FILES = (
    "index.html",
    "index.css",
    "index.js",
    "overlay.html",
    "overlay.css",
    "overlay.js",
)


class WebAssetsTest(unittest.TestCase):
    """The front end lives in aiwatcher_cli/web/ and is spliced back together at
    import. These guard the two ways that can break quietly: a file missing from
    the package, and an include that never got substituted -- both of which serve
    a page that looks fine in the diff and is broken in the browser."""

    def test_every_asset_ships(self):
        for name in WEB_FILES:
            with self.subTest(asset=name):
                self.assertTrue(
                    (ui._WEB_DIR / name).is_file(),
                    "%s is missing from aiwatcher_cli/web/; check package-data in "
                    "pyproject.toml if this only fails from an installed wheel" % name,
                )

    def test_no_unresolved_includes(self):
        for label, document in (("HTML", ui.HTML), ("OVERLAY_HTML", ui.OVERLAY_HTML)):
            with self.subTest(document=label):
                self.assertNotIn("@@INCLUDE:", document)

    def test_documents_are_whole(self):
        for label, document in (("HTML", ui.HTML), ("OVERLAY_HTML", ui.OVERLAY_HTML)):
            with self.subTest(document=label):
                self.assertTrue(document.startswith("<!doctype html>"))
                self.assertTrue(document.rstrip().endswith("</html>"))

    def test_styles_and_scripts_are_inlined(self):
        # The dashboard is served as a single self-contained response: the CSS and
        # JS must arrive inside the document, not as separate requests.
        self.assertIn("<style>", ui.HTML)
        self.assertIn(":root {", ui.HTML)
        self.assertIn("<script>", ui.HTML)
        self.assertIn("function showView(", ui.HTML)
        self.assertNotIn("<link rel=\"stylesheet\"", ui.HTML)
        self.assertNotIn("<script src=", ui.HTML)

    def test_no_external_resources_are_loaded(self):
        # Local-only is the product's trust boundary. Guard the mechanisms that
        # would fetch something, rather than the string "http" -- the document
        # legitimately contains the SVG namespace URI, which is an identifier and
        # not an address anything is requested from.
        for pattern in ('<link rel="stylesheet"', "<script src=", "@import", "url(http"):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, ui.HTML)
                self.assertNotIn(pattern, ui.OVERLAY_HTML)

    def test_outbound_urls_are_the_known_two(self):
        # Anything new here is a deliberate decision about the trust boundary and
        # should have to be made in this test as well as in the markup.
        allowed = {
            "https://www.getaiwatcher.com",   # marketing link, user-initiated
            "http://www.w3.org/2000/svg",     # XML namespace, never fetched
        }
        found = set(re.findall(r'https?://[^\s"\'`<>)]+', ui.HTML))
        found |= set(re.findall(r'https?://[^\s"\'`<>)]+', ui.OVERLAY_HTML))
        self.assertEqual(found - allowed, set())

    def test_loader_rejects_paths(self):
        # @@INCLUDE:@@ names come from our own files, but the loader should not be
        # a way to read outside web/ if one is ever built from anything else.
        with self.assertRaises(Exception):
            ui._load_asset("../ui.py")


class LiveRefreshTest(unittest.TestCase):
    """The dashboard is meant to sit open in a tab, so it has to keep itself
    current. There is no JS runtime in this suite, so these assert the contract's
    shape -- the cadence, the listener, the tab hooks -- rather than its behaviour;
    the timing itself was exercised in a browser against nextRefreshDelay."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")

    def test_cadence_constants(self):
        # 10s visible is comfortably under one turn; 60s hidden is what browsers
        # throttle background timers to anyway, chosen rather than inherited.
        for name, value in (("REFRESH_VISIBLE_MS", "10000"),
                            ("REFRESH_HIDDEN_MS", "60000"),
                            ("REFRESH_CATCHUP_MS", "1800")):
            with self.subTest(constant=name):
                self.assertIn("const %s = %s;" % (name, value), self.js)

    def test_refreshes_immediately_when_the_tab_is_shown(self):
        # Without this the first thing you see after switching tabs is up to a
        # minute stale, which is exactly when being wrong costs the most.
        self.assertIn("visibilitychange", self.js)
        self.assertIn("document.hidden", self.js)

    def test_catchup_polling_is_bounded(self):
        # A flat catch-up interval meant a long evidence rebuild fired a request
        # every 1.8s for as long as it ran.
        self.assertIn("REFRESH_CATCHUP_FACTOR", self.js)
        self.assertNotIn("window.setTimeout(() => load(false, false), 1800)", self.js)

    def test_scheduled_loads_do_not_stack(self):
        self.assertIn("loadInFlight", self.js)

    def test_tab_carries_the_state(self):
        # The tab title and favicon are the surface for most of the working day.
        self.assertIn("document.title", self.js)
        self.assertIn('id="favicon"', self.html)
        self.assertIn("faviconFor", self.js)

    def test_favicon_is_self_contained(self):
        # A favicon fetched from anywhere would break the local-only guarantee.
        link = re.search(r'<link id="favicon"[^>]*href="([^"]*)"', self.html)
        self.assertIsNotNone(link, "favicon link element is missing")
        self.assertTrue(link.group(1).startswith("data:image/svg+xml,"))


class AmbientSurfaceTest(unittest.TestCase):
    """The ambient surface is the one screen the dashboard is meant to be glanced
    at. Its two states share five slots so the layout never reflows; these guard
    the parts of that contract visible in the source. The measured equality of the
    two states was checked in a browser against live data."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")

    def test_surface_exists(self):
        self.assertIn('id="ambient"', self.html)
        self.assertIn("function renderAmbient(", self.js)

    def test_both_states_are_built(self):
        self.assertIn("function ambientRunning(", self.js)
        self.assertIn("function ambientQuiet(", self.js)

    def _ambient_source(self):
        """Just the ambient renderers, so assertions here cannot be satisfied --
        or tripped -- by unrelated code elsewhere in the file."""
        start = self.js.index("function ambientRunning(")
        end = self.js.index("function renderAmbient(")
        return self.js[start:end]

    def test_thresholds_are_not_hardcoded(self):
        # They come from the same payload the runway chart uses, so the two
        # surfaces cannot disagree about where "act now" sits.
        source = self._ambient_source()
        self.assertIn("chart.pressure_tokens_n", source)
        self.assertIn("chart.critical_tokens_n", source)
        for literal in ("150000", "200000"):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, source)

    def test_meter_track_is_dynamic(self):
        # A session at 354k against a 200k limit would push its own fill off the
        # end of a fixed track.
        self.assertIn("trackMax", self.js)

    def test_no_headroom_claimed_once_past_the_threshold(self):
        # turns_to_critical is null when a session is already over; claiming
        # headroom there would be a lie the rest of the product does not tell.
        self.assertIn("turns_to_critical", self.js)
        self.assertIn("no headroom left to project", self.js)

    def test_dom_is_not_rewritten_when_nothing_changed(self):
        # It re-renders every 10s. Rewriting unconditionally would drop focus from
        # the buttons and repaint for no reason.
        self.assertIn("ambientMarkup", self.js)
        self.assertIn("if (markup === ambientMarkup) return;", self.js)

    def test_slots_are_styled_on_the_container(self):
        for rule in (".ambient-hero", ".ambient-meter", ".ambient-say",
                     ".ambient-acts", ".ambient-facts"):
            with self.subTest(rule=rule):
                self.assertIn(rule, self.css)


if __name__ == "__main__":
    unittest.main()
