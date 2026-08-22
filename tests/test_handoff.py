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
        self.assertIn("Summary from previous work", capsule["next_brief"])
        self.assertIn("Decisions made", capsule["next_brief"])
        self.assertIn("Current state", capsule["next_brief"])
        self.assertIn("Open next step", capsule["next_brief"])
        self.assertIn("Files/evidence to inspect first", capsule["next_brief"])
        self.assertLess(capsule["next_brief"].index("Summary from previous work"), capsule["next_brief"].index("Source session identity"))
        self.assertIn("git status --short", capsule["next_brief"])
        self.assertIn("smallest next checkpoint", capsule["next_brief"])
        self.assertIn("What appears done", capsule["next_brief"])
        self.assertIn("What remains uncertain", capsule["next_brief"])
        self.assertIn("How to continue", capsule["next_brief"])
        self.assertIn("If this is a forked chat", capsule["next_brief"])
        self.assertIn("If this is a subagent task", capsule["next_brief"])
        self.assertIn("Recommended next checkpoint", capsule["next_brief"])
        self.assertIn("Usage pressure", capsule["next_brief"])
        self.assertNotIn("source diff", rendered.lower())

    def test_handoff_brief_separates_source_session_identity_from_workspace_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            run(["git", "init"], temp_dir)
            (repo / "api").mkdir()
            (repo / "api" / "index.js").write_text("export default function handler() {}\n", encoding="utf-8")
            run(["git", "add", "-N", "api/index.js"], temp_dir)
            session = LocalSession(
                session_id="01a00644-0f16-7291-b259-144420adaed1",
                tool="codex-cli",
                surface="desktop",
                project_path=temp_dir,
                source_path="/Users/example/.codex/sessions/rollout.jsonl",
                updated_at=datetime(2026, 8, 15, 17, 4, tzinfo=timezone.utc),
                model="gpt-5.5",
                tokens_in=210_464,
                tokens_out=12_000,
                agent_calls=18,
            )

            capsule = build_handoff_capsule(
                session,
                [],
                runtime_attachment={
                    "identity_label": "Likely workspace",
                    "identity_reason": "AIWatcher can identify the workspace, but not the exact running AI chat.",
                    "exact_return_label": "Workspace only",
                    "exact_return_reason": "Exact chat return is unavailable without a live process/window attachment.",
                    "confidence": "medium",
                    "surface": "desktop",
                    "app_name": "Codex",
                },
                same_project_session_count=4,
            )

        brief = capsule["next_brief"]
        self.assertIn("Source session match: Likely workspace", brief)
        self.assertIn("4 same-project sessions were observed", brief)
        self.assertIn("Source session identity", brief)
        self.assertIn("Identity confidence: Likely workspace (medium)", brief)
        self.assertIn("01a00644-0f16-7291-b259-144420adaed1", brief)
        self.assertIn("Same-project sessions observed: 4", brief)
        self.assertIn("AIWatcher has not verified the exact active chat", brief)
        self.assertIn("Workspace evidence to inspect (not guaranteed source-session evidence)", brief)
        self.assertIn("Changed file: api/index.js", brief)
        self.assertIn("Changed files are workspace evidence, not proof that the source AI session created those edits.", brief)
        self.assertIn("not proof from this source session", brief)
        self.assertNotIn("treat them as in-progress work.", brief)

    def test_handoff_never_uses_filesystem_root_as_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                session = LocalSession(
                    session_id="root-project",
                    tool="claude-code",
                    project_path="/",
                    updated_at=datetime.now(timezone.utc),
                    model="claude-sonnet",
                    agent_calls=300,
                )

                capsule = build_handoff_capsule(session, [], outcome=None)

        self.assertEqual(capsule["project"], "unknown project")
        self.assertFalse(capsule["project_reliable"])
        self.assertIn("- Project: unknown project", capsule["next_brief"])
        self.assertIn("- Project confidence: unconfirmed", capsule["next_brief"])
        self.assertIn("Ask the user to confirm the repository/path before editing.", capsule["next_brief"])
        self.assertIn("Project path was not reliable", capsule["next_brief"])

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

    def test_product_handoff_carries_objective_sources_constraints_and_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                session = LocalSession(
                    session_id="product-session",
                    tool="codex-desktop",
                    project_path="/repo/product-docs",
                    updated_at=datetime.now(timezone.utc),
                    model="gpt-5.5",
                    tokens_in=180_000,
                    agent_calls=220,
                    tool_calls=80,
                )

                capsule = build_handoff_capsule(
                    session,
                    [],
                    target="codex",
                    handoff_type="product",
                    objective="Align the product plan with the latest strategy before implementation.",
                    source_refs=["strategy.md", "enterprise/test-cases.md"],
                    constraints=["Do not build a generic dashboard."],
                    acceptance_criteria=["First reply restates the thesis and smallest next artifact."],
                )

        brief = capsule["next_brief"]
        self.assertEqual(capsule["handoff_type"], "product")
        self.assertEqual(capsule["handoff_type_label"], "Product/strategy continuation")
        self.assertEqual(capsule["objective"], "Align the product plan with the latest strategy before implementation.")
        self.assertEqual(capsule["source_refs"], ["strategy.md", "enterprise/test-cases.md"])
        self.assertEqual(capsule["constraints"], ["Do not build a generic dashboard."])
        self.assertEqual(
            capsule["acceptance_criteria"],
            ["First reply restates the thesis and smallest next artifact."],
        )
        self.assertIn("fresh product/strategy session", brief)
        self.assertIn("Continuation type: Product/strategy continuation.", brief)
        self.assertIn("User objective: Align the product plan", brief)
        self.assertIn("Source of truth to load first", brief)
        self.assertIn("- strategy.md", brief)
        self.assertIn("Do not lose these constraints", brief)
        self.assertIn("- Do not build a generic dashboard.", brief)
        self.assertIn("Acceptance checks", brief)
        self.assertIn("Read the source-of-truth files first", brief)

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

    def test_related_workspaces_are_context_not_duplicate_checkout_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                session = LocalSession(
                    session_id="session-related",
                    tool="codex-cli",
                    project_path="/repo/main",
                    updated_at=datetime.now(timezone.utc),
                    model="gpt-5.5",
                    tokens_in=300_000,
                    agent_calls=500,
                    tool_calls=200,
                )

                capsule = build_handoff_capsule(
                    session,
                    [],
                    outcome="useful",
                    related_workspaces=["/repo/docs", "/repo/main", "/repo/enterprise"],
                )

        brief = capsule["next_brief"]
        self.assertEqual(capsule["related_workspaces"], ["/repo/docs", "/repo/enterprise"])
        self.assertIn("- Related active workspace: /repo/docs", brief)
        self.assertIn("- Related active workspace: /repo/enterprise", brief)
        self.assertIn("confirm which repo owns the next checkpoint", brief)
        self.assertIn("Continue in the same workspace/repository unless the user explicitly asks", brief)
        self.assertIn("keep the same repository path active before editing", brief)
        self.assertNotIn("clone the repository", brief.lower())


if __name__ == "__main__":
    unittest.main()
