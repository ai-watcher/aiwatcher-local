from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiwatcher_cli import scanner, ui
from aiwatcher_cli.scanner import LocalSession
from aiwatcher_cli.session_presence import SessionPresence

T0 = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)


def claude_row(minutes: float, **fields) -> dict:
    return {"timestamp": (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"), **fields}


class ScannedTitleTests(unittest.TestCase):
    """A session carries what the chat is called."""

    def scan(self, rows: list[dict]) -> LocalSession:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            (projects / "-repo").mkdir(parents=True)
            path = projects / "-repo" / "chat-1.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                (session,) = scanner.scan_claude_code()
        return session

    def rows(self, *extra: dict) -> list[dict]:
        return [
            claude_row(0, type="user", uuid="u1", cwd="/repo", message={"role": "user", "content": "build it"}),
            claude_row(1, type="assistant", uuid="a1", requestId="r1", cwd="/repo", message={
                "model": "claude-opus-5", "usage": {"input_tokens": 10, "output_tokens": 20}, "content": []}),
            *extra,
        ]

    def test_the_users_name_wins_over_the_generated_one(self) -> None:
        session = self.scan(self.rows(
            {"type": "ai-title", "aiTitle": "Generated name"},
            {"type": "custom-title", "customTitle": "Headroom display bug"},
        ))
        self.assertEqual(session.title, "Headroom display bug")

    def test_the_generated_name_is_used_when_the_user_gave_none(self) -> None:
        self.assertEqual(self.scan(self.rows({"type": "ai-title", "aiTitle": "Generated name"})).title, "Generated name")

    def test_a_renamed_chat_takes_its_latest_name(self) -> None:
        session = self.scan(self.rows(
            {"type": "custom-title", "customTitle": "First"},
            {"type": "custom-title", "customTitle": "Second"},
        ))
        self.assertEqual(session.title, "Second")

    def test_a_chat_with_no_name_has_none(self) -> None:
        self.assertIsNone(self.scan(self.rows()).title)

    def test_the_attach_marker_is_not_part_of_the_prompt(self) -> None:
        text = scanner._user_prompt_text("<!-- attach -->\n> 20k, 30k\n\nI don't get it")
        self.assertEqual(text, "> 20k, 30k\n\nI don't get it")
        self.assertIsNone(scanner._user_prompt_text("<!-- attach -->"))


class SessionPayloadTests(unittest.TestCase):
    def test_session_rows_carry_the_name_the_start_and_the_short_id(self) -> None:
        session = LocalSession(
            session_id="0b4bfd95-4c9b-45d2-8301-4d44178f005b", tool="claude-code", project_path="/repo",
            started_at=T0, updated_at=T0 + timedelta(hours=1), title="Headroom display bug",
        )
        row = ui._session_row_json(session, {}, {})
        self.assertEqual(row["title"], "Headroom display bug")
        self.assertEqual(row["started_at"], T0.isoformat())
        self.assertEqual(row["session_short"], ui.short_session_id(session.session_id))

    def test_the_cached_summary_is_invalidated_for_the_new_fields(self) -> None:
        self.assertGreaterEqual(ui.SUMMARY_CACHE_SCHEMA_VERSION, 9)

    def test_the_saved_session_index_keeps_the_name(self) -> None:
        # The dashboard restores sessions from session-index.json on start; a
        # name dropped there showed every chat by project and tool until the
        # next full scan.
        session = LocalSession(session_id="s1", tool="claude-code", project_path="/repo", title="Issue exploration")
        (saved,) = ui._session_index_payload([session])
        self.assertEqual(ui._session_from_json(saved).title, "Issue exploration")
        self.assertIsNone(ui._session_from_json({**saved, "title": None}).title)
        # An index written before titles existed is not restored.
        self.assertGreaterEqual(ui.SESSION_SNAPSHOT_SCHEMA_VERSION, 2)


class CompanionNameTests(unittest.TestCase):
    def test_the_bar_uses_the_scanned_name_without_rereading_the_transcript(self) -> None:
        session = LocalSession(
            session_id="s1", tool="claude-code", project_path="/repo/app", source_path="/tmp/s1.jsonl",
            started_at=T0, updated_at=T0, title="Headroom display bug",
        )
        row = SessionPresence(session_id="s1", tool="claude-code", state="working", label="working", measurable=True)
        segment = {"prompt": "x", "at": datetime.now(timezone.utc).isoformat(), "requests": 1, "priced": True,
                   "cost_usd": 1.0, "cost_resent_usd": 0.5, "cost_recached_usd": 0.0,
                   "context_before": 1000, "context_after": 2000, "took_seconds": 5, "gap_seconds": 30}
        with patch.object(ui, "_current_prompt_cached", return_value=(segment, None)) as cached:
            block = ui._prompt_status_block([row], [session])
        self.assertEqual(block["items"][0]["name"], "Headroom display bug")
        self.assertFalse(cached.call_args.kwargs["read_title"])


class PageTests(unittest.TestCase):
    """The page names chats by name and tells matching names apart."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = (ui._WEB_DIR / "index.js").read_text(encoding="utf-8")
        cls.html = (ui._WEB_DIR / "index.html").read_text(encoding="utf-8")

    def function(self, name: str) -> str:
        start = self.js.index(f"function {name}(")
        end = self.js.index("\nfunction ", start + 1)
        return self.js[start:end]

    def test_a_chat_without_a_name_falls_back_to_project_and_tool(self) -> None:
        source = self.function("chatName")
        self.assertIn("row.title || row.session_title", source)
        self.assertIn("[projectName(row), row && row.tool]", source)

    def test_the_session_review_leads_with_the_chat_name_and_keeps_the_id(self) -> None:
        hero = self.function("renderSessionHero")
        self.assertIn("s.title || s.project_short", hero)
        strip = self.function("renderIdentityStrip")
        self.assertIn("shortSessionId(sessionId)", strip)

    def test_the_sessions_table_leads_with_the_chat(self) -> None:
        self.assertIn("setSessionSort('title')\">Chat", self.html)
        rows = self.function("renderSessionRows")
        self.assertIn("duplicateChatNames(rows)", rows)
        self.assertIn("sharedNames.has(chatName(s))", rows)
        self.assertIn("chatDisambiguation(s)", rows)

    def test_only_matching_names_are_told_apart(self) -> None:
        source = self.function("duplicateChatNames")
        self.assertIn("count > 1", source)
        self.assertRegex(self.function("chatDisambiguation"), re.compile(r"started \$\{started\}"))


if __name__ == "__main__":
    unittest.main()
