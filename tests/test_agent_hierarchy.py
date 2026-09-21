from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
    "evidence_source",
    "evidence_at",
    "confidence",
    "stale_after",
    "relationship_status",
    "metadata_warning",
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
                    name TEXT,
                    rollout_path TEXT
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
        rollout_path: Path | None = None,
    ) -> None:
        created_at = updated_at.timestamp() - 60
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO threads (
                    id, cwd, title, first_user_message, preview,
                    created_at, updated_at, created_at_ms, updated_at_ms,
                    archived, agent_nickname, agent_role, name, rollout_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    str(rollout_path) if rollout_path else None,
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

    def _scan(
        self,
        db_path: Path,
        *,
        since: datetime | None = None,
        current_thread_id: str = "",
    ) -> dict:
        scanner._CODEX_LIFECYCLE_CACHE.clear()
        with (
            patch.object(scanner, "CODEX_DB_PATHS", [db_path]),
            patch.object(scanner, "CODEX_SESSIONS_DIRS", [db_path.parent]),
            patch.dict(
                os.environ,
                {"CODEX_THREAD_ID": current_thread_id, "CODEX_SESSION_ID": ""},
            ),
        ):
            return scanner.scan_codex_agent_hierarchy(since=since)

    def _write_rollout(
        self,
        directory: Path,
        thread_id: str,
        events: list[tuple[str, datetime]],
        *,
        malformed_tail: bool = False,
    ) -> Path:
        path = directory / f"rollout-{thread_id}.jsonl"
        rows = [
            json.dumps({
                "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                "type": "event_msg",
                "payload": {"type": event},
            })
            for event, timestamp in events
        ]
        if malformed_tail:
            rows.append("{not-json")
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        if events:
            modified_at = events[-1][1].timestamp()
            os.utime(path, (modified_at, modified_at))
        return path

    def test_nested_hierarchy_maps_edge_and_archive_statuses(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            db_path = directory / "state_5.sqlite"
            self._create_db(db_path)
            root_rollout = self._write_rollout(directory, "root", [("task_started", now)])
            chloe_rollout = self._write_rollout(directory, "chloe", [("task_started", now)])
            adam_rollout = self._write_rollout(
                directory,
                "adam",
                [("task_started", now), ("task_complete", now)],
            )
            darwin_rollout = self._write_rollout(directory, "darwin", [("task_started", now)])
            self._insert_thread(db_path, "root", updated_at=now, rollout_path=root_rollout)
            self._insert_thread(
                db_path,
                "chloe",
                updated_at=now,
                nickname="Chloe",
                role="explorer",
                rollout_path=chloe_rollout,
            )
            self._insert_thread(
                db_path,
                "adam",
                updated_at=now,
                nickname="Adam",
                role="reviewer",
                rollout_path=adam_rollout,
            )
            self._insert_thread(
                db_path,
                "darwin",
                updated_at=now,
                nickname="Darwin",
                role="worker",
                archived=True,
                rollout_path=darwin_rollout,
            )
            self._insert_edge(db_path, "root", "chloe", "open")
            self._insert_edge(db_path, "chloe", "adam", "closed")
            self._insert_edge(db_path, "root", "darwin", "open")

            result = self._scan(db_path)

        self.assertTrue(result["available"])
        self.assertEqual(result["source"], "codex-topology-and-rollout-lifecycle")
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
        self.assertEqual(agents["chloe"]["relationship_status"], "open")
        self.assertEqual(agents["adam"]["parent_agent_id"], "chloe")
        self.assertEqual((agents["adam"]["status"], agents["adam"]["latest_event"]), ("completed", "returned"))
        self.assertEqual(agents["adam"]["relationship_status"], "closed")
        self.assertEqual(agents["darwin"]["parent_agent_id"], "root")
        self.assertEqual(
            (agents["darwin"]["status"], agents["darwin"]["latest_event"]),
            ("interrupted", "interrupted"),
        )

    def test_completed_rollout_overrides_stale_open_edge(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            db_path = directory / "state_5.sqlite"
            self._create_db(db_path)
            root_rollout = self._write_rollout(
                directory,
                "root",
                [("task_started", now), ("task_complete", now)],
            )
            child_rollout = self._write_rollout(
                directory,
                "child",
                [("task_started", now), ("task_complete", now)],
                malformed_tail=True,
            )
            self._insert_thread(db_path, "root", updated_at=now, rollout_path=root_rollout)
            self._insert_thread(
                db_path,
                "child",
                updated_at=now,
                nickname="Einstein",
                rollout_path=child_rollout,
            )
            self._insert_edge(db_path, "root", "child", "open")

            result = self._scan(db_path)

        session = result["sessions"][0]
        agents = {agent["agent_id"]: agent for agent in session["agents"]}
        self.assertEqual((session["status"], session["active_count"]), ("idle", 0))
        self.assertEqual(session["returned_count"], 1)
        self.assertEqual(session["stale_record_count"], 1)
        self.assertEqual((agents["root"]["status"], agents["root"]["latest_event"]), ("idle", "idle"))
        self.assertEqual(
            (agents["child"]["status"], agents["child"]["latest_event"]),
            ("completed", "returned"),
        )
        self.assertEqual(agents["child"]["metadata_warning"], "stale_open_edge")
        self.assertEqual(agents["child"]["confidence"], "high")

    def test_unmatched_started_event_uses_activity_freshness(self) -> None:
        now = datetime.now(timezone.utc)
        old = now - scanner.CODEX_AGENT_RUNNING_FRESHNESS - timedelta(minutes=1)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            db_path = directory / "state_5.sqlite"
            self._create_db(db_path)
            root_rollout = self._write_rollout(directory, "root", [("task_started", now)])
            fresh_rollout = self._write_rollout(directory, "fresh", [("task_started", now)])
            stale_rollout = self._write_rollout(directory, "stale", [("task_started", old)])
            self._insert_thread(db_path, "root", updated_at=now, rollout_path=root_rollout)
            self._insert_thread(db_path, "fresh", updated_at=now, rollout_path=fresh_rollout)
            self._insert_thread(db_path, "stale", updated_at=old, rollout_path=stale_rollout)
            self._insert_edge(db_path, "root", "fresh", "open")
            self._insert_edge(db_path, "root", "stale", "open")

            result = self._scan(db_path)

        session = result["sessions"][0]
        agents = {agent["agent_id"]: agent for agent in session["agents"]}
        self.assertEqual(session["active_count"], 2)
        self.assertEqual(session["stale_count"], 1)
        self.assertEqual(agents["fresh"]["status"], "running")
        self.assertEqual(agents["stale"]["status"], "stale")
        self.assertIsNotNone(agents["stale"]["stale_after"])

    def test_open_edge_without_rollout_is_unknown_not_running(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state_5.sqlite"
            self._create_db(db_path)
            self._insert_thread(db_path, "root", updated_at=now)
            self._insert_thread(db_path, "child", updated_at=now)
            self._insert_edge(db_path, "root", "child", "open")

            result = self._scan(db_path)

        session = result["sessions"][0]
        agents = {agent["agent_id"]: agent for agent in session["agents"]}
        self.assertEqual(session["active_count"], 0)
        self.assertEqual(agents["root"]["status"], "unknown")
        self.assertEqual(agents["child"]["status"], "unknown")
        self.assertEqual(agents["child"]["confidence"], "low")

    def test_launch_thread_environment_identifies_and_prioritizes_its_session(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            db_path = directory / "state_5.sqlite"
            self._create_db(db_path)
            for prefix, updated_at in (("other", now), ("current", now - timedelta(minutes=1))):
                root = f"{prefix}-root"
                child = f"{prefix}-child"
                root_rollout = self._write_rollout(directory, root, [("task_started", updated_at)])
                child_rollout = self._write_rollout(directory, child, [("task_started", updated_at)])
                self._insert_thread(db_path, root, updated_at=updated_at, rollout_path=root_rollout)
                self._insert_thread(db_path, child, updated_at=updated_at, rollout_path=child_rollout)
                self._insert_edge(db_path, root, child, "open")

            result = self._scan(db_path, current_thread_id="current-child")

        self.assertEqual(result["sessions"][0]["session_id"], "current-root")
        self.assertTrue(result["sessions"][0]["is_launch_session"])
        self.assertEqual(result["sessions"][0]["launch_agent_id"], "current-child")
        self.assertFalse(result["sessions"][1]["is_launch_session"])

    def test_working_child_makes_session_working_without_changing_root_status(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            db_path = directory / "state_5.sqlite"
            self._create_db(db_path)
            root_rollout = self._write_rollout(
                directory,
                "root",
                [("task_started", now), ("task_complete", now)],
            )
            child_rollout = self._write_rollout(directory, "child", [("task_started", now)])
            self._insert_thread(db_path, "root", updated_at=now, rollout_path=root_rollout)
            self._insert_thread(db_path, "child", updated_at=now, rollout_path=child_rollout)
            self._insert_edge(db_path, "root", "child", "open")

            result = self._scan(db_path)

        session = result["sessions"][0]
        self.assertEqual(session["status"], "running")
        self.assertEqual(session["root_status"], "idle")
        self.assertEqual(session["active_count"], 1)

    def test_stale_child_makes_idle_root_session_stale(self) -> None:
        now = datetime.now(timezone.utc)
        old = now - scanner.CODEX_AGENT_RUNNING_FRESHNESS - timedelta(minutes=1)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            db_path = directory / "state_5.sqlite"
            self._create_db(db_path)
            root_rollout = self._write_rollout(
                directory,
                "root",
                [("task_started", old), ("task_complete", old)],
            )
            child_rollout = self._write_rollout(directory, "child", [("task_started", old)])
            self._insert_thread(db_path, "root", updated_at=old, rollout_path=root_rollout)
            self._insert_thread(db_path, "child", updated_at=old, rollout_path=child_rollout)
            self._insert_edge(db_path, "root", "child", "open")

            result = self._scan(db_path)

        session = result["sessions"][0]
        self.assertEqual(session["status"], "stale")
        self.assertEqual(session["root_status"], "idle")
        self.assertEqual(session["active_count"], 0)
        self.assertEqual(session["stale_count"], 1)

    def test_latest_lifecycle_record_wins_over_event_timestamp_order(self) -> None:
        now = datetime.now(timezone.utc)
        later_timestamp = now + timedelta(minutes=1)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            rollout = self._write_rollout(
                directory,
                "agent",
                [("task_complete", later_timestamp), ("task_started", now)],
            )
            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [directory]):
                lifecycle = scanner._latest_codex_lifecycle_event(rollout)

        self.assertIsNotNone(lifecycle)
        self.assertEqual(lifecycle["event"], "task_started")

    def test_lifecycle_cache_invalidates_when_rollout_is_appended(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            rollout = self._write_rollout(directory, "agent", [("task_started", now)])
            scanner._CODEX_LIFECYCLE_CACHE.clear()
            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [directory]):
                first = scanner._latest_codex_lifecycle_event(rollout)
                with rollout.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "timestamp": now.isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_complete"},
                    }) + "\n")
                second = scanner._latest_codex_lifecycle_event(rollout)

        self.assertEqual(first["event"], "task_started")
        self.assertEqual(second["event"], "task_complete")

    def test_archived_rollout_directory_supplies_completion_evidence(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            sessions_dir = directory / "sessions"
            archived_dir = directory / "archived_sessions"
            sessions_dir.mkdir()
            archived_dir.mkdir()
            db_path = directory / "state_5.sqlite"
            self._create_db(db_path)
            root_rollout = self._write_rollout(
                archived_dir,
                "root",
                [("task_started", now), ("task_complete", now)],
            )
            child_rollout = self._write_rollout(
                archived_dir,
                "child",
                [("task_started", now), ("task_complete", now)],
            )
            self._insert_thread(
                db_path, "root", updated_at=now, archived=True, rollout_path=root_rollout
            )
            self._insert_thread(
                db_path, "child", updated_at=now, archived=True, rollout_path=child_rollout
            )
            self._insert_edge(db_path, "root", "child", "closed")
            scanner._CODEX_LIFECYCLE_CACHE.clear()
            with (
                patch.object(scanner, "CODEX_DB_PATHS", [db_path]),
                patch.object(scanner, "CODEX_SESSIONS_DIRS", [sessions_dir]),
                patch.object(scanner, "CODEX_ARCHIVED_SESSIONS_DIRS", [archived_dir]),
                patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CODEX_SESSION_ID": ""}),
            ):
                result = scanner.scan_codex_agent_hierarchy()

        agents = {agent["agent_id"]: agent for agent in result["sessions"][0]["agents"]}
        self.assertEqual(agents["root"]["status"], "idle")
        self.assertEqual(agents["child"]["status"], "completed")
        self.assertEqual(agents["child"]["evidence_source"], "rollout_lifecycle")

    def test_request_budget_bounds_uncached_scans_and_does_not_cache_exhaustion(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            first_path = self._write_rollout(directory, "first", [("task_complete", now)])
            second_path = self._write_rollout(directory, "second", [("task_complete", now)])
            scanner._CODEX_LIFECYCLE_CACHE.clear()
            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [directory]):
                limited = scanner._CodexRolloutScanBudget(
                    bytes_remaining=1024 * 1024,
                    files_remaining=1,
                    records_remaining=100,
                )
                first = scanner._latest_codex_lifecycle_event(
                    first_path, request_budget=limited
                )
                second_limited = scanner._latest_codex_lifecycle_event(
                    second_path, request_budget=limited
                )
                fresh = scanner._CodexRolloutScanBudget(
                    bytes_remaining=1024 * 1024,
                    files_remaining=1,
                    records_remaining=100,
                )
                first_cached = scanner._latest_codex_lifecycle_event(
                    first_path, request_budget=fresh
                )
                second = scanner._latest_codex_lifecycle_event(
                    second_path, request_budget=fresh
                )

        self.assertEqual(first["event"], "task_complete")
        self.assertEqual(second_limited["event"], "scan_budget_exhausted")
        self.assertEqual(first_cached["event"], "task_complete")
        self.assertEqual(second["event"], "task_complete")
        self.assertEqual(fresh.files_remaining, 0)

    def test_rollout_scan_budgets_degrade_to_unknown(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            db_path = directory / "state_5.sqlite"
            self._create_db(db_path)
            root_rollout = self._write_rollout(
                directory,
                "root",
                [("task_started", now), ("task_complete", now)],
            )
            child_rollout = self._write_rollout(directory, "child", [("task_started", now)])
            with child_rollout.open("a", encoding="utf-8") as handle:
                for index in range(20):
                    handle.write(json.dumps({"type": "response_item", "index": index}) + "\n")
            self._insert_thread(db_path, "root", updated_at=now, rollout_path=root_rollout)
            self._insert_thread(db_path, "child", updated_at=now, rollout_path=child_rollout)
            self._insert_edge(db_path, "root", "child", "open")

            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [directory]):
                with (
                    patch.object(scanner, "CODEX_ROLLOUT_SCAN_MAX_BYTES", 128),
                    patch.object(scanner, "CODEX_ROLLOUT_SCAN_MAX_RECORDS", 10_000),
                ):
                    scanner._CODEX_LIFECYCLE_CACHE.clear()
                    byte_limited = scanner._latest_codex_lifecycle_event(child_rollout)
                with (
                    patch.object(scanner, "CODEX_ROLLOUT_SCAN_MAX_BYTES", 1024 * 1024),
                    patch.object(scanner, "CODEX_ROLLOUT_SCAN_MAX_RECORDS", 5),
                ):
                    scanner._CODEX_LIFECYCLE_CACHE.clear()
                    record_limited = scanner._latest_codex_lifecycle_event(child_rollout)
            with patch.object(scanner, "CODEX_ROLLOUT_SCAN_MAX_BYTES", 128):
                result = self._scan(db_path)

        self.assertEqual(byte_limited["event"], "scan_budget_exhausted")
        self.assertEqual(record_limited["event"], "scan_budget_exhausted")
        child = next(agent for agent in result["sessions"][0]["agents"] if agent["agent_id"] == "child")
        self.assertEqual((child["status"], child["latest_event"]), ("unknown", "scan_limited"))
        self.assertEqual(child["evidence_source"], "rollout_scan_budget")
        self.assertEqual(child["confidence"], "low")

    def test_rollout_symlink_cannot_escape_codex_sessions_directory(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside_dir:
            directory = Path(temp_dir)
            outside = self._write_rollout(Path(outside_dir), "outside", [("task_complete", now)])
            link = directory / "linked.jsonl"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlinks are unavailable on this platform")
            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [directory]):
                lifecycle = scanner._latest_codex_lifecycle_event(link)

        self.assertIsNone(lifecycle)

    def test_future_rollout_activity_degrades_to_unknown(self) -> None:
        now = datetime.now(timezone.utc)
        future = now + scanner.CODEX_AGENT_CLOCK_SKEW_TOLERANCE + timedelta(minutes=1)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            db_path = directory / "state_5.sqlite"
            self._create_db(db_path)
            root_rollout = self._write_rollout(directory, "root", [("task_started", future)])
            child_rollout = self._write_rollout(directory, "child", [("task_started", future)])
            self._insert_thread(db_path, "root", updated_at=now, rollout_path=root_rollout)
            self._insert_thread(db_path, "child", updated_at=now, rollout_path=child_rollout)
            self._insert_edge(db_path, "root", "child", "open")

            result = self._scan(db_path)
            second_result = self._scan(db_path)

        agents = {agent["agent_id"]: agent for agent in result["sessions"][0]["agents"]}
        self.assertEqual(agents["root"]["status"], "unknown")
        self.assertEqual(agents["child"]["status"], "unknown")
        self.assertEqual(agents["child"]["evidence_source"], "rollout_lifecycle+clock_skew")
        self.assertLessEqual(
            datetime.fromisoformat(agents["child"]["evidence_at"]),
            datetime.now(timezone.utc),
        )
        self.assertLessEqual(
            datetime.fromisoformat(result["sessions"][0]["updated_at"]),
            datetime.now(timezone.utc),
        )
        self.assertEqual(
            result["sessions"][0]["updated_at"],
            second_result["sessions"][0]["updated_at"],
        )

    def test_future_completion_degrades_to_unknown(self) -> None:
        now = datetime.now(timezone.utc)
        future = now + scanner.CODEX_AGENT_CLOCK_SKEW_TOLERANCE + timedelta(minutes=1)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            db_path = directory / "state_5.sqlite"
            self._create_db(db_path)
            root_rollout = self._write_rollout(
                directory,
                "root",
                [("task_started", future), ("task_complete", future)],
            )
            child_rollout = self._write_rollout(
                directory,
                "child",
                [("task_started", future), ("task_complete", future)],
            )
            self._insert_thread(db_path, "root", updated_at=future, rollout_path=root_rollout)
            self._insert_thread(db_path, "child", updated_at=future, rollout_path=child_rollout)
            self._insert_edge(db_path, "root", "child", "closed")

            result = self._scan(db_path)

        agents = {agent["agent_id"]: agent for agent in result["sessions"][0]["agents"]}
        self.assertEqual(agents["root"]["status"], "unknown")
        self.assertEqual(agents["child"]["status"], "unknown")
        self.assertEqual(agents["child"]["latest_event"], "clock_skew")
        self.assertIsNone(result["sessions"][0]["updated_at"])
        self.assertIsNone(agents["child"]["evidence_at"])
        self.assertIsNone(agents["child"]["updated_at"])

    def test_oversized_rollout_content_is_skipped_without_leaking(self) -> None:
        now = datetime.now(timezone.utc)
        secret = "SECRET-ROLLOUT-CONTENT"
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            db_path = directory / "state_5.sqlite"
            self._create_db(db_path)
            root_rollout = self._write_rollout(
                directory,
                "root",
                [("task_started", now), ("task_complete", now)],
            )
            child_rollout = self._write_rollout(
                directory,
                "child",
                [("task_started", now), ("task_complete", now)],
            )
            with child_rollout.open("ab") as handle:
                handle.write(
                    json.dumps({
                        "type": "response_item",
                        "payload": secret * (scanner.CODEX_ROLLOUT_MAX_LINE_BYTES // len(secret) + 10),
                    }).encode("utf-8") + b"\n"
                )
            os.utime(child_rollout, (now.timestamp(), now.timestamp()))
            self._insert_thread(db_path, "root", updated_at=now, rollout_path=root_rollout)
            self._insert_thread(db_path, "child", updated_at=now, rollout_path=child_rollout)
            self._insert_edge(db_path, "root", "child", "open")

            result = self._scan(db_path)

        child = next(agent for agent in result["sessions"][0]["agents"] if agent["agent_id"] == "child")
        self.assertEqual(child["status"], "completed")
        self.assertNotIn(secret, json.dumps(result))

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
        self.assertEqual((agents["recent-child"]["status"], agents["recent-child"]["latest_event"]), ("unknown", "relationship_closed"))
        self.assertEqual(agents["recent-child"]["confidence"], "low")

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
        self.assertEqual(
            (agents["child"]["status"], agents["child"]["latest_event"]),
            ("unknown", "archived"),
        )
        self.assertEqual(agents["child"]["confidence"], "low")

    def test_archived_closed_edge_is_unknown_without_lifecycle(self) -> None:
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
        self.assertEqual(
            (agents["root"]["status"], agents["root"]["latest_event"]),
            ("unknown", "archived"),
        )
        self.assertEqual(
            (agents["child"]["status"], agents["child"]["latest_event"]),
            ("unknown", "archived"),
        )
        self.assertEqual(agents["child"]["evidence_source"], "thread_metadata")
        self.assertEqual(agents["child"]["confidence"], "low")

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
        self.assertEqual(agents["child"]["updated_at"], now.isoformat())

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

    def test_undated_orphan_is_excluded_from_a_date_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite"
            self._create_db(db_path)
            self._insert_edge(db_path, "missing-root", "missing-child", "open")
            result = self._scan(db_path, since=datetime(2026, 9, 19, tzinfo=timezone.utc))
        self.assertEqual(result["sessions"], [])
        self.assertEqual(result["undated_count"], 1)

    def test_edge_limit_is_explicit_and_private_columns_are_never_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite"
            self._create_db(db_path)
            now = datetime.now(timezone.utc)
            for index in range(5):
                self._insert_thread(db_path, str(index), updated_at=now)
            for index in range(1, 5):
                self._insert_edge(db_path, "0", str(index), "open")
            connect = sqlite3.connect
            def safe_connection(*args, **kwargs):
                conn = connect(*args, **kwargs)
                def authorize(action, table, column, *_):
                    if action == sqlite3.SQLITE_READ and table == "threads" and column in {"title", "first_user_message", "preview", "name"}:
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                conn.set_authorizer(authorize)
                return conn
            with patch.object(scanner, "AGENT_HIERARCHY_EDGE_LIMIT", 2), patch.object(scanner.sqlite3, "connect", side_effect=safe_connection):
                result = self._scan(db_path)
        self.assertTrue(result["available"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["sessions"][0]["agent_count"], 3)
        self.assertEqual(result["sessions"][0]["active_count"], 0)

    def test_cycles_do_not_hang_or_invent_a_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite"
            self._create_db(db_path)
            self._insert_edge(db_path, "a", "b", "open")
            self._insert_edge(db_path, "b", "a", "open")
            result = self._scan(db_path)
        self.assertEqual(result["sessions"], [])

    def test_limited_scan_recovers_the_real_root(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.sqlite"
            self._create_db(db)
            old = datetime(2025, 1, 1, tzinfo=timezone.utc)
            now = datetime(2026, 9, 20, tzinfo=timezone.utc)
            for ident, stamp in (("root", old), ("middle", old), ("leaf", now)):
                self._insert_thread(db, ident, updated_at=stamp)
            self._insert_edge(db, "root", "middle", "open")
            self._insert_edge(db, "middle", "leaf", "closed")
            with patch.object(scanner, "AGENT_HIERARCHY_EDGE_LIMIT", 1):
                result = self._scan(db)
        self.assertEqual(result["sessions"][0]["session_id"], "root")
        self.assertEqual(result["sessions"][0]["agent_count"], 3)

    def test_parent_timestamp_and_nullable_milliseconds_rank_recent_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.sqlite"
            self._create_db(db)
            old = datetime(2025, 1, 1, tzinfo=timezone.utc)
            now = datetime(2026, 9, 20, tzinfo=timezone.utc)
            for ident, stamp in (("root", now), ("child", old), ("expired-root", old), ("expired-child", old)):
                self._insert_thread(db, ident, updated_at=stamp)
            self._insert_edge(db, "root", "child", "open")
            self._insert_edge(db, "expired-root", "expired-child", "closed")
            conn = sqlite3.connect(db)
            try:
                conn.execute("UPDATE threads SET updated_at_ms = NULL WHERE id = 'root'")
                conn.commit()
            finally:
                conn.close()
            with patch.object(scanner, "AGENT_HIERARCHY_EDGE_LIMIT", 1):
                result = self._scan(db, since=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual([row["session_id"] for row in result["sessions"]], ["root"])

    def test_ancestry_budget_exhaustion_does_not_invent_a_root(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.sqlite"
            self._create_db(db)
            old = datetime(2025, 1, 1, tzinfo=timezone.utc)
            now = datetime(2026, 9, 20, tzinfo=timezone.utc)
            for ident in ("root", "a", "b", "leaf"):
                self._insert_thread(db, ident, updated_at=now if ident == "leaf" else old)
            for parent, child in (("root", "a"), ("a", "b"), ("b", "leaf")):
                self._insert_edge(db, parent, child, "open")
            with patch.object(scanner, "AGENT_HIERARCHY_EDGE_LIMIT", 1):
                result = self._scan(db)
        self.assertEqual(result["sessions"], [])
        self.assertTrue(result["truncated"])


class ClaudeAgentHierarchyTests(unittest.TestCase):
    def test_session_membership_does_not_read_prompts_or_claim_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = Path(temp) / "projects"
            agents = storage / "encoded-project" / "session-a" / "subagents"
            agents.mkdir(parents=True)
            transcript = agents / "agent-worker.jsonl"
            transcript.write_text('SECRET-PROMPT and PRIVATE-SOURCE', encoding="utf-8")
            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [storage]), patch.object(Path, "open", side_effect=AssertionError("Transcript content must not be opened")):
                result = scanner.scan_claude_agent_hierarchy()
        self.assertTrue(result["available"])
        session = result["sessions"][0]
        self.assertEqual(session["tool"], "claude-code")
        self.assertEqual(session["session_id"], "session-a")
        self.assertEqual(session["active_count"], 0)
        self.assertTrue(all(agent["status"] == "unknown" for agent in session["agents"]))
        self.assertEqual(session["agents"][1]["relationship_status"], "session_member")
        self.assertIn("nested parents", session["relationship_note"])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_old_files_and_directory_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = Path(temp)
            agents = storage / "project" / "session" / "subagents"
            agents.mkdir(parents=True)
            transcript = agents / "agent-old.jsonl"
            transcript.touch()
            os.utime(transcript, (1, 1))
            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [storage]):
                result = scanner.scan_claude_agent_hierarchy(since=datetime(2026, 1, 1, tzinfo=timezone.utc))
                self.assertEqual(result["sessions"], [])
                with patch.object(scanner, "AGENT_HIERARCHY_ENTRY_LIMIT", 1):
                    limited = scanner.scan_claude_agent_hierarchy()
                self.assertTrue(limited["truncated"])

    def test_tools_with_same_session_id_do_not_collide(self):
        sources = {
            "available": True, "sessions": [{"session_id": "same", "updated_at": None, "agents": []}],
        }
        with patch.object(scanner, "scan_codex_agent_hierarchy", return_value=sources), patch.object(scanner, "scan_claude_agent_hierarchy", return_value=sources):
            result = scanner.scan_agent_hierarchy()
        self.assertEqual({row["selection_id"] for row in result["sessions"]}, {"codex-cli:same", "claude-code:same"})
        self.assertIn("Cursor", result["unsupported_tools"])
        self.assertEqual(len(result["coverage"]), 2)

    def test_one_tool_unavailable_does_not_hide_the_other(self):
        codex = {"available": False, "reason": "schema unavailable", "sessions": []}
        claude = {"available": True, "sessions": [{"session_id": "c", "updated_at": None, "agents": []}]}
        with patch.object(scanner, "scan_codex_agent_hierarchy", return_value=codex), patch.object(scanner, "scan_claude_agent_hierarchy", return_value=claude):
            result = scanner.scan_agent_hierarchy()
        self.assertTrue(result["available"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["sessions"][0]["tool"], "claude-code")


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
        with patch.object(ui, "scan_agent_hierarchy", return_value=payload) as scan:
            result = ui.build_agent_hierarchy(days=7)

        self.assertEqual(result["sessions"][0]["project"], "/work/payments")
        self.assertEqual(result["sessions"][0]["project_full"], "/work/payments")
        since = scan.call_args.kwargs["since"]
        self.assertIsNotNone(since.tzinfo)
        self.assertNotIn("prompt", json.dumps(result).lower())


if __name__ == "__main__":
    unittest.main()
