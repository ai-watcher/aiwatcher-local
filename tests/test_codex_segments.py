from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from aiwatcher_cli import scanner, tasks
from aiwatcher_cli.scanner import LocalSession


T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def _ts(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _rollout_rows() -> list[dict]:
    def count(total: int, last_in: int, last_out: int, minute: int) -> dict:
        return {"timestamp": _ts(minute), "type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": total, "input_tokens": total - 50, "output_tokens": 50},
            "last_token_usage": {"input_tokens": last_in, "output_tokens": last_out}}}}
    return [
        {"timestamp": _ts(0), "type": "session_meta", "payload": {"id": "codex-1", "cwd": "/tmp/repo", "originator": "codex_cli"}},
        {"timestamp": _ts(0), "type": "turn_context", "payload": {"cwd": "/tmp/repo", "model": "gpt-5-codex"}},
        {"timestamp": _ts(0), "type": "event_msg", "payload": {"type": "user_message", "message": "fix the pricing table"}},
        {"timestamp": _ts(1), "type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
        count(1000, 900, 100, 1),
        count(1000, 900, 100, 1),  # duplicate cumulative row, must not double count
        {"timestamp": _ts(2), "type": "event_msg", "payload": {"type": "user_message", "message": "yes"}},
        count(1600, 500, 100, 2),
        {"timestamp": _ts(40), "type": "event_msg", "payload": {"type": "user_message", "message": "now write the release notes for v2"}},
        count(1900, 250, 50, 41),
    ]


class CodexSegmentTests(unittest.TestCase):
    def test_rollout_is_split_into_prompt_bounded_turns(self) -> None:
        scanner.SEGMENT_CACHE.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in _rollout_rows()) + "\n", encoding="utf-8")
            segments = scanner.segment_codex_session_by_prompt(str(path))
        self.assertEqual([seg["prompt"] for seg in segments], ["fix the pricing table", "yes", "now write the release notes for v2"])
        self.assertEqual([seg["turn"] for seg in segments], [1, 2, 3])
        self.assertEqual(segments[0]["tokens"], 1000)
        self.assertEqual(segments[0]["tool_calls"], 1)
        self.assertEqual(segments[1]["tokens"], 600)
        self.assertEqual(segments[2]["at"], _ts(40))

    def test_codex_events_carry_turn_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions_dir = Path(temp_dir) / "sessions"
            sessions_dir.mkdir()
            (sessions_dir / "rollout.jsonl").write_text("\n".join(json.dumps(row) for row in _rollout_rows()) + "\n", encoding="utf-8")
            scanner.CODEX_ROLLOUT_CACHE.clear() if hasattr(scanner.CODEX_ROLLOUT_CACHE, "clear") else None
            with patch.object(scanner, "CODEX_SESSIONS_DIRS", [sessions_dir]):
                _, events = scanner.scan_codex_rollouts()
        self.assertEqual([event.turn for event in events], [1, 2, 3])

    def test_codex_sessions_become_tasks(self) -> None:
        scanner.SEGMENT_CACHE.clear()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in _rollout_rows()) + "\n", encoding="utf-8")
            row = LocalSession(session_id="codex-1", tool="codex-cli", project_path="/tmp/repo", started_at=T0,
                               updated_at=T0 + timedelta(minutes=45), source_path=str(path))
            result = tasks.build_tasks([row], now=T0 + timedelta(days=1))
        self.assertEqual([task["label"] for task in result["tasks"]], ["fix the pricing table", "now write the release notes for v2"])
        self.assertEqual(result["tasks"][0]["turns"], 2)
        self.assertEqual(result["unmeasurable"], [])


if __name__ == "__main__":
    unittest.main()
