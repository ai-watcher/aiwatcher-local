from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from aiwatcher_cli import companion, ui


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
        self.assertNotIn("--presence-visibility", command)

    def test_command_can_place_collapsed_presence(self) -> None:
        command = companion.companion_command(
            30,
            presence=True,
            presence_position="top-left",
            presence_visibility="ai-apps",
        )

        self.assertIn("--presence", command)
        self.assertIn("--presence-position", command)
        self.assertIn("--presence-visibility", command)
        self.assertEqual(command[command.index("--presence-position") + 1], "top-left")
        self.assertEqual(command[command.index("--presence-visibility") + 1], "ai-apps")

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
            installed = companion.install_login_autostart(
                interval_seconds=10,
                presence_position="top-left",
                presence_visibility="nudges-only",
            )
            target = Path(str(installed["path"]))
            content = target.read_text(encoding="utf-8")
            removed = companion.uninstall_login_autostart()

        self.assertTrue(installed["ok"])
        self.assertIn("com.aiwatcher.local.companion", content)
        self.assertIn("top-left", content)
        self.assertIn("nudges-only", content)
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
                    presence_visibility="ai-apps",
                )

        self.assertTrue(result["ok"])
        launched = popen.call_args.args[0]
        self.assertIn("--presence", launched)
        self.assertIn("bottom-left", launched)
        self.assertIn("ai-apps", launched)

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


class WaitingSessionCompanionTests(unittest.TestCase):
    """The Companion's whole job is saying when something needs you."""

    def _summary(self, sessions, **extra):
        return {
            "presence": {"sessions": sessions},
            "totals": {"window_label": "Last 7 days", "sessions": 3},
            "watcher": {"running": True},
            **extra,
        }

    def _waiting(self, session_id="abc", *, idle=424.0, label="waiting 7m", project="/repo/aiwatcher-local"):
        return {
            "session_id": session_id, "tool": "claude-code", "state": "waiting",
            "label": label, "idle_seconds": idle, "project_path": project, "live": True,
        }

    def _state(self, summary, gate=None):
        with (
            patch.object(ui, "build_summary_cached", return_value=summary),
            patch.object(ui, "active_prompt_gate", return_value=gate),
        ):
            return ui.build_companion_state()

    def test_a_waiting_session_takes_over_the_companion(self):
        # It used to read "Watching quietly - 7 days: 3 sessions" while a
        # session sat blocked, which is the one case this surface must not miss.
        state = self._state(self._summary([self._waiting()]))
        self.assertEqual(state["state"], "session_waiting")
        self.assertEqual(state["label"], "Waiting on you")
        self.assertIn("7m", state["subtitle"])
        self.assertIn("Claude", state["subtitle"])

    def test_the_subtitle_fits_the_widget(self):
        # The widget truncates at 46 characters. A full project path spends
        # thirty of them on a prefix identical for every project, and the first
        # real render cut the project name off the end.
        for label, project in (
            ("waiting 7m", "/Users/dannylo/very-long-project-name-here"),
            ("waiting on you", "/Users/dannylo/aiwatcher-local"),
        ):
            with self.subTest(project=project):
                state = self._state(self._summary([self._waiting(label=label, project=project)]))
                self.assertLessEqual(len(str(state["subtitle"])), 46)
                self.assertIn(project.rsplit("/", 1)[-1][:12], str(state["subtitle"]))

    def test_a_sub_minute_wait_reads_as_a_sentence(self):
        # The per-session label is "waiting on you" under a minute, and pasting
        # that in gave "Claude - aiwatcher-local - on you".
        state = self._state(self._summary([self._waiting(label="waiting on you", idle=20.0)]))
        self.assertNotIn("on you", str(state["subtitle"]))

    def test_it_offers_a_way_into_the_session(self):
        state = self._state(self._summary([self._waiting("sess-42")]))
        self.assertEqual(state["primary_action"], "open_url")
        self.assertIn("sess-42", str(state["primary_url"]))
        self.assertEqual(state["primary_session_id"], "sess-42")

    def test_the_prompt_gate_still_outranks_it(self):
        # There AIWatcher is itself holding a prompt, and nothing proceeds
        # until the developer answers.
        state = self._state(
            self._summary([self._waiting()]),
            gate={"id": "g1", "tool": "claude-code", "risk": "high", "url": "/?view=prompt"},
        )
        self.assertEqual(state["state"], "prompt_gate")

    def test_it_outranks_every_advisory_state(self):
        # Fresh start, proof and optimize are advice about work still moving.
        state = self._state(self._summary(
            [self._waiting()],
            optimize={"status": "needs_action", "top": {"project": "/repo", "summary": "stale worktrees"}},
        ))
        self.assertEqual(state["state"], "session_waiting")

    def test_the_longest_wait_leads(self):
        state = self._state(self._summary([
            self._waiting("short", idle=90.0, label="waiting 2m"),
            self._waiting("long", idle=900.0, label="waiting 15m"),
        ]))
        self.assertIn("15m", state["subtitle"])
        self.assertEqual(state["primary_session_id"], "long")

    def test_several_waiting_sessions_say_so(self):
        state = self._state(self._summary([
            self._waiting("a", idle=900.0, label="waiting 15m"),
            self._waiting("b", idle=90.0, label="waiting 2m"),
        ]))
        self.assertIn("2 sessions", state["subtitle"])
        self.assertIn("15m", state["subtitle"])

    def test_nothing_waiting_leaves_the_companion_alone(self):
        state = self._state(self._summary([
            {"session_id": "busy", "tool": "claude-code", "state": "working",
             "label": "working", "idle_seconds": 5.0, "live": True},
        ]))
        self.assertNotEqual(state["state"], "session_waiting")

    def test_a_payload_without_presence_does_not_break_it(self):
        # The shell payload and older caches predate this field.
        with (
            patch.object(ui, "build_summary_cached", return_value={"totals": {}, "watcher": {"running": True}}),
            patch.object(ui, "active_prompt_gate", return_value=None),
        ):
            self.assertNotEqual(ui.build_companion_state()["state"], "session_waiting")


class WaitingWidgetAttentionTests(unittest.TestCase):
    """A state the widget does not know about renders calmly."""

    @classmethod
    def setUpClass(cls):
        from aiwatcher_cli import native_overlay

        cls.source = Path(native_overlay.__file__).read_text(encoding="utf-8")

    def test_both_widgets_treat_it_as_needing_attention(self):
        # Left out of these lists it would print "Waiting on you" in the calm
        # style with no button -- urgent words, idle appearance.
        for marker in ("func hasPrimaryAction()", "func needsAttentionState()",
                       "def has_primary_action()", "needs_attention = state_var.get()"):
            with self.subTest(marker=marker):
                block = self.source[self.source.index(marker):]
                self.assertIn("session_waiting", block[:400])
