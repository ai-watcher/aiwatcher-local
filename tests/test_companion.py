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
        self.assertIn("--presence", command)
        self.assertEqual(command[command.index("--interval") + 1], "15")

    def test_command_can_disable_collapsed_presence(self) -> None:
        command = companion.companion_command(30, presence=False)

        self.assertNotIn("--presence", command)
        self.assertNotIn("--presence-position", command)

    def test_command_can_place_collapsed_presence(self) -> None:
        command = companion.companion_command(30, presence=True, presence_position="top-left")

        self.assertIn("--presence", command)
        self.assertIn("--presence-position", command)
        self.assertEqual(command[-1], "top-left")

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

    def test_start_passes_presence_to_background_command(self) -> None:
        process = Mock(pid=322)
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(companion, "companion_log_path", return_value=Path(temp_dir) / "companion.log"),
                patch.object(
                    companion,
                    "get_watcher_status",
                    side_effect=[
                        {"running": False},
                        {"running": True, "mode": "companion", "pid": 322},
                    ],
                ),
                patch.object(companion.subprocess, "Popen", return_value=process) as popen,
                patch.object(companion.time, "sleep"),
            ):
                result = companion.start_companion(
                    interval_seconds=30,
                    presence=True,
                    presence_position="bottom-left",
                )

        self.assertTrue(result["ok"])
        launched = popen.call_args.args[0]
        self.assertIn("--presence", launched)
        self.assertIn("bottom-left", launched)

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
