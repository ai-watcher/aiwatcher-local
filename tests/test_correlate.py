from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from aiwatcher_cli import local_state
from aiwatcher_cli.correlate import _same_project, link_recent_fresh_start_receipts_to_sessions, link_recent_interventions_to_sessions
from aiwatcher_cli.scanner import LocalSession


def session(
    *,
    session_id: str = "session-1",
    tool: str = "codex-cli",
    project: str = "/repo/app",
    started_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> LocalSession:
    stamp = started_at or datetime.now(timezone.utc)
    return LocalSession(
        session_id=session_id,
        tool=tool,
        project_path=project,
        started_at=stamp,
        updated_at=updated_at or stamp + timedelta(minutes=5),
        model="gpt-5.5",
        tokens_in=1000,
        tokens_out=500,
        cost_usd=0,
        agent_calls=1,
        tool_calls=1,
    )


class CorrelateTests(unittest.TestCase):
    def test_linked_worktrees_are_the_same_project_but_a_clone_is_not(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            main = os.path.join(temp_dir, "main")
            worktree = os.path.join(temp_dir, "review")
            clone = os.path.join(temp_dir, "clone")
            Path(main).mkdir()
            subprocess.run(["git", "-C", main, "init", "-q"], check=True)
            subprocess.run(["git", "-C", main, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", main, "config", "user.email", "test@example.com"], check=True)
            Path(main, "README.md").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "-C", main, "add", "README.md"], check=True)
            subprocess.run(["git", "-C", main, "commit", "-q", "-m", "root"], check=True)
            subprocess.run(["git", "-C", main, "worktree", "add", "-q", "-b", "review", worktree], check=True)
            subprocess.run(["git", "clone", "-q", main, clone], check=True)

            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                self.assertTrue(_same_project(main, worktree))
                self.assertFalse(_same_project(main, clone))
                self.assertFalse(
                    _same_project(main, main, session_raw_cwd=clone),
                    "a fresh, unserialized session must reject its conflicting observed CWD",
                )
                decision = local_state.record_handoff_decision(
                    session_id="conflict-source",
                    decision="new_chat",
                    reason="Context pressure.",
                    source_project_path=main,
                    source_raw_cwd=clone,
                )
                linked = link_recent_fresh_start_receipts_to_sessions([
                    session(
                        session_id="normal-candidate",
                        project=main,
                        started_at=datetime.now(timezone.utc) + timedelta(minutes=3),
                    )
                ])
                stored = next(
                    row for row in local_state.recent_handoff_decisions()
                    if row["id"] == decision["id"]
                )

        self.assertEqual(linked, 0)
        self.assertEqual(stored["source_identity_source"], "identity_conflict")
        self.assertIsNone(stored["source_repository_id"])
        self.assertIsNone(stored["source_checkout_id"])

    def test_missing_legacy_paths_do_not_correlate(self) -> None:
        self.assertFalse(_same_project("/missing/repo", "/missing/repo/app"))

    def test_links_recent_intervention_to_matching_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            project_root = Path(temp_dir, "repo")
            project = project_root / "app"
            project.mkdir(parents=True)
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                intervention_id = local_state.record_intervention(
                    tool="codex",
                    cwd=str(project_root),
                    risk="medium",
                    score=5,
                    findings=["Broad scope"],
                    original_prompt="Refactor the app",
                    suggested_prompt="Inspect first",
                    decision="brief_accepted",
                    selected_prompt="Inspect first",
                )

                linked = link_recent_interventions_to_sessions([session(project=str(project))])
                rows = local_state.recent_interventions()

        self.assertEqual(linked, 1)
        record = next(row for row in rows if row["id"] == intervention_id)
        self.assertEqual(record["session_id"], "session-1")

    def test_does_not_link_wrong_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_intervention(
                    tool="claude",
                    cwd="/repo",
                    risk="medium",
                    score=5,
                    findings=["Broad scope"],
                    original_prompt="Refactor the app",
                    suggested_prompt="Inspect first",
                    decision="brief_accepted",
                    selected_prompt="Inspect first",
                )

                linked = link_recent_interventions_to_sessions([session(tool="codex-cli", project="/repo/app")])

        self.assertEqual(linked, 0)

    def test_links_existing_conversation_updated_after_intervention(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            project = Path(temp_dir, "repo")
            project.mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                intervention_id = local_state.record_intervention(
                    tool="claude",
                    cwd=str(project),
                    risk="high",
                    score=8,
                    findings=["Broad scope"],
                    original_prompt="Refactor everything",
                    suggested_prompt="Inspect first",
                    decision="brief_accepted",
                    selected_prompt="Inspect first",
                )
                linked = link_recent_interventions_to_sessions([
                    session(
                        tool="claude-code",
                        project=str(project),
                        started_at=now - timedelta(days=3),
                        updated_at=now + timedelta(seconds=5),
                    )
                ])
                rows = local_state.recent_interventions()

        self.assertEqual(linked, 1)
        record = next(row for row in rows if row["id"] == intervention_id)
        self.assertEqual(record["session_id"], "session-1")

    def test_does_not_link_blocked_intervention(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_intervention(
                    tool="claude",
                    cwd="/repo",
                    risk="high",
                    score=8,
                    findings=["Destructive action"],
                    original_prompt="Delete everything",
                    suggested_prompt="Inspect first",
                    decision="blocked",
                    selected_prompt=None,
                )
                linked = link_recent_interventions_to_sessions([
                    session(tool="claude-code", project="/repo")
                ])

        self.assertEqual(linked, 0)

    def test_records_first_later_same_project_session_as_possible_followup(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            project = Path(temp_dir, "repo", "app")
            project.mkdir(parents=True)
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                decision = local_state.record_handoff_decision(
                    session_id="source",
                    decision="new_chat",
                    reason="Context pressure.",
                )
                linked = link_recent_fresh_start_receipts_to_sessions([
                    session(session_id="source", project=str(project), started_at=now - timedelta(hours=2)),
                    session(session_id="later", project=str(project), started_at=now + timedelta(minutes=3)),
                    session(session_id="latest", project=str(project), started_at=now + timedelta(minutes=20)),
                ])
                rows = local_state.recent_handoff_decisions()

        self.assertEqual(linked, 1)
        record = next(row for row in rows if row["id"] == decision["id"])
        self.assertEqual(record["session_id"], "source")
        self.assertIsNone(record["next_session_id"])
        self.assertEqual(record["next_session_correlation"]["status"], "candidate")
        self.assertEqual(record["next_session_correlation"]["candidate_session_id"], "later")
        self.assertEqual(record["next_session_correlation"]["confidence"], "medium")

    def test_fresh_start_receipt_does_not_link_continue_here_or_wrong_project(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            project = Path(temp_dir, "repo", "app")
            project.mkdir(parents=True)
            wrong_project = Path(temp_dir, "repo", "other")
            wrong_project.mkdir()
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                continue_decision = local_state.record_handoff_decision(
                    session_id="source",
                    decision="continue_here",
                    reason="Expected velocity.",
                )
                fresh_decision = local_state.record_handoff_decision(
                    session_id="source",
                    decision="new_chat",
                    reason="Context pressure.",
                )
                linked = link_recent_fresh_start_receipts_to_sessions([
                    session(session_id="source", project=str(project), started_at=now - timedelta(hours=2)),
                    session(session_id="wrong-project", project=str(wrong_project), started_at=now + timedelta(minutes=3)),
                ])
                rows = local_state.recent_handoff_decisions()

        self.assertEqual(linked, 0)
        by_id = {row["id"]: row for row in rows}
        self.assertNotIn("next_session_id", by_id[continue_decision["id"]])
        self.assertIsNone(by_id[fresh_decision["id"]]["next_session_id"])
        self.assertEqual(by_id[fresh_decision["id"]]["next_session_correlation"]["status"], "waiting")

    def test_fresh_start_receipt_does_not_link_source_session_updated_after_decision(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            project = Path(temp_dir, "repo", "app")
            project.mkdir(parents=True)
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                decision = local_state.record_handoff_decision(
                    session_id="source",
                    decision="copy_handoff",
                    reason="Context pressure.",
                )
                linked = link_recent_fresh_start_receipts_to_sessions([
                    session(
                        session_id="source",
                        project=str(project),
                        started_at=now - timedelta(hours=2),
                        updated_at=now + timedelta(minutes=10),
                    ),
                ])
                rows = local_state.recent_handoff_decisions()

        self.assertEqual(linked, 0)
        record = next(row for row in rows if row["id"] == decision["id"])
        self.assertIsNone(record["next_session_id"])

    def test_fresh_start_receipt_does_not_link_without_known_source_project(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                decision = local_state.record_handoff_decision(
                    session_id="missing-source",
                    decision="new_chat",
                    reason="Context pressure.",
                )
                linked = link_recent_fresh_start_receipts_to_sessions([
                    session(session_id="candidate", project="/repo/app", started_at=now + timedelta(minutes=3)),
                ])
                rows = local_state.recent_handoff_decisions()

        self.assertEqual(linked, 0)
        record = next(row for row in rows if row["id"] == decision["id"])
        self.assertIsNone(record["next_session_id"])
        self.assertIn("Source project is unavailable", record["next_session_correlation"]["reason"])

    def test_fresh_start_receipt_uses_stored_source_project_when_source_session_is_absent(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            project = Path(temp_dir, "repo", "app")
            project.mkdir(parents=True)
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                decision = local_state.record_handoff_decision(
                    session_id="missing-source",
                    decision="new_chat",
                    reason="Context pressure.",
                    source_project_path=str(project),
                )
                linked = link_recent_fresh_start_receipts_to_sessions([
                    session(session_id="candidate", project=str(project), started_at=now + timedelta(minutes=3)),
                ])
                rows = local_state.recent_handoff_decisions()

        self.assertEqual(linked, 1)
        record = next(row for row in rows if row["id"] == decision["id"])
        self.assertIsNone(record["next_session_id"])
        self.assertEqual(record["next_session_correlation"]["candidate_session_id"], "candidate")
        self.assertEqual(record["next_session_correlation"]["status"], "candidate")


if __name__ == "__main__":
    unittest.main()
