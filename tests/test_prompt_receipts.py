from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiwatcher_cli import scanner, ui
from aiwatcher_cli.pricing import CACHE_READ_MULTIPLIER, CACHE_WRITE_1H_MULTIPLIER, estimate_cost, lookup

T0 = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
MODEL = "claude-opus-5"


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def prompt(uuid: str, minutes: float, text: str) -> dict:
    return {"type": "user", "uuid": uuid, "timestamp": at(minutes), "message": {"role": "user", "content": text}}


def reply_line(uuid: str, minutes: float, request_id: str, *, read: int, write_1h: int, fresh: int = 10, out: int = 100) -> dict:
    """One content-block line of a request. Claude Code writes one of these per
    block, each with the same copy of the request's usage."""
    return {
        "type": "assistant", "uuid": uuid, "requestId": request_id, "timestamp": at(minutes),
        "message": {
            "model": MODEL,
            "usage": {
                "input_tokens": fresh, "output_tokens": out, "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write_1h,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": write_1h},
            },
            "content": [{"type": "tool_use", "id": f"tool-{uuid}", "name": "Read", "input": {}}],
        },
    }


def write(rows: list[dict]) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    with handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return handle.name


class SegmentCountingTests(unittest.TestCase):
    """Per-prompt costs count what the event scan counts."""

    def setUp(self) -> None:
        self.first = prompt("p1", 0, "build the thing")
        self.rows = [
            self.first,
            # One request written as two lines: same requestId, same usage.
            reply_line("a1", 1, "req-a", read=0, write_1h=50_000),
            reply_line("a2", 1, "req-a", read=0, write_1h=50_000),
            prompt("p2", 2, "and the tests"),
            reply_line("b1", 2.5, "req-b", read=50_000, write_1h=5_000),
            # Back two hours later: the cache has expired and the chat is written again.
            prompt("p3", 120, "anything else?"),
            reply_line("c1", 120.5, "req-c", read=0, write_1h=56_000),
            # What Claude Code does at a compaction: earlier rows written again.
            dict(self.first),
        ]
        self.segments = scanner.segment_session_by_prompt(write(self.rows))
        self.price_in = float(lookup(MODEL, T0)["in"]) / 1_000_000

    def test_a_request_split_over_lines_and_a_copied_prompt_count_once(self) -> None:
        self.assertEqual(len(self.segments), 3)
        first = self.segments[0]
        self.assertEqual(first["requests"], 1)
        self.assertEqual(first["tool_calls"], 2)   # each line carries its own block
        once = estimate_cost(MODEL, 10, 100, cache_write_1h=50_000, when=T0 + timedelta(minutes=1))
        self.assertAlmostEqual(first["cost_usd"], once)

    def test_each_prompt_knows_its_sizes_times_and_pause(self) -> None:
        first, second, third = self.segments
        self.assertIsNone(first["context_before"])
        self.assertEqual(second["context_before"], 50_010)
        self.assertEqual(second["context_after"], 55_010)
        self.assertEqual(second["took_seconds"], 30)
        self.assertEqual(second["gap_seconds"], 60)
        self.assertEqual(third["gap_seconds"], round((117.5) * 60))
        self.assertEqual(third["at"], (T0 + timedelta(minutes=120)).isoformat())

    def test_the_compaction_summary_row_is_flagged_and_still_opens_a_turn(self) -> None:
        rows = self.rows[:-1] + [
            {"type": "user", "uuid": "s1", "timestamp": at(130), "isCompactSummary": True,
             "message": {"role": "user", "content": "This session is being continued from a previous conversation."}},
            reply_line("d1", 131, "req-d", read=20_000, write_1h=10_000),
        ]
        segments = scanner.segment_session_by_prompt(write(rows))
        self.assertEqual([seg["compact_summary"] for seg in segments], [False, False, False, True])

    def test_the_cost_splits_into_resent_and_recached(self) -> None:
        _, second, third = self.segments
        self.assertAlmostEqual(second["cost_resent_usd"], 50_000 * self.price_in * CACHE_READ_MULTIPLIER)
        # The chat grew 5,000 and 5,000 was written: nothing was re-cached.
        self.assertEqual(second["cost_recached_usd"], 0.0)
        # It grew 1,000 but 56,000 was written: 55,000 of the existing chat went back in.
        self.assertAlmostEqual(third["cost_recached_usd"], 55_000 * self.price_in * CACHE_WRITE_1H_MULTIPLIER)
        self.assertTrue(third["priced"])

    def test_the_cache_lifetime_follows_what_the_chat_has_written(self) -> None:
        # Nothing written yet before the first prompt; 1-hour entries after.
        self.assertEqual([seg["cache_lifetime_seconds"] for seg in self.segments], [300, 3600, 3600])


class CurrentPromptTests(unittest.TestCase):
    """Which prompt a chat is on now, for the Companion and the statusline."""

    def test_an_interrupted_marker_with_nothing_behind_it_is_passed_over(self) -> None:
        segments = [
            {"prompt": "build it", "requests": 5},
            {"prompt": "[Request interrupted by user]", "requests": 0},
        ]
        self.assertEqual(scanner.current_prompt_segment(segments)["prompt"], "build it")

    def test_a_prompt_just_sent_is_the_current_one(self) -> None:
        segments = [{"prompt": "build it", "requests": 5}, {"prompt": "now the tests", "requests": 0}]
        self.assertEqual(scanner.current_prompt_segment(segments)["prompt"], "now the tests")

    def test_claude_carrying_on_after_a_compaction_counts(self) -> None:
        segments = [{"prompt": "build it", "requests": 5}, {"prompt": "summary", "requests": 3, "compact_summary": True}]
        self.assertTrue(scanner.current_prompt_segment(segments)["compact_summary"])
        self.assertIsNone(scanner.current_prompt_segment([]))


def segment(**overrides) -> dict:
    base = {
        "turn": 1, "prompt": "do it", "at": T0.isoformat(), "requests": 4, "priced": True,
        "cost_usd": 10.0, "cost_resent_usd": 2.0, "cost_recached_usd": 7.0,
        "context_before": 500_000, "context_after": 520_000, "took_seconds": 90, "gap_seconds": 2 * 3600,
    }
    base.update(overrides)
    return base


class PromptReceiptTests(unittest.TestCase):
    def test_a_receipt_splits_the_same_money_and_names_the_break(self) -> None:
        receipts = ui.build_prompt_receipts([segment()])
        (row,) = receipts["rows"]
        self.assertEqual(row["api_value"], ui.money(10.0))
        self.assertAlmostEqual(row["new_usd"], 1.0)
        self.assertEqual(row["added_label"], "+" + ui.compact_int(20_000))
        self.assertEqual(row["took_label"], "2 min")
        self.assertEqual(row["note"], "re-cached after 2h 0m away")
        self.assertEqual((receipts["resent_share_pct"], receipts["recached_share_pct"], receipts["new_share_pct"]), (20, 70, 10))

    def test_a_short_pause_does_not_explain_a_re_cache(self) -> None:
        (row,) = ui.build_prompt_receipts([segment(gap_seconds=180)])["rows"]
        self.assertEqual(row["note"], "")
        self.assertFalse(row["after_break"])

    def test_a_pause_inside_a_one_hour_cache_is_not_a_break(self) -> None:
        within, past = ui.build_prompt_receipts([
            segment(cache_lifetime_seconds=3600, gap_seconds=13 * 60),
            segment(cache_lifetime_seconds=3600, gap_seconds=2 * 3600),
        ])["rows"]
        self.assertFalse(within["after_break"])
        self.assertEqual(within["note"], "")
        self.assertTrue(past["after_break"])

    def test_only_a_break_is_noted(self) -> None:
        # Re-sending was most of nearly every prompt's cost, so noting it on each
        # row said nothing.
        resent, balanced = ui.build_prompt_receipts([
            segment(cost_resent_usd=8.0, cost_recached_usd=0.0),
            segment(cost_resent_usd=4.0, cost_recached_usd=1.0),
        ])["rows"]
        self.assertEqual((resent["note"], balanced["note"]), ("", ""))

    def test_the_chat_level_figures_the_drawer_opens_on(self) -> None:
        receipts = ui.build_prompt_receipts([
            segment(turn=1, cost_usd=10.0, cost_resent_usd=2.0, cost_recached_usd=7.0),                  # a break
            segment(turn=2, cost_usd=4.0, cost_resent_usd=3.0, cost_recached_usd=0.0, gap_seconds=60),
            segment(turn=3, cost_usd=6.0, cost_resent_usd=5.0, cost_recached_usd=0.0, gap_seconds=60),
            segment(turn=4, cost_usd=1.0, cost_resent_usd=0.5, cost_recached_usd=0.0, gap_seconds=60),
        ])
        self.assertEqual(receipts["resent_label"], ui.money(10.5))
        self.assertEqual(receipts["recached_label"], ui.money(7.0))
        self.assertEqual(receipts["new_label"], ui.money(3.5))
        self.assertEqual([row["turn"] for row in receipts["costliest"]], [1, 3, 2])
        self.assertEqual(receipts["top_share_pct"], round(100 * 20 / 21))
        self.assertEqual(receipts["breaks"], {"count": 1, "usd": 10.0, "usd_label": ui.money(10.0), "gaps": ["2h 0m"]})

    def test_an_interrupted_reply_is_not_shown_as_a_prompt(self) -> None:
        (row,) = ui.build_prompt_receipts([segment(prompt="[Request interrupted by user for tool use]")])["rows"]
        self.assertTrue(row["interrupted"])
        self.assertTrue(row["prompt"].startswith("Interrupted"))

    def test_unmeasurable_figures_carry_their_reason(self) -> None:
        first, compacted, resumed = ui.build_prompt_receipts([
            segment(context_before=None),
            segment(context_before=500_000, context_after=80_000),
            segment(took_seconds=3 * 24 * 3600),
        ])["rows"]
        self.assertIsNone(first["added_label"])
        self.assertIn("First prompt", first["added_reason"])
        self.assertIsNone(compacted["added_label"])
        self.assertIn("compacted", compacted["added_reason"])
        self.assertIsNone(resumed["took_label"])
        self.assertIn("picked up again later", resumed["took_reason"])

    def test_an_unpriced_model_shows_no_dollar_figure(self) -> None:
        receipts = ui.build_prompt_receipts([segment(priced=False), segment()])
        unpriced = receipts["rows"][0]
        self.assertIsNone(unpriced["api_value"])
        self.assertIsNone(unpriced["cost_usd"])
        self.assertEqual(unpriced["note"], "")
        self.assertEqual(receipts["unpriced_prompts"], 1)
        self.assertEqual(receipts["total_label"], ui.money(10.0))

    def test_the_summary_after_a_compaction_is_not_shown_as_a_prompt(self) -> None:
        (row,) = ui.build_prompt_receipts([
            segment(compact_summary=True, prompt="This session is being continued from a previous conversation"),
        ])["rows"]
        self.assertTrue(row["prompt"].startswith("Picked up after a compaction"))
        self.assertNotIn("being continued", row["prompt"])

    def test_a_prompt_that_caused_no_request_is_left_out(self) -> None:
        receipts = ui.build_prompt_receipts([segment(requests=0), segment(turn=2)])
        self.assertEqual([row["turn"] for row in receipts["rows"]], [2])
        self.assertIsNone(ui.build_prompt_receipts([segment(requests=0)]))


if __name__ == "__main__":
    unittest.main()
