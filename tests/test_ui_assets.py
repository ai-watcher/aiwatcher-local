from __future__ import annotations

import inspect
import re
import unittest

from aiwatcher_cli import ui


NEXT_FUNCTION = chr(10) + "function "


def js_function_source(js, name):
    """The source of one JS function, from its definition to the next top-level
    declaration. Slicing on a bare newline-plus-"function" is not enough: a
    function followed by `let` or by `async function` would run past its own end
    and pick up matches from the rest of the file.
    """
    start = js.index("function %s(" % name)
    boundary = re.compile(r"^(?:async function |function |let |const |class )", re.M)
    match = boundary.search(js, js.index(chr(123), start))
    return js[start:match.start()] if match else js[start:]

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


class NavigationTest(unittest.TestCase):
    """Three views used to be reachable only from buttons inside other pages, and
    the sidebar highlighted a section you were not in when you landed on them."""

    # Coverage is reached from inside Settings and its content also lives there,
    # so it deliberately borrows that highlight rather than owning an entry.
    BORROWS_HIGHLIGHT = {"coverage": "setup"}

    @classmethod
    def setUpClass(cls):
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.views = set(re.findall(r'<section id="view-([\w-]+)"', cls.html))
        nav = re.search(r'<nav class="product-nav".*?</nav>', cls.html, re.S).group(0)
        cls.nav_views = set(re.findall(r'data-view="([\w-]+)"', nav))

    def test_every_view_is_reachable_from_the_nav(self):
        unreachable = self.views - self.nav_views - set(self.BORROWS_HIGHLIGHT)
        self.assertEqual(
            unreachable, set(),
            "these views have no nav entry, so they are reachable only from "
            "buttons inside other pages: %s" % sorted(unreachable))

    def test_nav_entries_all_point_at_real_views(self):
        self.assertEqual(self.nav_views - self.views, set())

    def test_the_highlight_does_not_lie(self):
        # Anything remapped here highlights a section other than the one you are
        # in; only the documented exception may do that.
        remap = re.search(r"const activeView = \(\{([^}]*)\}\)", self.js).group(1)
        remapped = set(re.findall(r"(\w+):", remap))
        self.assertEqual(remapped, set(self.BORROWS_HIGHLIGHT))

    def test_subviews_are_nested_not_promoted(self):
        # The top level stays six verb-named destinations; Projects and the
        # Changes ledger read as views within Watch.
        for view in ("projects", "changes"):
            with self.subTest(view=view):
                self.assertRegex(
                    self.html,
                    r'class="nav-tab nav-sub" data-view="%s"' % view)


class TrimmedHomeTest(unittest.TestCase):
    """Home was nine sections and 5.2 screens, and stated the same thing about
    context pressure in five of them. It is now the ambient surface, plus a
    receipt slot that stays hidden until you act."""

    # Each of these lived on Home and now lives somewhere it is not repeated.
    MOVED = {
        "Models and Tools": "view-insights",
        "Privacy at a glance": "view-setup",
        "Preflight decisions": "view-receipts",
    }

    @classmethod
    def setUpClass(cls):
        html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")
        cls.html = html
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        start = html.index('<section id="view-today"')
        cls.home = html[start:html.index('<section id="view-prompt"')]

    def test_home_is_the_ambient_surface(self):
        self.assertIn('id="ambient"', self.home)
        # The bubble is a receipt slot now: hidden until a Fresh Start is copied,
        # never an alert. The alert it used to carry is what the ambient surface
        # says once instead of five times.
        self.assertIn('id="handoffBubble"', self.home)
        self.assertIn("hidden", self.home)
        self.assertNotIn("renderHandoffBubble", self.js)

    def test_the_cut_sections_stay_cut(self):
        for heading in ("What needs attention", "Latest AI work",
                        "One thing worth changing", "Context health",
                        "Proof snapshot", "Spend leakage",
                        "Projects Driving AI Usage", "Recent sessions"):
            with self.subTest(section=heading):
                self.assertNotIn(heading, self.home)

    def test_moved_sections_landed_inside_their_new_view(self):
        # A section spliced between two views renders on every tab. This asserts
        # each one is inside the view that now owns it.
        for heading, view in self.MOVED.items():
            with self.subTest(section=heading):
                start = self.html.index('<section id="%s"' % view)
                nxt = self.html.find('<section id="view-', start + 1)
                end = nxt if nxt != -1 else len(self.html)   # view-setup is last
                self.assertIn(heading, self.html[start:end])

    def test_moved_sections_are_not_left_on_home(self):
        for heading in self.MOVED:
            with self.subTest(section=heading):
                self.assertNotIn(heading, self.home)

    def test_no_render_targets_a_deleted_element(self):
        # load() writes by id; a lookup with no element throws and stops the whole
        # dashboard rendering, which is the way this change could break quietly.
        built_at_runtime = {
            "evidencePanel", "handoffAcceptance", "handoffBrief", "handoffConstraints",
            "handoffObjective", "handoffSources", "handoffStatus", "handoffType",
            "optimizeReward", "outcomePanel", "promptBrief", "todayDigest",
        }
        ids = set(re.findall(r'id="([\w-]+)"', self.html))
        looked_up = set(re.findall(r"""getElementById\(['"]([\w-]+)['"]\)""", self.js))
        self.assertEqual(sorted(looked_up - ids - built_at_runtime), [])


class SessionDrawerTest(unittest.TestCase):
    """The drawer is a 619px column, so its order matters more than a full-width
    page's would: what is open is what gets read. Three things stay open -- who
    this session is, what needs doing, and the prompt worth tightening -- and the
    supporting evidence sits behind summaries."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        start = cls.js.index("document.getElementById('detailContent').innerHTML = `<div class=\"session-review-shell\">")
        cls.composition = cls.js[start:cls.js.index("`;", start)]

    def test_the_conclusion_comes_before_its_evidence(self):
        # renderVerdict says what to do and promptReview says why; the evidence
        # rail and the asks table are what those conclusions rest on.
        order = [self.composition.index(part) for part in
                 ("renderVerdict(s)", "promptReview", "renderEvidenceRail")]
        self.assertEqual(order, sorted(order),
                         "the drawer should reach its conclusion before its evidence")

    def test_supporting_sections_are_collapsed(self):
        for summary in ("Expensive asks", "Outcome evidence", "Evidence trail",
                        "What to check next", "Cost by event type"):
            with self.subTest(section=summary):
                self.assertIn("<summary>%s" % summary, self.js)

    def test_hero_does_not_restate_sections_below_it(self):
        # It used to carry Next step, API-equivalent and Return as a fact grid,
        # each of which has its own section immediately underneath.
        hero = self.js[self.js.index("function renderSessionHero("):]
        hero = hero[:hero.index("\nfunction ")]
        self.assertNotIn("session-hero-grid", hero)
        self.assertNotIn("Next step", hero)
        self.assertIn("session-hero-pressure", hero)

    def test_the_hero_number_is_named_for_what_it_measures(self):
        # It was labelled "Context pressure" while carrying the session's
        # cumulative token total, and the meter beside it compared that total
        # against a per-turn threshold -- so it read "critical" and sat full for
        # every real session. Context pressure means tokens per turn elsewhere in
        # the product, and the session payload has no per-turn figure.
        hero = js_function_source(self.js, "renderSessionHero")
        self.assertIn("<span>Tokens</span>", hero)
        self.assertNotIn("Context pressure", hero)
        self.assertNotIn("session-meter", hero)
        self.assertNotIn("function contextPressure(", self.js)

    def test_the_session_value_is_stated_once_above_the_fold(self):
        """It appeared three times above the fold. It now sits in the hero beside
        the token count -- the two are read together -- and nowhere else that is
        visible without opening something."""
        hero = js_function_source(self.js, "renderSessionHero")
        self.assertIn("s.api_value", hero)
        actions = js_function_source(self.js, "renderSessionActions")
        self.assertNotIn("API-equivalent", actions)
        self.assertNotIn("s.tokens_label", actions)

    def test_the_verdict_is_three_separate_judgements(self):
        """One verdict answered three questions at once with a single token
        threshold, so it could not say anything. They are now separate lines,
        because they become answerable at different times: room left is knowable
        now, cost when the session stops, worth only after its commits age."""
        verdict = js_function_source(self.js, "verdictLines")
        for key in ("'room'", "'cost'", "'worth'"):
            with self.subTest(line=key):
                self.assertIn("key: %s" % key, verdict)

    def test_opening_a_second_session_cannot_be_undone_by_the_first(self):
        """A retry scheduled for one session used to fire after you had opened
        another, overwriting the good render with the old one's loading message.
        Each selection claims a token; stale continuations stop writing."""
        source = js_function_source(self.js, "selectSession")
        self.assertIn("sessionSelectToken", source)
        self.assertEqual(source.count("if (!isCurrent()) return;"), 2)
        self.assertIn("if (isCurrent()) selectSession(sessionId, attempt + 1, token)", source)

    def test_a_retry_says_that_it_is_retrying(self):
        # Re-entering selectSession reset the message to "Loading session
        # identity", so five retries at a round trip each read as a freeze.
        source = js_function_source(self.js, "selectSession")
        self.assertIn("Still looking for this session", source)
        self.assertIn("SESSION_LOOKUP_ATTEMPTS", source)

    def test_a_non_answer_is_not_coloured_as_a_pass(self):
        """survivalLabel renders a string for every status, "unknown" included,
        so testing whether a label exists treated "the check could not tell" as
        "the work stuck" -- a green bar next to the word unknown. The four cases
        are distinct: survived, churned, checked-but-inconclusive, too-early."""
        verdict = js_function_source(self.js, "verdictLines")
        self.assertIn("survivalStatus(", verdict)
        self.assertNotIn("survivalLabel(", verdict)
        self.assertIn("could not tell whether the work stuck", verdict)
        for status in ("'survived'", "'churned'"):
            with self.subTest(status=status):
                self.assertIn(status, verdict)

    def test_an_unknown_line_does_not_condemn_the_others(self):
        # The worth line is unknowable for about a week. It must render as a
        # not-yet rather than suppress the two that are knowable now.
        verdict = js_function_source(self.js, "verdictLines")
        self.assertIn("judged after 7 days", verdict)
        self.assertIn("tone: 'unknown'", verdict)

    def test_the_broken_token_threshold_is_gone(self):
        # 500,000 was a per-turn figure applied to a session's cumulative total,
        # so it fired for 65% of real sessions and the bar sat at their 35th
        # percentile. Nothing in the drawer may judge on it again.
        self.assertNotIn("function sessionVerdict(", self.js)
        self.assertNotIn("tokens >= 500000", self.js)

    def test_replay_share_comes_from_spend_not_tokens(self):
        # The token-weighted reading is ~98% for every session because cache
        # reads dominate the count; weighted by what was billed it discriminates.
        server = inspect.getsource(ui._session_verdict_inputs)
        self.assertIn("bloat_ratio", server)
        self.assertIn("bloat_measurable", server)
        self.assertNotIn("replayed_share_pct", server)

    def test_the_chosen_threshold_is_marked_as_chosen(self):
        # It is picked, not derived. The comment saying so is the only thing
        # stopping it becoming another 500,000.
        self.assertTrue(hasattr(ui, "REPLAY_SHARE_HIGH_PCT"))
        source = inspect.getsource(ui)
        marker = source[source.index("REPLAY_SHARE_HIGH_PCT") - 500:source.index("REPLAY_SHARE_HIGH_PCT")]
        self.assertIn("Picked, not derived", marker)

    def test_nothing_is_only_reachable_by_scrolling_past_it(self):
        # Collapsing must not become hiding: every summary has a body.
        self.assertNotIn("<summary></summary>", self.js)


class PlanControlTest(unittest.TestCase):
    """The tab is for planning the next prompt. A housekeeping checklist had
    grown to 64% of it, sitting above the tool the tab is named for."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")

    def test_housekeeping_opens_on_demand(self):
        card = self.html[self.html.index('id="optimizeWorkspace"'):]
        card = card[:card.index("</section>")]
        self.assertIn("<details", card)
        self.assertIn("optimizeWorkspaceSummary", card)

    def test_the_summary_carries_the_count(self):
        # A collapsed card with a bare title hides whether there is anything in it.
        self.assertIn("to review)", self.js)

    def test_pending_fresh_starts_are_grouped(self):
        """Every pending decision became its own row, and they carry no rendered
        field that tells them apart -- same title, summary and evidence. Three
        identical rows in a list whose job is to say what to do next is worse
        than one row saying three."""
        source = inspect.getsource(ui._group_pending_fresh_starts)
        self.assertIn("fresh_start_pending", source)
        self.assertIn("decisions)", source)

    def test_an_unmeasured_impact_is_not_rendered(self):
        # "context at risk" with no number in front of it is a unit with no
        # value, and it rendered as a pill reading "review".
        server = inspect.getsource(ui.build_optimize_inventory)
        self.assertNotIn('else "context at risk"', server)
        self.assertNotIn("item.impact_label || 'review'", self.js)


if __name__ == "__main__":
    unittest.main()
