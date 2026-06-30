from __future__ import annotations

import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
