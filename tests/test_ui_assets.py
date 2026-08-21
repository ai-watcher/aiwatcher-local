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
        remap = re.search(r"const activeView = ([^;]*);", self.js).group(1).strip()
        if remap == "view":
            self.assertEqual(self.BORROWS_HIGHLIGHT, {})
            return
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
    """Home carries a standing summary of the window under the panel about right
    now. It was cut on the grounds that each number appears elsewhere -- true,
    and beside the point: four numbers in four places is not four numbers in one
    glance."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")
        start = cls.html.index('<section id="view-today"')
        cls.home = cls.html[start:cls.html.index('<section id="view-prompt"')]

    def test_home_carries_three_tiles(self):
        for tile in ("usefulOutcomes", "sessions", "apiValue"):
            with self.subTest(tile=tile):
                self.assertIn('id="%s"' % tile, self.home)

    def test_preflight_stays_where_it_moved(self):
        # It was the one tile with no equivalent elsewhere, so it went to Prove.
        # Bringing it back here would undo that move.
        self.assertNotIn('id="preflightDecisions"', self.home)
        self.assertIn('id="preflightDecisions"', self.html)

    def test_the_quiet_panel_does_not_repeat_the_tiles(self):
        """The quiet hero was the window's API-equivalent value, which is now a
        tile a few pixels below it -- the same figure twice. The panel answers
        "right now", so it leads with the session that just finished."""
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
        # as a card which had failed to load.
        kpis = self.css[self.css.index(".kpis {"):]
        kpis = kpis[:kpis.index("}")]
        self.assertIn("repeat(auto-fit, minmax(240px, 1fr))", kpis)
        # .mini-grid keeps repeat(4, ...) and should: it holds four children.
        self.assertNotIn("repeat(4,", kpis)

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
        for present in ("Copy my prompt", "Copy without opening", "Copy execution brief"):
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
        # calls the reason to use the product.
        kpis = self.css[self.css.index(".kpis {"):]
        kpis = kpis[:kpis.index("}")]
        self.assertIn("repeat(auto-fit", kpis)
        self.assertNotIn("grid-column", kpis)

    def test_the_runtime_strip_is_one_line_of_text(self):
        # It carried the same visual weight as a content card to report a
        # watcher state and a timestamp, and its 34px was what kept Home from
        # fitting an 800px-tall viewport.
        strip = self.css[self.css.index("    .runtime-strip {"):]
        strip = strip[:strip.index("}")]
        self.assertNotIn("border:", strip)
        self.assertNotIn("box-shadow", strip)
        self.assertIn("color: var(--muted)", strip)


if __name__ == "__main__":
    unittest.main()
