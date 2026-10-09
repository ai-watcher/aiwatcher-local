from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiwatcher_cli.command_evidence import (
    _command_classification,
    command_evidence_coverage,
    command_evidence_for_session,
    environment_session_identity,
    verification_classification,
    verification_runner,
    verification_scope_for_cwd,
)


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class CommandEvidenceTests(unittest.TestCase):
    def test_runner_labels_are_fixed_and_argument_free(self) -> None:
        self.assertEqual(verification_runner(["python3", "-m", "pytest", "secret-test-name"]), "python -m pytest")
        self.assertEqual(verification_runner(["npm", "run", "check", "--", "private"]), "npm run check")
        self.assertIsNone(verification_runner(["bash", "-lc", "pytest"]))

    def test_verification_scope_is_conservative_and_argument_free(self) -> None:
        self.assertEqual(verification_classification(["pytest"]), ("pytest", "project_default"))
        self.assertEqual(verification_classification(["pytest", "-q"]), ("pytest", "targeted"))
        self.assertEqual(verification_classification(["npm", "run", "check"]), ("npm run check", "named_check"))
        self.assertEqual(verification_classification(["npm", "run", "check", "--", "private"]), ("npm run check", "targeted"))
        self.assertEqual(verification_classification(["cargo", "test"]), ("cargo test", "named_check"))
        self.assertEqual(verification_classification(["go", "test"]), ("go test", "named_check"))
        self.assertEqual(verification_scope_for_cwd("project_default", "/repo/pkg", "/repo"), "targeted")
        self.assertEqual(
            _command_classification("PYTEST_ADDOPTS=tests/private pytest"),
            ("verification", "pytest", "targeted"),
        )
        self.assertEqual(
            _command_classification("export PYTEST_ADDOPTS=tests/private && pytest"),
            (None, None, "unknown"),
        )

    def test_claude_pairs_bash_result_without_persisting_command_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "claude.jsonl")
            call = {
                "uuid": "call-row", "type": "assistant", "sessionId": "session-1",
                "cwd": "/repo/review", "timestamp": "2026-10-03T12:00:00Z",
                "message": {"content": [{
                    "type": "tool_use", "id": "tool-1", "name": "Bash",
                    "input": {"command": "python3 -m pytest tests/private_test.py"},
                }]},
            }
            result = {
                "uuid": "result-row", "type": "user", "sessionId": "session-1",
                "cwd": "/repo/review", "timestamp": "2026-10-03T12:01:00Z",
                "message": {"content": [{
                    "type": "tool_result", "tool_use_id": "tool-1",
                    "is_error": False, "content": "private output",
                }]},
                "toolUseResult": {"stdout": "private output", "stderr": "", "interrupted": False},
            }
            write_rows(path, [call, call, result])
            session = SimpleNamespace(source_path=str(path), session_id="session-1", tool="claude-code")

            evidence = command_evidence_for_session(session)

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].session_id, "session-1")
        self.assertEqual(evidence[0].cwd, "/repo/review")
        self.assertEqual(evidence[0].runner, "python -m pytest")
        self.assertEqual(evidence[0].verification_scope, "targeted")
        self.assertEqual(evidence[0].status, "passed")
        self.assertNotIn("command", evidence[0].__dict__)
        self.assertNotIn("output", evidence[0].__dict__)

    def test_claude_interrupted_and_background_results_never_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "claude.jsonl")
            rows: list[dict] = []
            for index, result in enumerate((
                {"interrupted": True, "stdout": ""},
                {"backgroundTaskId": "task-1", "stdout": ""},
            )):
                tool_id = f"tool-{index}"
                rows.extend([
                    {
                        "uuid": f"call-{index}", "type": "assistant", "sessionId": "s1", "cwd": "/repo",
                        "timestamp": f"2026-10-03T12:0{index}:00Z",
                        "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": "pytest"}}]},
                    },
                    {
                        "uuid": f"result-{index}", "type": "user", "sessionId": "s1", "cwd": "/repo",
                        "timestamp": f"2026-10-03T12:0{index}:30Z", "toolUseResult": result,
                        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "is_error": False, "content": ""}]},
                    },
                ])
            write_rows(path, rows)
            session = SimpleNamespace(source_path=str(path), session_id="s1", tool="claude-code")

            evidence = command_evidence_for_session(session)

        self.assertEqual({row.completion_state for row in evidence}, {"interrupted", "background"})
        self.assertTrue(all(row.status == "incomplete" for row in evidence))

    def test_codex_follows_async_poll_to_final_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "codex.jsonl")
            write_rows(path, [
                {"timestamp": "2026-10-03T12:00:00Z", "type": "session_meta", "payload": {"id": "codex-1", "cwd": "/repo"}},
                {"timestamp": "2026-10-03T12:00:01Z", "type": "response_item", "payload": {
                    "type": "function_call", "name": "exec_command", "call_id": "call-1",
                    "arguments": json.dumps({"cmd": "npm run check", "workdir": "/repo/worktree"}),
                }},
                {"timestamp": "2026-10-03T12:00:02Z", "type": "response_item", "payload": {
                    "type": "function_call_output", "call_id": "call-1",
                    "output": "Process running with session ID 42",
                }},
                {"timestamp": "2026-10-03T12:00:03Z", "type": "response_item", "payload": {
                    "type": "function_call", "name": "write_stdin", "call_id": "poll-1",
                    "arguments": json.dumps({"session_id": 42, "chars": ""}),
                }},
                {"timestamp": "2026-10-03T12:00:04Z", "type": "response_item", "payload": {
                    "type": "function_call_output", "call_id": "poll-1",
                    "output": "Process exited with code 0\nFinal output:\nprivate details",
                }},
            ])
            session = SimpleNamespace(source_path=str(path), session_id="codex-1", tool="codex-cli")

            evidence = command_evidence_for_session(session)

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].session_id, "codex-1")
        self.assertEqual(evidence[0].cwd, "/repo/worktree")
        self.assertEqual(evidence[0].status, "passed")

    def test_git_commit_chain_extracts_sha_but_rejects_directory_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "codex.jsonl")
            write_rows(path, [
                {"timestamp": "2026-10-03T12:00:00Z", "type": "session_meta", "payload": {"id": "s1", "cwd": "/repo"}},
                {"timestamp": "2026-10-03T12:00:01Z", "type": "response_item", "payload": {
                    "type": "function_call", "name": "exec_command", "call_id": "good",
                    "arguments": json.dumps({"cmd": "git add app.py && git commit -m fix"}),
                }},
                {"timestamp": "2026-10-03T12:00:02Z", "type": "response_item", "payload": {
                    "type": "function_call_output", "call_id": "good",
                    "output": "Process exited with code 0\nFinal output:\n[main abc1234] fix",
                }},
                {"timestamp": "2026-10-03T12:00:03Z", "type": "response_item", "payload": {
                    "type": "function_call", "name": "exec_command", "call_id": "cd",
                    "arguments": json.dumps({"cmd": "cd ../other && pytest"}),
                }},
                {"timestamp": "2026-10-03T12:00:04Z", "type": "response_item", "payload": {
                    "type": "function_call", "name": "exec_command", "call_id": "git-c",
                    "arguments": json.dumps({"cmd": "git -C ../other commit -m wrong"}),
                }},
            ])
            session = SimpleNamespace(source_path=str(path), session_id="s1", tool="codex-cli")

            evidence = command_evidence_for_session(session)

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].command_kind, "git_commit")
        self.assertEqual(evidence[0].commit_sha, "abc1234")

    def test_confirmed_push_extracts_bounded_delivery_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "claude.jsonl")
            write_rows(path, [
                {
                    "uuid": "call", "type": "assistant", "sessionId": "s1", "cwd": "/repo",
                    "timestamp": "2026-10-09T12:00:00Z",
                    "message": {"content": [{
                        "type": "tool_use", "id": "push-1", "name": "Bash",
                        "input": {"command": "git push origin feature"},
                    }]},
                },
                {
                    "uuid": "result", "type": "user", "sessionId": "s1", "cwd": "/repo",
                    "timestamp": "2026-10-09T12:00:02Z",
                    "message": {"content": [{
                        "type": "tool_result", "tool_use_id": "push-1", "is_error": False, "content": "private",
                    }]},
                    "toolUseResult": {
                        "stdout": "To github.com:owner/repo.git\n   abc1234..def5678  feature -> feature",
                        "stderr": "", "interrupted": False,
                    },
                },
            ])
            evidence = command_evidence_for_session(SimpleNamespace(
                source_path=str(path), session_id="s1", tool="claude-code",
            ))

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].command_kind, "git_push")
        self.assertEqual(evidence[0].delivery_base_sha, "abc1234")
        self.assertEqual(evidence[0].delivery_head_sha, "def5678")
        self.assertEqual(evidence[0].remote, "origin")
        self.assertEqual(evidence[0].remote_ref, "feature")
        self.assertNotIn("github.com", json.dumps(evidence[0].__dict__))

    def test_failed_or_background_push_never_has_delivery_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "claude.jsonl")
            rows = []
            for index, result in enumerate((
                {"stdout": "abc1234..def5678 feature -> feature"},
                {"stdout": "abc1234..def5678 feature -> feature", "backgroundTaskId": "task-1"},
            )):
                tool_id = f"push-{index}"
                rows.extend([
                    {
                        "uuid": f"call-{index}", "type": "assistant", "sessionId": "s1", "cwd": "/repo",
                        "timestamp": "2026-10-09T12:00:00Z",
                        "message": {"content": [{
                            "type": "tool_use", "id": tool_id, "name": "Bash",
                            "input": {"command": "git push"},
                        }]},
                    },
                    {
                        "uuid": f"result-{index}", "type": "user", "sessionId": "s1", "cwd": "/repo",
                        "timestamp": "2026-10-09T12:00:02Z", "toolUseResult": result,
                        "message": {"content": [{
                            "type": "tool_result", "tool_use_id": tool_id,
                            "is_error": True if index == 0 else False, "content": "private",
                        }]},
                    },
                ])
            write_rows(path, rows)
            evidence = command_evidence_for_session(SimpleNamespace(
                source_path=str(path), session_id="s1", tool="claude-code",
            ))

        self.assertEqual(len(evidence), 2)
        self.assertTrue(all(row.delivery_head_sha is None for row in evidence))
        self.assertEqual({row.completion_state for row in evidence}, {"completed", "background"})

    def test_forced_push_range_is_parsed_without_claiming_more_than_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "claude.jsonl")
            write_rows(path, [
                {
                    "uuid": "call", "type": "assistant", "sessionId": "s1", "cwd": "/repo",
                    "timestamp": "2026-10-09T12:00:00Z",
                    "message": {"content": [{
                        "type": "tool_use", "id": "push-force", "name": "Bash",
                        "input": {"command": "git push --force-with-lease origin feature"},
                    }]},
                },
                {
                    "uuid": "result", "type": "user", "sessionId": "s1", "cwd": "/repo",
                    "timestamp": "2026-10-09T12:00:02Z",
                    "message": {"content": [{
                        "type": "tool_result", "tool_use_id": "push-force", "is_error": False,
                        "content": "private",
                    }]},
                    "toolUseResult": {
                        "stdout": "To github.com:owner/repo.git\n + abc1234...def5678 feature -> feature (forced update)",
                        "stderr": "", "interrupted": False,
                    },
                },
            ])
            evidence = command_evidence_for_session(SimpleNamespace(
                source_path=str(path), session_id="s1", tool="claude-code",
            ))

        self.assertEqual(evidence[0].delivery_base_sha, "abc1234")
        self.assertEqual(evidence[0].delivery_head_sha, "def5678")

    def test_new_branch_push_is_confirmed_from_claude_stderr_without_inventing_a_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "claude.jsonl")
            write_rows(path, [
                {
                    "uuid": "call", "type": "assistant", "sessionId": "s1", "cwd": "/repo",
                    "timestamp": "2026-10-09T12:00:00Z",
                    "message": {"content": [{
                        "type": "tool_use", "id": "push-new", "name": "Bash",
                        "input": {"command": "git push --set-upstream origin feature"},
                    }]},
                },
                {
                    "uuid": "result", "type": "user", "sessionId": "s1", "cwd": "/repo",
                    "timestamp": "2026-10-09T12:00:02Z",
                    "message": {"content": [{
                        "type": "tool_result", "tool_use_id": "push-new", "is_error": False,
                        "content": "private",
                    }]},
                    "toolUseResult": {
                        "stdout": "", "stderr": " * [new branch] feature -> feature",
                        "interrupted": False,
                    },
                },
            ])
            evidence = command_evidence_for_session(SimpleNamespace(
                source_path=str(path), session_id="s1", tool="claude-code",
            ))

        self.assertTrue(evidence[0].delivery_ref_confirmed)
        self.assertIsNone(evidence[0].delivery_head_sha)
        self.assertEqual(evidence[0].remote_ref, "feature")

    def test_successful_gh_pr_create_requires_returned_github_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "codex.jsonl")
            write_rows(path, [
                {"timestamp": "2026-10-09T12:00:00Z", "type": "session_meta", "payload": {"id": "s1", "cwd": "/repo"}},
                {"timestamp": "2026-10-09T12:00:01Z", "type": "response_item", "payload": {
                    "type": "function_call", "name": "exec_command", "call_id": "pr-1",
                    "arguments": json.dumps({"cmd": "gh pr create --fill", "workdir": "/repo"}),
                }},
                {"timestamp": "2026-10-09T12:00:02Z", "type": "response_item", "payload": {
                    "type": "function_call_output", "call_id": "pr-1",
                    "output": json.dumps({"exit_code": 0, "output": "https://github.com/owner/repo/pull/42"}),
                }},
            ])
            evidence = command_evidence_for_session(SimpleNamespace(
                source_path=str(path), session_id="s1", tool="codex-cli",
            ))

        self.assertEqual(evidence[0].command_kind, "gh_pr_create")
        self.assertEqual(evidence[0].pull_request_url, "https://github.com/owner/repo/pull/42")

    def test_gh_pr_create_for_explicit_other_head_is_not_attributed_to_current_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "codex.jsonl")
            write_rows(path, [
                {"timestamp": "2026-10-09T12:00:00Z", "type": "session_meta", "payload": {"id": "s1", "cwd": "/repo"}},
                {"timestamp": "2026-10-09T12:00:01Z", "type": "response_item", "payload": {
                    "type": "function_call", "name": "exec_command", "call_id": "pr-other",
                    "arguments": json.dumps({"cmd": "gh pr create --head other-branch --fill", "workdir": "/repo"}),
                }},
                {"timestamp": "2026-10-09T12:00:02Z", "type": "response_item", "payload": {
                    "type": "function_call_output", "call_id": "pr-other",
                    "output": json.dumps({"exit_code": 0, "output": "https://github.com/owner/repo/pull/43"}),
                }},
            ])
            evidence = command_evidence_for_session(SimpleNamespace(
                source_path=str(path), session_id="s1", tool="codex-cli",
            ))

        self.assertEqual(evidence, [])

    def test_compact_gh_head_and_repo_selectors_are_not_attributed(self) -> None:
        for selector in ("-Hother-branch", "-Rowner/other"):
            with self.subTest(selector=selector), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir, "codex.jsonl")
                write_rows(path, [
                    {"timestamp": "2026-10-09T12:00:00Z", "type": "session_meta", "payload": {"id": "s1", "cwd": "/repo"}},
                    {"timestamp": "2026-10-09T12:00:01Z", "type": "response_item", "payload": {
                        "type": "function_call", "name": "exec_command", "call_id": "pr-other",
                        "arguments": json.dumps({"cmd": f"gh pr create {selector} --fill", "workdir": "/repo"}),
                    }},
                    {"timestamp": "2026-10-09T12:00:02Z", "type": "response_item", "payload": {
                        "type": "function_call_output", "call_id": "pr-other",
                        "output": json.dumps({"exit_code": 0, "output": "https://github.com/owner/repo/pull/43"}),
                    }},
                ])
                evidence = command_evidence_for_session(SimpleNamespace(
                    source_path=str(path), session_id="s1", tool="codex-cli",
                ))

            self.assertEqual(evidence, [])

    def test_environment_parameterized_delivery_commands_are_not_attributed(self) -> None:
        commands = (
            "GH_REPO=other/other gh pr create --fill",
            "GIT_DIR=../other/.git git push origin feature",
        )
        for index, command in enumerate(commands):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir, "claude.jsonl")
                write_rows(path, [
                    {
                        "uuid": f"call-{index}", "type": "assistant", "sessionId": "s1", "cwd": "/repo",
                        "timestamp": "2026-10-09T12:00:00Z",
                        "message": {"content": [{
                            "type": "tool_use", "id": f"delivery-{index}", "name": "Bash",
                            "input": {"command": command},
                        }]},
                    },
                    {
                        "uuid": f"result-{index}", "type": "user", "sessionId": "s1", "cwd": "/repo",
                        "timestamp": "2026-10-09T12:00:02Z",
                        "message": {"content": [{
                            "type": "tool_result", "tool_use_id": f"delivery-{index}",
                            "is_error": False, "content": "private",
                        }]},
                        "toolUseResult": {
                            "stdout": "https://github.com/other/other/pull/99\n abc1234..def5678 feature -> feature",
                            "stderr": "", "interrupted": False,
                        },
                    },
                ])
                evidence = command_evidence_for_session(SimpleNamespace(
                    source_path=str(path), session_id="s1", tool="claude-code",
                ))

            self.assertEqual(evidence, [])

    def test_opaque_codex_exec_is_disclosed_and_not_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "codex.jsonl")
            write_rows(path, [{
                "timestamp": "2026-10-03T12:00:00Z", "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "opaque", "input": "arbitrary javascript"},
            }])
            session = SimpleNamespace(source_path=str(path), session_id="s1", tool="codex-cli")

            self.assertEqual(command_evidence_for_session(session), [])
            self.assertEqual(command_evidence_coverage(session), "opaque_codex_exec")

    def test_mixed_codex_shell_coverage_discloses_opaque_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "codex.jsonl")
            write_rows(path, [
                {
                    "timestamp": "2026-10-03T12:00:00Z", "type": "response_item",
                    "payload": {
                        "type": "function_call", "name": "exec_command", "call_id": "structured",
                        "arguments": json.dumps({"cmd": "pytest", "workdir": "/repo"}),
                    },
                },
                {
                    "timestamp": "2026-10-03T12:00:01Z", "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call", "name": "exec", "call_id": "opaque",
                        "input": "arbitrary javascript",
                    },
                },
            ])
            session = SimpleNamespace(source_path=str(path), session_id="s1", tool="codex-cli")

            coverage = command_evidence_coverage(session)

        self.assertEqual(coverage, "partial_opaque_codex_exec")

    def test_environment_binding_prefers_explicit_then_codex(self) -> None:
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-1", "CODEX_SESSION_ID": "session-1"}, clear=False):
            self.assertEqual(environment_session_identity("manual"), ("manual", "explicit_cli"))
            self.assertEqual(environment_session_identity(), ("thread-1", "codex_thread_environment"))


if __name__ == "__main__":
    unittest.main()
