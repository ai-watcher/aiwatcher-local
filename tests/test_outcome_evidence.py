from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiwatcher_cli.outcome_evidence import (
    annotate_same_file_reprompt,
    build_outcome_evidence,
    check_commit_survival,
    evidence_for_sessions,
)
from aiwatcher_cli.scanner import LocalSession


def run(command: list[str], cwd: str, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, env=env)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)


def run_out(command: list[str], cwd: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, env=env)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


def init_repo(temp_dir: str) -> None:
    run(["git", "init"], temp_dir)
    run(["git", "config", "user.email", "test@example.com"], temp_dir)
    run(["git", "config", "user.name", "AIWatcher Test"], temp_dir)


def commit_file(temp_dir: str, filename: str, content: str, message: str, *, when: datetime) -> str:
    (Path(temp_dir) / filename).write_text(content, encoding="utf-8")
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S%z")
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    run(["git", "add", filename], temp_dir, env=env)
    run(["git", "commit", "-m", message], temp_dir, env=env)
    return run_out(["git", "rev-parse", "HEAD"], temp_dir)


class OutcomeEvidenceTests(unittest.TestCase):
    def test_detects_nearby_commit_and_captures_real_subject_and_body(self) -> None:
        # Commit subjects/bodies are intentionally captured as real text, not
        # hashed: unlike a prompt, a commit message is written by whoever made
        # the change specifically to explain it to a future reader, so it is
        # the strongest available signal for "why" in a handoff brief. This is
        # local-only -- the persistent evidence_snapshot store (local_state.py)
        # still only ever writes hashed/truncated fields to disk; see
        # test_local_state.test_evidence_snapshot_never_persists_commit_text.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            run(["git", "init"], temp_dir)
            run(["git", "config", "user.email", "test@example.com"], temp_dir)
            run(["git", "config", "user.name", "AIWatcher Test"], temp_dir)
            (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
            stamp = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S%z")
            env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
            run(["git", "add", "app.py"], temp_dir, env=env)
            run(
                ["git", "commit", "-m", "fix login bug", "-m", "Session tokens were not being refreshed."],
                temp_dir,
                env=env,
            )

            session = LocalSession(
                session_id="session-1",
                tool="claude-code",
                project_path=temp_dir,
                started_at=now,
                updated_at=now + timedelta(minutes=1),
            )
            evidence = build_outcome_evidence(session)

        self.assertEqual(evidence.inferred_outcome, "useful")
        self.assertEqual(evidence.confidence, "low")
        self.assertEqual(len(evidence.commits), 1)
        self.assertEqual(evidence.commits[0]["subject"], "fix login bug")
        self.assertEqual(evidence.commits[0]["body"], "Session tokens were not being refreshed.")

    def test_detects_changed_files_as_needs_review(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            run(["git", "init"], temp_dir)
            run(["git", "config", "user.email", "test@example.com"], temp_dir)
            run(["git", "config", "user.name", "AIWatcher Test"], temp_dir)
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")

            session = LocalSession(
                session_id="session-2",
                tool="codex-cli",
                project_path=temp_dir,
                started_at=now,
                updated_at=now,
            )
            evidence = build_outcome_evidence(session)

        self.assertEqual(evidence.inferred_outcome, "needs_review")
        self.assertEqual(evidence.changed_files, ["app.py"])

    def test_captures_files_touched_by_the_session_own_commits(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "print('hello')\n", "initial commit", when=now - timedelta(hours=1))
            commit_file(temp_dir, "auth.py", "def login(): pass\n", "add auth", when=now + timedelta(minutes=5))

            session = LocalSession(
                session_id="session-3", tool="claude-code", project_path=temp_dir,
                started_at=now, updated_at=now + timedelta(minutes=10),
            )
            evidence = build_outcome_evidence(session)

        self.assertEqual(evidence.files_touched, ["auth.py"])


class CommitSurvivalTests(unittest.TestCase):
    def test_commit_still_on_branch_is_survived(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            sha = commit_file(temp_dir, "app.py", "v1\n", "first", when=now)
            self.assertEqual(check_commit_survival(temp_dir, sha), "survived")

    def test_commit_reset_away_is_churned(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "v1\n", "first", when=now)
            sha = commit_file(temp_dir, "app.py", "v2\n", "second", when=now + timedelta(minutes=1))
            run(["git", "reset", "--hard", "HEAD~1"], temp_dir)
            self.assertEqual(check_commit_survival(temp_dir, sha), "churned")

    def test_unknown_sha_is_unknown(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "v1\n", "first", when=now)
            self.assertEqual(check_commit_survival(temp_dir, "0" * 40), "unknown")

    def test_nonexistent_repo_is_unknown(self) -> None:
        self.assertEqual(check_commit_survival("/no/such/repo/path", "abc123"), "unknown")


class ChurnDowngradesInferredOutcomeTests(unittest.TestCase):
    def test_churned_survival_downgrades_useful_to_churned(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "v1\n", "fix bug", when=now)
            session = LocalSession(
                session_id="session-4", tool="claude-code", project_path=temp_dir,
                started_at=now, updated_at=now,
            )
            evidence = build_outcome_evidence(session, survival={"7": "churned"})

        self.assertEqual(evidence.inferred_outcome, "churned")
        self.assertEqual(evidence.confidence, "medium")
        self.assertTrue(any("did not survive" in reason for reason in evidence.reasons))

    def test_survived_status_does_not_change_useful(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "v1\n", "fix bug", when=now)
            session = LocalSession(
                session_id="session-5", tool="claude-code", project_path=temp_dir,
                started_at=now, updated_at=now,
            )
            evidence = build_outcome_evidence(session, survival={"7": "survived"})

        self.assertEqual(evidence.inferred_outcome, "useful")

    def test_churn_on_needs_review_session_is_left_alone(self) -> None:
        # Only a "useful" (has-a-commit) verdict can be downgraded by churn --
        # there's no commit-survival signal to apply to an uncommitted-files case.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            (Path(temp_dir) / "app.py").write_text("uncommitted\n", encoding="utf-8")
            session = LocalSession(
                session_id="session-6", tool="claude-code", project_path=temp_dir,
                started_at=now, updated_at=now,
            )
            evidence = build_outcome_evidence(session, survival={"7": "churned"})

        self.assertEqual(evidence.inferred_outcome, "needs_review")


class SameFileRepromptTests(unittest.TestCase):
    def test_flags_when_a_later_session_touches_the_same_file_soon_after(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "auth.py", "v1\n", "first attempt", when=now)
            commit_file(temp_dir, "auth.py", "v2\n", "second attempt", when=now + timedelta(hours=10))

            first = LocalSession(
                session_id="first", tool="claude-code", project_path=temp_dir,
                started_at=now, updated_at=now,
            )
            second = LocalSession(
                session_id="second", tool="claude-code", project_path=temp_dir,
                started_at=now + timedelta(hours=10), updated_at=now + timedelta(hours=10),
            )
            evidence_map = evidence_for_sessions([first, second])

        self.assertTrue(evidence_map["first"].same_file_reprompt)
        self.assertFalse(evidence_map["second"].same_file_reprompt)  # nothing comes after it

    def test_does_not_flag_when_the_later_session_is_outside_the_window(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "auth.py", "v1\n", "first attempt", when=now)
            commit_file(temp_dir, "auth.py", "v2\n", "second attempt", when=now + timedelta(hours=200))

            first = LocalSession(
                session_id="first", tool="claude-code", project_path=temp_dir,
                started_at=now, updated_at=now,
            )
            second = LocalSession(
                session_id="second", tool="claude-code", project_path=temp_dir,
                started_at=now + timedelta(hours=200), updated_at=now + timedelta(hours=200),
            )
            evidence_map = evidence_for_sessions([first, second])

        self.assertFalse(evidence_map["first"].same_file_reprompt)

    def test_does_not_flag_when_files_do_not_overlap(self) -> None:
        # Sessions are spaced > COMMIT_LOOKAHEAD_HOURS (24h) apart so each
        # session's own 24h commit-lookahead window doesn't pick up the
        # other's commit too -- otherwise "first" would appear to have
        # touched billing.py itself, which is a separate, pre-existing
        # blurriness in _recent_commits() unrelated to what this test checks.
        # Still well within REPROMPT_WINDOW_HOURS (72h), so the reprompt
        # comparison itself is genuinely exercised.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "auth.py", "v1\n", "auth work", when=now)
            commit_file(temp_dir, "billing.py", "v1\n", "unrelated work", when=now + timedelta(hours=30))

            first = LocalSession(
                session_id="first", tool="claude-code", project_path=temp_dir,
                started_at=now, updated_at=now,
            )
            second = LocalSession(
                session_id="second", tool="claude-code", project_path=temp_dir,
                started_at=now + timedelta(hours=30), updated_at=now + timedelta(hours=30),
            )
            evidence_map = evidence_for_sessions([first, second])

        self.assertFalse(evidence_map["first"].same_file_reprompt)

    def test_different_projects_are_never_compared(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as dir_a, tempfile.TemporaryDirectory() as dir_b:
            init_repo(dir_a)
            commit_file(dir_a, "auth.py", "v1\n", "work in repo a", when=now)
            init_repo(dir_b)
            commit_file(dir_b, "auth.py", "v1\n", "same filename, different repo", when=now + timedelta(hours=1))

            first = LocalSession(session_id="first", tool="claude-code", project_path=dir_a, started_at=now, updated_at=now)
            second = LocalSession(
                session_id="second", tool="claude-code", project_path=dir_b,
                started_at=now + timedelta(hours=1), updated_at=now + timedelta(hours=1),
            )
            evidence_map = evidence_for_sessions([first, second])

        self.assertFalse(evidence_map["first"].same_file_reprompt)


if __name__ == "__main__":
    unittest.main()
