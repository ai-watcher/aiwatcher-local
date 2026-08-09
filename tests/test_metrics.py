from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from aiwatcher_cli.metrics import (
    model_cost_comparison,
    pace_vs_baseline,
    replayed_context_cost,
)
from aiwatcher_cli.pricing import CACHE_READ_MULTIPLIER, cache_read_cost
from aiwatcher_cli.scanner import LocalEvent, LocalSession


def session(
    session_id: str,
    *,
    model: str = "claude-sonnet-5",
    cost: float = 1.0,
    tokens_in: int = 1_000,
    tokens_out: int = 100,
    cache_read: int = 0,
    days_ago: float = 0.0,
) -> LocalSession:
    stamp = datetime.now().astimezone() - timedelta(days=days_ago)
    return LocalSession(
        session_id=session_id,
        tool="claude-code",
        project_path="/repo",
        started_at=stamp,
        updated_at=stamp,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read_tokens=cache_read,
        cost_usd=cost,
    )


class ModelCostComparisonTests(unittest.TestCase):
    def test_needs_two_models_with_enough_sessions(self) -> None:
        rows = [session(f"s{i}", model="claude-sonnet-5") for i in range(10)]
        result = model_cost_comparison(rows)
        self.assertFalse(result["available"])
        self.assertEqual(result["models_eligible"], 1)

    def test_a_thin_model_is_excluded_rather_than_compared(self) -> None:
        rows = [session(f"s{i}", model="claude-sonnet-5") for i in range(6)]
        rows += [session("o1", model="claude-opus-5", cost=99.0)]
        self.assertFalse(model_cost_comparison(rows)["available"])

    def test_bigger_sessions_at_the_same_rate_read_as_volume_driven(self) -> None:
        # Both models cost the same per token; one is simply used for 10x
        # larger sessions. Reporting only cost-per-session would blame the
        # model when the real story is how it gets used.
        big = [session(f"b{i}", model="claude-sonnet-5", cost=10.0,
                       tokens_in=10_000_000, tokens_out=0) for i in range(5)]
        small = [session(f"s{i}", model="claude-opus-5", cost=1.0,
                         tokens_in=1_000_000, tokens_out=0) for i in range(5)]
        result = model_cost_comparison(big + small)

        self.assertTrue(result["available"])
        self.assertEqual(result["driver"], "volume")
        self.assertAlmostEqual(result["volume_factor"], 10.0, places=1)
        self.assertAlmostEqual(result["rate_factor"], 1.0, places=2)

    def test_same_size_sessions_at_a_higher_rate_read_as_rate_driven(self) -> None:
        dear = [session(f"d{i}", model="claude-opus-5", cost=10.0,
                        tokens_in=1_000_000, tokens_out=0) for i in range(5)]
        cheap = [session(f"c{i}", model="claude-sonnet-5", cost=1.0,
                         tokens_in=1_000_000, tokens_out=0) for i in range(5)]
        result = model_cost_comparison(dear + cheap)

        self.assertEqual(result["driver"], "rate")
        self.assertAlmostEqual(result["volume_factor"], 1.0, places=2)
        self.assertAlmostEqual(result["rate_factor"], 10.0, places=1)

    def test_flags_when_dearest_per_session_is_cheapest_per_token(self) -> None:
        # The trap this metric exists to avoid: a card built on cost-per-session
        # alone would recommend switching TO the pricier-per-token model.
        heavy_cheap_rate = [session(f"h{i}", model="claude-sonnet-5", cost=20.0,
                                    tokens_in=100_000_000, tokens_out=0) for i in range(5)]
        light_dear_rate = [session(f"l{i}", model="claude-opus-5", cost=1.0,
                                   tokens_in=1_000_000, tokens_out=0) for i in range(5)]
        result = model_cost_comparison(heavy_cheap_rate + light_dear_rate)

        self.assertTrue(result["rankings_disagree"])
        self.assertEqual(result["by_session"]["dearest"]["model"], "claude-sonnet-5")
        self.assertEqual(result["by_token"]["cheapest"]["model"], "claude-sonnet-5")
        self.assertLess(result["rate_factor"], 1.0)

    def test_subscription_models_are_excluded_not_counted_as_free(self) -> None:
        rows = [session(f"s{i}", model="claude-sonnet-5") for i in range(5)]
        rows += [session(f"g{i}", model="gpt-5.2-codex", cost=0.0) for i in range(5)]
        result = model_cost_comparison(rows)
        self.assertFalse(result["available"])
        self.assertEqual(result["models_eligible"], 1)


def pace_event(name: str, *, cost: float, days_ago: float) -> LocalEvent:
    return LocalEvent(
        event_id=name, session_id=name, tool="claude-code", event_type="assistant",
        timestamp=datetime.now().astimezone() - timedelta(days=days_ago),
        project_path="/repo", model="claude-sonnet-5", cost_usd=cost,
    )


class PaceVsBaselineTests(unittest.TestCase):
    def test_needs_prior_windows_with_activity(self) -> None:
        events = [pace_event(f"e{i}", cost=1.0, days_ago=1) for i in range(5)]
        result = pace_vs_baseline(events)
        self.assertFalse(result["available"])
        self.assertEqual(result["baseline_windows"], 0)

    def test_reports_ratio_against_prior_windows(self) -> None:
        events = [
            pace_event("now", cost=20.0, days_ago=1),
            pace_event("w1", cost=10.0, days_ago=9),
            pace_event("w2", cost=10.0, days_ago=16),
        ]
        result = pace_vs_baseline(events)

        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["current_usd"], 20.0, places=6)
        self.assertAlmostEqual(result["baseline_usd"], 10.0, places=6)
        self.assertAlmostEqual(result["ratio"], 2.0, places=2)
        self.assertEqual(result["direction"], "above")

    def test_idle_windows_are_dropped_not_counted_as_zero(self) -> None:
        # A fortnight away from the keyboard must not halve the baseline and
        # make the return week look like a spike.
        events = [
            pace_event("now", cost=10.0, days_ago=1),
            pace_event("w1", cost=10.0, days_ago=9),
            pace_event("w4", cost=10.0, days_ago=25),
        ]
        result = pace_vs_baseline(events)

        self.assertEqual(result["baseline_windows"], 2)
        self.assertAlmostEqual(result["baseline_usd"], 10.0, places=6)
        self.assertAlmostEqual(result["ratio"], 1.0, places=2)

    def test_spend_counts_in_the_window_it_happened_in(self) -> None:
        # The bug this replaced: bucketing a whole session by its last-touch
        # time credited the current window with spend from earlier ones, so the
        # reported "excess" could exceed the window's entire spend.
        events = [
            pace_event("recent-turn", cost=5.0, days_ago=1),
            pace_event("older-turn", cost=10.0, days_ago=9),
            pace_event("oldest-turn-same-session", cost=95.0, days_ago=20),
        ]
        result = pace_vs_baseline(events)

        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["current_usd"], 5.0, places=6)
        # The $95 sits in its own prior window, not in the current one.
        self.assertAlmostEqual(result["baseline_usd"], 52.5, places=6)

    def test_zero_cost_events_are_ignored(self) -> None:
        events = [
            pace_event("now", cost=10.0, days_ago=1),
            pace_event("meta", cost=0.0, days_ago=1),
            pace_event("w1", cost=5.0, days_ago=9),
            pace_event("w2", cost=5.0, days_ago=16),
        ]
        self.assertAlmostEqual(pace_vs_baseline(events)["current_usd"], 10.0, places=6)


class ReplayedContextCostTests(unittest.TestCase):
    def test_unavailable_without_meaningful_replay(self) -> None:
        rows = [session("s1", cache_read=0)]
        self.assertFalse(replayed_context_cost(rows)["available"])

    def test_prices_replay_at_the_cache_rate_not_the_full_input_rate(self) -> None:
        # The claim is the discount, not a dollar figure: replay is priced at
        # 0.1x the model's input rate *as it stood when the session ran*, so the
        # expectation is read from the same table rather than written in here.
        row = session("s1", model="claude-sonnet-5", cache_read=1_000_000,
                      tokens_in=1_000_000, tokens_out=0, cost=1.0)
        result = replayed_context_cost([row])
        expected = cache_read_cost("claude-sonnet-5", 1_000_000, row.updated_at)

        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["sessions"][0]["replayed_usd"], expected, places=6)
        # Full input rate would be 10x this; that gap is the whole point.
        full_rate = expected / CACHE_READ_MULTIPLIER
        self.assertLess(result["sessions"][0]["replayed_usd"], full_rate)

    def test_reports_replay_as_a_share_of_the_session(self) -> None:
        # Session cost is set to exactly twice the replayed cost so the expected
        # share is 50% at whatever rate is in effect.
        probe = session("s1", model="claude-sonnet-5", cache_read=1_000_000,
                        tokens_in=1_000_000, tokens_out=0, cost=1.0)
        replayed = cache_read_cost("claude-sonnet-5", 1_000_000, probe.updated_at)
        rows = [session("s1", model="claude-sonnet-5", cache_read=1_000_000,
                        tokens_in=1_000_000, tokens_out=0, cost=replayed * 2)]
        row = replayed_context_cost(rows)["sessions"][0]

        self.assertEqual(row["replayed_pct"], 100.0)
        self.assertAlmostEqual(row["share_of_session_pct"], 50.0, places=1)

    def test_ranks_by_replay_cost_not_replay_volume(self) -> None:
        # A cheap model replaying more tokens should still rank below a dear
        # model replaying fewer, because the dollar figure is what matters.
        rows = [
            session("cheap", model="claude-sonnet-5", cache_read=2_000_000,
                    tokens_in=2_000_000, cost=5.0),
            session("dear", model="claude-fable-5", cache_read=1_500_000,
                    tokens_in=1_500_000, cost=5.0),
        ]
        result = replayed_context_cost(rows)
        self.assertEqual(result["sessions"][0]["session_id"], "dear")

    def test_subscription_model_is_skipped(self) -> None:
        rows = [session("s1", model="gpt-5.2-codex", cache_read=5_000_000,
                        tokens_in=5_000_000, cost=0.0)]
        self.assertFalse(replayed_context_cost(rows)["available"])


if __name__ == "__main__":
    unittest.main()
