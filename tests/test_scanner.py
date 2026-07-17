from __future__ import annotations

import os
import tempfile
import unittest
import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiwatcher_cli import scanner


class ProjectPathTests(unittest.TestCase):
    def test_decode_claude_path_preserves_hyphenated_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "my-project"
            project.mkdir()
            if os.name == "nt":
                encoded = str(project).replace(":", "-").replace("\\", "-")
            else:
                encoded = "-" + str(project).lstrip("/").replace("/", "-")
            self.assertEqual(scanner._decode_claude_project_path(encoded), str(project))

    def test_choose_project_prefers_cost_then_event_count(self) -> None:
        normalized = {
            "/repo/a": "/repo/a",
            "/repo/b": "/repo/b",
            "/fallback": "/fallback",
        }
        with patch.object(scanner, "_normalize_project_path", side_effect=lambda value: normalized.get(value)):
            selected = scanner._choose_project_path(
                "/fallback",
                {"/repo/a": 20, "/repo/b": 3},
                {"/repo/a": 1.0, "/repo/b": 5.0},
            )
        self.assertEqual(selected, "/repo/b")

    def test_codex_rollout_uses_measured_token_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "sessions"
            root.mkdir()
            rollout = root / "rollout-session-1.jsonl"
            rows = [
                {"timestamp": "2026-07-01T10:00:00Z", "type": "session_meta", "payload": {"id": "session-1", "cwd": temp_dir}},
                {"timestamp": "2026-07-01T10:00:01Z", "type": "turn_context", "payload": {"model": "gpt-5.2-codex", "cwd": temp_dir}},
                {"timestamp": "2026-07-01T10:00:02Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                    "last_token_usage": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                }}},
                {"timestamp": "2026-07-01T10:00:03Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 1800, "output_tokens": 200, "total_tokens": 2000},
                    "last_token_usage": {"input_tokens": 800, "output_tokens": 100, "total_tokens": 900},
                }}},
            ]
            rollout.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [root]):
                sessions, events = scanner.scan_codex_rollouts()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].tokens_in, 1800)
        self.assertEqual(sessions[0].tokens_out, 200)
        self.assertEqual(sessions[0].agent_calls, 2)
        self.assertEqual(len(events), 2)
        self.assertNotIn("cumulative", " ".join(sessions[0].notes).lower())


class ScanDateTests(unittest.TestCase):
    def test_updated_at_uses_mtime_when_tail_events_lack_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            session_file = project / "sess.jsonl"
            lines = [
                '{"type":"assistant","timestamp":"2026-06-24T10:00:00Z",'
                '"message":{"model":"claude-opus-4-8","usage":{"input_tokens":10,"output_tokens":5}}}',
                '{"type":"queue-operation"}',
                '{"type":"attachment"}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            later = datetime(2026, 6, 30, 20, 0, 0, tzinfo=timezone.utc).timestamp()
            os.utime(session_file, (later, later))

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertIsNotNone(session.updated_at)
            self.assertEqual(session.updated_at.astimezone(timezone.utc).date(), date(2026, 6, 30))
            self.assertIsNotNone(session.started_at)
            self.assertEqual(session.started_at.astimezone(timezone.utc).date(), date(2026, 6, 24))

    def test_updated_at_ignores_mtime_when_fully_timestamped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            session_file = project / "sess.jsonl"
            lines = [
                '{"type":"assistant","timestamp":"2026-06-24T10:00:00Z",'
                '"message":{"model":"claude-opus-4-8","usage":{"input_tokens":10,"output_tokens":5}}}',
                '{"type":"assistant","timestamp":"2026-06-24T10:05:00Z",'
                '"message":{"model":"claude-opus-4-8","usage":{"input_tokens":10,"output_tokens":5}}}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            # Simulate the file being copied/restored/synced well after the session ended.
            later = datetime(2026, 6, 30, 20, 0, 0, tzinfo=timezone.utc).timestamp()
            os.utime(session_file, (later, later))

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertIsNotNone(session.updated_at)
            self.assertEqual(session.updated_at.astimezone(timezone.utc).date(), date(2026, 6, 24))


class ModelAttributionTests(unittest.TestCase):
    """A session that switches models mid-conversation must keep every model's
    usage visible, instead of the earlier behavior where `model = event_model`
    silently overwrote the session's model field on every event, so only the
    last model used in the file survived into the session summary."""

    def test_multi_model_session_keeps_every_model_in_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            session_file = project / "sess.jsonl"
            lines = [
                '{"type":"assistant","timestamp":"2026-07-12T10:00:00Z","entrypoint":"claude-desktop",'
                '"message":{"model":"claude-fable-5","usage":{"input_tokens":1000,"output_tokens":200}}}',
                '{"type":"assistant","timestamp":"2026-07-12T10:05:00Z","entrypoint":"claude-desktop",'
                '"message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":2000,"output_tokens":400}}}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            # Fable was used first but Sonnet used more tokens — Sonnet should be
            # the reported primary model, but Fable must not disappear entirely.
            self.assertEqual(session.model, "claude-sonnet-4-6")
            self.assertIn("claude-fable-5", session.model_breakdown)
            self.assertIn("claude-sonnet-4-6", session.model_breakdown)
            self.assertEqual(session.model_breakdown["claude-fable-5"]["tokens_in"], 1000)
            self.assertEqual(session.model_breakdown["claude-sonnet-4-6"]["tokens_in"], 2000)
            # Session-level totals still sum across every model used.
            self.assertEqual(session.tokens_in, 3000)

    def test_desktop_entrypoint_is_recorded_as_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            session_file = project / "sess.jsonl"
            lines = [
                '{"type":"assistant","timestamp":"2026-07-12T10:00:00Z","entrypoint":"claude-desktop",'
                '"message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":10,"output_tokens":5}}}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()

            self.assertEqual(sessions[0].surface, "desktop")

    def test_cli_entrypoint_is_recorded_as_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            session_file = project / "sess.jsonl"
            lines = [
                '{"type":"assistant","timestamp":"2026-07-12T10:00:00Z","entrypoint":"cli",'
                '"message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":10,"output_tokens":5}}}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()

            self.assertEqual(sessions[0].surface, "cli")

    def test_missing_entrypoint_leaves_surface_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            project = projects / "-tmp-demo"
            project.mkdir(parents=True)
            session_file = project / "sess.jsonl"
            lines = [
                '{"type":"assistant","timestamp":"2026-07-12T10:00:00Z",'
                '"message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":10,"output_tokens":5}}}',
            ]
            session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects]):
                sessions = scanner.scan_claude_code()

            self.assertIsNone(sessions[0].surface)

    def test_model_usage_totals_aggregates_across_sessions_and_falls_back(self) -> None:
        with_breakdown = scanner.LocalSession(
            session_id="a",
            tool="claude-code",
            model="claude-sonnet-4-6",
            tokens_in=100,
            tokens_out=50,
            cost_usd=1.0,
            model_breakdown={
                "claude-fable-5": {"tokens_in": 40, "tokens_out": 20, "cost_usd": 0.4, "agent_calls": 1, "tool_calls": 0},
                "claude-sonnet-4-6": {"tokens_in": 60, "tokens_out": 30, "cost_usd": 0.6, "agent_calls": 1, "tool_calls": 1},
            },
        )
        # Older/other-tool sessions with no breakdown fall back to their single model field.
        without_breakdown = scanner.LocalSession(
            session_id="b",
            tool="cursor",
            model="cursor-ai",
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.1,
            agent_calls=1,
        )

        totals = scanner.model_usage_totals([with_breakdown, without_breakdown])

        self.assertEqual(totals["claude-fable-5"]["tokens_in"], 40)
        self.assertEqual(totals["claude-sonnet-4-6"]["tokens_in"], 60)
        self.assertEqual(totals["cursor-ai"]["tokens_in"], 10)
        self.assertEqual(totals["claude-fable-5"]["sessions"], 1)


class CodexOriginatorTests(unittest.TestCase):
    def test_codex_desktop_originator_is_recorded_as_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions_dir = Path(temp_dir) / "sessions"
            day_dir = sessions_dir / "2026" / "07" / "12"
            day_dir.mkdir(parents=True)
            rollout = day_dir / "rollout-test.jsonl"
            lines = [
                json.dumps({
                    "type": "session_meta",
                    "timestamp": "2026-07-12T10:00:00Z",
                    "payload": {"id": "codex-1", "cwd": "/repo", "originator": "Codex Desktop"},
                }),
                json.dumps({
                    "type": "turn_context",
                    "timestamp": "2026-07-12T10:00:01Z",
                    "payload": {"cwd": "/repo", "model": "gpt-5.5"},
                }),
                json.dumps({
                    "type": "event_msg",
                    "timestamp": "2026-07-12T10:00:02Z",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 100, "input_tokens": 80, "output_tokens": 20},
                            "last_token_usage": {"input_tokens": 80, "output_tokens": 20},
                        },
                    },
                }),
            ]
            rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [sessions_dir]):
                scanner.CODEX_ROLLOUT_CACHE = None
                sessions, _events = scanner.scan_codex_rollouts()

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].surface, "desktop")

    def test_codex_tui_originator_is_recorded_as_cli_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions_dir = Path(temp_dir) / "sessions"
            day_dir = sessions_dir / "2026" / "07" / "12"
            day_dir.mkdir(parents=True)
            rollout = day_dir / "rollout-test.jsonl"
            lines = [
                json.dumps({
                    "type": "session_meta",
                    "timestamp": "2026-07-12T10:00:00Z",
                    "payload": {"id": "codex-2", "cwd": "/repo", "originator": "codex-tui"},
                }),
                json.dumps({
                    "type": "turn_context",
                    "timestamp": "2026-07-12T10:00:01Z",
                    "payload": {"cwd": "/repo", "model": "gpt-5.5"},
                }),
                json.dumps({
                    "type": "event_msg",
                    "timestamp": "2026-07-12T10:00:02Z",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 100, "input_tokens": 80, "output_tokens": 20},
                            "last_token_usage": {"input_tokens": 80, "output_tokens": 20},
                        },
                    },
                }),
            ]
            rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [sessions_dir]):
                scanner.CODEX_ROLLOUT_CACHE = None
                sessions, _events = scanner.scan_codex_rollouts()

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].surface, "cli")


if __name__ == "__main__":
    unittest.main()