from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiwatcher_cli.handoff import build_handoff_capsule, render_handoff_capsule
from aiwatcher_cli.scanner import LocalSession


def run(command: list[str], cwd: str, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, env=env)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)


class HandoffTests(unittest.TestCase):
    def test_handoff_capsule_prioritizes_fresh_session_guidance(self) -> None:
        session = LocalSession(
            session_id="session-1",
            tool="claude-code",
            project_path="/repo/app",
            updated_at=datetime.now(timezone.utc),
            model="claude-sonnet",
            tokens_in=700_000,
            tokens_out=20_000,
            cost_usd=9.03,
            agent_calls=984,
            tool_calls=547,
        )

        capsule = build_handoff_capsule(session, [], outcome="useful")
        rendered = render_handoff_capsule(capsule)

        self.assertEqual(capsule["usage"]["tokens_label"], "720.0k")
        self.assertIn("continue with a smaller checkpoint", "\n".join(capsule["warnings"]))
        self.assertIn("fresh Claude/Codex/Cursor session", rendered)
        self.assertIn("Inspect the current git status", capsule["next_brief"])
        self.assertNotIn("source diff", rendered.lower())

    def test_handoff_capsule_formats_for_target_tool(self) -> None:
        session = LocalSession(
            session_id="session-2",
            tool="claude-code",
            project_path="/repo/app",
            updated_at=datetime.now(timezone.utc),
            model="claude-sonnet",
        )

        capsule = build_handoff_capsule(session, [], target="codex")
        rendered = render_handoff_capsule(capsule)

        self.assertEqual(capsule["target"], "codex")
        self.assertEqual(capsule["target_label"], "Codex")
        self.assertIn("fresh Codex session", rendered)
        self.assertIn("Paste this as the first prompt in a fresh Codex session", capsule["next_brief"])

    def test_next_brief_includes_commit_sha_and_changed_files(self) -> None:
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
            (repo / "app.py").write_text("print('still editing')\n", encoding="utf-8")

            session = LocalSession(
                session_id="session-3",
                tool="claude-code",
                project_path=temp_dir,
                started_at=now,
                updated_at=now + timedelta(minutes=1),
            )
            capsule = build_handoff_capsule(session, [], outcome="useful")

        commit_sha = capsule["evidence"]["commits"][0]["sha"]
        self.assertIn(f"Commit {commit_sha}", capsule["next_brief"])
        self.assertIn(f"git show {commit_sha} --stat", capsule["next_brief"])
        self.assertIn("Changed file: app.py", capsule["next_brief"])
        self.assertNotIn("secret customer fix", capsule["next_brief"])

    def test_prompt_excerpt_is_embedded_only_when_opted_in(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "session.jsonl")
            with open(source_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "user",
                    "message": {"content": "please refactor the billing module for clarity"},
                }) + "\n")

            session = LocalSession(
                session_id="session-4",
                tool="claude-code",
                project_path=temp_dir,
                source_path=source_path,
                started_at=now,
                updated_at=now,
            )

            capsule_off = build_handoff_capsule(session, [], include_prompt_excerpt=False)
            self.assertNotIn("billing module", capsule_off["next_brief"])
            self.assertIsNone(capsule_off["costliest_prompt"])

            capsule_on = build_handoff_capsule(session, [], include_prompt_excerpt=True)
            self.assertIn("billing module", capsule_on["next_brief"])
            self.assertIn("Task context", capsule_on["next_brief"])
            self.assertTrue(capsule_on["include_prompt_excerpt"])


if __name__ == "__main__":
    unittest.main()
