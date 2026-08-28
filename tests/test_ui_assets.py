from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

BS = chr(92)
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

    def test_the_sentence_slot_is_reserved_so_the_states_cannot_reflow(self):
        """The one slot whose height followed its own text.

        Every other slot on this surface is fixed height, which is what lets the
        running and quiet states swap without the page shifting under a reader
        who is not looking. .ambient-say was not: measured at 1280x800 against
        real data, the running sentence wrapped to 42px and the quiet one fit in
        21px, so the surface lost a line the moment a session went quiet. With
        two lines reserved both states measure 321px at 1280x800, 659px at
        1280x1280 and 316px at 1024x600, slot for slot.
        """
        rule = self.css[self.css.index("    .ambient-say {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("min-height:", rule)

    def test_the_context_row_is_reserved_so_the_states_cannot_reflow(self):
        """The same defect as the sentence slot, one row up.

        The context row carries different amounts of text in the two states:
        the running one names the project, the tool and a live count, the quiet
        one is shorter. Measured against real local data at 12px with a 16px
        gap, laid out on one line the row needs 429px quiet, 458px running with
        a short path, and 667px running with a long one -- against a container
        that is 946px inside a 1280px window. Between roughly 1001px and 763px
        of window the running state has wrapped and the quiet state has not, so
        the surface moves by a line exactly when a session starts or stops.

        That band is ordinary working width for a surface that shares a screen
        with an editor, which is why this is reserved rather than left to the
        text. Two lines covers every case measured, including 28px against 14px
        at 560px.
        """
        rule = self.css[self.css.index("    .ambient-top {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("min-height:", rule)

    def test_the_bloat_sentence_says_whose_spend_it_is(self):
        """Instance 7 of the recurring defect, pinned so it cannot come back.

        `bloat_label` is one session's ratio -- ui.py builds it from
        health.bloat_ratio, the representative session -- while
        `replayed_cost_label` beside it is a project total, summed across the
        whole group in _context_health_card. Printed together under a hero that
        is also one session, "86% of what it has cost went on re-sending
        history, $791.49 so far" read as one measurement of one thing; on real
        data the percentage covered one session and the dollars covered
        thirteen.

        The percentage stays and says whose it is. The project-wide dollar is
        not relabelled, it leaves -- it is already in the facts row as "on
        replay", and one sentence cannot carry both scopes honestly.
        """
        source = self._ambient_source()
        # Just the sentence expression. Scoping to the whole renderer would trip
        # on the comment above it, which quotes the old wording to explain it,
        # and on the facts row, where the project-wide dollar legitimately still
        # appears under its own "on replay" label.
        start = source.index("const bloat =")
        sentence = source[start:source.index(";", start)]

        self.assertIn("of this session's spend", sentence)
        # The unscoped wording is what let one session's percentage and the
        # project's dollars read as one measurement.
        self.assertNotIn("of what it has cost", sentence)
        # The project total must not come back into this sentence. One sentence,
        # one scope -- relabelling the pair with the larger scope would make the
        # percentage wrong instead, which is two instances of the defect rather
        # than none.
        self.assertNotIn("replayed_cost_label", sentence)

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

    # Every view now owns its nav entry. Coverage used to borrow this one: it was
    # a separate page duplicating 36 of its 38 sentences from Settings, and its
    # only way in was a button inside Settings pointing at content two lines
    # further down the same page.
    BORROWS_HIGHLIGHT: dict[str, str] = {}

    @classmethod
    def setUpClass(cls):
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.views = set(re.findall(r'<section id="view-([\w-]+)"', cls.html))
        nav = re.search(r'<nav class="product-nav".*?</nav>', cls.html, re.S).group(0)
        # Kept whole as well as as a set: order and nesting carry meaning here,
        # and the loop-contiguity test needs both.
        cls.nav_html = nav
        cls.nav_views = set(re.findall(r'data-view="([\w-]+)"', nav))

    def test_every_view_is_reachable_from_the_nav(self):
        unreachable = self.views - self.nav_views - set(self.BORROWS_HIGHLIGHT)
        self.assertEqual(
            unreachable, set(),
            "these views have no nav entry, so they are reachable only from "
            "buttons inside other pages: %s" % sorted(unreachable))

    def test_nav_entries_all_point_at_real_views(self):
        self.assertEqual(self.nav_views - self.views, set())

    def test_every_deep_link_target_exists_in_the_markup(self):
        """`?view=x#target` links are built in ui.py and the targets live in
        index.html, and nothing connected the two.

        #contextHealth was emitted by seven links -- Companion nudge
        primary_urls, control_url and watch_url -- against an element that was
        called sessionContextHealth. It had never resolved. A rename or a moved
        card breaks these silently, because the link still opens the right page
        and simply fails to go anywhere on it.
        """
        emitted = set(re.findall(r'"/\?view=[\w-]+#([\w-]+)"', inspect.getsource(ui)))
        self.assertTrue(emitted, "no deep links found -- has the URL shape changed?")
        # Comments stripped first. A comment explaining a target mentions the id
        # in full, which would satisfy a plain substring check and let the real
        # attribute be renamed out from under these links -- the exact bug this
        # test exists for, passing because someone documented it.
        markup = re.sub(r"<!--.*?-->", "", self.html, flags=re.S)
        missing = {t for t in emitted if 'id="%s"' % t not in markup}
        self.assertEqual(
            missing, set(),
            "ui.py links to these anchors and no element carries them: %s"
            % sorted(missing))

    def test_deep_link_targets_are_resolved_in_js_not_by_the_browser(self):
        """Views are hidden at load, so a native anchor jump finds nothing and
        gives up before showView reveals the target. Every target has to be
        resolved after the view is shown -- generically, because the one
        hand-written branch that did this was the only anchor that worked."""
        source = self.js[self.js.index("location.hash ?"):]
        source = source[:source.index("\n  }") + 4]
        self.assertIn("closest('.view')", source)
        self.assertIn("scrollIntoView", source)
        # A per-target branch is the thing this replaced.
        self.assertNotIn("location.hash === '#", self.js)

    def test_deep_link_allowlist_covers_every_view(self):
        """`?view=<id>` is filtered against a hardcoded list before showView is
        called, so a view missing from it is reachable by clicking and silently
        unreachable by link -- which is how a nudge or a docs link becomes a
        no-op that looks like a working URL. Control shipped in exactly that
        state for one commit.
        """
        allowed = set(re.findall(
            r"'([\w-]+)'",
            re.search(r"\[([^\]]*)\]\.includes\(requestedView\)", self.js).group(1)))
        self.assertEqual(
            self.views - allowed, set(),
            "these views cannot be reached by ?view= link: %s"
            % sorted(self.views - allowed))

    def test_the_highlight_does_not_lie(self):
        # Anything remapped here highlights a section other than the one you are
        # in; only the documented exception may do that.
        remap = re.search(r"const activeView = ([^;]*);", self.js).group(1).strip()
        if remap == "view":
            self.assertEqual(self.BORROWS_HIGHLIGHT, {})
            return
        remapped = set(re.findall(r"(\w+):", remap))
        self.assertEqual(remapped, set(self.BORROWS_HIGHLIGHT))

    def test_the_changes_ledger_is_nested_under_what_it_evidences(self):
        # Per-commit cost and survival is the drill-down behind Prove's claim,
        # so it reads as a view within Prove rather than a destination of its
        # own. It used to nest under Watch, which put a retrospective table
        # inside a live-triage stage.
        self.assertRegex(
            self.html, r'class="nav-tab nav-sub" data-view="changes"')

    def test_projects_is_top_level_not_nested(self):
        # Projects answers "where is the spend going" -- a lens you drop into
        # from anywhere, not a stage you pass through. Nested under Watch it
        # also sat *inside* the loop sequence; see the contiguity test below,
        # which is the real reason this moved.
        self.assertRegex(self.html, r'class="nav-tab" data-view="projects"')
        self.assertNotRegex(
            self.html, r'class="nav-tab nav-sub" data-view="projects"')

    def test_the_loop_stages_are_contiguous_in_the_nav(self):
        """The five stages the README defines are the product's spine and its
        vocabulary, so the nav must not interleave anything else with them.

        Projects and the Changes ledger used to sit between Watch and Prove,
        breaking the sequence a reader is meant to internalise. Control had no
        entry at all, so the stage where the product actually acts was
        reachable only from buttons inside other pages.
        """
        order = [m for m in re.findall(r'data-view="([\w-]+)"', self.nav_html)]
        stages = ["prompt", "watch", "control", "receipts", "insights"]
        positions = [order.index(s) for s in stages]

        self.assertEqual(
            positions, sorted(positions),
            "the loop stages are out of order in the nav: %s" % order)

        # Sub-views hang off their parent stage and do not break the run; any
        # other top-level entry between two stages does.
        subs = set(re.findall(
            r'class="nav-tab nav-sub" data-view="([\w-]+)"', self.nav_html))
        between = [
            v for v in order[positions[0]:positions[-1] + 1]
            if v not in stages and v not in subs
        ]
        self.assertEqual(
            between, [],
            "these sit inside the loop sequence without being stages: %s" % between)


class TrimmedHomeTest(unittest.TestCase):
    """Home was nine sections and 5.2 screens, and stated the same thing about
    context pressure in five of them. It is now the ambient surface, plus a
    receipt slot that stays hidden until you act."""

    # Each of these lived on Home and now lives somewhere it is not repeated.
    MOVED = {
        "Models and Tools": "view-insights",
        "Privacy at a glance": "view-setup",
        # Lowercase because it is no longer a tile label: the count folded into
        # the Fresh Start receipts subtitle, where it reads as a sentence
        # ("6 preflight decisions, last 7 days") rather than a floating card.
        "preflight decisions": "view-receipts",
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
            "optimizeReward", "outcomePanel", "planDerivedZone", "promptBrief",
            "todayDigest",
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
        start = cls.js.index("setDrawerContent(`<div class=\"session-review-shell\">")
        cls.composition = cls.js[start:cls.js.index("`);", start)]

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

    def test_no_fixed_line_decides_whether_replay_is_high(self):
        # The 60% constant that used to live here fired for 47% of the local
        # corpus and sat within a point of the owner's own median, so it called
        # a typical session expensive. Replaced by a comparison against the
        # owner's own sessions, the way pace_vs_baseline works.
        self.assertFalse(hasattr(ui, "REPLAY_SHARE_HIGH_PCT"))
        server = inspect.getsource(ui._session_verdict_inputs)
        self.assertIn("replay_share_vs_baseline", server)
        # No baseline must not collapse into "fine".
        self.assertNotIn("threshold_pct", self.js)
        self.assertIn("c.available", self.js)

    def test_nothing_is_only_reachable_by_scrolling_past_it(self):
        # Collapsing must not become hiding: every summary has a body.
        self.assertNotIn("<summary></summary>", self.js)


class DrawerArrivalTest(unittest.TestCase):
    """A drawer populates in up to three writes -- a loading line, a fast
    summary, then full evidence 0.2s to 4.5s later -- so content has to resolve
    rather than jump. The hero is byte-identical across the summary -> detail
    swap, so fading the whole node would blink content that had already
    settled."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")

    def test_every_drawer_write_goes_through_the_helper(self):
        # A raw assignment would skip the diff and animate nothing, which is the
        # jump this replaced.
        self.assertNotIn("detailContent').innerHTML =", self.js)
        self.assertGreater(self.js.count("setDrawerContent("), 10)

    def test_only_blocks_that_changed_are_animated(self):
        helper = self.js[self.js.index("function setDrawerContent("):]
        helper = helper[:helper.index(chr(10) + "}")]
        self.assertIn("settled.has(el.outerHTML)", helper)
        # Recorded before the class lands, or the next write compares against
        # markup carrying a leftover .aiw-arrive and re-animates everything.
        self.assertLess(helper.index("node.aiwSettled = new Set("),
                        helper.index("classList.add('aiw-arrive')"))

    def test_reopening_a_drawer_resolves_again(self):
        opener = self.js[self.js.index("function openDrawer("):]
        self.assertIn("aiwSettled = null", opener[:opener.index(chr(10) + "}")])

    def test_a_write_that_lands_while_hidden_is_not_parked_invisible(self):
        # The animation fills backwards, so a pending one holds opacity 0 and
        # this page spends most of its life hidden on a second monitor.
        helper = self.js[self.js.index("function setDrawerContent("):]
        helper = helper[:helper.index(chr(10) + "}")]
        self.assertIn("document.hidden", helper)
        self.assertLess(helper.index("document.hidden"),
                        helper.index("classList.add('aiw-arrive')"))

    def test_the_arrival_is_short_and_respects_reduced_motion(self):
        self.assertIn("@keyframes aiwArrive", self.css)
        self.assertIn(".aiw-arrive { animation: aiwArrive .18s", self.css)
        # Covered by the global reduce block rather than a rule of its own.
        reduce = self.css[self.css.index("@media (prefers-reduced-motion: reduce)"):]
        self.assertIn("animation-duration: .001ms !important", reduce[:400])


class HomeStatRowTest(unittest.TestCase):
    """The stat row is meant to be read across, not card by card.

    It used to align without comparing: one card carried two metrics and two
    supporting lines and the other two carried one each, so the row was 216px
    tall because of one card and the other two ended 53px short of it. Equal
    boxes, unequal content, nothing lining up to compare.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")
        start = cls.html.index('class="grid kpis"')
        cls.row = cls.html[start:cls.html.index("</section>", start)]

    def test_one_supporting_line_per_card(self):
        # Two cards' worth of supporting copy in one card is what set the row's
        # height; the reserved min-height is what makes the other cards agree
        # with it rather than stopping short.
        for card in self.row.split('<div class="card metric-card')[1:]:
            with self.subTest(card=card[:60]):
                self.assertEqual(card.count('class="sub"'), 1)
        self.assertIn(".metric-card .sub { min-height:", self.css)

    def test_cost_per_surviving_line_has_its_own_card(self):
        self.assertIn('<div class="label">Cost per surviving line</div>', self.row)
        # Not a sentence inside the Useful outcomes card any more.
        self.assertNotIn("per surviving line —", self.js)

    def test_every_card_ends_in_the_same_place(self):
        # Two of the four tiles caveat their sparkline and two do not, which left
        # those two 17px short. The caveat line is emitted either way.
        self.assertIn("series.caveat || ''", self.js)
        self.assertIn(".metric-spark-caveat {", self.css)
        caveat = self.css[self.css.index(".metric-spark-caveat {"):]
        self.assertIn("min-height:", caveat[:caveat.index("}")])

    def test_unmeasured_survival_keeps_its_slot(self):
        # Q5 of the number rule: unmeasurable is a distinct state and has to be
        # shown as unmeasurable with the reason, without reflowing a row that
        # sits on an ambient surface.
        tile = self.js[self.js.index("function renderSurvivalTile("):]
        tile = tile[:tile.index(chr(10) + "}")]
        self.assertIn("survival.reason", tile)
        self.assertNotIn("hidden = true", tile)
        reason = self.css[self.css.index(".survival-reason {"):]
        reason = reason[:reason.index("}")]
        # 45px is the meter slot's height, so both states measure the same.
        self.assertIn("height: 45px", reason)
        self.assertIn("overflow: hidden", reason)

    def test_survival_is_a_meter_not_a_trend(self):
        # A trend would have to be drawn over the date picker's window, and
        # survival ignores the picker -- that is a different question from the
        # one the number answers.
        self.assertIn("function drawSurvivalMeter(", self.js)
        self.assertNotIn('data-tile-spark="costPerSurviving"', self.html)
        self.assertIn("not the selected range", self.js)


class HorizontalCompositionTest(unittest.TestCase):
    """Screens that compose instead of stacking.

    Every view was one column of full-width cards: at 1492x945 the content
    column is 1189px and --w-prose caps a paragraph at 68ch, so roughly 700px
    sat empty beside every line of text while the page grew downwards. Watch was
    1592px tall there -- a 583px health card above a 995px table, never visible
    at the same time.
    """

    @classmethod
    def setUpClass(cls):
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")

    def test_watch_is_one_zone_now_that_the_table_has_left(self):
        """The two-zone shell put health beside the work table on the reasoning
        that "what is happening right now" belongs next to "the work it is
        happening to". Composing beat stacking, and that was right for a view
        holding both.

        It holds one thing now. Searching sessions is a sit-down task -- its
        search can fall back to a per-session git lookup taking seconds -- and
        it is a whole view under Projects, so Watch is triage alone and there is
        no second zone to compose with. The grid and its 1300px breakpoint
        existed to make two real columns; keeping them for one card would
        reserve a column for something that cannot arrive.
        """
        watch = self.html[self.html.index('id="view-watch"'):]
        watch = watch[:watch.index("</section>")]
        self.assertNotIn('<div class="watch-shell">', watch)
        self.assertEqual(watch.count('<div class="card"'), 1)
        self.assertIn('id="sessionContextHealth"', watch)
        # The rule, not the name: the comment left in its place still says what
        # was there and why it went, which is worth keeping.
        self.assertNotIn(".watch-shell {", self.css)

    def test_the_facts_strip_can_only_break_between_scopes(self):
        # Item 5 removed a 68ch prose cap that broke this data strip mid-phrase,
        # leaving "session . 1 critical" on a line of its own. A narrower column
        # brings that pressure back, so the two scopes are separate runs that
        # cannot split internally -- true at any width, rather than true only
        # while the dollar figures in it stay short.
        facts = js_function_source(self.js, "healthFacts")
        self.assertIn("filter(Boolean);", facts)
        self.assertNotIn("filter(Boolean).join('  ", facts)
        rule = self.css[self.css.index(".health-facts > span {"):]
        self.assertIn("white-space: nowrap", rule[:rule.index("}")])
        container = self.css[self.css.index("    .health-facts { margin: 0;"):]
        container = container[:container.index("}")]
        self.assertIn("flex-wrap: wrap", container)

    def test_watch_carries_no_stray_inline_margin(self):
        # The health card once carried an inline margin-bottom, which added a
        # stray 14px under the left column when the two zones sat side by side.
        # The zones are gone; the inline margin should not come back with a
        # second card, so this outlives the grid it was written for.
        watch = self.html[self.html.index('id="view-watch"'):]
        watch = watch[:watch.index("</section>")]
        self.assertNotIn("margin-bottom:14px", watch)


class InsightEvidencePairingTest(unittest.TestCase):
    """An insight's claim beside the chart that evidences it.

    The claim is prose and stops at 68ch, so at 1492x945 it used 513px of a
    1151px row and the chart sat underneath it -- about 600px of every row
    empty while the feed grew downwards.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")

    def test_the_claim_and_its_evidence_are_separate_containers(self):
        # Grid auto-placement cannot put a title, a paragraph and a chart into
        # two columns in the right order, so the two halves are wrapped.
        feed = js_function_source(self.js, "renderInsightFeed")
        self.assertIn('class="feed-says"', feed)
        self.assertIn('class="feed-shows"', feed)
        # Only rows that actually carry a chart become two columns.
        self.assertIn("card.chart ? ' has-evidence' : ''", feed)

    def test_the_evidence_column_never_shrinks_the_chart_below_its_canvas(self):
        # These two charts are a fixed 640-unit viewBox scaled by CSS, unlike
        # the trend chart on Watch which redraws at its measured width. So the
        # column width sets the type size: under 640px, 11px axis labels render
        # smaller than the smallest size in the scale.
        rule = self.css[self.css.index(".feed-main.has-evidence {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("minmax(0, 640px)", rule)
        for name in ("drawDailySpend", "drawReplaySplit"):
            with self.subTest(chart=name):
                self.assertIn("const W = 640", js_function_source(self.js, name))


class SecondOpinionZoneTest(unittest.TestCase):
    """Zone A, once there is something to put in it.

    Every heading maps to one schema field, so a reader can see the block was
    extracted rather than composed. The zone had to stay honest while empty for
    a whole milestone before this; it does not get to stop now that it is full.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")
        cls.render = js_function_source(cls.js, "renderSecondOpinion")

    def test_every_heading_is_a_schema_field(self):
        for heading, field in (
            ("What this asks for", "a.outcome"),
            ("Done when", "a.success_check"),
            ("Likely in scope", "a.scope_paths"),
            ("Could not locate", "a.unresolved_nouns"),
            ("Requested removals", "removals"),
            ("Worth deciding before you send", "a.ambiguities"),
            ("First checkpoint", "a.first_checkpoint"),
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, self.render)
                self.assertIn(field, self.render)

    def test_removals_render_above_the_guardrails(self):
        # Spec 4.1. A bullet telling the agent not to expand into cleanup must
        # never sit above the deletion the prompt actually asked for.
        plan = self.js[self.js.index("function renderDerivedZone("):
                       self.js.index("async function preflightPrompt(")]
        self.assertIn("Requested removals", plan)
        body = self.js[self.js.index("async function preflightPrompt("):]
        body = body[:body.index(chr(10) + "}")]
        self.assertLess(body.index("renderDerivedZone(data)"), body.index("renderGuardrailZone(data)"))

    def test_an_empty_field_does_not_become_an_empty_heading(self):
        # Spec 4: omit the heading entirely when there is nothing under it.
        for guard in ("(a.scope_paths || []).length",
                      "(a.unresolved_nouns || []).length",
                      "(a.ambiguities || []).length"):
            with self.subTest(guard=guard):
                self.assertIn(guard, self.render)

    def test_the_run_is_priced_where_it_was_paid_for(self):
        # Spec 5: on the screen, not buried in Settings.
        self.assertIn("secondOpinionCost", self.render)
        cost = js_function_source(self.js, "secondOpinionCost")
        self.assertIn("cost_usd", cost)
        self.assertIn("tokens", cost)
        self.assertIn(".second-opinion-cost", self.css)

    def test_the_confidence_chip_never_claims_observation(self):
        # Zone B's word for something read off this machine is "observed".
        # Everything in zone A came from a model, so it may not borrow it.
        self.assertNotIn("'observed'", self.render)
        self.assertIn("'inferred'", self.render)
        self.assertIn("'unknown'", self.render)

    def test_stage_two_never_blocks_stage_one(self):
        # Spec 4.2: zones B and C are complete and usable throughout. A real
        # analyst run takes about 30s, so it is a separate request made after
        # the first result is already on screen.
        body = self.js[self.js.index("async function preflightPrompt("):]
        body = body[:body.index(chr(10) + "}")]
        self.assertLess(body.index("resultNode.innerHTML = `${renderPlanAction"),
                        body.index("loadSecondOpinion(prompt, tool, cwd)"))
        self.assertIn("/api/second-opinion", self.js)

    def test_the_gate_decides_the_spend_not_the_click(self):
        body = self.js[self.js.index("async function preflightPrompt("):]
        body = body[:body.index(chr(10) + "}")]
        self.assertIn("(data.second_opinion || {}).pending", body)


class PlanControlTest(unittest.TestCase):
    """Plan is for planning the next prompt. A housekeeping checklist had grown
    to 64% of it, sitting above the tool the tab is named for.

    Collapsing it treated the symptom. The queue is on Control now -- clearing
    stale chats and worktrees is not preparation for a prompt, it is the acting
    the loop's third stage is named for, and the only reason it lived on Plan
    is that Plan was the nearest surface that existed.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")

    def _view(self, name, nxt):
        start = self.html.index('<section id="view-%s"' % name)
        return self.html[start:self.html.index('<section id="view-%s"' % nxt)]

    def test_the_queue_is_on_control_not_plan(self):
        self.assertIn('id="optimizeWorkspace"', self._view("control", "receipts"))
        self.assertNotIn('id="optimizeWorkspace"', self._view("prompt", "projects"))

    def test_the_queue_is_not_collapsed_where_it_belongs(self):
        """It was folded behind a summary for competing with the prompt tool.
        On Control there is nothing to compete with, so hiding a queue of things
        to do behind a click would be inherited caution rather than a reason."""
        card = self.html[self.html.index('id="optimizeWorkspace"'):]
        card = card[:card.index("</section>")]
        self.assertNotIn("<details", card)
        self.assertIn("optimizeWorkspaceSummary", card)

    def test_the_deep_link_points_at_the_view_that_holds_it(self):
        """The Companion nudge's Review button and older ask answers both link
        to #optimizeWorkspace. Moving the section without moving the links is
        how a nudge's only action becomes a no-op.

        How the anchor is resolved is not this test's business -- that is
        generic now, and NavigationTest owns it. This owns the pairing: the link
        names control, and control is where the target lives.
        """
        self.assertIn("view=control#optimizeWorkspace", inspect.getsource(ui))
        self.assertNotIn("view=prompt#optimizeWorkspace", inspect.getsource(ui))
        control = self.html[self.html.index('<section id="view-control"'):]
        control = control[:control.index('<section id="view-receipts"')]
        self.assertIn('id="optimizeWorkspace"', control)

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


class SettingsTest(unittest.TestCase):
    """Settings was 4.2 screens, most of it a nine-tool table and an eleven-step
    guide -- reference material you consult rather than read. It also embedded a
    full copy of a separate Coverage view."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")

    def test_the_duplicate_coverage_view_is_gone(self):
        # 36 of its 38 sentences were already in Settings, and its only entry
        # point was a button inside Settings pointing at content two lines
        # further down the same page.
        self.assertNotIn('id="view-coverage"', self.html)
        self.assertNotIn('id="coverageRows"', self.html)
        self.assertNotIn("showView('coverage')", self.html)
        self.assertNotIn("showView('coverage')", self.js)

    def test_reference_material_opens_on_demand(self):
        for anchor in ("coverageSummary", "setupSummary"):
            with self.subTest(section=anchor):
                self.assertIn(anchor, self.html)
                self.assertIn(anchor, self.js)

    def test_summaries_count_what_the_payload_actually_carries(self):
        """Guessed field names reported "0 of 10 gated" and "0 of 11 done" --
        both wrong, and the second doubly so: setup steps are recommended or
        optional and carry no completion state at all, so nothing could ever be
        counted done."""
        self.assertIn("row.status === 'automatic'", self.js)
        self.assertIn("step.status === 'recommended'", self.js)
        self.assertNotIn("step.status === 'done'", self.js)
        self.assertNotIn("tools gated)", self.js)

    def test_the_setup_list_is_not_called_a_checklist(self):
        # Nothing tracks completion, so calling it a checklist promises a state
        # the data does not have.
        self.assertNotIn("Setup checklist", self.html)


class ChangesLedgerTest(unittest.TestCase):
    """The ledger's widest column held a value identical on 47 of 49 rows, and
    two more columns are structurally empty in the default window because
    survival needs a commit to be seven days old before it means anything."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")

    def test_empty_survival_says_why(self):
        """A bare dash reads as missing data. In a seven-day window every commit
        is younger than survival.MIN_AGE_DAYS, so all 49 cells were dashes and
        the column looked broken rather than young -- it fooled the audit."""
        rows = js_function_source(self.js, "renderChangeRows")
        self.assertIn("tooYoungToJudge(row)", rows)
        self.assertIn("too new", rows)

    def test_the_age_rule_matches_the_server(self):
        # If these drift, the table explains an emptiness the server did not
        # cause. survival.py owns the real number.
        from aiwatcher_cli import survival
        self.assertIn(
            "const SURVIVAL_MIN_AGE_DAYS = %d;" % survival.MIN_AGE_DAYS, self.js)

    def test_the_project_column_is_a_name_not_a_path(self):
        # It was the widest column in the table, holding a left-truncated path
        # that was the same on nearly every row.
        rows = js_function_source(self.js, "renderChangeRows")
        self.assertIn("repoLabel(row)", rows)
        self.assertNotIn("esc(row.project)}</td>", rows)

    def test_coverage_caveats_sit_on_the_counts_they_qualify(self):
        """Two sentences under the table were the only way to learn it covers
        about two thirds of what happened. They are counts, so they belong on
        the counts."""
        totals = js_function_source(self.js, "renderChangeTotals")
        self.assertIn("by other authors, excluded", totals)
        self.assertIn("has no commit to attach to", totals)
        self.assertNotIn("receipt-note", totals)


class WatchTest(unittest.TestCase):
    """Watch drew four project cards of equal weight, each with four stat tiles
    and its own chart scaled to its own peak."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")

    def test_position_and_shape_are_separate_marks(self):
        """One chart could not do both. Forcing a common y-axis so the limit sat
        on one row -- the point of a shared scale -- flattened three of four
        series to under 5px of travel, because a project 690k deep barely moves
        in relative terms. Position went to a meter on a shared scale; shape kept
        its own."""
        self.assertIn("function drawMeter(", self.js)
        self.assertIn("function drawTrend(", self.js)
        meter = js_function_source(self.js, "drawMeter")
        # the meter's track is the limit, not this project's peak, or it would
        # not be comparable with the card above it
        self.assertIn("critical * 1.25", meter)
        trend = js_function_source(self.js, "drawTrend")
        self.assertIn("Math.min(...series)", trend)

    def test_the_worst_project_leads(self):
        rank = js_function_source(self.js, "healthRank")
        self.assertIn("latest >= critical", rank)
        self.assertIn("healthLeadCard", self.js)
        self.assertIn("healthQuietRow", self.js)

    def test_the_verdict_comes_before_the_marks(self):
        # Same rule as the session drawer: reach the conclusion, then show the
        # evidence for it.
        card = js_function_source(self.js, "healthLeadCard")
        self.assertLess(card.index("health-verdict"), card.index("meter-host"))
        self.assertLess(card.index("meter-host"), card.index("trend-host"))

    def test_the_superseded_chart_is_gone(self):
        # drawRunway, its legend and its caption had no host left once the cards
        # stopped emitting data-runway.
        for name in ("function drawRunway(", "function runwayLegend(",
                     "function runwayCaption(", "drawRunwayMini"):
            with self.subTest(symbol=name):
                self.assertNotIn(name, self.js)
        # runwayVerdict survives: the lead card still states the deadline.
        self.assertIn("function runwayVerdict(", self.js)

    def test_projects_are_named_not_pathed(self):
        self.assertIn("function healthProjectName(", self.js)
        card = js_function_source(self.js, "healthLeadCard")
        self.assertIn("healthProjectName(row)", card)


class WindowSummaryTest(unittest.TestCase):
    """The standing summary of the window, and the surface it belongs on.

    It was cut from Home once on the grounds that each number appears elsewhere
    -- true, and beside the point: four numbers in four places is not four
    numbers in one glance. That objection is still right and these tests still
    enforce it; what changed is which surface they are glanced at on.

    Home answers "is something happening right now that I should deal with".
    Cost per useful change and cost per surviving line answer "was the spend
    worth it" -- a question asked at the end of a period, not mid-task with an
    editor open. On Prove they are also the claim the receipt tables below are
    evidence for; while they sat on Home, that surface had figures with no
    backing and Prove had backing with no figures.
    """

    TILES = ("usefulOutcomes", "costPerSurviving", "sessions", "apiValue")

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")
        start = cls.html.index('<section id="view-today"')
        cls.home = cls.html[start:cls.html.index('<section id="view-prompt"')]
        start = cls.html.index('<section id="view-receipts"')
        cls.prove = cls.html[start:cls.html.index('<section id="view-insights"')]

    def test_the_window_summary_is_on_prove(self):
        for tile in self.TILES:
            with self.subTest(tile=tile):
                self.assertIn('id="%s"' % tile, self.prove)

    def test_home_does_not_keep_a_copy(self):
        # Leaving them on both surfaces would be the worst of the two layouts:
        # the same four figures twice, and Home still answering a question it
        # is not for.
        for tile in self.TILES:
            with self.subTest(tile=tile):
                self.assertNotIn('id="%s"' % tile, self.home)

    def test_they_stay_in_one_row_wherever_they_live(self):
        """The reason they were restored in the first place.

        Split across cards, sections or screens they stop being comparable and
        become four separate footnotes. One `grid kpis` row holding all four is
        what makes them read across.
        """
        rows = re.findall(
            r'<section class="grid kpis"[^>]*>(.*?)</section>', self.prove, re.S)
        self.assertEqual(
            len(rows), 1, "the summary should be exactly one row on Prove")
        for tile in self.TILES:
            with self.subTest(tile=tile):
                self.assertIn('id="%s"' % tile, rows[0])

    def test_preflight_stays_where_it_moved(self):
        # It was the one tile with no equivalent elsewhere, so it went to Prove.
        # Bringing it back here would undo that move.
        self.assertNotIn('id="preflightDecisions"', self.home)
        self.assertIn('id="preflightDecisions"', self.html)

    def test_the_quiet_panel_does_not_repeat_the_tiles(self):
        """The quiet hero was the window's API-equivalent value, which was also
        a tile a few pixels below it -- the same figure twice.

        The duplication is gone now that the tiles are on Prove, but the rule
        holds for the original reason rather than that one: this panel answers
        "right now", and a window total is not a statement about right now. It
        leads with the session that just finished.
        """
        quiet = js_function_source(self.js, "ambientQuiet")
        self.assertIn("recent_sessions", quiet)
        self.assertNotIn("hero: esc(totals.api_value_label", quiet)

    def test_cost_copy_uses_the_spend_weighted_share(self):
        """"of what they cost" has to read the money figure. The token-weighted
        one is ~98% on every window, because replayed context is billed at the
        cache-read rate, and it made that sentence wrong by thirty points --
        while the Improve card, computing it properly, said 70%."""
        quiet = js_function_source(self.js, "ambientQuiet")
        self.assertIn("totals.replayed_spend_share_pct", quiet)
        self.assertNotIn("totals.replayed_share_pct", quiet)
        self.assertIn("replayed_spend_share_pct", inspect.getsource(ui))


class RefreshRecoveryTest(unittest.TestCase):
    """The dashboard is ambient, so the worst failure is not an error message --
    it is freezing on stale data while still looking live. Both bugs pinned here
    did exactly that and were found by clicking, not by reading the diff."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")

    def test_cleanup_runs_even_when_a_render_throws(self):
        # The button restore, loadInFlight and the next poll all used to sit at
        # the end of the happy path with ~140 lines of rendering above them. One
        # bad field skipped all three: the button stuck disabled on "Updating...",
        # loadInFlight pinned true, and refreshTick then rescheduled itself
        # forever without ever loading again.
        load = js_function_source(self.js, "load")
        self.assertIn("finally", load)
        for restored in ("refreshButton.disabled = false",
                         "loadInFlight = false",
                         "scheduleRefresh("):
            with self.subTest(restored=restored):
                after_finally = load[load.index("finally"):]
                self.assertIn(restored, after_finally)

    def test_the_label_is_restored_to_a_literal(self):
        # It used to be restored to whatever the button happened to say. The 10s
        # poll drives the same button, so a tick landing mid-refresh captured
        # "Updating..." and restored that -- permanently, and self-sustaining.
        self.assertNotIn("previousRefreshText", self.js)
        load = js_function_source(self.js, "load")
        self.assertIn("refreshButton.textContent = 'Refresh data'", load)

    def test_no_object_can_reach_the_brief_preview(self):
        # Decision records are dicts with a `summary` key and no `text` key, so
        # `item.text || item` fell through to the dict and the preview rendered
        # "[object Object]". The copied brief was always correct -- handoff.py
        # reads .summary -- but the preview is what the artefact is judged by.
        self.assertNotIn("map(item => item.text || item)", self.js)
        brief = js_function_source(self.js, "briefText")
        self.assertIn("item.summary", brief)
        # And a backstop, so this class of bug cannot ship silently again.
        self.assertIn(r"/\[object \w+\]/", self.js)


class HealthCardActionTest(unittest.TestCase):
    """A button on a health card has to go where its label says.

    The label used to be advice from _context_action ("Compact", "Keep going")
    while the handler was hardcoded in index.js to open the session review, so
    the two drifted. "Compact" was the sharpest case -- it reads as an
    instruction the button carries out, and the control that actually compacts
    sits beside it -- and in the healthy state the two labels were swapped
    outright. The review filed this as "five buttons do nothing"; every one of
    them fired, they just went somewhere else."""

    class _Health:
        def __init__(self, severity="healthy", pressure=False, bloat=False, stale=False):
            self.severity = severity
            self.is_context_pressure = pressure
            self.is_high_bloat = bloat
            self.is_stale = stale

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")

    def _states(self):
        return {
            "critical": self._Health(severity="critical"),
            "pressure": self._Health(pressure=True),
            "bloat": self._Health(bloat=True),
            "stale": self._Health(stale=True),
            "healthy": self._Health(),
        }

    def test_every_label_names_its_destination(self):
        allowed = {"Start fresh": "handoff", "Review session": "review"}
        for name, health in self._states().items():
            action = ui._context_action(health)
            with self.subTest(state=name):
                self.assertEqual(allowed.get(action["label"]), action["kind"])
                self.assertEqual(allowed.get(action["secondary_label"]), action["secondary_kind"])
                # Two buttons must not both go to the same place.
                self.assertNotEqual(action["kind"], action["secondary_kind"])

    def test_pressure_leads_with_the_fresh_start(self):
        for name in ("critical", "pressure", "bloat"):
            with self.subTest(state=name):
                self.assertEqual(ui._context_action(self._states()[name])["kind"], "handoff")
        for name in ("stale", "healthy"):
            with self.subTest(state=name):
                self.assertEqual(ui._context_action(self._states()[name])["kind"], "review")

    def test_advice_is_no_longer_wearing_a_button(self):
        for name, health in self._states().items():
            with self.subTest(state=name):
                action = ui._context_action(health)
                for label in (action["label"], action["secondary_label"]):
                    self.assertNotIn(label, {"Compact", "Keep going", "Fresh Start",
                                             "Prepare Fresh Start", "Fresh session", "Review"})
                # The advice still exists, just not as a control.
                self.assertTrue(action["reason"])

    def test_the_button_dispatches_on_the_kind_it_was_given(self):
        self.assertIn("function runHealthAction", self.js)
        self.assertNotIn('onclick="selectSession(this.dataset.session)">${esc(row.action.label)}', self.js)
        button = js_function_source(self.js, "healthActionButton")
        # Offering "Start fresh" where no handoff exists would be the same bug again.
        self.assertIn("row.can_handoff", button)


class FalseAffordanceTest(unittest.TestCase):
    """Three controls that lied about what they were, from the review's P0 tier."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")

    def test_clear_costs_the_prompt_and_not_the_analysis(self):
        # It sat 20px from the primary action and wiped the whole Route result --
        # route, risk score, findings, suggestions, brief -- with no undo.
        clear = js_function_source(self.js, "clearPromptCompanion")
        self.assertIn("promptInput", clear)
        self.assertNotIn("promptResult", clear)

    def test_no_live_return_is_not_shaped_like_a_button(self):
        # A disabled button is still a button: same height, border and padding as
        # the three real ones beside it, with its reason hidden in a title.
        # Narrowed to status text specifically. A disabled button is honest in a
        # loading skeleton, where it really does become available -- these two
        # never do, because they report a fact rather than offer an action.
        for status in ("No live return", "No exact return"):
            with self.subTest(status=status):
                self.assertNotIn(f'disabled>{status}', self.js)
                self.assertNotIn(f"disabled title=\"${{esc(openToolNote)}}\">{status}", self.js)
        self.assertIn("action-note", self.js)
        self.assertIn(".action-note", self.css)

    def test_the_watcher_command_is_readable_without_clicking(self):
        # Starting it needs a terminal, so the product shows the command instead
        # of offering a clipboard action for something the user cannot see.
        self.assertIn('id="watcherCommandText"', self.html)
        self.assertNotIn('id="watcherCommandButton"', self.html)
        render = js_function_source(self.js, "renderWatcher")
        self.assertIn("commandText.textContent", render)
        # Stopped is a warning: every screen is reporting on data that is not
        # being collected. It used to read in the same blue as "building".
        self.assertIn("cache-pill refreshing", render)
        self.assertNotIn("cache-pill stale", render)

    def test_the_watcher_toast_does_not_promise_an_undefined_feature(self):
        # "ambient nudges" appears nowhere else in the product.
        self.assertNotIn("ambient nudges", self.js)


class ScopeAndScaleTest(unittest.TestCase):
    """P1: figures that were true but did not say what they measured."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")

    def test_the_risk_scale_is_named_not_invented(self):
        # The score is an unbounded sum of penalty points; cli.py's
        # _risk_for_score bands it and nothing caps the total. The review asked
        # for "n of 5" -- there is no 5, and printing one would be a made-up
        # denominator dressed as a fact.
        from aiwatcher_cli import cli
        self.assertEqual(cli._risk_for_score(cli_medium := 3), "medium")
        self.assertEqual(cli._risk_for_score(cli_high := 6), "high")
        self.assertEqual(cli._risk_for_score(cli_medium - 1), "low")
        self.assertEqual(cli._risk_for_score(cli_high - 1), "medium")
        # The front end states those same two bands, so they cannot drift apart.
        self.assertIn(f"RISK_MEDIUM_AT = {cli_medium}", self.js)
        self.assertIn(f"RISK_HIGH_AT = {cli_high}", self.js)
        self.assertNotIn("score ${esc(data.score)}", self.js)

    def test_project_scoped_figures_say_so(self):
        # Home's fact row carried three session figures and one project figure
        # with nothing to tell them apart, and "sessions here" left "here"
        # undefined.
        self.assertNotIn("'sessions here'", self.js)
        self.assertIn("'project sessions'", self.js)
        # turns_since_reset diverges from the session total after a /compact.
        self.assertNotIn("['turns', String(chart.turns_since_reset)]", self.js)

    def test_watch_separates_the_session_from_the_project(self):
        facts = js_function_source(self.js, "healthFacts")
        self.assertIn("this session:", facts)
        self.assertIn("this project:", facts)

    def test_survival_names_its_own_window(self):
        # survival ignores the date picker: cli.py fixes it at
        # SURVIVAL_WINDOW_DAYS because a commit must age past MIN_AGE_DAYS
        # before survival means anything. It sat under the picker never moving.
        from aiwatcher_cli import cli
        self.assertEqual(cli.SURVIVAL_WINDOW_DAYS, 30)
        self.assertIn("not the selected range", self.js)
        # And counts get separators rather than reading "12206".
        self.assertIn("formatCount(survival.lines_touched)", self.js)


class TileSparklineTest(unittest.TestCase):
    """The tile row must not change shape on a button that only promises to
    refresh the numbers."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")

    def test_a_shell_payload_does_not_wipe_the_sparklines(self):
        # A forced refresh returns summary_complete: false with no tile_trends,
        # and the old code cleared on "no data present" -- so pressing "Refresh
        # data" emptied every sparkline until some later poll restored them.
        self.assertIn("data.summary_complete", self.js)
        self.assertIn("windowChanged", self.js)

    def test_a_window_change_still_clears(self):
        # The original worry stands: the previous window's shape would be worse
        # than none at all.
        self.assertIn("tileSparkWindow", self.js)

    def test_the_slot_is_always_occupied(self):
        # usefulOutcomes has too few judged points to plot at short ranges, and
        # hiding it made that card a different height from its neighbours -- the
        # row's alignment shifted when the reader changed the range.
        self.assertIn("drawTileSparkPlaceholder", self.js)
        self.assertIn("not enough data yet", self.js)
        draw = js_function_source(self.js, "drawTileSpark")
        self.assertIn("return false;", draw)
        self.assertIn("return true;", draw)


class DesignScaleTest(unittest.TestCase):
    """P3-1/2/3/6/9. The stylesheet had grown 24 font sizes, 12 weights, 12 radii
    and 19 gap values, several of them fractional (.68rem = 10.88px) rather than
    chosen. These assert the scales exist and that raw values do not creep back."""

    @classmethod
    def setUpClass(cls):
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")

    def test_the_scales_are_defined(self):
        for token in ("--fs-1", "--fs-2", "--fs-3", "--fs-4", "--fs-5", "--fs-6", "--fs-hero",
                      "--fw-normal", "--fw-med", "--fw-bold",
                      "--r-1", "--r-2", "--r-pill",
                      "--sp-1", "--sp-4", "--sp-7", "--w-prose"):
            with self.subTest(token=token):
                self.assertIn(f"{token}:", self.css)

    def test_no_raw_values_outside_the_scales(self):
        # Only `inherit`, `0` and `50%` (real circles) may stay literal.
        allowed = {"inherit", "0", "50%"}
        for prop in ("font-size", "font-weight", "border-radius", "gap"):
            raw = re.findall(rf"(?<![-\w]){prop}\s*:\s*([^;}}]+)", self.css)
            leftovers = [v.strip() for v in raw if "var(" not in v and v.strip() not in allowed]
            with self.subTest(prop=prop):
                self.assertEqual(leftovers, [], f"{prop} still has raw values: {leftovers}")

    def test_inline_styles_use_the_scale_too(self):
        # A CSS-only pass left .75rem and .7rem in the markup, which is how
        # 11.2px survived an audit that only read the stylesheet.
        self.assertNotIn("font-size:.7rem", self.html)
        self.assertNotIn("font-size:.75rem", self.html)

    def test_running_text_is_capped(self):
        # Measured at 2100px, .receipt-note reached 252 characters per line on
        # five views. Capped on the element, since enumerating containers missed
        # paragraphs in plain divs.
        self.assertIn("--w-prose", self.css)
        self.assertIn("p, .empty, .lede, .prompt-list li { max-width: var(--w-prose); }", self.css)

    def test_the_stat_row_fills_its_container(self):
        # repeat(4, ...) with three children left a quarter-width hole that read
        # as a card which had failed to load. The guard is the property, not the
        # spelling: however many columns the row declares at any width, the cards
        # have to divide into them evenly or the last row has a gap in it.
        cards = self.html.count('class="card metric-card')
        self.assertEqual(cards, 4)
        declared = re.findall(r"\.kpis \{[^}]*grid-template-columns:\s*([^;]+);", self.css)
        self.assertTrue(declared, "the stat row declares no columns")
        for value in declared:
            with self.subTest(columns=value.strip()):
                if "auto-fit" in value or "auto-fill" in value:
                    continue
                repeated = re.match(r"repeat\((\d+),", value.strip())
                count = int(repeated.group(1)) if repeated else len(value.split())
                self.assertEqual(cards % count, 0,
                                 "%d cards do not fill %d columns" % (cards, count))

    def test_root_declares_a_font_family(self):
        # Without it :root computed to Times New Roman and only body caught Inter.
        root = self.css[self.css.index(":root"):self.css.index("}", self.css.index(":root"))]
        self.assertIn("font-family:", root)

    def test_rows_with_an_action_respond_to_hover(self):
        self.assertIn("tbody tr:has(.row-action):hover", self.css)
        self.assertIn("--surface-hover", self.css)

    def test_nav_children_read_as_children(self):
        # They were the same size and weight as their parents; only a 12px indent
        # distinguished them.
        sub = self.css[self.css.index(".nav-sub {"):]
        sub = sub[:sub.index("}")]
        self.assertIn("var(--fs-2)", sub)
        self.assertIn("var(--fw-normal)", sub)


class CopyAndAffordanceTest(unittest.TestCase):
    """P2-4, P2-8 and the P4 copy edits."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")

    def test_a_sortable_column_has_something_to_sort_by(self):
        # $/surviving line offered a sort and shipped only a label, so clicking
        # it genuinely did nothing. Every other sortable column ships both.
        source = inspect.getsource(ui)
        self.assertIn('"usd_per_surviving_line":', source)
        self.assertIn('"usd_per_surviving_line_label":', source)

    def test_pill_shape_is_reserved_for_things_you_can_click(self):
        # Six phrasings of one privacy claim, plus two card descriptors, were
        # inert <span>s shaped exactly like filter chips.
        self.assertNotIn('<span class="pill">', self.html)
        self.assertIn("note-chip", self.css)
        for gone in ("Local evidence only", "Local machine only", "Local logs only",
                     "Prompt text stays private"):
            with self.subTest(phrase=gone):
                self.assertNotIn(gone, self.html)

    def test_targets_are_named_as_products(self):
        # The buttons rendered their raw ids: "Generic claude codex cursor vscode".
        self.assertIn("HANDOFF_TARGET_LABELS", self.js)
        for label in ("Claude Code", "VS Code", "Codex", "Cursor"):
            with self.subTest(label=label):
                self.assertIn(label, self.js)

    def test_the_tool_default_follows_the_observed_tool(self):
        # It defaulted to whichever option came first (Codex) while every
        # observed session on this machine is claude-code.
        self.assertIn("function setDefaultPromptTool", self.js)
        self.assertIn("promptToolTouched", self.js)
        self.assertIn('<option value="claude">Claude Code</option>', self.html)

    def test_the_status_column_holds_states_not_instructions(self):
        source = inspect.getsource(ui._project_health) if hasattr(ui, "_project_health") else inspect.getsource(ui)
        self.assertIn('"label": "Needs review"', source)

    def test_nav_uses_one_case_convention(self):
        self.assertNotIn("Plan / Control", self.html)


class InformationArchitectureTest(unittest.TestCase):
    """The P2 block: what is shown, in what order, and what looks clickable."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")

    def test_two_controls_at_rest_in_the_drawer(self):
        # Five same-sized controls wrapped across two rows with no ranking, and
        # mixed two questions: "what next" and "grade what happened".
        actions = js_function_source(self.js, "renderSessionActions")
        self.assertIn('class="action-more"', actions)
        self.assertIn("Start fresh", actions)
        self.assertIn("Continue here", actions)
        # It only scrolled to the outcome triad, which is its own labelled step.
        self.assertNotIn(">Mark outcome<", actions)

    def test_a_settled_session_does_not_make_a_demand(self):
        actions = js_function_source(self.js, "renderSessionActions")
        self.assertIn("Optional next step", actions)
        self.assertIn("const settled = s.outcome === 'useful'", actions)
        self.assertIn(".recommended-action.settled", self.css)

    def test_the_brief_comes_after_the_form_that_shapes_it(self):
        # The generated output sat above five empty fields, so a first-time
        # reader could not tell whether it was ready or waiting on them.
        body = self.js[self.js.index("handoff-refine"):]
        self.assertIn("Refine this brief (optional)", self.js)
        self.assertIn("Ready to copy", self.js)
        refine_at = self.js.index("handoff-refine")
        preview_at = self.js.index("${renderFreshStartPreview(capsule)}")
        self.assertLess(refine_at, preview_at)

    def test_copy_feedback_lands_on_the_button(self):
        # The toast renders up to 750px from the control, and two copy buttons
        # fired none at all.
        self.assertIn("function flashCopied", self.js)
        self.assertIn("lastPressedButton", self.js)
        # Captured on the way down, because several copy paths await a fetch
        # first and window.event is gone by then.
        self.assertIn("}, true);", self.js)
        self.assertIn("button.copied", self.css)

    def test_each_copy_button_names_its_artefact(self):
        for gone in (">Copy original<", ">Copy brief only<"):
            with self.subTest(label=gone):
                self.assertNotIn(gone, self.js)
        # "Copy without opening" named the mechanism, not the artifact, which
        # was the whole premise of the rename that fixed the collision.
        self.assertNotIn("Copy without opening", self.js)
        for present in ("Copy my prompt", "Copy brief", "Copy execution brief"):
            with self.subTest(label=present):
                self.assertIn(present, self.js)

    def test_the_header_stays_reachable(self):
        # The nav was already sticky; the header was not, so the date range and
        # Refresh scrolled out of reach on long views.
        header = self.css[self.css.index("    header {"):]
        header = header[:header.index("}")]
        self.assertIn("position: sticky", header)
        # The nav offset is derived from the header height so they cannot overlap.
        self.assertIn("--header-h", self.css)
        self.assertIn("top: calc(var(--header-h)", self.css)


class AmbientScalingTest(unittest.TestCase):
    """P3-5, the Home question.

    Measured before deciding: Home filled 102% of a 1280x800 viewport but 63% of
    a 1280x1287 one. The review's fix -- hero left, stat cards stacked right --
    was measured too and makes it worse: two columns take Home from 584px to
    about 330px, so it fills 42% at 800px rather than 102%. It fills the viewport
    by removing height. The surface scales instead: same content, same slots,
    sized to the room available."""

    @classmethod
    def setUpClass(cls):
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")

    def test_the_surface_scales_with_the_viewport(self):
        ambient = self.css[self.css.index("    .ambient {"):]
        ambient = ambient[:ambient.index("}")]
        self.assertIn("min-height: clamp(", ambient)
        self.assertIn("vh", ambient)
        # Clamped at both ends so a short window and a very tall one stay sane.
        self.assertIn("300px", ambient)
        self.assertIn("760px", ambient)

    def test_the_hero_tracks_the_surface(self):
        # The scaling lives in --fs-hero rather than as a raw value in the rule,
        # so the type scale still owns every size. The token test enforces that.
        root = self.css[self.css.index(":root"):self.css.index("}", self.css.index(":root"))]
        self.assertIn("--fs-hero: clamp(", root)
        self.assertIn("vh", root)
        hero = self.css[self.css.index("    .ambient-hero {"):]
        hero = hero[:hero.index("}")]
        self.assertIn("font-size: var(--fs-hero)", hero)

    def test_home_is_not_rebuilt_as_two_columns(self):
        # The stat row stays below the ambient surface. Putting it beside the
        # hero would halve the width of the prose verdicts, which Appendix B
        # calls the reason to use the product. What matters is that the row is
        # never placed into a column of a page-level grid -- how many columns it
        # divides itself into is its own business, and is guarded by
        # test_the_stat_row_fills_its_container.
        kpis = self.css[self.css.index(".kpis {"):]
        kpis = kpis[:kpis.index("}")]
        self.assertNotIn("grid-column", kpis)
        self.assertNotIn("grid-row", kpis)

    def test_the_runtime_strip_is_one_line_of_text(self):
        # It carried the same visual weight as a content card to report a
        # watcher state and a timestamp, and its 34px was what kept Home from
        # fitting an 800px-tall viewport.
        strip = self.css[self.css.index("    .runtime-strip {"):]
        strip = strip[:strip.index("}")]
        self.assertNotIn("border:", strip)
        self.assertNotIn("box-shadow", strip)
        self.assertIn("color: var(--muted)", strip)


class PathsAndChartsTest(unittest.TestCase):
    """The rest of P3 and P4."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")

    def test_project_name_handles_both_separators(self):
        # The same project appeared three ways in four adjacent rows. The third
        # was a Windows path that never shortened, because the split matched
        # forward slashes only.
        self.assertEqual(ui.project_name("C:/Users/me/Work/thing"), "Work/thing")
        self.assertEqual(
            ui.project_name("C:" + BS + "Users" + BS + "me" + BS + "Work" + BS + "thing"),
            "Work/thing",
        )
        self.assertEqual(ui.project_name(""), "unknown")
        self.assertEqual(ui.project_name(None), "unknown")
        # Two segments, because the leaf alone does not separate these.
        self.assertNotEqual(
            ui.project_name("/a/AgentWatch/aiwatcher-local-public"),
            ui.project_name("/a/AgentWatch/aiwatcher-local-pr46"),
        )

    def test_the_front_end_shortens_paths_the_same_way(self):
        self.assertIn("function projectName", self.js)
        # Left-truncating mid-word is what made the column unscannable.
        self.assertNotIn("esc(p.short_name || p.name)", self.js)
        self.assertNotIn("esc(s.project)}<br>", self.js)

    def test_one_spelling_for_the_thousands_suffix(self):
        # Python rendered 564.2k and the front end rendered 564K, and both
        # appeared in the same Watch card; five call sites had grown a
        # .replace(/K$/, 'k') to paper over it.
        self.assertNotIn("replace(/K$/", self.js)
        self.assertIn("+ 'k'", self.js)

    def test_sparklines_show_where_the_line_sits(self):
        draw = js_function_source(self.js, "drawTileSpark")
        self.assertIn("svgEl('line'", draw)     # baseline
        self.assertIn("svgEl('circle'", draw)   # endpoint marker

    def test_prove_opens_with_one_card(self):
        # A quarter-width tile floated above the card it described, with nothing
        # connecting them. Both ids stay: load() writes to them.
        self.assertNotIn('metric-card metric-amber', self.html)
        self.assertIn('id="preflightDecisions"', self.html)
        self.assertIn('id="windowLabel"', self.html)

    def test_the_pending_reason_is_stated_once(self):
        # The same 22 words repeated on every pending row.
        rows = js_function_source(self.js, "renderHandoffDecisionRows")
        # proof_reason is still read, but only for rows whose status is not the
        # generic pending one -- that is the whole fix, so assert the guard
        # rather than the absence of the field.
        self.assertIn("decision.proof_status !== 'Proof pending'", rows)
        # And the sentence it replaced is stated once, on the card.
        self.assertIn("Proof stays pending until", self.html)

    def test_an_empty_table_says_so_where_the_rows_would_be(self):
        self.assertIn("td .empty", self.css)
        self.assertIn("text-align: center", self.css)


class SearchRankingTest(unittest.TestCase):
    """P2-11. Raw substring matching over the whole project path made every
    ancestor directory a match: on this machine the projects live under
    Downloads/AgentWatch/, so searching "agentwatch" returned 13 of 14 sessions
    across three unrelated projects, only one of which is named that."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")

    def _row(self, project, tool="claude-code", model="claude-opus-5", sid="s1"):
        return SimpleNamespace(session_id=sid, tool=tool, model=model, project_path=project)

    def test_the_project_itself_outranks_its_parent(self):
        from aiwatcher_cli import cli
        own = self._row("/home/me/AgentWatch/agentwatch")
        sibling = self._row("/home/me/AgentWatch/aiwatcher-local-public")
        self.assertEqual(cli.search_field_rank(own, "agentwatch"), cli.SEARCH_RANK_PROJECT_LEAF)
        self.assertEqual(cli.search_field_rank(sibling, "agentwatch"), cli.SEARCH_RANK_PROJECT_TAIL)
        self.assertLess(
            cli.search_field_rank(own, "agentwatch"),
            cli.search_field_rank(sibling, "agentwatch"),
        )

    def test_a_sibling_still_matches_rather_than_disappearing(self):
        # Ranked, not filtered: hiding the siblings would answer a different
        # question from the one the reader asked.
        from aiwatcher_cli import cli
        sibling = self._row("/home/me/AgentWatch/aiwatcher-local-public")
        self.assertIsNotNone(cli.search_field_rank(sibling, "agentwatch"))

    def test_identity_fields_rank_above_any_path(self):
        from aiwatcher_cli import cli
        row = self._row("/home/me/AgentWatch/agentwatch", tool="codex-cli")
        self.assertEqual(cli.search_field_rank(row, "codex"), cli.SEARCH_RANK_IDENTITY)
        self.assertLess(cli.SEARCH_RANK_IDENTITY, cli.SEARCH_RANK_PROJECT_LEAF)

    def test_windows_paths_rank_the_same_way(self):
        from aiwatcher_cli import cli
        row = self._row("C:" + BS + "me" + BS + "AgentWatch" + BS + "agentwatch")
        self.assertEqual(cli.search_field_rank(row, "agentwatch"), cli.SEARCH_RANK_PROJECT_LEAF)

    def test_no_match_is_none(self):
        from aiwatcher_cli import cli
        self.assertIsNone(cli.search_field_rank(self._row("/a/b/c"), "nothinghere"))

    def test_the_front_end_keeps_the_server_order(self):
        # renderSessionRows re-sorted every payload by recency, which threw the
        # relevance order away before it reached the screen.
        self.assertIn("sessionSortChosen", self.js)
        rows = js_function_source(self.js, "renderSessionRows")
        self.assertIn("sessionSortChosen ? sortedRows(rows, sessionSort) : rows", rows)
        # And the row says why it matched.
        self.assertIn("match_field", self.js)


class PlanZoneTest(unittest.TestCase):
    """Plan item 1 / spec M1. The result was one undifferentiated block, so the
    house template read as though it had been worked out from the prompt.
    Diffing two unrelated prompts produced identical output apart from the tool
    name, which comes from a dropdown."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")

    def test_the_result_has_three_zones_in_order(self):
        for fn in ("renderDerivedZone", "renderObservedZone", "renderGuardrailZone"):
            with self.subTest(zone=fn):
                self.assertIn(f"function {fn}", self.js)
        body = js_function_source(self.js, "preflightPrompt")
        self.assertLess(body.index("renderDerivedZone"), body.index("renderObservedZone"))
        self.assertLess(body.index("renderObservedZone"), body.index("renderGuardrailZone"))

    def test_the_derived_zone_is_never_silent(self):
        zone = js_function_source(self.js, "renderDerivedZone")
        self.assertIn("Nothing in this prompt matched a signal worth a second opinion.", zone)
        self.assertIn("RISK_MEDIUM_AT", zone)   # gated-out vs unavailable read differently

    def test_the_guardrails_are_labelled_as_house_advice(self):
        zone = js_function_source(self.js, "renderGuardrailZone")
        self.assertIn("Standard execution guardrails", zone)
        self.assertIn("Not derived from yours", zone)
        self.assertIn("<details", zone)         # collapsed by default

    def test_the_observed_zone_never_has_an_unavailable_state(self):
        # Stage 1 is local and free, so there is no condition under which it
        # cannot answer.
        zone = js_function_source(self.js, "renderObservedZone")
        self.assertNotIn("unavailable", zone)
        self.assertIn("signal-chip", zone)

    def test_the_server_ships_the_signals(self):
        source = inspect.getsource(ui.build_prompt_preflight)
        self.assertIn('"signals"', source)
        self.assertIn('"removals"', source)


class RegressionRepairTest(unittest.TestCase):
    """Plan items 4, 5 and 6 -- damage from the scales/sticky-header pass."""

    @classmethod
    def setUpClass(cls):
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")

    def _rule(self, selector):
        start = self.css.index(selector)
        return self.css[start:self.css.index("}", start)]

    def test_a_card_title_sits_above_its_body_text_on_the_scale(self):
        # They were separated by weight alone -- and the scales pass had just
        # weakened that, moving unstyled headings from the UA default 700 to 600.
        self.assertIn("var(--fs-4)", self._rule("    h3 {"))
        self.assertIn("var(--fs-5)", self._rule("    h2 {"))

    def test_data_strips_are_not_capped_like_prose(self):
        # .health-facts is a dot-separated row of figures. At 68ch it wrapped
        # inside an item, so one line ended "1" and the next began "session".
        self.assertIn(".health-facts", self.css)
        rule = self._rule(".health-facts, [class$=")
        self.assertIn("max-width: none", rule)

    def test_the_nav_stacks_above_the_page_and_below_the_header(self):
        # The sticky header got a stacking context and the nav did not, so below
        # roughly 470px of viewport height the nav slid under the header band.
        nav = self._rule("    .product-nav {")
        self.assertIn("z-index: 10", nav)
        header = self._rule("    header {")
        self.assertIn("z-index: 20", header)


class ContextHealthChartTest(unittest.TestCase):
    """Plan item 7. The largest object on the most-visited screen, and it
    communicated almost nothing.

    The old chart scaled y to the series' own min and max. On a session at 838k
    per turn against a 200k limit, that drew 752k-810k across the whole canvas:
    a gentle 7% rise, with both thresholds off the chart entirely. The one fact
    that mattered -- four times over the line -- was the one thing a reader could
    not see."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")
        cls.draw = js_function_source(cls.js, "drawTrend")

    def test_the_scale_stays_the_data_s_own(self):
        # Shape is this chart's job -- the meter beside it carries position
        # against the limit on a shared scale. Stretching y to include a 200k
        # threshold for a session running at 838k squeezes sixty turns into six
        # percent of the frame, which is the flattening the meter/trend split
        # exists to prevent.
        self.assertIn("Math.min(...series)", self.draw)
        self.assertIn("Math.max(...series)", self.draw)

    def test_the_magnitudes_are_readable(self):
        # What was missing was never the thresholds; it was any way to tell what
        # the y values were.
        self.assertIn("compactTokens(Math.round(top))", self.draw)
        self.assertIn("compactTokens(Math.round(base))", self.draw)

    def test_a_threshold_off_the_scale_is_stated_not_omitted(self):
        # Silently dropping it is how a reader concludes the axis starts at zero
        # and that they are comfortably under the limit.
        self.assertIn("limit is", self.draw)
        self.assertIn("below this range", self.draw)
        self.assertIn("stroke-dasharray", self.draw)

    def test_the_axes_exist(self):
        self.assertIn("turn 1", self.draw)
        self.assertIn("turn ${series.length}", self.draw)
        self.assertIn("trend-tick", self.css)

    def test_the_floating_captions_are_gone(self):
        # They sat in the bottom corners at a height that related to nothing.
        self.assertNotIn(".trend-host::before", self.css)
        self.assertNotIn(".trend-host::after", self.css)
        self.assertNotIn("data-delta", self.js)

    def test_it_is_drawn_at_its_real_size(self):
        # A fixed 1000x60 viewBox scaled to fit collapsed the whole plot into a
        # 14px band floating in the middle of a narrow card.
        self.assertIn("node.getBoundingClientRect().width", self.draw)
        self.assertIn("viewBox=\"0 0 ${W} ${H}\"", self.draw)

    def test_it_redraws_when_its_width_changes(self):
        # Drawn 1:1, so a chart built while its view is hidden measures zero and
        # would otherwise stay stretched at whatever CSS made of it.
        self.assertIn("ResizeObserver", self.js)
        observe = js_function_source(self.js, "observeTrend")
        # Guarded so a redraw cannot drive the observer that triggered it.
        self.assertIn("node.dataset.drawnAt", observe)

    def test_a_line_chart_can_be_read_point_by_point(self):
        self.assertIn("function attachTrendHover", self.js)
        self.assertIn("trend-crosshair", self.css)
        self.assertIn("trend-tip", self.css)

    def test_the_compact_row_variant_stays_a_sparkline(self):
        # Axis furniture does not fit in 26px, and the collapsed rows are a
        # glance, not a reading.
        self.assertIn("health-row-trend", self.draw)
        self.assertIn("compact ? '' :", self.draw)


class CorrectnessSweepTest(unittest.TestCase):
    """Plan items 11-21. Marked done in the backlog, verified not done."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")

    def test_a_number_column_shares_one_unit_and_one_precision(self):
        # A column is read down, not across. Tokens showed "331.2M" above
        # "158.3k"; $/line showed "$1.14" above "$0.0030".
        self.assertIn("function tokenColumnFormatter", self.js)
        self.assertIn("function currencyColumnFormatter", self.js)
        rows = js_function_source(self.js, "renderChangeRows")
        self.assertIn("currencyColumnFormatter", rows)
        self.assertNotIn("usd_per_line_label", rows)

    def test_a_sub_cent_value_says_so_rather_than_rounding_to_zero(self):
        fmt = js_function_source(self.js, "currencyColumnFormatter")
        self.assertIn("<$0.01", fmt)

    def test_numeric_cells_line_up(self):
        # It was on 32 of 168, which reads the same as being on none.
        self.assertIn("td.num, td.mono", self.css)
        block = self.css[self.css.index("td.num, td.mono"):]
        self.assertIn("tabular-nums", block[:400])

    def test_the_surviving_line_label_is_not_doubled(self):
        # The span renders "$0.03 per surviving line"; the prefix said it again
        # and made the line longer than before the fix that added the scope note.
        self.assertNotIn("Cost per surviving line: <span", self.html)
        self.assertIn('id="costPerSurviving"', self.html)

    def test_the_quiet_state_states_its_session_count_once(self):
        quiet = js_function_source(self.js, "ambientQuiet")
        self.assertNotIn("' session' +", quiet)
        self.assertIn("Sessions observed", self.html)

    def test_one_verb_opens_the_fresh_start_drawer(self):
        self.assertNotIn("Try Fresh Start demo", self.html)
        self.assertIn("Try it with sample data", self.html)
        self.assertNotIn('"primary_label": "Open Fresh Start"', inspect.getsource(ui))
        # Home keeps its own name because it copies rather than opens; naming it
        # "Start fresh" would be the label lying about the behaviour again.
        self.assertIn("Copy Fresh Start brief", self.js)

    def test_copy_labels_name_artifacts(self):
        self.assertNotIn("Copy without opening", self.js)

    def test_points_are_pluralised(self):
        score = js_function_source(self.js, "riskScore")
        self.assertIn("'pt'", score)
        self.assertIn("'pts'", score)

    def test_a_sort_target_is_the_whole_header_cell(self):
        # It was a 16px inline button covering 4-28% of its cell, under the
        # 24x24 minimum -- and the reason a reviewer clicking the th concluded
        # sorting was broken.
        rule = self.css[self.css.index("    .sort-head {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("display: block", rule)
        self.assertIn("width: 100%", rule)
        self.assertIn("min-height: 24px", rule)

    def test_a_project_row_is_reachable_by_keyboard(self):
        # The Watch health rows were already real buttons; Projects was a
        # <tr onclick> with no role and no tab stop.
        self.assertIn("row-open", self.js)
        self.assertIn(".row-open", self.css)
        self.assertNotIn('<tr class="clickable" onclick="selectProject', self.js)

    def test_the_quiet_toggle_reports_its_own_state(self):
        self.assertIn('aria-pressed="false"', self.html)
        handler = js_function_source(self.js, "quietFreshStartReminders")
        self.assertIn("aria-pressed", handler)


if __name__ == "__main__":
    unittest.main()


class LivePresenceStripTests(unittest.TestCase):
    """The line that says what is running right now."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")

    def test_the_strip_exists_and_is_painted(self):
        self.assertIn('id="presenceStrip"', self.html)
        self.assertIn("function renderPresence(", self.js)
        self.assertIn("renderPresence(data.presence", self.js)

    def test_nothing_live_renders_nothing(self):
        # A permanent "0 running" on an idle machine, on every view.
        source = js_function_source(self.js, "renderPresence")
        self.assertIn("node.hidden = true", source)

    def test_the_count_carries_no_status_colour(self):
        # Colour is a claim. The only thing on the ambient surface that earns
        # one is a running session's context pressure -- a number of running
        # sessions is neither good news nor bad.
        rule = self.css[self.css.index("    .presence-strip {"):]
        rule = rule[:rule.index("}")]
        for token in ("--green", "--red", "--amber"):
            with self.subTest(token=token):
                self.assertNotIn(token, rule)

    def test_it_says_whose_machine_it_is_counting(self):
        # Sessions on another computer or in the cloud write nothing locally,
        # so this count is never a total.
        source = js_function_source(self.js, "renderPresence")
        self.assertIn("on this machine", source)

    def test_aiwatchers_own_sessions_are_declared(self):
        # Second Opinion spawns a real session. Counted, because hiding what a
        # feature costs is the thing this product refuses to do -- but labelled,
        # so it cannot pass as work the developer started.
        source = js_function_source(self.js, "presenceText")
        self.assertIn("analyst_runs", source)
        self.assertIn("AIWatcher's own", source)

    def test_dom_is_not_rewritten_when_nothing_changed(self):
        source = js_function_source(self.js, "renderPresence")
        self.assertIn("dataset.markup", source)

    def test_the_panel_admits_it_is_showing_one_of_several(self):
        # It picks the worst reachable session. Saying nothing about the others
        # reads as "this is what is happening" rather than "this is the worst".
        source = js_function_source(self.js, "ambientRunning")
        self.assertIn("liveCount", source)
        self.assertIn("of ' + liveCount + ' live", source)

    def test_the_total_leads_so_truncation_cannot_hide_a_tool(self):
        # The line ellipsises on a narrow window, which is the normal case here.
        # With the per-tool breakdown leading, the truncation swallowed the
        # second tool whole and read as though the first was all of it.
        source = js_function_source(self.js, "renderPresence")
        self.assertIn("live > 1", source)
        self.assertIn("' live", source)

    def test_the_watcher_pill_does_not_lose_room_to_the_count(self):
        # Measured at 700px: the count pushed the pill down to "Wat", hiding
        # whether the watcher was running or stopped. A status claim must not
        # lose space to a count.
        self.assertIn(".runtime-copy .cache-pill { flex: none; }", self.css)
        rule = self.css[self.css.index("    .presence-strip {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("flex: 0 1 auto", rule)
        self.assertIn("text-overflow: ellipsis", rule)

    def test_quiet_state_does_not_claim_an_idle_machine_while_sessions_run(self):
        # It asserted "no session running" from the absence of a *chartable*
        # session. A Codex thread is live and unplottable at the same time.
        source = js_function_source(self.js, "ambientQuiet")
        self.assertIn("liveCount", source)
        self.assertIn("sessions running", source)


class WorkingTreeCollisionStripTests(unittest.TestCase):
    """The warning that two live sessions share one checkout."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")

    def test_the_warning_leads_the_line(self):
        # The line ellipsises from the right. The one clause here that predicts
        # lost work must not be the first thing dropped.
        source = js_function_source(self.js, "renderPresence")
        self.assertIn("collisionMarkup(presence) + lead", source)

    def test_nothing_shared_renders_nothing(self):
        source = js_function_source(self.js, "collisionMarkup")
        self.assertIn("if (!clashes.length) return ''", source)

    def test_it_says_what_the_risk_is(self):
        # "2 sharing repo" states a fact. What the reader needs is that one can
        # overwrite the other with nothing to show for it.
        source = js_function_source(self.js, "collisionMarkup")
        self.assertIn("overwrite", source)
        self.assertIn("title=", source)

    def test_only_the_warning_carries_colour(self):
        # The counts beside it stay neutral: how many sessions are running is
        # neither good news nor bad.
        # Scoped: `.runtime-copy span` sets --muted at a higher specificity
        # than a lone class, so a bare .presence-clash renders grey.
        clash = self.css[self.css.index("    .presence-strip .presence-clash {"):]
        self.assertIn("var(--amber)", clash[:clash.index("}")])
        strip = self.css[self.css.index("    .presence-strip {"):]
        strip = strip[:strip.index("}")]
        for token in ("--green", "--red", "--amber"):
            with self.subTest(token=token):
                self.assertNotIn(token, strip)


class WaitingOnYouStripTests(unittest.TestCase):
    """The state that comes from the tool rather than from a timestamp."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.css = (ui._WEB_DIR / "index.css").read_text(encoding="utf-8")

    def test_it_leads_everything_else(self):
        # The shared-tree warning predicts work you might lose; this is time you
        # are losing now, and the line truncates from the right.
        source = js_function_source(self.js, "renderPresence")
        self.assertIn("waitingMarkup(presence) + collisionMarkup(presence)", source)

    def test_nothing_waiting_renders_nothing(self):
        source = js_function_source(self.js, "waitingMarkup")
        self.assertIn("if (!blocked.length) return ''", source)

    def test_it_leads_with_the_longest_wait(self):
        # How long you have been the bottleneck is the part that makes you look.
        source = js_function_source(self.js, "waitingMarkup")
        self.assertIn("idle_seconds", source)
        self.assertIn("sort(", source)

    def test_it_is_the_loudest_thing_on_the_line(self):
        alert = self.css[self.css.index("    .presence-strip .presence-alert {"):]
        alert = alert[:alert.index("}")]
        self.assertIn("var(--red)", alert)


class WaitingTabTests(unittest.TestCase):
    """The tab is the surface for most of the working day."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")

    def test_a_waiting_session_outranks_context_pressure_in_the_tab(self):
        # Pressure is a cost you are choosing; a blocked session is a stop.
        source = js_function_source(self.js, "tabStateFor")
        self.assertIn("waitingSessions(data).length", source)
        self.assertIn("return 'critical'", source)

    def test_the_title_leads_with_the_wait(self):
        # A browser tab shows perhaps twenty characters, and the whole point is
        # that it reads without switching to it.
        source = js_function_source(self.js, "renderTabState")
        self.assertIn("Waiting ${wait}", source)

    def test_the_title_still_falls_back_when_nothing_waits(self):
        source = js_function_source(self.js, "renderTabState")
        self.assertIn("latest_turn_tokens", source)
        self.assertIn("api_value_label", source)
