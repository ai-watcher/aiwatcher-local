"""The Companion bar's "Task finished?" question: detected on the poll, asked once, answered into task records."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aiwatcher_cli import local_state, tasks, ui
from aiwatcher_cli.scanner import LocalSession


def _isolated_state(test: unittest.TestCase) -> None:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    env = patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp.name, "local-state.json")})
    env.start()
    test.addCleanup(env.stop)


class TaskAskStateTests(unittest.TestCase):
    def setUp(self) -> None:
        _isolated_state(self)

    def test_an_ask_is_recorded_once_and_shown_while_fresh(self) -> None:
        ask = {"task_id": "t1", "session_id": "s", "label": "review the PR", "turns": 5, "tokens": 1000, "boundary_turn": 6}
        self.assertIsNotNone(local_state.record_task_ask(ask))
        self.assertIsNone(local_state.record_task_ask(ask), "the same task must not be asked about twice")
        shown = local_state.open_task_ask()
        self.assertEqual(shown["task_id"], "t1")
        self.assertIsNone(shown["answer"])

    def test_a_stale_ask_is_not_shown(self) -> None:
        local_state.record_task_ask({"task_id": "t1", "session_id": "s", "label": "x", "boundary_turn": 2})
        later = datetime.now(timezone.utc) + timedelta(seconds=local_state.TASK_ASK_TTL_SECONDS + 60)
        self.assertIsNone(local_state.open_task_ask(later))

    def test_answering_closes_it(self) -> None:
        local_state.record_task_ask({"task_id": "t1", "session_id": "s", "label": "x", "boundary_turn": 2})
        record = local_state.answer_task_ask("t1", "done")
        self.assertEqual(record["answer"], "done")
        self.assertIsNone(local_state.open_task_ask())
        with self.assertRaises(ValueError):
            local_state.answer_task_ask("t1", "maybe")

    def test_the_server_side_answer_writes_the_matching_task_record(self) -> None:
        local_state.record_task_ask({"task_id": "t1", "session_id": "s", "label": "x", "boundary_turn": 4})
        ui._answer_task_ask("t1", "done")
        self.assertEqual(local_state.task_verdicts(), {"t1": "done"})
        local_state.record_task_ask({"task_id": "t2", "session_id": "s", "label": "y", "boundary_turn": 7})
        ui._answer_task_ask("t2", "same_task")
        self.assertEqual(local_state.task_boundary_overrides(), {"s": {7: False}})
        self.assertIsNone(ui._answer_task_ask("nope", "done"))

    def test_turn_end_is_one_stamp_per_session(self) -> None:
        local_state.record_session_turn_end(session_id="s", tool="claude-code", cwd="/repo")
        local_state.record_session_turn_end(session_id="s", tool="claude-code", cwd="/repo")
        ends = local_state.session_turn_ends()
        self.assertEqual(list(ends), ["s"])
        self.assertEqual(ends["s"]["tool"], "claude-code")


T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def _segments(count: int, *, last_started: datetime) -> list[dict]:
    prompts = ["review PR #98", "ok", "now fix the pricing table in pricing.py", "yes", "now write the release notes for v2"]
    rows = []
    for index in range(count):
        at = last_started if index == count - 1 else T0 + timedelta(minutes=index)
        rows.append({"prompt": prompts[index], "turn": index + 1, "at": at.isoformat(), "tokens": 100, "cost_usd": 0.1, "tool_calls": 1})
    return rows


class FreshBoundaryTests(unittest.TestCase):
    def _row(self) -> LocalSession:
        return LocalSession(session_id="s", tool="claude-code", project_path="/repo", started_at=T0,
                            updated_at=T0 + timedelta(minutes=30), source_path="/tmp/s.jsonl")

    def test_first_look_sets_the_baseline_and_asks_nothing(self) -> None:
        baseline: dict = {}
        with patch.object(tasks, "segment_session_by_prompt", return_value=_segments(3, last_started=T0 + timedelta(minutes=30))):
            asks = tasks.find_fresh_boundaries([self._row()], baseline=baseline, now=T0 + timedelta(minutes=31))
        self.assertEqual(asks, [])
        self.assertEqual(baseline, {"s": 2})

    def test_a_new_task_asks_about_the_one_it_closed(self) -> None:
        baseline = {"s": 2}
        now = T0 + timedelta(minutes=31)
        with patch.object(tasks, "segment_session_by_prompt", return_value=_segments(5, last_started=now - timedelta(seconds=20))):
            asks = tasks.find_fresh_boundaries([self._row()], baseline=baseline, now=now)
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["label"], "now fix the pricing table in pricing.py")
        self.assertEqual(asks[0]["turns"], 2)
        self.assertEqual(asks[0]["boundary_turn"], 5)
        self.assertEqual(baseline, {"s": 3})

    def test_an_old_split_from_the_dashboard_does_not_ask(self) -> None:
        # The count grew, but the "new" task started an hour ago: someone split
        # history, nobody switched topics just now.
        baseline = {"s": 2}
        now = T0 + timedelta(hours=2)
        with patch.object(tasks, "segment_session_by_prompt", return_value=_segments(5, last_started=T0 + timedelta(minutes=40))):
            asks = tasks.find_fresh_boundaries([self._row()], baseline=baseline, now=now)
        self.assertEqual(asks, [])


class CompanionTaskFinishedTests(unittest.TestCase):
    def setUp(self) -> None:
        _isolated_state(self)

    def _summary(self):
        return {"totals": {"window_label": "Last 7 days", "sessions": 1}, "watcher": {"running": True}}

    def _state(self, *, ask, sessions=(), signals=None):
        with (
            patch.object(ui, "build_summary_cached", return_value=self._summary()),
            patch.object(ui, "active_prompt_gate", return_value=None),
            patch.object(ui, "_cached_session_rows", return_value=list(sessions)),
            patch.object(ui, "session_waiting_signals", return_value=signals or {}),
            patch.object(ui, "_waiting_row_return_available", return_value=False),
            patch.object(ui, "find_fresh_boundaries", return_value=[]),
            patch.object(ui, "open_task_ask", return_value=ask),
        ):
            return ui.build_companion_state()

    def _ask(self):
        return {"task_id": "abc123", "session_id": "sess", "project_path": "/repo/aiwatcher-local",
                "label": "Help me review this PR raised by my…", "turns": 7, "tokens": 12_600_000,
                "cost_usd": 35.61, "tool_calls": 39, "boundary_turn": 8}

    def test_a_fresh_ask_becomes_the_bar_state(self) -> None:
        state = self._state(ask=self._ask())
        self.assertEqual(state["state"], "task_finished")
        self.assertEqual(state["label"], "Task finished?")
        self.assertLessEqual(len(state["subtitle"]), 46)
        self.assertIn("7 turns", state["subtitle"])
        self.assertIn("12.6M", state["subtitle"])
        self.assertEqual(state["task_id"], "abc123")
        self.assertEqual((state["primary_label"], state["primary_action"]), ("Done", "task_ask"))
        self.assertEqual((state["continue_label"], state["continue_action"]), ("Not done", "task_ask"))
        self.assertEqual((state["skip_label"], state["skip_state"]), ("Same", "task_finished"))

    def test_a_waiting_session_outranks_it(self) -> None:
        session = LocalSession(session_id="sess", tool="claude-code", project_path="/repo", raw_cwd="/repo",
                               updated_at=datetime.now(timezone.utc) - timedelta(minutes=9))
        signals = {"sess": {"at": (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat(), "tool": "claude-code", "kind": "permission"}}
        state = self._state(ask=self._ask(), sessions=[session], signals=signals)
        self.assertEqual(state["state"], "session_waiting")

    def test_no_ask_leaves_the_bar_alone(self) -> None:
        state = self._state(ask=None)
        self.assertNotEqual(state["state"], "task_finished")


if __name__ == "__main__":
    unittest.main()
