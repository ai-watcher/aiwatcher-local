from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from aiwatcher_cli.handoff import build_handoff_capsule, render_handoff_capsule
from aiwatcher_cli.local_state import record_decision
from aiwatcher_cli.scanner import LocalSession


def run(command: list[str], cwd: str, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, env=env)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)


class HandoffTests(unittest.TestCase):
    def test_handoff_capsule_prioritizes_fresh_session_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
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
        self.assertIn("fresh AI coding session", capsule["next_brief"])
        self.assertIn("Do not assume access to the previous chat", capsule["next_brief"])
        self.assertIn("git status --short", capsule["next_brief"])
        self.assertIn("smallest next checkpoint", capsule["next_brief"])
        self.assertNotIn("source diff", rendered.lower())

    def test_handoff_capsule_formats_for_target_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
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
        self.assertIn("Treat this as a fresh Codex session with no prior chat context", capsule["next_brief"])
        self.assertNotIn("Paste this as the first prompt", capsule["next_brief"])

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
            run(
                ["git", "commit", "-m", "fix login bug", "-m", "Session tokens were not being refreshed."],
                temp_dir,
                env=env,
            )
            (repo / "app.py").write_text("print('still editing')\n", encoding="utf-8")

            session = LocalSession(
                session_id="session-3",
                tool="claude-code",
                project_path=temp_dir,
                started_at=now,
                updated_at=now + timedelta(minutes=1),
            )
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}):
                capsule = build_handoff_capsule(session, [], outcome="useful")

        commit_sha = capsule["evidence"]["commits"][0]["sha"]
        self.assertIn(f"Commit: {commit_sha}: fix login bug", capsule["next_brief"])
        self.assertIn(f"git show {commit_sha} --stat", capsule["next_brief"])
        self.assertIn("Changed file: app.py", capsule["next_brief"])
        self.assertIn(f"Most recent commit message ({commit_sha})", capsule["next_brief"])
        self.assertIn("Session tokens were not being refreshed.", capsule["next_brief"])
        self.assertIn("git diff --stat", capsule["next_brief"])

    def test_body_less_commit_omits_commit_message_section(self) -> None:
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
            run(["git", "commit", "-m", "wip"], temp_dir, env=env)

            session = LocalSession(
                session_id="session-5",
                tool="claude-code",
                project_path=temp_dir,
                started_at=now,
                updated_at=now + timedelta(minutes=1),
            )
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}):
                capsule = build_handoff_capsule(session, [], outcome="useful")

        commit_sha = capsule["evidence"]["commits"][0]["sha"]
        self.assertIn(f"Commit: {commit_sha}: wip", capsule["next_brief"])
        self.assertNotIn("Most recent commit message", capsule["next_brief"])

    def test_commit_message_body_truncates_at_line_boundary_not_mid_word(self) -> None:
        now = datetime.now(timezone.utc)
        bullets = [f"- bullet point number {i} with some descriptive words attached" for i in range(30)]
        long_body = "\n".join(bullets)
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            run(["git", "init"], temp_dir)
            run(["git", "config", "user.email", "test@example.com"], temp_dir)
            run(["git", "config", "user.name", "AIWatcher Test"], temp_dir)
            (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
            stamp = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S%z")
            env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
            run(["git", "add", "app.py"], temp_dir, env=env)
            run(["git", "commit", "-m", "big change", "-m", long_body], temp_dir, env=env)

            session = LocalSession(
                session_id="session-6",
                tool="claude-code",
                project_path=temp_dir,
                started_at=now,
                updated_at=now + timedelta(minutes=1),
            )
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}):
                capsule = build_handoff_capsule(session, [], outcome="useful")

        brief = capsule["next_brief"]
        self.assertNotIn(long_body, brief)
        section = brief.split("Most recent commit message")[1].split("\n\nFresh-session instructions")[0]
        header_line, _, body_text = section.partition("\n")
        self.assertTrue(body_text.rstrip().endswith("..."))
        kept_text = body_text.rstrip()[:-3].rstrip()
        for line in kept_text.splitlines():
            if not line.strip():
                continue
            self.assertIn(line, bullets)

    def test_evidence_flags_when_more_commits_or_files_exist_than_shown(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            run(["git", "init"], temp_dir)
            run(["git", "config", "user.email", "test@example.com"], temp_dir)
            run(["git", "config", "user.name", "AIWatcher Test"], temp_dir)

            seed_stamp = (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S%z")
            seed_env = {**os.environ, "GIT_AUTHOR_DATE": seed_stamp, "GIT_COMMITTER_DATE": seed_stamp}
            for i in range(6):
                (repo / f"changed{i}.py").write_text(f"print({i})\n", encoding="utf-8")
            run(["git", "add"] + [f"changed{i}.py" for i in range(6)], temp_dir, env=seed_env)
            run(["git", "commit", "-m", "seed files"], temp_dir, env=seed_env)

            for i in range(4):
                stamp = (now + timedelta(minutes=2, seconds=i)).strftime("%Y-%m-%dT%H:%M:%S%z")
                env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
                (repo / f"extra{i}.py").write_text(f"print('extra {i}')\n", encoding="utf-8")
                run(["git", "add", f"extra{i}.py"], temp_dir, env=env)
                run(["git", "commit", "-m", f"commit number {i}"], temp_dir, env=env)

            for i in range(6):
                (repo / f"changed{i}.py").write_text(f"print('modified {i}')\n", encoding="utf-8")

            session = LocalSession(
                session_id="session-7",
                tool="claude-code",
                project_path=temp_dir,
                started_at=now,
                updated_at=now + timedelta(minutes=3),
            )
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}):
                capsule = build_handoff_capsule(session, [], outcome="useful")

        brief = capsule["next_brief"]
        self.assertEqual(len(capsule["evidence"]["commits"]), 5)
        self.assertEqual(len(capsule["evidence"]["changed_files"]), 6)
        self.assertIn("...and 2 more commit(s) (see git log)", brief)
        self.assertIn("...and 1 more changed file(s) (see git status)", brief)

    def test_changed_files_without_commits_warns_not_to_overwrite_work(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            run(["git", "init"], temp_dir)
            run(["git", "config", "user.email", "test@example.com"], temp_dir)
            run(["git", "config", "user.name", "AIWatcher Test"], temp_dir)
            (repo / "app.py").write_text("print('in progress')\n", encoding="utf-8")

            session = LocalSession(
                session_id="session-no-commit",
                tool="codex-cli",
                project_path=temp_dir,
                started_at=now,
                updated_at=now + timedelta(minutes=1),
            )
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}):
                capsule = build_handoff_capsule(session, [], outcome=None)

        brief = capsule["next_brief"]
        self.assertIn("Changed file: app.py", brief)
        self.assertIn("No nearby commit evidence was found", brief)
        self.assertIn("avoid overwriting local edits", brief)

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

            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}):
                capsule_off = build_handoff_capsule(session, [], include_prompt_excerpt=False)
                capsule_on = build_handoff_capsule(session, [], include_prompt_excerpt=True)

        self.assertNotIn("billing module", capsule_off["next_brief"])
        self.assertIsNone(capsule_off["costliest_prompt"])
        self.assertIn("billing module", capsule_on["next_brief"])
        self.assertIn("Task context", capsule_on["next_brief"])
        self.assertTrue(capsule_on["include_prompt_excerpt"])

    def test_logged_decisions_are_surfaced_and_hedged(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record_decision(
                    "session-8",
                    "Considered a token-based tiebreaker",
                    reasoning="Still picks turn #1 for unrelated reasons.",
                    alternatives_rejected=["token-based tiebreaker", "git diff --stat only"],
                )

                session = LocalSession(
                    session_id="session-8",
                    tool="claude-code",
                    project_path=temp_dir,
                    started_at=now,
                    updated_at=now,
                )
                capsule = build_handoff_capsule(session, [], outcome="useful")

        brief = capsule["next_brief"]
        self.assertIn("Decisions logged this session (self-reported, not verified against what actually happened)", brief)
        self.assertIn("- Considered a token-based tiebreaker", brief)
        self.assertIn("Why: Still picks turn #1 for unrelated reasons.", brief)
        self.assertIn("Rejected: token-based tiebreaker, git diff --stat only", brief)
        self.assertEqual(len(capsule["decisions"]), 1)

    def test_no_decisions_omits_the_section(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                session = LocalSession(
                    session_id="session-9",
                    tool="claude-code",
                    project_path=temp_dir,
                    started_at=now,
                    updated_at=now,
                )
                capsule = build_handoff_capsule(session, [], outcome="useful")

        self.assertNotIn("Decisions logged this session", capsule["next_brief"])
        self.assertEqual(capsule["decisions"], [])


if __name__ == "__main__":
    unittest.main()
