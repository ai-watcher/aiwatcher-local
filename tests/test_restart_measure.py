"""Fresh Start before/after: the last turns before a restart against the first turns after it."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiwatcher_cli import restart_measure, scanner
from aiwatcher_cli.scanner import LocalSession


T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def _iso(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _transcript(directory: Path, name: str, turns: list[tuple[int, int, int]], *, compact_after_turn: int | None = None) -> Path:
    """turns: (minute, input_tokens, cache_read_tokens) per prompt; each gets one assistant reply with two tool calls."""
    rows = []
    for index, (minute, tokens_in, cache_read) in enumerate(turns):
        rows.append({"type": "user", "timestamp": _iso(minute), "cwd": "/tmp/repo",
                     "message": {"role": "user", "content": [{"type": "text", "text": f"prompt {index + 1} in {name}"}]}})
        rows.append({"type": "assistant", "timestamp": _iso(minute), "cwd": "/tmp/repo", "requestId": f"{name}-{index}",
                     "message": {"role": "assistant", "model": "claude-sonnet-4-6",
                                 "content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {}}, {"type": "tool_use", "id": "b", "name": "Read", "input": {}}],
                                 "usage": {"input_tokens": tokens_in, "output_tokens": 100, "cache_read_input_tokens": cache_read}}})
        if compact_after_turn == index + 1:
            rows.append({"type": "system", "subtype": "compact_boundary", "timestamp": _iso(minute), "cwd": "/tmp/repo"})
    path = directory / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _session(session_id: str, path: Path, *, started: datetime, updated: datetime) -> LocalSession:
    return LocalSession(session_id=session_id, tool="claude-code", project_path="/tmp/repo",
                        started_at=started, updated_at=updated, source_path=str(path))


class MeasureRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        scanner.SEGMENT_CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        # Eight turns getting heavier, then a restart at minute 60 into a light session.
        self.source = _session("src", _transcript(self.dir, "src", [(m, 100_000 + m * 10_000, 50_000 + m * 5_000, ) for m in range(0, 56, 7)]),
                               started=T0, updated=T0 + timedelta(minutes=55))
        self.next = _session("nxt", _transcript(self.dir, "nxt", [(61 + m, 20_000, 2_000) for m in range(0, 25, 5)]),
                             started=T0 + timedelta(minutes=61), updated=T0 + timedelta(minutes=90))
        self.decision = {"decision": "copy_handoff", "created_at": _iso(60), "source_session_id": "src", "next_session_id": "nxt",
                         "next_session_correlation": {"status": "linked"}}

    def test_measures_last_five_before_against_first_five_after(self) -> None:
        result = restart_measure.measure_restart(decision=self.decision, source=self.source, next_session=self.next)
        self.assertEqual(result["status"], "measured")
        self.assertEqual(result["before"]["turns"], 5)
        self.assertEqual(result["after"]["turns"], 5)
        # Before: minutes 28..56 -> input 380k..660k plus cache, output 100; after: 22k + 100 each.
        self.assertGreater(result["before"]["tokens_per_turn"], result["after"]["tokens_per_turn"])
        self.assertGreater(result["before"]["cache_read_per_turn"], result["after"]["cache_read_per_turn"])
        self.assertGreater(result["tokens_per_turn_change_pct"], 90)
        self.assertEqual(result["after"]["tool_calls_per_turn"], 2.0)
        self.assertIn("Fresh Start cut this work", result["label"])
        self.assertIn("tool calls", result["label"])
        self.assertFalse(result["compacted_before"])

    def test_only_turns_before_the_restart_count_as_before(self) -> None:
        early = dict(self.decision, created_at=_iso(20))  # restart after the 3rd prompt
        result = restart_measure.measure_restart(decision=early, source=self.source, next_session=self.next)
        self.assertEqual(result["before"]["turns"], 3)

    def test_a_filling_after_window_is_measuring_not_a_partial_number(self) -> None:
        short = _session("nxt2", _transcript(self.dir, "nxt2", [(61, 20_000, 0), (62, 20_000, 0)]),
                         started=T0 + timedelta(minutes=61), updated=T0 + timedelta(minutes=62))
        result = restart_measure.measure_restart(decision=self.decision, source=self.source, next_session=short)
        self.assertEqual(result["status"], "measuring")
        self.assertEqual(result["after_turns_so_far"], 2)
        self.assertIsNone(result["after"])
        self.assertIsNone(result["tokens_per_turn_change_pct"])

    def test_unlinked_and_ambiguous_restarts_are_named_not_zeroed(self) -> None:
        waiting = dict(self.decision, next_session_id=None, next_session_correlation={"status": "waiting"})
        result = restart_measure.measure_restart(decision=waiting, source=self.source, next_session=None)
        self.assertEqual(result["status"], "unlinked")
        ambiguous = dict(self.decision, next_session_correlation={"status": "ambiguous"})
        result = restart_measure.measure_restart(decision=ambiguous, source=self.source, next_session=None)
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNotNone(result["before"], "the before window is known even when the after is not")

    def test_compaction_inside_the_before_window_is_disclosed(self) -> None:
        compacted = _session("cmp", _transcript(self.dir, "cmp", [(m, 200_000, 100_000) for m in range(0, 56, 7)], compact_after_turn=6),
                             started=T0, updated=T0 + timedelta(minutes=55))
        result = restart_measure.measure_restart(decision=self.decision, source=compacted, next_session=self.next)
        self.assertTrue(result["compacted_before"])
        self.assertIn("already compacted", result["label"])

    def test_a_dismissed_nudge_is_not_a_restart(self) -> None:
        result = restart_measure.measure_restart(decision={"decision": "dismissed", "created_at": _iso(60)}, source=self.source, next_session=self.next)
        self.assertEqual(result["status"], "not_a_restart")

    def test_the_improve_figure_is_a_median_and_honest_when_empty(self) -> None:
        measured = restart_measure.measure_restart(decision=self.decision, source=self.source, next_session=self.next)
        waiting = restart_measure.measure_restart(decision=dict(self.decision, next_session_correlation={"status": "waiting"}), source=self.source, next_session=None)
        summary = restart_measure.summarize_restarts([measured, waiting, {"status": "not_a_restart"}])
        self.assertEqual((summary["taken"], summary["measured"], summary["unlinked"]), (2, 1, 1))
        self.assertTrue(summary["measurable"])
        self.assertEqual(summary["median_tokens_per_turn_change_pct"], measured["tokens_per_turn_change_pct"])
        empty = restart_measure.summarize_restarts([{"status": "not_a_restart"}])
        self.assertFalse(empty["measurable"])
        self.assertIn("No Fresh Start", empty["reason"])


if __name__ == "__main__":
    unittest.main()
