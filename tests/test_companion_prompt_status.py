from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aiwatcher_cli import ui
from aiwatcher_cli.local_state import default_companion_preferences
from aiwatcher_cli.scanner import LocalSession
from aiwatcher_cli.session_presence import SessionPresence

NOW = datetime.now(timezone.utc)


def session(session_id: str, *, tool: str = "claude-code", path: str | None = None) -> LocalSession:
    return LocalSession(
        session_id=session_id, tool=tool, project_path="/repo/aiwatcher-local",
        source_path=path or f"/tmp/{session_id}.jsonl", model="claude-opus-5",
        started_at=NOW - timedelta(hours=1), updated_at=NOW,
    )


def presence(session_id: str, state: str, *, tool: str = "claude-code", analyst_run: bool = False) -> SessionPresence:
    return SessionPresence(
        session_id=session_id, tool=tool, state=state, label=state, measurable=True,
        project_path="/repo/aiwatcher-local", analyst_run=analyst_run,
    )


def segment(**overrides) -> dict:
    # Stamped when built, not when the module loads: the full suite can reach
    # these tests minutes after import, and "3 min" would read as "5 min".
    base = {
        "turn": 7, "prompt": "ok build it", "at": (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(),
        "requests": 4, "priced": True, "cost_usd": 1.35, "cost_resent_usd": 1.0, "cost_recached_usd": 0.0,
        "context_before": 350_000, "context_after": 374_500, "took_seconds": 170, "gap_seconds": 60,
        "cache_lifetime_seconds": 3600, "compact_summary": False,
    }
    base.update(overrides)
    return base


class PromptStatusBlockTests(unittest.TestCase):
    """Each live Claude Code chat's current prompt, in the same figures as the
    session review, named by the chat and never by the prompt."""

    def block(self, rows, sessions, prompts):
        with patch.object(ui, "_current_prompt_cached", side_effect=lambda path: prompts.get(path, (None, None))):
            return ui._prompt_status_block(rows, sessions)

    def test_a_working_prompt_shows_what_it_is_costing_so_far(self) -> None:
        block = self.block([presence("s1", "working")], [session("s1")],
                           {"/tmp/s1.jsonl": (segment(), "Headroom display bug")})
        (item,) = block["items"]
        self.assertTrue(item["working"])
        self.assertEqual(item["name"], "Headroom display bug")
        self.assertEqual(item["rest"], f"{ui.money(1.35)} so far · +{ui.compact_int(24_500)} · 3 min")
        self.assertEqual(item["tag"], "working 3 min")
        self.assertNotIn("ok build it", str(block))

    def test_a_quiet_chat_shows_the_receipt(self) -> None:
        block = self.block([presence("s1", "quiet")], [session("s1")],
                           {"/tmp/s1.jsonl": (segment(cost_usd=8.66, context_after=438_300, took_seconds=840), "Headroom display bug")})
        (item,) = block["items"]
        self.assertFalse(item["working"])
        self.assertEqual(item["rest"], f"{ui.money(8.66)} · +{ui.compact_int(88_300)} · 14 min")
        self.assertEqual(item["tag"], "done")

    def test_a_receipt_after_a_break_names_the_break(self) -> None:
        block = self.block([presence("s1", "quiet")], [session("s1")], {"/tmp/s1.jsonl": (
            segment(cost_usd=2.57, cost_resent_usd=0.02, cost_recached_usd=2.5, gap_seconds=42_300, took_seconds=7), None)})
        (item,) = block["items"]
        self.assertEqual(item["rest"], f"{ui.money(2.57)} · re-cached after 11h 45m away")

    def test_a_prompt_just_sent_is_working_at_nothing_so_far(self) -> None:
        just_sent = segment(requests=0, cost_usd=0.0, context_after=None, took_seconds=None,
                            at=(NOW - timedelta(seconds=10)).isoformat())
        block = self.block([presence("s1", "working")], [session("s1")], {"/tmp/s1.jsonl": (just_sent, None)})
        (item,) = block["items"]
        self.assertTrue(item["rest"].startswith("$0.00 so far"))
        # No title in the log: the chat is named by tool and project.
        self.assertEqual(item["name"], f"{ui.tool_label('claude-code')} · aiwatcher-local")

    def test_only_live_claude_code_chats_the_user_started_are_shown(self) -> None:
        rows = [
            presence("quiet-nothing", "quiet"),
            presence("gone", "gone"),
            presence("codex", "working", tool="codex-cli"),
            presence("analyst", "working", analyst_run=True),
        ]
        sessions = [session("quiet-nothing"), session("gone"), session("codex", tool="codex-cli"), session("analyst")]
        prompts = {f"/tmp/{sid}.jsonl": (segment(requests=0, cost_usd=0.0), None) for sid in ("quiet-nothing", "gone", "codex", "analyst")}
        self.assertIsNone(self.block(rows, sessions, prompts))

    def test_working_chats_come_first(self) -> None:
        block = self.block(
            [presence("done", "quiet"), presence("busy", "working")],
            [session("done"), session("busy")],
            {"/tmp/done.jsonl": (segment(), "Done chat"), "/tmp/busy.jsonl": (segment(), "Busy chat")},
        )
        self.assertEqual([item["name"] for item in block["items"]], ["Busy chat", "Done chat"])
        self.assertEqual((block["working"], block["done"]), (1, 1))


def item(name: str, *, working: bool, cost: float = 1.0, rest: str = "$1.00 so far · +2.0k · 1 min") -> dict:
    return {
        "session_id": name, "name": name, "working": working, "rest": rest,
        "tag": "working 1 min" if working else "done", "cost_usd": cost, "at": NOW.isoformat(),
        "url": f"/?session={name}",
    }


class PromptStatusStateTests(unittest.TestCase):
    def test_one_chat_is_a_headline_within_the_bar_caps(self) -> None:
        state = ui._prompt_status_state({"badge": None}, {"items": [
            item("A very long chat name that will not fit on the bar", working=True, rest="$12.34 so far · +123.4k · 12 min"),
        ], "working": 1, "done": 0})
        self.assertEqual(state["state"], "prompt_status")
        self.assertEqual(state["label"], "Working")
        self.assertLessEqual(len(state["subtitle"]), 46)
        self.assertTrue(state["subtitle"].endswith("$12.34 so far · +123.4k · 12 min"))
        self.assertEqual(state["waiting_sessions"], [])
        self.assertIsNone(state["badge"])

    def test_several_chats_get_a_row_each(self) -> None:
        state = ui._prompt_status_state({"badge": None}, {"items": [
            item("Busy", working=True, cost=1.35), item("Done", working=False, cost=8.66, rest="$8.66 · +88.3k · 14 min"),
        ], "working": 1, "done": 1})
        self.assertEqual(state["label"], "1 working · 1 done")
        self.assertEqual([row["kind"] for row in state["waiting_sessions"]], ["prompt_working", "prompt_done"])
        self.assertEqual(state["waiting_sessions"][1]["text"], "Done · $8.66 · +88.3k · 14 min")
        self.assertEqual(state["subtitle"], f"{ui.money(10.01)} across 2 chats' current prompts")

    def test_rows_stop_at_the_bars_five_and_a_long_count_still_fits(self) -> None:
        items = [item(f"chat {n}", working=True) for n in range(12)] + [item("last", working=False)]
        state = ui._prompt_status_state({"badge": None}, {"items": items, "working": 12, "done": 1})
        self.assertEqual(len(state["waiting_sessions"]), ui.PROMPT_STATUS_MAX_ROWS)
        self.assertLessEqual(len(state["label"]), 18)
        self.assertEqual(state["label"], "13 chats")


class CompanionOrderTests(unittest.TestCase):
    """The nudge always wins; the prompt status takes the resting bar."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(self.tmp.name, "state.json")})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def build(self, *, compact=None, prompts=None, finished=None, prefs=None):
        with (
            patch.object(ui, "build_summary_cached", return_value={"watcher": {"running": True}, "handoff_decisions": []}),
            patch.object(ui, "_cached_session_rows", return_value=[]),
            patch.object(ui, "_compact_block", return_value=compact),
            patch.object(ui, "_prompt_status_block", return_value=prompts),
            patch.object(ui, "_finished_rows", return_value=finished or []),
            patch.object(ui, "companion_preferences", return_value=prefs or default_companion_preferences()),
            patch.object(ui, "_update_away_digest"),
            patch.object(ui, "_active_away_digest", return_value=None),
        ):
            return ui.build_companion_state()

    def test_the_prompt_status_takes_the_resting_bar(self) -> None:
        state = self.build(prompts={"items": [item("Busy", working=True)], "working": 1, "done": 0})
        self.assertEqual(state["state"], "prompt_status")
        self.assertEqual(state["label"], "Working")

    def test_the_compact_nudge_wins_over_the_prompt_status(self) -> None:
        compact = {"sessions": [{"stage": "nudge"}]}
        with patch.object(ui, "_compact_companion_state", return_value={"state": "compact_recommended"}) as nudge:
            state = self.build(compact=compact, prompts={"items": [item("Busy", working=True)], "working": 1, "done": 0})
        self.assertEqual(state["state"], "compact_recommended")
        nudge.assert_called_once()

    def test_the_compact_nudge_wins_over_a_finished_run(self) -> None:
        prefs = {**default_companion_preferences(), "finished_sessions": "expanded"}
        finished = [(time.time(), presence("done", "quiet"))]
        with patch.object(ui, "_compact_companion_state", return_value={"state": "compact_recommended"}):
            state = self.build(compact={"sessions": [{"stage": "nudge"}]}, finished=finished, prefs=prefs)
        self.assertEqual(state["state"], "compact_recommended")

    def test_a_finished_run_carries_its_prompt_receipt(self) -> None:
        prefs = {**default_companion_preferences(), "finished_sessions": "expanded"}
        finished = [(time.time(), presence("done", "quiet"))]
        receipt = item("done", working=False, rest="$8.66 · +88.3k · 14 min")
        receipt["name"] = "Headroom display bug"
        state = self.build(finished=finished, prefs=prefs, prompts={"items": [receipt], "working": 0, "done": 1})
        self.assertEqual(state["state"], "session_finished")
        self.assertEqual(state["subtitle"], "Headroom display bug · $8.66 · +88.3k · 14 min")


if __name__ == "__main__":
    unittest.main()
