from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from aiwatcher_cli.pricing import CACHE_WRITE_5M_MULTIPLIER, cache_read_cost, lookup
from aiwatcher_cli.scanner import LocalEvent, LocalSession
from aiwatcher_cli.session_health import (
    EXTREME_BLOAT_RATIO,
    HIGH_BLOAT_RATIO,
    analyze_session_health,
)

# Rates are read from the table at the event's own timestamp rather than written
# in here as numbers. A promotional rate would otherwise make these fixtures
# disagree with the code they exercise while the promotion runs, and agree again
# the day it lapses -- a test that passes on a calendar rather than on behaviour.
MODEL = "claude-sonnet-5"


def _session(session_id: str = "s1", *, tool: str = "claude-code", model: str | None = MODEL) -> LocalSession:
    now = datetime.now(timezone.utc)
    return LocalSession(
        session_id=session_id,
        tool=tool,
        project_path="/repo",
        started_at=now - timedelta(hours=1),
        updated_at=now,
        model=model,
    )


def _event(
    index: int,
    *,
    session_id: str = "s1",
    tool: str = "claude-code",
    tokens_in: int = 100_000,
    tokens_out: int = 1_000,
    cache_read: int = 0,
    cache_write: int = 0,
    model: str | None = MODEL,
) -> LocalEvent:
    """One model_usage event, priced the way the scanner prices it.

    tokens_in is all billed input and the cache buckets are a subset of it, so
    cost is built from the uncached remainder plus the discounted cache reads.
    """
    when = datetime.now(timezone.utc) - timedelta(minutes=10 - index)
    rates = lookup(model, when) or {"in": 0.0, "out": 0.0}
    pricing_in, pricing_out = float(rates["in"]), float(rates["out"])
    uncached = max(0, tokens_in - cache_read - cache_write)
    cost = (
        uncached * pricing_in
        + cache_write * pricing_in * CACHE_WRITE_5M_MULTIPLIER
        + tokens_out * pricing_out
    ) / 1_000_000 + cache_read_cost(model, cache_read, when)
    return LocalEvent(
        event_id=f"e{index}",
        session_id=session_id,
        tool=tool,
        event_type="model_usage",
        timestamp=when,
        project_path="/repo",
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost,
    )


class BloatIsMeasuredInDollarsTests(unittest.TestCase):
    """The token share of replayed context is 78-99% for every real session,
    so it ranks nothing. These pin the cost-share definition that replaced it.
    """

    def test_bloat_ratio_is_cost_share_not_token_share(self) -> None:
        # 95% of billed input is replayed, but cache reads bill at 0.1x, so
        # they are a much smaller share of the dollars.
        events = [_event(i, tokens_in=100_000, cache_read=95_000) for i in range(4)]
        health = analyze_session_health(_session(), events)

        assert health is not None
        self.assertTrue(health.bloat_measurable)
        # The old token-share metric would have reported ~0.95 here.
        self.assertLess(health.bloat_ratio, 0.60)
        self.assertAlmostEqual(
            health.bloat_ratio,
            health.replayed_cost_usd / health.analyzed_cost_usd,
            places=9,
        )

    def test_efficiency_is_the_complement_of_bloat(self) -> None:
        events = [_event(i, cache_read=90_000) for i in range(4)]
        health = analyze_session_health(_session(), events)

        assert health is not None
        self.assertAlmostEqual(
            health.efficiency_pct, (1 - health.bloat_ratio) * 100, places=9
        )

    def test_replay_heavy_session_trips_the_bloat_flags(self) -> None:
        # Almost all input is cached and there is very little new output, so
        # the discounted replay still dominates the bill.
        events = [
            _event(i, tokens_in=400_000, tokens_out=200, cache_read=399_000, cache_write=500)
            for i in range(5)
        ]
        health = analyze_session_health(_session(), events)

        assert health is not None
        self.assertGreater(health.bloat_ratio, EXTREME_BLOAT_RATIO)
        self.assertTrue(health.is_high_bloat)
        self.assertTrue(health.is_extreme_bloat)
        self.assertEqual(health.severity, "critical")

    def test_fresh_session_with_little_replay_is_not_flagged(self) -> None:
        events = [
            _event(i, tokens_in=20_000, tokens_out=4_000, cache_read=2_000, cache_write=1_000)
            for i in range(4)
        ]
        health = analyze_session_health(_session(), events)

        assert health is not None
        self.assertLess(health.bloat_ratio, HIGH_BLOAT_RATIO)
        self.assertFalse(health.is_high_bloat)
        self.assertFalse(health.is_extreme_bloat)

    def test_replayed_cost_is_priced_at_the_cache_read_rate(self) -> None:
        events = [_event(i, tokens_in=100_000, cache_read=50_000) for i in range(4)]
        health = analyze_session_health(_session(), events)

        assert health is not None
        # Each event's 50k cache reads at that event's own rate x the read
        # discount -- the relationship under test, not a fixed dollar figure.
        expected = sum(cache_read_cost(MODEL, 50_000, e.timestamp) for e in events)
        self.assertAlmostEqual(health.replayed_cost_usd, expected, places=9)
        self.assertGreater(expected, 0.0)

    def test_latest_turn_replayed_tokens_is_measured_not_derived(self) -> None:
        events = [_event(i, cache_read=10_000) for i in range(3)]
        events.append(_event(3, cache_read=77_777))
        health = analyze_session_health(_session(), events)

        assert health is not None
        self.assertEqual(health.latest_turn_replayed_tokens, 77_777)


class UnmeasurableReplayTests(unittest.TestCase):
    """Codex reports no cache buckets at all. Reading that as 0% replay would
    claim a perfectly efficient session on the strength of missing data.
    """

    def test_source_without_cache_buckets_is_unmeasurable(self) -> None:
        events = [
            _event(i, tool="codex-cli", session_id="c1", cache_read=0, cache_write=0)
            for i in range(4)
        ]
        health = analyze_session_health(_session("c1", tool="codex-cli"), events)

        assert health is not None
        self.assertFalse(health.bloat_measurable)
        self.assertEqual(health.bloat_ratio, 0.0)
        self.assertFalse(health.is_high_bloat)
        self.assertFalse(health.is_extreme_bloat)

    def test_unmeasurable_session_never_reads_as_healthy_by_default(self) -> None:
        # Nothing about a missing cache count should be reported as a clean bill.
        events = [
            _event(i, tool="codex-cli", session_id="c1", cache_read=0, cache_write=0)
            for i in range(4)
        ]
        health = analyze_session_health(_session("c1", tool="codex-cli"), events)

        assert health is not None
        self.assertEqual(health.efficiency_pct, 0.0)
        self.assertNotIn(
            "efficiency", " ".join(health.recommendations).lower()
        )

    def test_cache_writes_alone_prove_the_source_reports_cache(self) -> None:
        # A session that got no read hits genuinely replayed nothing; that is a
        # real 0%, distinguishable from "the source never told us".
        events = [_event(i, cache_read=0, cache_write=5_000) for i in range(4)]
        health = analyze_session_health(_session(), events)

        assert health is not None
        self.assertTrue(health.bloat_measurable)
        self.assertEqual(health.bloat_ratio, 0.0)

    def test_subscription_model_is_unmeasurable(self) -> None:
        # Priced at zero, so there is no bill to take a share of.
        events = [
            _event(i, model="gpt-5.3-codex", cache_read=90_000, cache_write=1_000)
            for i in range(4)
        ]
        for event in events:
            event.cost_usd = 0.0
        health = analyze_session_health(_session(), events)

        assert health is not None
        self.assertFalse(health.bloat_measurable)
        self.assertEqual(health.bloat_ratio, 0.0)


class ContextResetTests(unittest.TestCase):
    """A reset is one huge negative delta, and averaging it in inverts the answer.

    These pin the case that motivated the change: a session growing steadily,
    compacted once, whose whole-session mean delta comes out *negative* — so the
    dashboard would print "context accumulating" next to a growth figure claiming
    the session is shrinking, and any projection built on it would never fire.
    """

    def _health(self, values: list[int], model: str | None = MODEL):
        events = [_event(i, tokens_in=v) for i, v in enumerate(values)]
        return analyze_session_health(_session(model=model), events)

    def test_reset_delta_is_excluded_from_growth_rate(self) -> None:
        # +8K a turn throughout, with one reset from 74K down to 20K.
        health = self._health([50_000, 58_000, 66_000, 74_000, 20_000, 28_000, 36_000])
        assert health is not None
        self.assertEqual(health.context_resets, 1)
        self.assertAlmostEqual(health.growth_rate, 8_000.0)
        # Without the exclusion this is -2,333: the single -54K delta outweighs
        # five +8K ones, and a steadily growing session reports as shrinking.
        self.assertGreater(health.growth_rate, 0)

    def test_projection_uses_only_the_segment_since_the_last_reset(self) -> None:
        health = self._health([50_000, 58_000, 66_000, 74_000, 20_000, 28_000, 36_000])
        assert health is not None
        self.assertEqual(health.turns_since_reset, 2)
        self.assertAlmostEqual(health.segment_growth_rate, 8_000.0)
        # From 36K at +8K/turn, Sonnet 5's 1M window is 120.5 turns out.
        self.assertEqual(health.context_window, 1_000_000)
        self.assertEqual(health.turns_to_critical, 121)

    def test_no_projection_when_the_session_is_not_on_that_trajectory(self) -> None:
        flat = self._health([100_000, 100_000, 100_000, 100_000])
        assert flat is not None
        self.assertEqual(flat.context_resets, 0)
        self.assertIsNone(flat.turns_to_critical)

        at_window = self._health([185_000, 190_000, 195_000, 200_000], model="claude-haiku-4-5")
        assert at_window is not None
        self.assertEqual(at_window.context_window, 200_000)
        self.assertIsNone(at_window.turns_to_critical)

    def test_small_session_jitter_is_not_a_reset(self) -> None:
        """Below the floor, a halving is noise — every session would show resets."""
        health = self._health([15_000, 5_000, 15_000, 5_000, 15_000])
        assert health is not None
        self.assertEqual(health.context_resets, 0)
        self.assertEqual(health.turns_since_reset, 4)

    def test_turns_since_reset_covers_the_whole_session_when_none_happened(self) -> None:
        health = self._health([50_000, 58_000, 66_000, 74_000])
        assert health is not None
        self.assertEqual(health.context_resets, 0)
        self.assertEqual(health.turns_since_reset, 3)
        self.assertAlmostEqual(health.segment_growth_rate, health.growth_rate)


class ContextWindowIsTheModelsOwnTests(unittest.TestCase):
    """The ceiling a turn is judged against is the model's window, not a constant.

    The old 150K/200K thresholds were Claude's 200K window applied to every
    model. A Codex session could read "211K against a 200K limit, no headroom
    left" at just over half of its real 400K window, and 1M-window sessions could
    be marked past the limit the same way.
    """

    def _health(self, values: list[int], model: str | None):
        events = [_event(i, tokens_in=v, model=model) for i, v in enumerate(values)]
        return analyze_session_health(_session(model=model), events)

    def test_the_same_turn_is_judged_against_each_models_own_window(self) -> None:
        turns = [150_000, 170_000, 190_000, 211_000]
        codex = self._health(turns, "gpt-5-codex")
        sonnet = self._health(turns, "claude-sonnet-5")
        assert codex is not None and sonnet is not None
        self.assertEqual(codex.context_window, 400_000)
        self.assertEqual(sonnet.context_window, 1_000_000)
        for health in (codex, sonnet):
            self.assertFalse(health.is_context_critical)
            self.assertEqual(health.severity, "healthy")
            self.assertIsNotNone(health.turns_to_critical)
        # Nearer its window, so fewer turns of headroom.
        self.assertLess(codex.turns_to_critical, sonnet.turns_to_critical)

    def test_at_the_window_is_the_one_thing_size_alone_makes_critical(self) -> None:
        health = self._health([150_000, 170_000, 190_000, 200_000], "claude-haiku-4-5")
        assert health is not None
        self.assertTrue(health.is_context_critical)
        self.assertEqual(health.severity, "critical")
        self.assertIn("200,000 window", " ".join(health.recommendations))

    def test_nothing_below_the_window_is_a_verdict(self) -> None:
        # 95% of the window, no bloat, not stale: healthy. There is no amber tier.
        health = self._health([180_000, 185_000, 190_000, 190_000], "claude-haiku-4-5")
        assert health is not None
        self.assertEqual(health.severity, "healthy")
        self.assertEqual(health.recommendations, ["Context is healthy."])

    def test_an_unknown_model_has_no_ceiling(self) -> None:
        health = self._health([500_000, 600_000, 700_000, 800_000], "model-nobody-knows")
        assert health is not None
        self.assertIsNone(health.context_window)
        self.assertIsNone(health.turns_to_critical)
        self.assertFalse(health.is_context_critical)
        self.assertEqual(health.severity, "healthy")

    def test_a_turn_bigger_than_the_table_allows_means_the_table_is_stale(self) -> None:
        # The provider accepted a 250K turn, so the window is not 200K whatever
        # the table says. Reporting "past the limit" here would be the original
        # defect wearing a lookup.
        health = self._health([150_000, 200_000, 225_000, 250_000], "claude-haiku-4-5")
        assert health is not None
        self.assertIsNone(health.context_window)
        self.assertIsNone(health.turns_to_critical)
        self.assertFalse(health.is_context_critical)

    def test_claude_codes_1m_suffix_names_the_bigger_window(self) -> None:
        health = self._health([150_000, 200_000, 225_000, 250_000], "claude-sonnet-4-5[1m]")
        assert health is not None
        self.assertEqual(health.context_window, 1_000_000)


if __name__ == "__main__":
    unittest.main()
