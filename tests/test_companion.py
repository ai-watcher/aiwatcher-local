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

    def test_tray_command_starts_native_tray_path(self) -> None:
        command = companion.tray_command(10)

        self.assertEqual(command[1:6], ["-m", "aiwatcher_cli", "companion", "tray", "start"])
        self.assertEqual(command[command.index("--interval") + 1], "15")

    def test_login_autostart_status_uses_user_level_path(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(companion.sys, "platform", "darwin"),
            patch.object(companion.Path, "home", return_value=Path(temp_dir)),
        ):
            status = companion.login_autostart_status()

        self.assertTrue(status["supported"])
        self.assertFalse(status["installed"])
        self.assertIn("LaunchAgents", status["path"])

    def test_install_and_uninstall_macos_login_autostart(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(companion.sys, "platform", "darwin"),
            patch.object(companion.Path, "home", return_value=Path(temp_dir)),
            patch.object(companion, "companion_log_path", return_value=Path(temp_dir) / "companion.log"),
        ):
            installed = companion.install_login_autostart(interval_seconds=10, presence_position="top-left")
            target = Path(str(installed["path"]))
            content = target.read_text(encoding="utf-8")
            removed = companion.uninstall_login_autostart()

        self.assertTrue(installed["ok"])
        self.assertIn("com.aiwatcher.local.companion", content)
        self.assertIn("top-left", content)
        self.assertTrue(removed["ok"])
        self.assertTrue(removed["removed"])

    def test_install_macos_tray_login_autostart(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(companion.sys, "platform", "darwin"),
            patch.object(companion.Path, "home", return_value=Path(temp_dir)),
            patch.object(companion, "companion_log_path", return_value=Path(temp_dir) / "companion.log"),
        ):
            installed = companion.install_login_autostart(interval_seconds=30, tray=True)
            content = Path(str(installed["path"])).read_text(encoding="utf-8")

        self.assertTrue(installed["ok"])
        self.assertIn("companion", content)
        self.assertIn("tray", content)
        self.assertIn("start", content)

    def test_tray_status_is_honest_packaging_boundary(self) -> None:
        with patch.object(companion.sys, "platform", "darwin"):
            status = companion.tray_status()

        self.assertTrue(status["supported"])
        self.assertEqual(status["mode"], "native_menu_bar")
        self.assertIn("menu-bar", status["label"])
        self.assertIn("Scan Now", status["detail"])

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
                patch.object(companion, "cleanup_orphan_companion_processes", return_value=[]),
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
                patch.object(companion, "cleanup_orphan_companion_processes", return_value=[]),
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

    def test_stop_cleans_orphan_presence_when_heartbeat_is_stale(self) -> None:
        with (
            patch.object(companion, "get_watcher_status", return_value={}),
            patch.object(companion, "cleanup_orphan_companion_processes", return_value=[111, 222]) as cleanup,
            patch.object(companion, "clear_watcher_heartbeat") as clear,
        ):
            result = companion.stop_companion()

        self.assertTrue(result["ok"])
        self.assertTrue(result["stopped"])
        self.assertEqual(result["orphan_pids"], [111, 222])
        cleanup.assert_called_once()
        clear.assert_called_once()

    def test_stop_cleans_orphans_after_primary_companion(self) -> None:
        with (
            patch.object(
                companion,
                "get_watcher_status",
                return_value={"running": True, "mode": "companion", "pid": 123},
            ),
            patch.object(companion, "_terminate_pid", return_value=True) as terminate,
            patch.object(companion, "cleanup_orphan_companion_processes", return_value=[456]) as cleanup,
            patch.object(companion, "clear_watcher_heartbeat") as clear,
        ):
            result = companion.stop_companion()

        self.assertTrue(result["ok"])
        self.assertTrue(result["stopped"])
        self.assertEqual(result["orphan_pids"], [456])
        terminate.assert_called_once_with(123)
        cleanup.assert_called_once_with(exclude_pid=123)
        clear.assert_called_once_with(pid=123)

    def test_pgrep_sweep_matches_presence_processes(self) -> None:
        with (
            patch.object(companion.sys, "platform", "darwin"),
            patch.object(companion.shutil, "which", return_value="/usr/bin/pgrep"),
            patch.object(companion.subprocess, "check_output", side_effect=["123\n", "456\n", "789\n"]),
            patch.object(companion.os, "getpid", return_value=999),
            patch.object(companion.os, "getppid", return_value=998),
        ):
            pids = companion._orphan_companion_pids()

        self.assertEqual(pids, {123, 456, 789})

    def test_terminate_pid_falls_back_to_direct_process_kill(self) -> None:
        with (
            patch.object(companion.sys, "platform", "darwin"),
            patch.object(companion.os, "getpid", return_value=999),
            patch.object(companion.os, "killpg", side_effect=ProcessLookupError, create=True) as killpg,
            patch.object(companion.os, "kill") as kill,
        ):
            result = companion._terminate_pid(123)

        self.assertTrue(result)
        killpg.assert_called_once_with(123, companion.signal.SIGTERM)
        kill.assert_called_once_with(123, companion.signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
