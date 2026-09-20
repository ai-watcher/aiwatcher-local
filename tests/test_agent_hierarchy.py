from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiwatcher_cli import scanner, ui


ROOT_KEYS = {
    "available",
    "source",
    "generated_at",
    "sessions",
}
SESSION_KEYS = {
    "session_id",
    "project_path",
    "updated_at",
    "status",
    "agent_count",
    "active_count",
    "agents",
}
AGENT_KEYS = {
    "agent_id",
    "parent_agent_id",
    "name",
    "role",
    "status",
    "latest_event",
    "created_at",
    "updated_at",
}


class CodexAgentHierarchyTests(unittest.TestCase):
    def _create_db(self, path: Path, *, with_edges: bool = True) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    title TEXT NOT NULL,
                    first_user_message TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    created_at_ms INTEGER,
                    updated_at_ms INTEGER,
                    archived INTEGER NOT NULL DEFAULT 0,
                    agent_nickname TEXT,
                    agent_role TEXT,
                    name TEXT
                )
                """
            )
            if with_edges:
                conn.execute(
                    """
                    CREATE TABLE thread_spawn_edges (
                        parent_thread_id TEXT NOT NULL,
                        child_thread_id TEXT NOT NULL PRIMARY KEY,
                        status TEXT NOT NULL
                    )
                    """
                )
            conn.commit()
        finally:
            conn.close()

    def _insert_thread(
        self,
        db_path: Path,
        thread_id: str,
        *,
        updated_at: datetime,
        nickname: str | None = None,
        role: str | None = None,
        archived: bool = False,
        project_path: str = "/work/agent-project",
    ) -> None:
        created_at = updated_at.timestamp() - 60
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO threads (
                    id, cwd, title, first_user_message, preview,
                    created_at, updated_at, created_at_ms, updated_at_ms,
                    archived, agent_nickname, agent_role, name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    project_path,
                    f"SECRET-TITLE-{thread_id}",
                    f"SECRET-PROMPT-{thread_id}",
                    f"SECRET-PREVIEW-{thread_id}",
                    int(created_at),
                    int(updated_at.timestamp()),
                    int(created_at * 1000),
                    int(updated_at.timestamp() * 1000),
                    int(archived),
                    nickname,
                    role,
                    f"SECRET-NAME-{thread_id}",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_edge(self, db_path: Path, parent: str, child: str, status: str) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) "
                "VALUES (?, ?, ?)",
                (parent, child, status),
            )
            conn.commit()
        finally:
            conn.close()

    def _scan(self, db_path: Path, *, since: datetime | None = None) -> dict:
        with patch.object(scanner, "CODEX_DB_PATHS", [db_path]):
            return scanner.scan_codex_agent_hierarchy(since=since)

    def test_nested_hierarchy_maps_edge_and_archive_statuses(self) -> None:
        now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state_5.sqlite"
            self._create_db(db_path)
            self._insert_thread(db_path, "root", updated_at=now)
            self._insert_thread(db_path, "chloe", updated_at=now, nickname="Chloe", role="explorer")
            self._insert_thread(db_path, "adam", updated_at=now, nickname="Adam", role="reviewer")
            self._insert_thread(
                db_path,
                "darwin",
                updated_at=now,
                nickname="Darwin",
                role="worker",
                archived=True,
            )
            self._insert_edge(db_path, "root", "chloe", "open")
            self._insert_edge(db_path, "chloe", "adam", "closed")
            self._insert_edge(db_path, "root", "darwin", "open")

            result = self._scan(db_path)

        self.assertTrue(result["available"])
        self.assertEqual(result["source"], "codex-sqlite-spawn-edges")
        self.assertEqual(len(result["sessions"]), 1)
        session = result["sessions"][0]
        agents = {agent["agent_id"]: agent for agent in session["agents"]}

        self.assertEqual(session["session_id"], "root")
        self.assertEqual(session["project_path"], "/work/agent-project")
        self.assertEqual(session["status"], "running")
        self.assertEqual(session["agent_count"], 4)
        self.assertEqual(session["active_count"], 2)
        self.assertEqual(set(agents), {"root", "chloe", "adam", "darwin"})

        self.assertIsNone(agents["root"]["parent_agent_id"])
        self.assertEqual((agents["root"]["status"], agents["root"]["latest_event"]), ("running", "working"))
        self.assertEqual(agents["chloe"]["parent_agent_id"], "root")
        self.assertEqual((agents["chloe"]["status"], agents["chloe"]["latest_event"]), ("running", "working"))
        self.assertEqual(agents["adam"]["parent_agent_id"], "chloe")
        self.assertEqual((agents["adam"]["status"], agents["adam"]["latest_event"]), ("completed", "returned"))
        self.assertEqual(agents["darwin"]["parent_agent_id"], "root")
        self.assertEqual((agents["darwin"]["status"], agents["darwin"]["latest_event"]), ("unknown", "unknown"))

    def test_uses_generated_names_and_safe_fallbacks_without_leaking_thread_content(self) -> None:
        now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state_5.sqlite"
            self._create_db(db_path)
            self._insert_thread(db_path, "root-private", updated_at=now)
            self._insert_thread(db_path, "named-child", updated_at=now, nickname="Chloe", role="explorer")
            self._insert_thread(db_path, "unnamed-child", updated_at=now)
            self._insert_edge(db_path, "root-private", "named-child", "closed")
            self._insert_edge(db_path, "root-private", "unnamed-child", "closed")

            result = self._scan(db_path)

        session = result["sessions"][0]
        agents = {agent["agent_id"]: agent for agent in session["agents"]}
        self.assertEqual((agents["named-child"]["name"], agents["named-child"]["role"]), ("Chloe", "explorer"))
        self.assertEqual((agents["root-private"]["name"], agents["root-private"]["role"]), ("Main agent", "orchestration"))
        self.assertEqual((agents["unnamed-child"]["name"], agents["unnamed-child"]["role"]), ("Agent unnamed-", "delegated"))

        self.assertTrue(ROOT_KEYS.issubset(result))
        self.assertTrue(SESSION_KEYS.issubset(session))
        for agent in agents.values():
            self.assertTrue(AGENT_KEYS.issubset(agent))
        serialized = json.dumps(result)
        self.assertNotIn("SECRET-TITLE", serialized)
        self.assertNotIn("SECRET-PROMPT", serialized)
        self.assertNotIn("SECRET-PREVIEW", serialized)
        self.assertNotIn("SECRET-NAME", serialized)

    def test_since_filters_old_sessions_without_inventing_root_completion(self) -> None:
        old = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        recent = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        since = datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state_5.sqlite"
            self._create_db(db_path)
            self._insert_thread(db_path, "old-root", updated_at=old)
            self._insert_thread(db_path, "old-child", updated_at=old, nickname="Adam")
            self._insert_edge(db_path, "old-root", "old-child", "closed")
            self._insert_thread(db_path, "recent-root", updated_at=recent)
            self._insert_thread(db_path, "recent-child", updated_at=recent, nickname="Darwin")
            self._insert_edge(db_path, "recent-root", "recent-child", "closed")

            result = self._scan(db_path, since=since)

        self.assertEqual([session["session_id"] for session in result["sessions"]], ["recent-root"])
        session = result["sessions"][0]
        self.assertEqual(session["status"], "unknown")
        self.assertEqual(session["active_count"], 0)
        agents = {agent["agent_id"]: agent for agent in session["agents"]}
        self.assertEqual((agents["recent-root"]["status"], agents["recent-root"]["latest_event"]), ("unknown", "unknown"))
        self.assertEqual((agents["recent-child"]["status"], agents["recent-child"]["latest_event"]), ("completed", "returned"))

    def test_archived_open_child_does_not_make_the_root_running(self) -> None:
        now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state_5.sqlite"
            self._create_db(db_path)
            self._insert_thread(db_path, "root", updated_at=now)
            self._insert_thread(db_path, "child", updated_at=now, archived=True)
            self._insert_edge(db_path, "root", "child", "open")

            result = self._scan(db_path)

        session = result["sessions"][0]
        agents = {agent["agent_id"]: agent for agent in session["agents"]}
        self.assertEqual((session["status"], session["active_count"]), ("unknown", 0))
        self.assertEqual(agents["root"]["status"], "unknown")
        self.assertEqual(agents["child"]["status"], "unknown")

    def test_closed_edge_remains_returned_when_the_thread_is_archived(self) -> None:
        now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state_5.sqlite"
            self._create_db(db_path)
            self._insert_thread(db_path, "root", updated_at=now, archived=True)
            self._insert_thread(db_path, "child", updated_at=now, archived=True)
            self._insert_edge(db_path, "root", "child", "closed")

            result = self._scan(db_path)

        session = result["sessions"][0]
        agents = {agent["agent_id"]: agent for agent in session["agents"]}
        self.assertEqual((session["status"], session["active_count"]), ("unknown", 0))
        self.assertEqual((agents["root"]["status"], agents["root"]["latest_event"]), ("unknown", "unknown"))
        self.assertEqual((agents["child"]["status"], agents["child"]["latest_event"]), ("completed", "returned"))

    def test_malformed_and_naive_timestamps_degrade_safely(self) -> None:
        now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state with hash#.sqlite"
            self._create_db(db_path)
            self._insert_thread(db_path, "root", updated_at=now)
            self._insert_thread(db_path, "child", updated_at=now)
            self._insert_edge(db_path, "root", "child", "closed")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE threads SET created_at_ms = NULL, updated_at_ms = NULL, "
                    "created_at = ?, updated_at = ? WHERE id = 'root'",
                    ("2026-09-20T11:59:00", "2026-09-20T12:00:00"),
                )
                conn.execute(
                    "UPDATE threads SET created_at_ms = ?, updated_at_ms = ? WHERE id = 'child'",
                    (9223372036854775807, 9223372036854775807),
                )
                conn.commit()
            finally:
                conn.close()

            result = self._scan(db_path, since=datetime(2026, 9, 19, tzinfo=timezone.utc))

        self.assertTrue(result["available"])
        self.assertEqual(len(result["sessions"]), 1)
        agents = {agent["agent_id"]: agent for agent in result["sessions"][0]["agents"]}
        self.assertEqual(agents["root"]["updated_at"], "2026-09-20T12:00:00+00:00")
        self.assertIsNone(agents["child"]["updated_at"])

    def test_missing_spawn_edge_table_is_reported_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state_5.sqlite"
            self._create_db(db_path, with_edges=False)
            self._insert_thread(
                db_path,
                "standalone",
                updated_at=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
            )

            result = self._scan(db_path)

        self.assertTrue(ROOT_KEYS.issubset(result))
        self.assertFalse(result["available"])
        self.assertEqual(result["source"], "codex-sqlite-spawn-edges")
        self.assertEqual(result["sessions"], [])
        self.assertIsNotNone(datetime.fromisoformat(result["generated_at"].replace("Z", "+00:00")))


class AgentHierarchyUiModelTests(unittest.TestCase):
    def test_ui_model_adds_safe_project_labels(self) -> None:
        payload = {
            "available": True,
            "source": "codex-sqlite-spawn-edges",
            "generated_at": "2026-09-20T12:00:00+00:00",
            "reason": "Observed metadata.",
            "sessions": [{
                "session_id": "root",
                "project_path": "/work/payments",
                "updated_at": "2026-09-20T12:00:00+00:00",
                "status": "running",
                "agent_count": 1,
                "active_count": 1,
                "agents": [],
            }],
        }
        with patch.object(ui, "scan_codex_agent_hierarchy", return_value=payload) as scan:
            result = ui.build_agent_hierarchy(days=7)

        self.assertEqual(result["sessions"][0]["project"], "/work/payments")
        self.assertEqual(result["sessions"][0]["project_full"], "/work/payments")
        since = scan.call_args.kwargs["since"]
        self.assertIsNotNone(since.tzinfo)
        self.assertNotIn("prompt", json.dumps(result).lower())


if __name__ == "__main__":
    unittest.main()
