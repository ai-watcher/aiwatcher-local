"""The turn boundary: which user-role transcript lines count as something the user typed.

Claude Code writes several kinds of user-role lines nobody typed -- slash-command
wrappers, injected reminders, and background-agent completion notices. A turn
counter that opens a turn on each of them inflates every per-task number that
sits on top of it (a session with 7 real prompts read as 22 turns).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiwatcher_cli import scanner


NOTIFICATION = (
    "<system-reminder>\n[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
    "<task-notification>\n<task-id>abc123</task-id>\n<tool-use-id>toolu_01</tool-use-id>\n"
    "<status>completed</status>\n<result>Agent finished.</result>\n</task-notification>\n"
    "</system-reminder>"
)


class UserPromptTextTests(unittest.TestCase):
    def test_background_agent_notification_is_not_a_prompt(self) -> None:
        self.assertIsNone(scanner._user_prompt_text(NOTIFICATION))
        bare = "<task-notification>\n<task-id>x</task-id>\n<status>completed</status>\n</task-notification>"
        self.assertIsNone(scanner._user_prompt_text(bare))

    def test_user_text_beside_a_notification_survives(self) -> None:
        text = NOTIFICATION + "\n\nanything?"
        self.assertEqual(scanner._user_prompt_text(text), "anything?")

    def test_quote_reply_marker_is_stripped_not_skipped(self) -> None:
        text = "<!-- attach -->\n> earlier line being quoted\n\nlet's explore this"
        self.assertEqual(
            scanner._user_prompt_text(text),
            "> earlier line being quoted\n\nlet's explore this",
        )

    def test_slash_command_wrappers_are_still_skipped(self) -> None:
        text = (
            "<command-name>/model</command-name>\n<command-message>model</command-message>\n"
            "<command-args>claude-fable-5-1</command-args>\n"
            "<local-command-stdout>Set model to `claude-fable-5-1`</local-command-stdout>"
        )
        self.assertIsNone(scanner._user_prompt_text(text))
        self.assertIsNone(scanner._user_prompt_text("Caveat: the messages below were generated locally"))

    def test_unclosed_injected_block_is_skipped(self) -> None:
        self.assertIsNone(scanner._user_prompt_text("<system-reminder>\ntruncated with no closing tag"))

    def test_list_content_goes_through_the_same_filter(self) -> None:
        content = [{"type": "text", "text": NOTIFICATION}, {"type": "text", "text": "and a real ask"}]
        self.assertEqual(scanner._user_prompt_text(content), "and a real ask")


def _line(kind: str, text: str | None = None, *, tokens: int = 0) -> dict:
    row: dict = {"type": kind, "timestamp": "2026-09-01T10:00:00Z", "cwd": "/tmp/repo"}
    if kind == "user":
        row["message"] = {"role": "user", "content": [{"type": "text", "text": text or ""}]}
    else:
        row["message"] = {
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": tokens, "output_tokens": 10},
        }
    return row


class SegmentationTests(unittest.TestCase):
    def _write(self, rows: list[dict]) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        with handle:
            handle.write("\n".join(json.dumps(row) for row in rows) + "\n")
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def test_notifications_fold_into_the_open_turn(self) -> None:
        rows = [
            _line("user", "review the PR"),
            _line("assistant", tokens=100),
            _line("user", NOTIFICATION),
            _line("assistant", tokens=250),
            _line("user", NOTIFICATION),
            _line("assistant", tokens=300),
            _line("user", "now fix the pricing table"),
            _line("assistant", tokens=50),
        ]
        segments = scanner.segment_session_by_prompt(str(self._write(rows)))
        self.assertEqual([seg["prompt"] for seg in segments], ["review the PR", "now fix the pricing table"])
        self.assertEqual(segments[0]["turn"], 1)
        self.assertEqual(segments[1]["turn"], 2)
        # The work Claude did after each notification still belongs to the task that was open.
        self.assertEqual(segments[0]["tokens"], 100 + 10 + 250 + 10 + 300 + 10)
        self.assertEqual(segments[1]["tokens"], 50 + 10)

    def test_event_turn_numbers_agree_with_segmentation(self) -> None:
        rows = [
            _line("user", "first ask"),
            _line("assistant", tokens=10),
            _line("user", NOTIFICATION),
            _line("assistant", tokens=10),
            _line("user", "<!-- attach -->\n> quoted\n\nsecond ask"),
            _line("assistant", tokens=10),
        ]
        path = self._write(rows)
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects" / "-tmp-repo"
            projects.mkdir(parents=True)
            target = projects / "sess.jsonl"
            target.write_bytes(path.read_bytes())
            with patch.object(scanner, "CLAUDE_PROJECTS_DIRS", [projects.parent]):
                events = scanner.scan_claude_code_events()
        self.assertEqual(max(event.turn for event in events), 2)
        segments = scanner.segment_session_by_prompt(str(path))
        self.assertEqual(len(segments), 2)


if __name__ == "__main__":
    unittest.main()
