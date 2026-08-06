from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from aiwatcher_cli.pricing import cache_read_cost
from aiwatcher_cli.scanner import LocalEvent, LocalSession
from aiwatcher_cli.session_health import (
    EXTREME_BLOAT_RATIO,
    HIGH_BLOAT_RATIO,
    analyze_session_health,
)

MODEL = "claude-sonnet-5"  # $3.00/Mtok in, $15.00/Mtok out


def _session(session_id: str = "s1", *, tool: str = "claude-code") -> LocalSession:
    now = datetime.now(timezone.utc)
    return LocalSession(
        session_id=session_id,
        tool=tool,
        project_path="/repo",
        started_at=now - timedelta(hours=1),
        updated_at=now,
        model=MODEL,
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
    pricing_in = 3.00
    uncached = max(0, tokens_in - cache_read - cache_write)
    cost = (
        uncached * pricing_in
        + cache_write * pricing_in * 1.25
        + tokens_out * 15.00
    ) / 1_000_000 + cache_read_cost(model, cache_read)
    return LocalEvent(
        event_id=f"e{index}",
        session_id=session_id,
        tool=tool,
        event_type="model_usage",
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=10 - index),
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
        # 4 events x 50k cache reads x $3.00/Mtok x 0.1
        self.assertAlmostEqual(health.replayed_cost_usd, 4 * 50_000 * 3.00 * 0.1 / 1e6, places=9)

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


if __name__ == "__main__":
    unittest.main()
