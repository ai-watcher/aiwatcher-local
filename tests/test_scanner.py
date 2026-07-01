from __future__ import annotations

import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()