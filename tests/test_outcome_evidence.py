from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiwatcher_cli import cli, local_state
from aiwatcher_cli.git_identity import resolve_git_identity
from aiwatcher_cli.handoff import build_handoff_capsule
from aiwatcher_cli.outcome_evidence import (
    OutcomeEvidence,
    _checkout_root,
    _checkout_state,
    _working_tree_fingerprint,
    annotate_same_file_reprompt,
    build_outcome_evidence,
    check_commit_survival,
    check_commit_undone,
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
    def test_command_checkout_selection_is_conservative_when_worktrees_conflict(self) -> None:
        from aiwatcher_cli.command_evidence import CommandEvidence

        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            main = Path(temp_dir, "main")
            first = Path(temp_dir, "first")
            second = Path(temp_dir, "second")
            main.mkdir()
            init_repo(str(main))
            sha = commit_file(str(main), "app.py", "base\n", "base", when=now)
            run(["git", "worktree", "add", "-b", "first-branch", str(first)], str(main))
            run(["git", "worktree", "add", "-b", "second-branch", str(second)], str(main))
            session = LocalSession(
                session_id="session-a", tool="codex-cli", project_path=str(main), raw_cwd=str(main),
            )
            common = {
                "session_id": "session-a", "tool": "codex-cli", "runner": "pytest",
                "command_kind": "verification", "started_at": now.isoformat(),
                "finished_at": now.isoformat(), "completion_state": "completed", "exit_code": 0,
            }
            observed = [
                CommandEvidence(source_id="one", cwd=str(first), **common),
                CommandEvidence(source_id="two", cwd=str(second), **common),
            ]

            ambiguous = _checkout_root(session, observed)
            observed[0] = CommandEvidence(
                source_id="commit", session_id="session-a", tool="codex-cli", cwd=str(first),
                runner=None, command_kind="git_commit", started_at=now.isoformat(),
                finished_at=now.isoformat(), completion_state="completed", exit_code=0,
                commit_sha=sha,
            )
            commit_selected = _checkout_root(session, observed)

        self.assertEqual(Path(ambiguous or "").resolve(), main.resolve())
        self.assertEqual(Path(commit_selected or "").resolve(), first.resolve())

    def test_legacy_receipt_does_not_cross_replacement_repository(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = os.path.join(temp_dir, "repo")
            os.mkdir(repo)
            init_repo(repo)
            old_sha = commit_file(repo, "old.py", "old\n", "old repository", when=now)
            replacement = os.path.join(temp_dir, "replacement")
            run(["git", "clone", "-q", repo, replacement], temp_dir)
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_commit_receipt({
                    "sha": old_sha,
                    "checkout_path": repo,
                    "repository_id": old_sha[:16],
                })
                os.rename(repo, os.path.join(temp_dir, "retired"))
                os.rename(replacement, repo)
                evidence = build_outcome_evidence(LocalSession(
                    session_id="replacement", tool="codex-cli", project_path=repo,
                    started_at=now, updated_at=now + timedelta(minutes=2),
                ))

        self.assertEqual(evidence.commit_receipts, [])

    def test_exact_commit_receipts_do_not_cross_overlapping_sessions(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            first = commit_file(temp_dir, "first.py", "first\n", "first session", when=now)
            second = commit_file(temp_dir, "second.py", "second\n", "second session", when=now + timedelta(minutes=1))
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                identity = resolve_git_identity(temp_dir)
                assert identity is not None
                for session_id, sha in (("session-a", first), ("session-b", second)):
                    local_state.record_commit_receipt({
                        "sha": sha, "subject": session_id,
                        "repository_id": identity.repository_id,
                        "repository_lineage_id": identity.repository_lineage_id,
                        "checkout_id": identity.checkout_id,
                        "checkout_path": identity.checkout_path,
                        "session_id": session_id,
                    })
                evidence = build_outcome_evidence(LocalSession(
                    session_id="session-a", tool="claude-code", project_path=temp_dir,
                    started_at=now - timedelta(minutes=1), updated_at=now + timedelta(minutes=2),
                ))

        self.assertEqual([row["sha"] for row in evidence.commit_receipts], [first])
        self.assertEqual([row["subject"] for row in evidence.commits], ["first session"])
        self.assertEqual(evidence.commit_attribution, "session_bound")

    def test_transcript_test_pass_is_session_bound_but_not_current(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "base\n", "base", when=now - timedelta(minutes=1))
            source = Path(temp_dir, "session.jsonl")
            rows = [
                {
                    "uuid": "call", "type": "assistant", "sessionId": "session-a",
                    "cwd": temp_dir, "timestamp": now.isoformat(),
                    "message": {"content": [{
                        "type": "tool_use", "id": "tool-1", "name": "Bash",
                        "input": {"command": "python3 -m unittest tests.test_app"},
                    }]},
                },
                {
                    "uuid": "result", "type": "user", "sessionId": "session-a",
                    "cwd": temp_dir, "timestamp": (now + timedelta(seconds=10)).isoformat(),
                    "message": {"content": [{
                        "type": "tool_result", "tool_use_id": "tool-1",
                        "is_error": False, "content": "private output",
                    }]},
                    "toolUseResult": {"stdout": "private output", "stderr": "", "interrupted": False},
                },
            ]
            source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                evidence = build_outcome_evidence(LocalSession(
                    session_id="session-a", tool="claude-code", project_path=temp_dir,
                    raw_cwd=temp_dir, source_path=str(source),
                    started_at=now, updated_at=now + timedelta(minutes=1),
                ))

        receipt = next(row for row in evidence.tests if row.get("name") == "python -m unittest")
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["attribution"], "session_bound")
        self.assertFalse(receipt["current"])
        self.assertNotIn("authoritative", receipt)

    def test_command_workdir_moves_evidence_to_linked_worktree(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            main = Path(temp_dir, "main")
            worktree = Path(temp_dir, "review")
            main.mkdir()
            init_repo(str(main))
            commit_file(str(main), "app.py", "base\n", "base", when=now - timedelta(hours=1))
            run(["git", "worktree", "add", "-b", "review-pr", str(worktree)], str(main))
            sha = commit_file(
                str(worktree), "fix.py", "fixed\n", "fix in worktree",
                when=now + timedelta(minutes=1),
            )
            source = Path(temp_dir, "session.jsonl")
            rows = [
                {
                    "timestamp": now.isoformat(), "type": "session_meta",
                    "payload": {"id": "session-a", "cwd": str(main)},
                },
                {
                    "timestamp": (now + timedelta(minutes=1)).isoformat(),
                    "type": "response_item", "payload": {
                        "type": "function_call", "name": "exec_command", "call_id": "commit-1",
                        "arguments": json.dumps({
                            "cmd": "git commit -m 'fix in worktree'", "workdir": str(worktree),
                        }),
                    },
                },
                {
                    "timestamp": (now + timedelta(minutes=1, seconds=5)).isoformat(),
                    "type": "response_item", "payload": {
                        "type": "function_call_output", "call_id": "commit-1",
                        "output": f"Process exited with code 0\nFinal output:\n[review-pr {sha[:7]}] fix in worktree",
                    },
                },
            ]
            source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                evidence = build_outcome_evidence(LocalSession(
                    session_id="session-a", tool="codex-cli", project_path=str(main),
                    raw_cwd=str(main), source_path=str(source),
                    started_at=now, updated_at=now + timedelta(minutes=2),
                ))

        self.assertEqual(Path(evidence.checkout_path or "").resolve(), worktree.resolve())
        self.assertEqual(evidence.commit_attribution, "session_bound")
        self.assertEqual([row["sha"] for row in evidence.commit_receipts], [sha])
        self.assertEqual([row["subject"] for row in evidence.commits], ["fix in worktree"])

    def test_fingerprint_distinguishes_surrogateescaped_git_bytes(self) -> None:
        def git_result(repo: str, args: list[str]):
            return subprocess.CompletedProcess(["git"], 0, "", "")

        with patch("aiwatcher_cli.outcome_evidence._run_git", side_effect=git_result):
            first = _working_tree_fingerprint("/repo", "?? notes-\udcff.txt\n")
            second = _working_tree_fingerprint("/repo", "?? notes-\udcfe.txt\n")

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)

    def test_checkout_state_does_not_report_clean_when_git_status_fails(self) -> None:
        failed = subprocess.CompletedProcess(["git"], 1, "", "status failed")

        def run_git(repo: str, args: list[str]):
            if args and args[0] == "status":
                return failed
            return subprocess.CompletedProcess(["git"], 0, "abc123\n", "")

        with patch("aiwatcher_cli.outcome_evidence._run_git", side_effect=run_git):
            state = _checkout_state("/repo")

        self.assertIsNone(state["dirty"])
        self.assertIsNone(state["dirty_fingerprint"])
        self.assertFalse(state["state_observed"])

    def test_dirty_fingerprint_changes_when_same_file_content_changes(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "base\n", "base", when=now)
            path = Path(temp_dir, "app.py")
            path.write_text("first edit\n", encoding="utf-8")
            first = _checkout_state(temp_dir)["dirty_fingerprint"]
            path.write_text("different edit\n", encoding="utf-8")
            second = _checkout_state(temp_dir)["dirty_fingerprint"]

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)

    def test_dirty_fingerprint_changes_when_untracked_content_changes(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "base\n", "base", when=now)
            path = Path(temp_dir, "notes.txt")
            path.write_text("first draft\n", encoding="utf-8")
            first = _checkout_state(temp_dir)["dirty_fingerprint"]
            path.write_text("different draft\n", encoding="utf-8")
            second = _checkout_state(temp_dir)["dirty_fingerprint"]

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)

    def test_dirty_fingerprint_changes_when_dirty_submodule_content_changes(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            child = Path(temp_dir, "child")
            parent = Path(temp_dir, "parent")
            child.mkdir()
            parent.mkdir()
            init_repo(str(child))
            commit_file(str(child), "nested.py", "base\n", "child base", when=now)
            init_repo(str(parent))
            run(
                ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(child), "nested"],
                str(parent),
            )
            run(["git", "commit", "-m", "add submodule"], str(parent))
            nested = parent / "nested" / "nested.py"
            nested.write_text("first edit\n", encoding="utf-8")
            first = _checkout_state(str(parent))["dirty_fingerprint"]
            nested.write_text("different edit\n", encoding="utf-8")
            second = _checkout_state(str(parent))["dirty_fingerprint"]

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)

    def test_dirty_fingerprint_ignores_submodule_dirty_display_policy(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            child = Path(temp_dir, "child")
            parent = Path(temp_dir, "parent")
            child.mkdir()
            parent.mkdir()
            init_repo(str(child))
            commit_file(str(child), "nested.py", "base\n", "child base", when=now)
            init_repo(str(parent))
            run(
                ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(child), "nested"],
                str(parent),
            )
            run(["git", "commit", "-m", "add submodule"], str(parent))
            run(["git", "config", "submodule.nested.ignore", "dirty"], str(parent))
            nested = parent / "nested" / "nested.py"
            nested.write_text("first hidden edit\n", encoding="utf-8")
            first = _checkout_state(str(parent))["dirty_fingerprint"]
            nested.write_text("different hidden edit\n", encoding="utf-8")
            second = _checkout_state(str(parent))["dirty_fingerprint"]

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)

    def test_dirty_fingerprint_tracks_submodule_head_when_parent_ignores_all(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            child = Path(temp_dir, "child")
            parent = Path(temp_dir, "parent")
            child.mkdir()
            parent.mkdir()
            init_repo(str(child))
            commit_file(str(child), "nested.py", "base\n", "child base", when=now)
            init_repo(str(parent))
            run(
                ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(child), "nested"],
                str(parent),
            )
            run(["git", "commit", "-m", "add submodule"], str(parent))
            run(["git", "config", "submodule.nested.ignore", "all"], str(parent))
            nested_repo = parent / "nested"
            run(["git", "config", "user.email", "test@example.com"], str(nested_repo))
            run(["git", "config", "user.name", "AIWatcher Test"], str(nested_repo))
            first = _checkout_state(str(parent))["dirty_fingerprint"]
            commit_file(
                str(nested_repo), "nested.py", "new committed state\n", "child update", when=now,
            )
            second = _checkout_state(str(parent))["dirty_fingerprint"]

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux permits undecodable byte filenames")
    def test_dirty_fingerprint_handles_non_utf8_untracked_filename(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "base\n", "base", when=now)
            raw_path = os.fsencode(temp_dir) + b"/notes-\xff.txt"
            descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                os.write(descriptor, b"local notes\n")
            finally:
                os.close(descriptor)
            fingerprint = _checkout_state(temp_dir)["dirty_fingerprint"]

        self.assertIsNotNone(fingerprint)

    def test_outcome_scan_skips_content_fingerprint_without_receipts(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "base\n", "base", when=now)
            session = LocalSession(
                session_id="no-receipts", tool="codex-cli", project_path=temp_dir,
                started_at=now, updated_at=now + timedelta(minutes=1),
            )
            with (
                patch("aiwatcher_cli.outcome_evidence.recent_verification_receipts", return_value=[]),
                patch("aiwatcher_cli.outcome_evidence._working_tree_fingerprint") as fingerprint,
            ):
                build_outcome_evidence(session)

        fingerprint.assert_not_called()

    def test_test_artifact_does_not_claim_a_passed_verification(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "base\n", "base", when=now - timedelta(minutes=2))
            artifact = repo / "junit-results.xml"
            artifact.write_text("<testsuite failures='1'/>\n", encoding="utf-8")
            os.utime(artifact, (now.timestamp(), now.timestamp()))
            session = LocalSession(
                session_id="artifact-only", tool="codex-cli", project_path=temp_dir,
                started_at=now - timedelta(minutes=1), updated_at=now + timedelta(minutes=1),
            )
            evidence = build_outcome_evidence(session)

        artifact_evidence = next(item for item in evidence.tests if item.get("artifact") == "junit-results.xml")
        self.assertEqual(artifact_evidence["status"], "result unknown")
        self.assertEqual(artifact_evidence["source"], "local test artifact")
        self.assertEqual(evidence.confidence, "low")

    def test_marks_verification_current_only_for_matching_git_state(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = os.path.join(temp_dir, "repo")
            os.mkdir(repo)
            init_repo(repo)
            head = commit_file(repo, "app.py", "base\n", "base", when=now)
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                identity = resolve_git_identity(repo)
                assert identity is not None
                local_state.record_verification_receipt(
                    runner="pytest",
                    checkout_path=repo,
                    repository_id=identity.repository_id,
                    repository_lineage_id=identity.repository_lineage_id,
                    started_checkout_id=identity.checkout_id,
                    started_head=head,
                    started_dirty_fingerprint="e3b0c44298fc1c149afbf4c8",
                    checkout_id=identity.checkout_id,
                    head=head,
                    dirty_fingerprint="e3b0c44298fc1c149afbf4c8",
                    started_at=now.isoformat(),
                    finished_at=(now + timedelta(minutes=1)).isoformat(),
                    exit_code=0,
                    session_id="verified",
                )
                session = LocalSession(
                    session_id="verified", tool="codex-cli", project_path=repo,
                    started_at=now, updated_at=now + timedelta(minutes=2),
                )
                evidence = build_outcome_evidence(session)

        self.assertEqual(evidence.tests[0]["name"], "pytest")
        self.assertEqual(evidence.tests[0]["status"], "passed")
        self.assertTrue(evidence.tests[0]["current"])
        self.assertTrue(evidence.tests[0]["authoritative"])

    def test_unbound_verification_is_never_authoritative_for_a_session(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = os.path.join(temp_dir, "repo")
            os.mkdir(repo)
            init_repo(repo)
            head = commit_file(repo, "app.py", "base\n", "base", when=now)
            fingerprint = _checkout_state(repo)["dirty_fingerprint"]
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                identity = resolve_git_identity(repo)
                assert identity is not None
                local_state.record_verification_receipt(
                    runner="pytest", checkout_path=repo,
                    repository_id=identity.repository_id,
                    repository_lineage_id=identity.repository_lineage_id,
                    started_checkout_id=identity.checkout_id,
                    started_head=head, started_dirty_fingerprint=fingerprint,
                    checkout_id=identity.checkout_id,
                    head=head, dirty_fingerprint=fingerprint,
                    started_at=now.isoformat(), finished_at=(now + timedelta(seconds=30)).isoformat(),
                    exit_code=0,
                )
                evidence = build_outcome_evidence(LocalSession(
                    session_id="different-session", tool="codex-cli", project_path=repo,
                    started_at=now, updated_at=now + timedelta(minutes=1),
                ))

        self.assertTrue(evidence.tests[0]["current"])
        self.assertEqual(evidence.tests[0]["attribution"], "inferred_time_window")
        self.assertNotIn("authoritative", evidence.tests[0])

    def test_newest_current_verification_failure_supersedes_older_pass(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = os.path.join(temp_dir, "repo")
            os.mkdir(repo)
            init_repo(repo)
            head = commit_file(repo, "app.py", "base\n", "base", when=now)
            fingerprint = _checkout_state(repo)["dirty_fingerprint"]
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                identity = resolve_git_identity(repo)
                assert identity is not None
                # Persist out of completion order: authority follows finished_at,
                # not lock/insertion order.
                for offset, exit_code in ((2, 1), (1, 0)):
                    local_state.record_verification_receipt(
                        runner="pytest", checkout_path=repo,
                        repository_id=identity.repository_id,
                        repository_lineage_id=identity.repository_lineage_id,
                        started_checkout_id=identity.checkout_id,
                        started_head=head, started_dirty_fingerprint=fingerprint,
                        checkout_id=identity.checkout_id,
                        head=head, dirty_fingerprint=fingerprint,
                        started_at=(now + timedelta(minutes=offset)).isoformat(),
                        finished_at=(now + timedelta(minutes=offset, seconds=30)).isoformat(),
                        exit_code=exit_code,
                        session_id="latest-failed",
                    )
                session = LocalSession(
                    session_id="latest-failed", tool="codex-cli", project_path=repo,
                    started_at=now, updated_at=now + timedelta(minutes=3),
                )
                evidence = build_outcome_evidence(session)

        self.assertEqual(evidence.tests[0]["status"], "failed")
        self.assertTrue(evidence.tests[0]["authoritative"])
        self.assertNotIn("authoritative", evidence.tests[1])
        self.assertEqual(evidence.confidence, "low")
        self.assertFalse(any("passing verification" in reason for reason in evidence.reasons))

    def test_changed_or_legacy_git_basis_is_visible_but_never_authoritative(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = os.path.join(temp_dir, "repo")
            os.mkdir(repo)
            init_repo(repo)
            head = commit_file(repo, "app.py", "base\n", "base", when=now)
            fingerprint = _checkout_state(repo)["dirty_fingerprint"]
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                identity = resolve_git_identity(repo)
                assert identity is not None
                common = {
                    "runner": "pytest", "checkout_path": repo,
                    "repository_id": identity.repository_id,
                    "repository_lineage_id": identity.repository_lineage_id,
                    "checkout_id": identity.checkout_id, "head": head,
                    "dirty_fingerprint": fingerprint,
                    "started_at": now.isoformat(), "exit_code": 0,
                    "session_id": "basis",
                }
                local_state.record_verification_receipt(
                    **common, finished_at=(now + timedelta(seconds=30)).isoformat(),
                    started_checkout_id=identity.checkout_id,
                    started_head="different", started_dirty_fingerprint=fingerprint,
                    state_binding="git_state_changed",
                )
                local_state.record_verification_receipt(
                    **common, finished_at=(now + timedelta(seconds=20)).isoformat(),
                    state_binding="git_state",
                )
                legacy = dict(common)
                legacy.pop("checkout_id")
                legacy["repository_id"] = head[:16]
                local_state.record_verification_receipt(
                    **legacy, finished_at=(now + timedelta(seconds=10)).isoformat(),
                    state_binding="historical",
                )
                evidence = build_outcome_evidence(LocalSession(
                    session_id="basis", tool="codex-cli", project_path=repo,
                    started_at=now, updated_at=now + timedelta(minutes=1),
                ))

        self.assertEqual(len(evidence.tests), 3)
        self.assertTrue(all(item["current"] is False for item in evidence.tests))
        self.assertTrue(all("authoritative" not in item for item in evidence.tests))

    def test_mutating_run_is_visible_but_does_not_suppress_fresh_start_warning(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir, "repo")
            repo.mkdir()
            init_repo(str(repo))
            commit_file(str(repo), "tracked.txt", "base\n", "base", when=now)
            test_path = repo / "test_mutating.py"
            test_path.write_text(
                "import pathlib\nimport unittest\n\n"
                "class MutatingTest(unittest.TestCase):\n"
                "    def test_mutates_tracked_file(self):\n"
                "        pathlib.Path('tracked.txt').write_text('changed\\n')\n",
                encoding="utf-8",
            )
            run(["git", "add", "test_mutating.py"], str(repo))
            run(["git", "commit", "-m", "add mutating test"], str(repo))
            state_file = os.path.join(temp_dir, "state.json")
            original_cwd = os.getcwd()
            try:
                os.chdir(repo)
                with (
                    patch.dict(os.environ, {
                        "AIWATCHER_STATE_FILE": state_file,
                        "PYTHONDONTWRITEBYTECODE": "1",
                    }),
                    patch.object(cli, "_verification_runner", return_value="python -m unittest"),
                    patch.object(cli, "environment_session_identity", return_value=("mutating", "codex")),
                    patch.object(cli, "scan_all", return_value=[]),
                ):
                    exit_code = cli.command_run(SimpleNamespace(
                        command=[sys.executable, "-m", "unittest", "test_mutating.py"]
                    ))
                    session = LocalSession(
                        session_id="mutating", tool="codex-cli", project_path=str(repo),
                        started_at=now, updated_at=datetime.now(timezone.utc),
                    )
                    evidence = build_outcome_evidence(session)
                    brief = build_handoff_capsule(session, [])["next_brief"]
            finally:
                os.chdir(original_cwd)

        self.assertEqual(exit_code, 0)
        receipt = next(item for item in evidence.tests if item.get("name") == "python -m unittest")
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["state_binding"], "git_state_changed")
        self.assertFalse(receipt["current"])
        self.assertNotIn("authoritative", receipt)
        self.assertIn("stale; Git state changed during verification", brief)
        self.assertIn("No session-bound completed verification was observed", brief)

    def test_uses_observed_linked_worktree_and_reports_unpushed_state(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            main = Path(temp_dir, "main")
            worktree = Path(temp_dir, "review")
            main.mkdir()
            init_repo(str(main))
            commit_file(str(main), "app.py", "base\n", "base", when=now - timedelta(hours=1))
            run(["git", "worktree", "add", "-b", "review-pr", str(worktree)], str(main))
            run(["git", "branch", "--set-upstream-to", "master", "review-pr"], str(worktree))
            commit_file(str(worktree), "fix.py", "fixed\n", "fix handoff", when=now + timedelta(minutes=1))

            session = LocalSession(
                session_id="worktree-session",
                tool="claude-code",
                project_path=str(main),
                raw_cwd=str(worktree),
                started_at=now,
                updated_at=now + timedelta(minutes=2),
            )
            evidence = build_outcome_evidence(session)

        self.assertEqual(Path(evidence.checkout_path or "").resolve(), worktree.resolve())
        self.assertEqual(Path(evidence.repo_root or "").resolve(), worktree.resolve())
        self.assertEqual(evidence.branch, "review-pr")
        self.assertEqual(evidence.upstream, "master")
        self.assertEqual(evidence.ahead, 1)
        self.assertEqual(evidence.behind, 0)
        self.assertFalse(evidence.dirty)
        self.assertEqual(evidence.unpushed_commits[0]["subject"], "fix handoff")

    def test_no_upstream_branch_reports_commits_ahead_of_local_base(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            main = Path(temp_dir, "main")
            worktree = Path(temp_dir, "review")
            main.mkdir()
            init_repo(str(main))
            commit_file(str(main), "app.py", "base\n", "base", when=now - timedelta(hours=1))
            run(["git", "worktree", "add", "-b", "review-pr", str(worktree)], str(main))
            commit_file(str(worktree), "fix.py", "fixed\n", "local review fix", when=now)

            session = LocalSession(
                session_id="no-upstream", tool="codex-cli", project_path=str(main), raw_cwd=str(worktree),
                started_at=now - timedelta(minutes=2), updated_at=now + timedelta(minutes=1),
            )
            evidence = build_outcome_evidence(session)

        self.assertIsNone(evidence.upstream)
        self.assertEqual(evidence.ahead, 1)
        self.assertEqual(evidence.unpushed_commits[0]["subject"], "local review fix")

    def test_receipts_from_sibling_checkout_are_not_attributed(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            main = Path(temp_dir, "main")
            worktree = Path(temp_dir, "review")
            main.mkdir()
            init_repo(str(main))
            head = commit_file(str(main), "app.py", "base\n", "base", when=now - timedelta(minutes=2))
            run(["git", "worktree", "add", "-b", "review-pr", str(worktree)], str(main))
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                main_identity = resolve_git_identity(str(main))
                assert main_identity is not None
                local_state.record_verification_receipt(
                    runner="pytest", checkout_path=str(main),
                    repository_id=main_identity.repository_id,
                    repository_lineage_id=main_identity.repository_lineage_id,
                    checkout_id=main_identity.checkout_id, head=head,
                    dirty_fingerprint="e3b0c44298fc1c149afbf4c8", started_at=now.isoformat(),
                    finished_at=(now + timedelta(seconds=5)).isoformat(), exit_code=0,
                )
                session = LocalSession(
                    session_id="sibling", tool="codex-cli", project_path=str(main), raw_cwd=str(worktree),
                    started_at=now - timedelta(minutes=1), updated_at=now + timedelta(minutes=1),
                )
                evidence = build_outcome_evidence(session)

        self.assertFalse(any(item.get("name") == "pytest" for item in evidence.tests))

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


class CommitUndoneTests(unittest.TestCase):
    """The cases check_commit_survival scores "survived" but a developer would
    call churn. Each test asserts both, so the gap stays visible."""

    def test_reverted_commit_is_undone_but_still_reachable(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "v1\n", "first", when=now)
            sha = commit_file(temp_dir, "feature.py", "feature\n", "add feature", when=now + timedelta(minutes=1))
            run(["git", "revert", "--no-edit", sha], temp_dir)

            self.assertEqual(check_commit_survival(temp_dir, sha), "survived")
            result = check_commit_undone(temp_dir, sha)

        self.assertTrue(result["undone"])
        self.assertEqual(len(result["reverted_by"]), 1)

    def test_commit_whose_files_were_all_deleted_is_undone(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            commit_file(temp_dir, "app.py", "v1\n", "first", when=now)
            sha = commit_file(temp_dir, "scratch.py", "temp\n", "add scratch", when=now + timedelta(minutes=1))
            run(["git", "rm", "scratch.py"], temp_dir)
            run(["git", "commit", "-m", "drop scratch"], temp_dir)

            self.assertEqual(check_commit_survival(temp_dir, sha), "survived")
            result = check_commit_undone(temp_dir, sha)

        self.assertTrue(result["undone"])
        self.assertEqual(result["files_missing"], 1)
        self.assertEqual(result["files_total"], 1)

    def test_live_commit_is_not_undone(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            sha = commit_file(temp_dir, "app.py", "v1\n", "first", when=now)
            commit_file(temp_dir, "other.py", "x\n", "unrelated", when=now + timedelta(minutes=1))
            result = check_commit_undone(temp_dir, sha)

        self.assertFalse(result["undone"])
        self.assertEqual(result["reverted_by"], [])
        self.assertEqual(result["files_missing"], 0)

    def test_partial_file_loss_is_reported_but_not_called_undone(self) -> None:
        # A rewrite that removes some of a commit's files is a signal, not
        # proof -- renames look identical from here. Line-level survival is
        # what resolves this; until then it must not flip the verdict.
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_repo(temp_dir)
            (Path(temp_dir) / "kept.py").write_text("keep\n", encoding="utf-8")
            (Path(temp_dir) / "dropped.py").write_text("drop\n", encoding="utf-8")
            stamp = now.strftime("%Y-%m-%dT%H:%M:%S%z")
            env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
            run(["git", "add", "kept.py", "dropped.py"], temp_dir, env=env)
            run(["git", "commit", "-m", "add two"], temp_dir, env=env)
            sha = run_out(["git", "rev-parse", "HEAD"], temp_dir)
            run(["git", "rm", "dropped.py"], temp_dir)
            run(["git", "commit", "-m", "drop one"], temp_dir)

            result = check_commit_undone(temp_dir, sha)

        self.assertFalse(result["undone"])
        self.assertEqual(result["files_missing"], 1)
        self.assertEqual(result["files_total"], 2)
        self.assertTrue(result["reasons"])

    def test_nonexistent_repo_reports_nothing(self) -> None:
        result = check_commit_undone("/no/such/repo/path", "abc123")
        self.assertFalse(result["undone"])
        self.assertEqual(result["files_total"], 0)


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
        self.assertTrue(any("is gone from" in reason for reason in evidence.reasons))

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
    def test_linked_worktree_paths_compare_by_repository_identity(self) -> None:
        now = datetime.now(timezone.utc)
        first = LocalSession(
            session_id="first", tool="codex-cli", project_path="/repo/main",
            repository_id="repository-1", started_at=now, updated_at=now,
        )
        second = LocalSession(
            session_id="second", tool="codex-cli", project_path="/repo/review",
            repository_id="repository-1",
            started_at=now + timedelta(hours=1), updated_at=now + timedelta(hours=1),
        )
        first_evidence = OutcomeEvidence(
            session_id="first", project_path=first.project_path,
            repository_id="repository-1", files_touched=["auth.py"],
        )
        second_evidence = OutcomeEvidence(
            session_id="second", project_path=second.project_path,
            repository_id="repository-1", files_touched=["auth.py"],
        )

        annotate_same_file_reprompt([
            (first, first_evidence),
            (second, second_evidence),
        ])

        self.assertTrue(first_evidence.same_file_reprompt)
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
