from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiwatcher_cli.outcome_evidence import build_outcome_evidence
from aiwatcher_cli.scanner import LocalSession


def run(command: list[str], cwd: str, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, env=env)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)


class OutcomeEvidenceTests(unittest.TestCase):
    def test_detects_nearby_commit_without_storing_subject_text(self) -> None:
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
            run(["git", "commit", "-m", "secret customer fix"], temp_dir, env=env)

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
        self.assertNotIn("secret customer fix", str(evidence.to_json()))

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


if __name__ == "__main__":
    unittest.main()
