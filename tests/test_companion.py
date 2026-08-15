from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from aiwatcher_cli import companion


class CompanionLifecycleTests(unittest.TestCase):
    def test_command_runs_dashboard_independent_companion(self) -> None:
        command = companion.companion_command(10)

        self.assertEqual(command[1:5], ["-m", "aiwatcher_cli", "companion", "run"])
        self.assertEqual(command[-1], "15")

    def test_existing_companion_is_reused(self) -> None:
        with patch.object(
            companion,
            "get_watcher_status",
            return_value={"running": True, "mode": "companion", "pid": 123},
        ):
            result = companion.start_companion()

        self.assertTrue(result["ok"])
        self.assertTrue(result["already_running"])

    def test_legacy_watch_must_stop_before_companion_starts(self) -> None:
        with patch.object(
            companion,
            "get_watcher_status",
            return_value={"running": True, "mode": "watch", "pid": 456},
        ):
            result = companion.start_companion()

        self.assertFalse(result["ok"])
        self.assertIn("not duplicated", result["message"])

    def test_start_waits_for_companion_heartbeat(self) -> None:
        process = Mock(pid=321)
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(companion, "companion_log_path", return_value=Path(temp_dir) / "companion.log"),
                patch.object(
                    companion,
                    "get_watcher_status",
                    side_effect=[
                        {"running": False},
                        {"running": True, "mode": "companion", "pid": 321},
                    ],
                ),
                patch.object(companion.subprocess, "Popen", return_value=process),
                patch.object(companion.time, "sleep"),
            ):
                result = companion.start_companion(interval_seconds=30)

        self.assertTrue(result["ok"])
        self.assertEqual(result["pid"], 321)

    def test_stop_does_not_kill_a_foreground_watch(self) -> None:
        with (
            patch.object(
                companion,
                "get_watcher_status",
                return_value={"running": True, "mode": "watch", "pid": 789},
            ),
            patch.object(companion.os, "kill") as kill,
        ):
            result = companion.stop_companion()

        self.assertFalse(result["ok"])
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
