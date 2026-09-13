from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from datetime import datetime, timedelta, timezone

from aiwatcher_cli import companion, compaction, ui
from aiwatcher_cli.local_state import compact_nudge, record_companion_skip, update_compact_nudge
from aiwatcher_cli.scanner import LocalSession


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

    def test_disabled_presence_survives_the_relaunch(self) -> None:
        """`companion start` relaunches itself in the background, so the flag has
        to be readable by the copy. Omitting --presence is not enough: the child
        only reads --no-presence and defaults to presence on, so the bar came
        back for everyone who asked for it to be off."""
        from aiwatcher_cli.cli import build_parser

        command = companion.companion_command(30, presence=False)
        self.assertIn("--no-presence", command)

        child = build_parser().parse_args(command[command.index("companion"):])
        self.assertTrue(child.no_presence)

    def test_enabled_presence_does_not_disable_itself(self) -> None:
        from aiwatcher_cli.cli import build_parser

        command = companion.companion_command(30, presence=True)
        self.assertNotIn("--no-presence", command)

        child = build_parser().parse_args(command[command.index("companion"):])
        self.assertFalse(child.no_presence)

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

    def test_windows_helpers_get_a_headless_console_not_no_console(self) -> None:
        """Inside a venv or pipx install sys.executable is the venv redirector,
        which relaunches the real python.exe with creation flags of zero. A
        DETACHED_PROCESS redirector has no console to hand down, so Windows
        allocated a visible one for the interpreter: `aiwatcher start` opened
        one blank terminal for the daemon and one for the presence widget."""
        with (
            patch.object(companion.sys, "platform", "win32"),
            patch.object(companion.subprocess, "DETACHED_PROCESS", 0x8, create=True),
            patch.object(companion.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True),
            patch.object(companion.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
        ):
            kwargs = companion.background_process_kwargs()

        self.assertFalse(kwargs["start_new_session"])
        self.assertEqual(kwargs["creationflags"], 0x08000200)
        self.assertFalse(kwargs["creationflags"] & 0x8, "DETACHED_PROCESS starves the venv redirector of a console")

    def test_start_launches_the_daemon_with_the_headless_console_flags(self) -> None:
        process = Mock(pid=323)
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(companion.sys, "platform", "win32"),
                patch.object(companion.subprocess, "DETACHED_PROCESS", 0x8, create=True),
                patch.object(companion.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True),
                patch.object(companion.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
                patch.object(companion, "companion_log_path", return_value=Path(temp_dir) / "companion.log"),
                patch.object(
                    companion,
                    "get_watcher_status",
                    side_effect=[
                        {"running": False},
                        {"running": True, "mode": "companion", "pid": 323},
                    ],
                ),
                patch.object(companion.subprocess, "Popen", return_value=process) as popen,
                patch.object(companion, "cleanup_orphan_companion_processes", return_value=[]),
                patch.object(companion.time, "sleep"),
            ):
                result = companion.start_companion(interval_seconds=30)

        self.assertTrue(result["ok"])
        kwargs = popen.call_args.kwargs
        self.assertFalse(kwargs["start_new_session"])
        self.assertEqual(kwargs["creationflags"], 0x08000200)

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

    # Presence is built here from real sessions and real signals rather than
    # stubbed into the summary, because the summary is exactly where it must
    # not come from: it is cached for six hours on disk, and this state is a
    # fact about right now.
    def _summary(self, **extra):
        return {
            "totals": {"window_label": "Last 7 days", "sessions": 3},
            "watcher": {"running": True},
            **extra,
        }

    def _session(self, session_id="abc", *, project="/repo/aiwatcher-local", idle_minutes=9.0):
        return LocalSession(
            session_id=session_id,
            tool="claude-code",
            project_path=project,
            raw_cwd=project,
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=idle_minutes),
        )

    def _signal(self, session_id="abc", *, minutes_ago=7.0):
        return {session_id: {
            "at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(),
            "tool": "claude-code",
            "kind": "permission",
        }}

    def _state(self, summary, *, sessions=(), signals=None, gate=None, return_available=False, prefs=None):
        companion_prefs = {
            "blocked_sessions": True,
            "fresh_start_context": True,
            "finished_sessions": "badge_only",
            "batch_finished_sessions": True,
            **(prefs or {}),
        }
        with (
            patch.object(ui, "build_summary_cached", return_value=summary),
            patch.object(ui, "companion_preferences", return_value=companion_prefs),
            patch.object(ui, "active_prompt_gate", return_value=gate),
            patch.object(ui, "_cached_session_rows", return_value=list(sessions)),
            patch.object(ui, "session_waiting_signals", return_value=signals or {}),
            # Pinned rather than classified: the real helper reads the live
            # process table, and whether a test machine happens to have a
            # matching window must not decide what these tests assert.
            patch.object(ui, "_waiting_row_return_available", return_value=return_available),
        ):
            return ui.build_companion_state()

    def test_a_waiting_session_takes_over_the_companion(self):
        # It used to read "Watching quietly - 7 days: 3 sessions" while a
        # session sat blocked, which is the one case this surface must not miss.
        state = self._state(self._summary(), sessions=[self._session()], signals=self._signal())
        self.assertEqual(state["state"], "session_waiting")
        self.assertEqual(state["label"], "Waiting on you")
        self.assertIn("7m", state["subtitle"])
        self.assertIn("Claude", state["subtitle"])

    def test_the_subtitle_fits_the_widget(self):
        # The widget truncates at 46 characters. A full project path spends
        # thirty of them on a prefix identical for every project, and the first
        # real render cut the project name off the end.
        for minutes, project in (
            (7.0, "/Users/dannylo/very-long-project-name-here"),
            (0.2, "/Users/dannylo/aiwatcher-local"),
        ):
            with self.subTest(project=project):
                state = self._state(
                    self._summary(),
                    sessions=[self._session(project=project, idle_minutes=minutes + 2)],
                    signals=self._signal(minutes_ago=minutes),
                )
                self.assertLessEqual(len(str(state["subtitle"])), 46)
                self.assertIn(project.rsplit("/", 1)[-1][:12], str(state["subtitle"]))

    def test_a_sub_minute_wait_reads_as_a_sentence(self):
        # The per-session label is "waiting on you" under a minute, and pasting
        # that in gave "Claude - aiwatcher-local - on you".
        state = self._state(self._summary(), sessions=[self._session(idle_minutes=0.5)], signals=self._signal(minutes_ago=0.3))
        self.assertNotIn("on you", str(state["subtitle"]))

    def test_it_offers_a_way_into_the_session(self):
        state = self._state(self._summary(), sessions=[self._session("sess-42")], signals=self._signal("sess-42"))
        self.assertEqual(state["primary_action"], "open_url")
        self.assertIn("sess-42", str(state["primary_url"]))
        self.assertEqual(state["primary_session_id"], "sess-42")

    def test_the_prompt_gate_still_outranks_it(self):
        # There AIWatcher is itself holding a prompt, and nothing proceeds
        # until the developer answers.
        state = self._state(
            self._summary(), sessions=[self._session()], signals=self._signal(),
            gate={"id": "g1", "tool": "claude-code", "risk": "high", "url": "/?view=prompt"},
        )
        self.assertEqual(state["state"], "prompt_gate")

    def test_it_outranks_every_advisory_state(self):
        # Fresh start, proof and optimize are advice about work still moving.
        state = self._state(
            self._summary(optimize={"status": "needs_action",
                                    "top": {"project": "/repo", "summary": "stale worktrees"}}),
            sessions=[self._session()], signals=self._signal(),
        )
        self.assertEqual(state["state"], "session_waiting")

    def test_the_longest_wait_leads(self):
        state = self._state(
            self._summary(),
            sessions=[self._session("short", idle_minutes=3.0), self._session("long", idle_minutes=17.0)],
            signals={**self._signal("short", minutes_ago=2.0), **self._signal("long", minutes_ago=15.0)},
        )
        self.assertIn("15m", state["subtitle"])
        self.assertEqual(state["primary_session_id"], "long")

    def test_several_waiting_sessions_say_so(self):
        state = self._state(
            self._summary(),
            sessions=[self._session("a", idle_minutes=17.0), self._session("b", idle_minutes=3.0)],
            signals={**self._signal("a", minutes_ago=15.0), **self._signal("b", minutes_ago=2.0)},
        )
        self.assertIn("2 runs", state["subtitle"])
        self.assertIn("15m", state["subtitle"])

    def test_nothing_waiting_leaves_the_companion_alone(self):
        state = self._state(self._summary(), sessions=[self._session("busy", idle_minutes=0.1)], signals={})
        self.assertNotEqual(state["state"], "session_waiting")

    def test_waiting_session_alerts_can_be_disabled_for_companion(self):
        state = self._state(
            self._summary(),
            sessions=[self._session()],
            signals=self._signal(),
            prefs={"blocked_sessions": False},
        )

        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["presence"]["waiting"], 1)

    def test_an_unreadable_signal_store_does_not_break_it(self):
        with (
            patch.object(ui, "build_summary_cached", return_value=self._summary()),
            patch.object(ui, "active_prompt_gate", return_value=None),
            patch.object(ui, "_cached_session_rows", return_value=[]),
            patch.object(ui, "session_waiting_signals", side_effect=OSError("locked")),
        ):
            self.assertNotEqual(ui.build_companion_state()["state"], "session_waiting")

    def test_it_does_not_read_the_cached_summary_for_this(self):
        # The summary is cached for six hours on disk. Served from there, a
        # wait that started thirty seconds ago would not appear until the cache
        # turned over, and the Companion would sit quiet through it.
        stale = self._summary(presence={"sessions": [
            {"session_id": "ghost", "state": "waiting", "label": "waiting 3h",
             "idle_seconds": 10800.0, "tool": "claude-code", "project_path": "/repo/old"},
        ]})
        state = self._state(stale, sessions=[], signals={})
        self.assertNotEqual(state["state"], "session_waiting")


class CompanionPresencePayloadTests(WaitingSessionCompanionTests):
    """The additive phase-1 payload: live presence, a waiting queue, a countdown.

    Subclassing borrows the _state/_session/_signal harness; the inherited
    tests re-run here, which is harmless and keeps the fixtures in one place.
    """

    def test_the_resting_subtitle_is_the_presence_line(self):
        # "What is happening now" replaces "what happened this week" on the
        # resting surface; the rollup is retrospective and moves to the tooltip.
        state = self._state(
            self._summary(),
            sessions=[
                self._session("w1", idle_minutes=0.25),
                self._session("w2", idle_minutes=0.75),
                self._session("q1", idle_minutes=10.0),
            ],
        )
        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["subtitle"], "2 working · 0 waiting")
        presence = state["presence"]
        self.assertTrue(presence["measurable"])
        self.assertEqual(
            (presence["working"], presence["waiting"], presence["quiet"]),
            (2, 0, 1),
        )

    def test_every_state_carries_the_presence_block(self):
        # The collapsed pill draws the waiting count in any state, so the block
        # lives in the base payload, not one branch.
        state = self._state(self._summary(), sessions=[self._session()], signals=self._signal())
        self.assertEqual(state["state"], "session_waiting")
        self.assertEqual(state["presence"]["waiting"], 1)
        self.assertEqual(state["badge"]["count"], 1)
        self.assertEqual(state["badge"]["tone"], "attention")

    def test_the_waiting_queue_is_preworded_capped_and_longest_first(self):
        sessions = [
            self._session("s-short", project="/repo/billing-service", idle_minutes=4.0),
            self._session("s-long", project="/repo/myapp", idle_minutes=17.0),
            self._session("s-mid", project="/repo/infra", idle_minutes=9.0),
            self._session("s-least", project="/repo/docs", idle_minutes=2.5),
        ]
        signals = {}
        for session_id, minutes in (("s-short", 3.0), ("s-long", 15.0), ("s-mid", 7.0), ("s-least", 1.0)):
            signals.update(self._signal(session_id, minutes_ago=minutes))
        state = self._state(self._summary(), sessions=sessions, signals=signals)
        queue = state["waiting_sessions"]
        # Capped at three rows -- the count lives in the subtitle instead.
        self.assertEqual(len(queue), 3)
        self.assertIn("4 runs", state["subtitle"])
        self.assertEqual([row["session_id"] for row in queue], ["s-long", "s-mid", "s-short"])
        first = queue[0]
        self.assertEqual(first["tool"], ui.tool_label("claude-code"))
        self.assertEqual(first["project"], "myapp")
        self.assertEqual(first["waited_label"], "15m")
        self.assertEqual(first["url"], "/?session=s-long")

    def test_passive_context_review_badge_matches_the_review_count(self):
        health = []
        for index in range(5):
            health.append({
                "session_id": f"s{index}",
                "project_full": f"/repo/project-{index}",
                "project": f"project-{index}",
                "tool": "codex-cli",
                "severity": "critical",
                "can_handoff": True,
                "estimated_replayed_context_tokens": 1000,
            })
        with patch.object(ui, "_foreground_matches_fresh_start_bubble", return_value=False):
            state = self._state(self._summary(context_health=health), sessions=[])

        self.assertEqual(state["state"], "context_review")
        self.assertEqual(state["label"], "Context review")
        self.assertEqual(state["primary_label"], "Review list")
        self.assertIn("5 projects", state["subtitle"])
        self.assertEqual(state["badge"]["count"], 5)
        self.assertEqual(state["badge"]["tone"], "info")
        self.assertEqual(len(state["waiting_sessions"]), 5)
        self.assertEqual(state["waiting_sessions"][0]["kind"], "context_review")
        self.assertEqual(state["waiting_sessions"][0]["project"], "project-0")
        self.assertEqual(state["waiting_sessions"][0]["severity_label"], "critical")
        self.assertEqual(state["waiting_sessions"][0]["impact_label"], "1.0k")
        self.assertEqual(state["skip_label"], "Later")
        self.assertEqual(len(state["skip_projects"]), 5)

    def test_context_review_rows_hide_unknown_zero_impact(self):
        rows = ui._context_review_companion_rows([{
            "session_id": "s0",
            "project_full": "/repo/project-0",
            "project": "project-0",
            "tool": "codex-cli",
            "severity": "critical",
            "can_handoff": True,
            "impact_label": 0,
            "latest_turn_tokens": 0,
        }])

        self.assertEqual(rows[0]["waited_label"], "")
        self.assertEqual(rows[0]["severity_label"], "critical")

    def test_context_review_rows_carry_activity_without_zero_noise(self):
        rows = ui._context_review_companion_rows([{
            "session_id": "s0",
            "project_full": "/repo/project-0",
            "project": "project-0",
            "tool": "codex-cli",
            "severity": "warning",
            "can_handoff": True,
            "estimated_replayed_context_label": 0,
            "session_status": "active",
            "session_status_label": "Active log",
        }])

        self.assertEqual(rows[0]["waited_label"], "")
        self.assertEqual(rows[0]["activity_label"], "active log")

    def test_context_review_can_be_disabled_for_companion(self):
        health = [
            {
                "session_id": "s0",
                "project_full": "/repo/project-0",
                "project": "project-0",
                "tool": "codex-cli",
                "severity": "critical",
                "can_handoff": True,
                "estimated_replayed_context_tokens": 1000,
            },
            {
                "session_id": "s1",
                "project_full": "/repo/project-1",
                "project": "project-1",
                "tool": "codex-cli",
                "severity": "warning",
                "can_handoff": True,
                "estimated_replayed_context_tokens": 1000,
            },
        ]
        with patch.object(ui, "_foreground_matches_fresh_start_bubble", return_value=True):
            state = self._state(
                self._summary(context_health=health),
                sessions=[],
                prefs={"fresh_start_context": False},
            )

        self.assertEqual(state["state"], "watching")

    def test_resting_state_with_no_badge_contract_shows_no_badge(self):
        state = self._state(self._summary(), sessions=[])
        self.assertEqual(state["state"], "watching")
        self.assertIsNone(state["badge"])

    def test_queue_rows_carry_the_wants_bucket(self):
        # Joined from the hook's waiting signal: the closed-vocabulary phrase,
        # or "" when the signal predates the field.
        signals = self._signal("sess-1")
        signals["sess-1"]["wants"] = "run Bash"
        state = self._state(self._summary(), sessions=[self._session("sess-1")], signals=signals)
        self.assertEqual(state["waiting_sessions"][0]["wants"], "run Bash")

        state = self._state(self._summary(), sessions=[self._session("sess-1")], signals=self._signal("sess-1"))
        self.assertEqual(state["waiting_sessions"][0]["wants"], "")

    def test_a_sub_minute_row_has_no_waited_label(self):
        # Under a minute the presence label is "waiting on you"; a row must
        # carry "" rather than the fragment "on you".
        state = self._state(
            self._summary(),
            sessions=[self._session(idle_minutes=0.5)],
            signals=self._signal(minutes_ago=0.3),
        )
        self.assertEqual(state["waiting_sessions"][0]["waited_label"], "")

    def test_no_snapshot_is_cannot_see_not_nothing_running(self):
        state = self._state(self._summary(), sessions=[])
        presence = state["presence"]
        self.assertFalse(presence["measurable"])
        self.assertTrue(presence["reason"])
        self.assertEqual(state["subtitle"], presence["line"])

    def test_analyst_spawns_do_not_count_as_the_users_work(self):
        rows = [
            ui.SessionPresence(
                session_id="own", tool="claude-code", state="working",
                label="working", measurable=True, idle_seconds=10.0,
            ),
            ui.SessionPresence(
                session_id="spawn", tool="claude-code", state="working",
                label="working", measurable=True, idle_seconds=5.0, analyst_run=True,
            ),
        ]
        self.assertEqual(ui._presence_block(rows)["working"], 1)

    def test_all_quiet_reads_as_quiet_not_as_nothing(self):
        state = self._state(self._summary(), sessions=[self._session("q", idle_minutes=12.0)])
        self.assertEqual(state["subtitle"], "1 quiet run")

    def test_the_prompt_gate_carries_its_countdown(self):
        expires = datetime.now(timezone.utc) + timedelta(seconds=90)
        state = self._state(
            self._summary(), gate={
                "id": "g1", "tool": "claude-code", "risk": "high",
                "url": "/?view=prompt", "expires_at": expires.isoformat(),
            },
        )
        self.assertEqual(state["state"], "prompt_gate")
        self.assertTrue(85 <= state["expires_in_seconds"] <= 90)

    def test_a_gate_without_expiry_shows_no_countdown(self):
        # None, not zero: "no deadline recorded" must not render as "expired".
        state = self._state(
            self._summary(),
            gate={"id": "g1", "tool": "claude-code", "risk": "high", "url": "/?view=prompt"},
        )
        self.assertIsNone(state["expires_in_seconds"])


class CompanionFinishedTests(WaitingSessionCompanionTests):
    """A working session that goes quiet is finished work awaiting review."""

    def setUp(self):
        ui._PRESENCE_LAST_STATES.clear()
        ui._FINISHED_NOTICES.clear()

    def test_presence_reads_the_transcripts_own_clock(self):
        # The session index refreshes on scan cadence -- minutes late for "is
        # it writing this second". A .jsonl transcript's mtime is the same
        # fact, fresh every poll; shared-file DB sources are excluded because
        # one session writing would mark all of them working.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            path.write_text("x", encoding="utf-8")
            stale = datetime.now(timezone.utc) - timedelta(minutes=10)
            row = LocalSession(
                session_id="s", tool="claude-code", project_path="/r",
                updated_at=stale, source_path=str(path),
            )
            fresh = ui._freshened_for_presence([row])[0]
            self.assertGreater(fresh.updated_at, stale)
            db_row = LocalSession(
                session_id="d", tool="codex", project_path="/r",
                updated_at=stale, source_path=str(Path(tmp) / "sessions.db"),
            )
            self.assertIs(ui._freshened_for_presence([db_row])[0], db_row)

    def test_a_working_to_quiet_transition_becomes_badged_by_default(self):
        state = self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=0.5)])
        self.assertEqual(state["state"], "watching")
        state = self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=3.0)])
        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["badge"]["count"], 1)
        self.assertEqual(state["badge"]["tone"], "info")
        self.assertEqual(len(state["finished_sessions"]), 1)

    def test_expanded_finished_preference_opens_a_grouped_review(self):
        self._state(
            self._summary(),
            sessions=[
                self._session("done-1", idle_minutes=0.5),
                self._session("done-2", idle_minutes=0.5),
            ],
        )
        state = self._state(
            self._summary(),
            sessions=[
                self._session("done-1", idle_minutes=3.0),
                self._session("done-2", idle_minutes=3.5),
            ],
            prefs={"finished_sessions": "expanded"},
        )

        self.assertEqual(state["state"], "session_finished")
        self.assertEqual(state["label"], "2 completed runs")
        self.assertEqual(state["primary_label"], "Review")
        self.assertEqual(state["primary_url"], "/?view=sessions")
        self.assertEqual(len(state["waiting_sessions"]), 2)
        self.assertEqual([row["kind"] for row in state["waiting_sessions"]], ["finished", "finished"])
        self.assertEqual(state["skip_label"], "Clear all")
        self.assertEqual(state["skip_state"], "session_finished_group")
        self.assertEqual(state["skip_session_ids"], ["done-1", "done-2"])

    def test_expanded_single_finished_session_keeps_direct_review(self):
        self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=0.5)])
        state = self._state(
            self._summary(),
            sessions=[self._session("done-1", idle_minutes=3.0)],
            prefs={"finished_sessions": "expanded"},
        )

        self.assertEqual(state["state"], "session_finished")
        self.assertEqual(state["label"], "Run completed")
        self.assertIn("done-1", state["primary_url"])
        self.assertEqual(state["skip_state"], "session_finished")

    def test_a_snapshot_alone_is_not_finished(self):
        # Statelessly, just-finished and long-abandoned look identical; only
        # the observed working -> quiet transition separates them.
        state = self._state(self._summary(), sessions=[self._session("q", idle_minutes=3.0)])
        self.assertEqual(state["state"], "watching")

    def test_live_work_outranks_the_finished_notice(self):
        # Field report: a finished headline owned the bar for its whole 15
        # minutes and hid the running session's meter and totals. While
        # anything is working, the resting layout wins and the completion
        # becomes a reward/status fragment instead of a collapsed badge.
        self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=0.25)])
        sessions = [
            self._session("done-1", idle_minutes=3.0),
            self._session("busy", idle_minutes=0.25),
        ]
        state = self._state(self._summary(), sessions=sessions)
        self.assertEqual(state["state"], "watching")
        self.assertIn("1 completed", state["subtitle"])
        self.assertEqual(len(state["finished_sessions"]), 1)

    def test_blocked_outranks_finished(self):
        self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=0.5)])
        sessions = [
            self._session("done-1", idle_minutes=3.0),
            self._session("blocked", idle_minutes=5.0),
        ]
        state = self._state(
            self._summary(), sessions=sessions, signals=self._signal("blocked", minutes_ago=4.0),
        )
        self.assertEqual(state["state"], "session_waiting")

    def test_the_notice_retires_when_the_session_works_again(self):
        self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=0.5)])
        self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=3.0)])
        state = self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=0.2)])
        self.assertEqual(state["state"], "watching")

    def test_the_notice_expires_as_stale_news(self):
        self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=0.5)])
        self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=3.0)])
        ui._FINISHED_NOTICES["done-1"] = time.time() - (ui.FINISHED_NOTICE_TTL_SECONDS + 60)
        state = self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=6.0)])
        self.assertEqual(state["state"], "watching")

    def test_a_skipped_finish_stays_quiet(self):
        self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=0.5)])
        with patch.object(
            ui, "companion_skip_active",
            side_effect=lambda key: key == "session_finished:done-1",
        ):
            state = self._state(self._summary(), sessions=[self._session("done-1", idle_minutes=3.0)])
        self.assertEqual(state["state"], "watching")


class CompanionAwayDigestTests(WaitingSessionCompanionTests):
    """The bar's first appearance after a real gap is a briefing, not a siren."""

    def setUp(self):
        ui._PRESENCE_LAST_STATES.clear()
        ui._FINISHED_NOTICES.clear()
        ui._LAST_COMPANION_POLL = None
        ui._AWAY_DIGEST = None

    def tearDown(self):
        ui._LAST_COMPANION_POLL = None
        ui._AWAY_DIGEST = None

    def _loop_record(self, minutes_ago=30.0):
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        return {
            "signal_kind": "loop", "updated_at": stamp, "severity": "warning",
            "session_id": "sig-1", "urls": {"dashboard": "/?session=sig-1"},
        }

    def test_a_gap_reconstructs_finished_work_and_missed_signals(self):
        ui._LAST_COMPANION_POLL = time.time() - 48 * 60
        finished = self._session("done-away", idle_minutes=25.0)
        before_gap = self._session("old", idle_minutes=120.0)
        with patch.object(ui, "recent_ambient_interventions", return_value=[self._loop_record()]):
            state = self._state(self._summary(), sessions=[finished, before_gap])
        self.assertEqual(state["state"], "away_digest")
        self.assertIn("1 finished", state["subtitle"])
        self.assertIn("1 signal", state["subtitle"])
        self.assertIn("gap 48m", state["subtitle"])
        self.assertEqual([row["kind"] for row in state["digest_rows"]], ["finished", "loop"])
        self.assertEqual(state["skip_state"], "away_digest")
        # The digest is those sessions' announcement; the finish notice must
        # not re-announce them after dismissal.
        self.assertNotIn("done-away", ui._FINISHED_NOTICES)

    def test_the_digest_persists_until_dismissed(self):
        ui._LAST_COMPANION_POLL = time.time() - 48 * 60
        finished = self._session("done-away", idle_minutes=25.0)
        self._state(self._summary(), sessions=[finished])
        state = self._state(self._summary(), sessions=[finished])
        self.assertEqual(state["state"], "away_digest")
        ui._dismiss_away_digest()
        state = self._state(self._summary(), sessions=[finished])
        self.assertEqual(state["state"], "watching")

    def test_a_short_gap_is_not_an_absence(self):
        ui._LAST_COMPANION_POLL = time.time() - 5 * 60
        state = self._state(self._summary(), sessions=[self._session("done-away", idle_minutes=3.0)])
        self.assertNotEqual(state["state"], "away_digest")

    def test_an_uneventful_gap_produces_no_digest(self):
        ui._LAST_COMPANION_POLL = time.time() - 48 * 60
        state = self._state(self._summary(), sessions=[])
        self.assertEqual(state["state"], "watching")

    def test_blocked_outranks_the_digest(self):
        ui._LAST_COMPANION_POLL = time.time() - 48 * 60
        sessions = [
            self._session("done-away", idle_minutes=25.0),
            self._session("blocked", idle_minutes=5.0),
        ]
        state = self._state(
            self._summary(), sessions=sessions, signals=self._signal("blocked", minutes_ago=4.0),
        )
        self.assertEqual(state["state"], "session_waiting")

    def test_the_digest_expires_as_stale_news(self):
        ui._LAST_COMPANION_POLL = time.time() - 48 * 60
        finished = self._session("done-away", idle_minutes=25.0)
        self._state(self._summary(), sessions=[finished])
        ui._AWAY_DIGEST["created"] = time.time() - (ui.AWAY_DIGEST_TTL_SECONDS + 60)
        state = self._state(self._summary(), sessions=[finished])
        self.assertEqual(state["state"], "watching")


class CompanionPressureAndSignalTests(WaitingSessionCompanionTests):
    """The meter and the missed-nudge chip: fresh numbers or honest absence."""

    def _working_session(self, session_id="w1", *, source_path="/tmp/w1.jsonl", notes=()):
        return LocalSession(
            session_id=session_id,
            tool="claude-code",
            project_path="/repo/aiwatcher-local",
            raw_cwd="/repo/aiwatcher-local",
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=20),
            source_path=source_path,
            notes=list(notes),
        )

    def test_pressure_reads_the_working_sessions_latest_turn(self):
        ui._PRESSURE_TRANSCRIPT_CACHE.clear()
        with patch.object(ui.statusline, "read_transcript", return_value={
            "available": True, "latest_context": 158_000, "peak_context": 158_000,
            "model": "claude-sonnet-5",
        }) as read:
            state = self._state(self._summary(), sessions=[self._working_session()])
        pressure = state["pressure"]
        self.assertTrue(pressure["available"])
        self.assertEqual(pressure["latest_turn_tokens"], 158_000)
        # 158K of Sonnet 5's 1M window. Under the old fixed 200K limit this
        # same turn read 79% and amber; a percent of the wrong model's window
        # was the defect, so the number and the colour both change here.
        self.assertEqual(pressure["severity"], "ok")
        self.assertEqual(pressure["pct_of_turn_limit"], 16)
        self.assertEqual(pressure["context_window"], 1_000_000)
        read.assert_called_once()

    def test_a_codex_session_gets_a_meter_against_its_400k_window(self):
        # The Slack-thread case: 211K on Codex read "past a 200K limit". Read
        # through the rollout reader against the real window it is 53%, ok.
        ui._PRESSURE_TRANSCRIPT_CACHE.clear()
        codex = self._working_session()
        codex.tool = "codex-cli"
        codex.model = "gpt-5-codex"
        with patch.object(ui.compaction, "codex_boundary_stats", return_value={
            "available": True, "latest_context": 211_400, "peak_context": 211_400, "model": "gpt-5-codex",
        }) as read:
            state = self._state(self._summary(), sessions=[codex])
        pressure = state["pressure"]
        self.assertTrue(pressure["available"])
        self.assertEqual(pressure["context_window"], 400_000)
        self.assertEqual(pressure["pct_of_turn_limit"], 53)
        self.assertEqual(pressure["severity"], "ok")
        read.assert_called_once()

    def test_the_meter_is_the_models_own_window_not_a_constant(self):
        ui._PRESSURE_TRANSCRIPT_CACHE.clear()
        with patch.object(ui.statusline, "read_transcript", return_value={
            "available": True, "latest_context": 158_000, "peak_context": 158_000,
            "model": "claude-haiku-4-5",
        }):
            state = self._state(self._summary(), sessions=[self._working_session()])
        pressure = state["pressure"]
        self.assertEqual(pressure["pct_of_turn_limit"], 79)
        self.assertEqual(pressure["severity"], "ok")

    def test_no_meter_when_the_window_is_unknown(self):
        # An unrecognised model, or a turn bigger than the table says the
        # model accepts: either way nobody knows the window, and a percent of
        # an unknown is not drawn as a meter.
        for stats in (
            {"available": True, "latest_context": 158_000, "peak_context": 158_000, "model": "model-nobody-knows"},
            {"available": True, "latest_context": 250_000, "peak_context": 250_000, "model": "claude-haiku-4-5"},
        ):
            with self.subTest(model=stats["model"]):
                ui._PRESSURE_TRANSCRIPT_CACHE.clear()
                with patch.object(ui.statusline, "read_transcript", return_value=stats):
                    state = self._state(self._summary(), sessions=[self._working_session()])
                pressure = state["pressure"]
                self.assertFalse(pressure["available"])
                self.assertIn("window unknown", pressure["reason"])

    # --- compact at the boundary -------------------------------------------

    SHA = "c" * 40

    def _assessment(self, **overrides):
        base = dict(
            session_id="w1", tool="claude-code", model="claude-sonnet-5", sha=self.SHA,
            subject="fix: thing", committed_at="2026-09-09T10:00:00+00:00",
            turns_since_commit=9, prompts_since_commit=3, latest_turn_tokens=431_000,
            context_at_commit=412_000, first_turn_tokens=58_000, dead_tokens=354_000,
            since_tokens=19_000, after_estimate=77_000, files_since=["a.py"],
            command="/compact Keep everything since commit ccccccc", priced=False,
            dead_usd_per_turn=None, recommend=True, reason="",
        )
        base.update(overrides)
        return compaction.Assessment(**base)

    def _compact_state(self, assessment, session=None):
        # Local state is shared across this module's tests, and a nudge is
        # keyed on (session, sha): each test uses its own sha so one test's
        # receipt or Later cannot leak into the next.
        ui._COMPACT_CACHE.clear()
        boundary = compaction.Boundary(sha=assessment.sha, subject="fix: thing", committed_at=datetime.now(timezone.utc))
        with (
            patch.object(ui.compaction, "head_commit", return_value=boundary),
            patch.object(ui.compaction, "assess", return_value=assessment),
        ):
            return self._state(self._summary(), sessions=[session or self._working_session()])

    def test_the_nudge_stays_while_the_session_is_quiet_and_goes_with_it(self):
        # /compact is typed once the model has stopped. The first rule hid
        # the nudge sixty seconds after the session's last write, so it was
        # on the bar while the user could not act and gone once they could.
        # Quiet keeps it; gone drops it.
        quiet = self._working_session()
        quiet.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        state = self._compact_state(self._assessment(sha="q" * 40), session=quiet)
        self.assertEqual(state["state"], "compact_recommended")
        self.assertEqual(state["label"], "Compact now")
        self.assertEqual(state["badge"], {"count": 1, "tone": "info"})
        gone = self._working_session()
        gone.updated_at = datetime.now(timezone.utc) - timedelta(minutes=40)
        state = self._compact_state(self._assessment(sha="g" * 40), session=gone)
        self.assertNotEqual(state["state"], "compact_recommended")
        self.assertIsNone(state["compact"])
        self.assertIsNone(compact_nudge("w1", "g" * 40))

    def test_a_commit_with_dead_history_puts_compact_on_the_bar(self):
        state = self._compact_state(self._assessment())
        self.assertEqual(state["state"], "compact_recommended")
        self.assertEqual(state["primary_action"], "copy_compact")
        # Two words for the title and one for the button: the bar caps them
        # at 18 and 12 characters, and the first version came out as
        # "Compact before the" and "Copy /compac".
        self.assertEqual(state["primary_label"], "Copy")
        self.assertEqual(state["label"], "Compact now")
        self.assertLessEqual(len(state["label"]), 18)
        self.assertLessEqual(len(state["subtitle"]), 46)
        self.assertTrue(state["compact_command"].startswith("/compact"))
        self.assertEqual(state["compact_sha"], self.SHA)
        # The subtitle names the window first -- with two open on one
        # project a hash does not say where to paste -- then the figures.
        # No session title here, so the name is tool and project. The
        # boundary fact moves to the tooltip.
        self.assertIn("Claude · aiwatcher-local · 354.0k of 431.0k", state["subtitle"])
        self.assertIn("Committed 3 turns ago", state["detail"])
        self.assertIn("is finished work", state["detail"])
        self.assertEqual(state["compact_stage"], "nudge")
        self.assertEqual(state["skip_state"], "compact_recommended")
        self.assertEqual(state["skip_session_ids"], ["w1"])
        # The collapsed bubble shows nothing but a count, so a nudge with
        # no badge waited invisibly until the bar happened to be open.
        self.assertEqual(state["badge"], {"count": 1, "tone": "info"})
        # The receipt is opened the first time the nudge shows.
        record = compact_nudge("w1", self.SHA)
        assert record is not None
        self.assertEqual(record["dead_tokens"], 354_000)
        self.assertIsNone(record["decision"])

    def test_a_commit_with_nothing_to_shed_leaves_the_bar_quiet(self):
        sha = "d" * 40
        state = self._compact_state(self._assessment(sha=sha, recommend=False, reason="Nothing has happened since the commit yet."))
        self.assertEqual(state["state"], "watching")
        self.assertIsNone(state["compact"])
        self.assertIsNone(compact_nudge("w1", sha))

    def test_later_hides_this_commits_nudge(self):
        sha = "e" * 40
        record_companion_skip(key=f"compact:w1:{sha}", reason="deferred", minutes=60)
        state = self._compact_state(self._assessment(sha=sha))
        self.assertEqual(state["state"], "watching")
        self.assertIsNone(state["compact"])

    def test_the_bar_says_this_turn_when_the_commit_just_landed(self):
        state = self._compact_state(self._assessment(sha="f" * 40, prompts_since_commit=0))
        self.assertIn("Committed this turn", state["detail"])

    # --- after the nudge: each step on a line the tool wrote ----------------

    def test_a_named_session_is_named_on_the_bar(self):
        state = self._compact_state(self._assessment(sha="1" * 40, title="Context health calibration"))
        self.assertEqual(state["subtitle"], "Context health calibration · 354.0k of 431.0k")
        self.assertIn("in Context health calibration", state["detail"])

    def test_copy_holds_the_bar_on_that_session_until_the_log_moves(self):
        sha = "2" * 40
        self._compact_state(self._assessment(sha=sha, title="Context health calibration"))
        update_compact_nudge("w1", sha, decision="copied", action_channel="companion")
        state = self._compact_state(self._assessment(sha=sha, title="Context health calibration"))
        self.assertEqual(state["state"], "compact_recommended")
        self.assertEqual(state["compact_stage"], "copied")
        self.assertEqual(state["label"], "Copied")
        self.assertEqual(state["subtitle"], "Paste into Context health calibration")
        # No button and no Later: the click is made, the log has not moved.
        self.assertEqual(state["primary_action"], "none")
        self.assertEqual(state["skip_state"], "")
        # Still something to do (paste it), so the bubble keeps its count.
        self.assertEqual(state["badge"], {"count": 1, "tone": "info"})

    def test_the_typed_command_moves_the_bar_to_compacting(self):
        sha = "3" * 40
        self._compact_state(self._assessment(sha=sha))
        state = self._compact_state(self._assessment(
            sha=sha, stage="compacting", command_seen_at="2026-09-09T12:14:31+00:00",
            reason="/compact was typed at 12:14; the tool has not finished yet.",
        ))
        self.assertEqual(state["compact_stage"], "compacting")
        self.assertEqual(state["label"], "Compacting…")
        self.assertIn("/compact seen", state["subtitle"])
        self.assertEqual(state["primary_action"], "none")
        # The receipt records the step, and a typed command with no click is
        # a decision of its own.
        record = compact_nudge("w1", sha)
        assert record is not None
        self.assertEqual(record["command_seen_at"], "2026-09-09T12:14:31+00:00")
        self.assertEqual(record["decision"], "typed")

    def test_the_boundary_row_moves_the_bar_to_compacted(self):
        sha = "4" * 40
        self._compact_state(self._assessment(sha=sha))
        state = self._compact_state(self._assessment(
            sha=sha, stage="compacted", boundary_seen_at="2026-09-09T12:15:31+00:00",
            reason="Compacted at 12:15; the next reply will show the new size.",
        ))
        self.assertEqual(state["label"], "Compacted")
        self.assertEqual(state["subtitle"], "Confirming the size on the next reply")
        self.assertEqual(compact_nudge("w1", sha)["boundary_seen_at"], "2026-09-09T12:15:31+00:00")
        # Nothing left to do, but the bar is collapsed most of the time and
        # a step with no count is a blank bubble: the count stays on.
        self.assertEqual(state["badge"], {"count": 1, "tone": "info"})

    def test_the_first_small_reply_confirms_with_the_real_number(self):
        sha = "5" * 40
        self._compact_state(self._assessment(sha=sha))
        # The step holds while the tool carries on working, so by the time a
        # person looks the latest reply is bigger than the shed: the bar
        # shows the shed's number and the receipt closes on it.
        state = self._compact_state(self._assessment(
            sha=sha, stage="confirmed", recommend=False, latest_turn_tokens=85_000,
            context_before_shed=592_810, context_after_shed=80_384,
            reason="The context already shed since that commit.",
        ))
        self.assertEqual(state["state"], "compact_recommended")
        self.assertEqual(state["compact_stage"], "confirmed")
        self.assertEqual(state["label"], "Compacted")
        self.assertIn("592.8k → 80.4k per reply", state["subtitle"])
        self.assertIn("estimate was 77.0k", state["subtitle"])
        self.assertEqual(state["badge"], {"count": 1, "tone": "info"})
        self.assertEqual(compact_nudge("w1", sha)["after_actual"], 80_384)

    def test_a_compaction_nobody_asked_for_is_not_announced(self):
        # The tool's own auto-compact writes the same boundary; with no
        # receipt and no typed command it is not this feature's to claim.
        state = self._compact_state(self._assessment(
            sha="6" * 40, stage="confirmed", recommend=False, latest_turn_tokens=58_000,
            context_before_shed=998_000, reason="The context already shed since that commit.",
        ))
        self.assertEqual(state["state"], "watching")
        self.assertIsNone(state["compact"])

    def test_two_windows_get_a_row_each(self):
        sha = "7" * 40
        ui._COMPACT_CACHE.clear()
        first = self._assessment(sha=sha, session_id="w1", title="AIWatcher efficacy feature scope", dead_tokens=594_926, latest_turn_tokens=646_536, after_estimate=51_610)
        second = self._assessment(sha=sha, session_id="w2", title="Context health calibration", dead_tokens=448_621, latest_turn_tokens=592_810, after_estimate=71_428)
        boundary = compaction.Boundary(sha=sha, subject="fix: thing", committed_at=datetime.now(timezone.utc))
        by_id = {"w1": first, "w2": second}
        with (
            patch.object(ui.compaction, "head_commit", return_value=boundary),
            patch.object(ui.compaction, "assess", side_effect=lambda session: by_id[session.session_id]),
        ):
            state = self._state(self._summary(), sessions=[
                self._working_session("w1", source_path="/tmp/w1.jsonl"),
                self._working_session("w2", source_path="/tmp/w2.jsonl"),
            ])
        self.assertEqual(state["state"], "compact_recommended")
        self.assertEqual(state["label"], "Compact 2 sessions")
        self.assertLessEqual(len(state["label"]), 18)
        self.assertEqual(state["subtitle"], "2 to compact")
        # No single button: each row carries its own, with its own command.
        self.assertEqual(state["primary_action"], "none")
        rows = state["compact_rows"]
        self.assertEqual([row["session_id"] for row in rows], ["w1", "w2"])   # most to shed first
        self.assertEqual(rows[0]["text"], "AIWatcher efficacy feature scope · 594.9k of 646.5k")
        self.assertEqual(rows[0]["tag"], "→ ~51.6k")
        self.assertEqual(rows[0]["action"], "copy_compact")
        self.assertTrue(rows[1]["command"].startswith("/compact"))
        self.assertEqual(state["skip_session_ids"], ["w1", "w2"])

    def test_only_the_clicked_row_changes(self):
        # Today's case: Copy on one window, paste into the other. The clicked
        # row is marked copied; the pasted-into row moves on the log alone.
        sha = "8" * 40
        ui._COMPACT_CACHE.clear()
        boundary = compaction.Boundary(sha=sha, subject="fix: thing", committed_at=datetime.now(timezone.utc))
        by_id = {
            "w1": self._assessment(sha=sha, session_id="w1", title="AIWatcher efficacy feature scope", dead_tokens=594_926, latest_turn_tokens=646_536),
            "w2": self._assessment(sha=sha, session_id="w2", title="Context health calibration"),
        }
        sessions = [self._working_session("w1", source_path="/tmp/w1.jsonl"), self._working_session("w2", source_path="/tmp/w2.jsonl")]
        with (
            patch.object(ui.compaction, "head_commit", return_value=boundary),
            patch.object(ui.compaction, "assess", side_effect=lambda session: by_id[session.session_id]),
        ):
            self._state(self._summary(), sessions=sessions)
            update_compact_nudge("w1", sha, decision="copied", action_channel="companion")
            by_id["w2"] = self._assessment(
                sha=sha, session_id="w2", title="Context health calibration", stage="compacting",
                command_seen_at="2026-09-09T12:14:31+00:00", reason="/compact was typed at 12:14; the tool has not finished yet.",
            )
            ui._COMPACT_CACHE.clear()
            state = self._state(self._summary(), sessions=sessions)
        rows = {row["session_id"]: row for row in state["compact_rows"]}
        self.assertEqual(rows["w1"]["tag"], "copied")
        self.assertEqual(rows["w1"]["action"], "copy_compact")
        self.assertEqual(rows["w2"]["tag"], "compacting…")
        self.assertEqual(rows["w2"]["action"], "")
        self.assertEqual(state["subtitle"], "1 copied · 1 compacting")
        self.assertEqual(state["skip_state"], "")

    def test_the_meter_carries_the_sessions_running_totals(self):
        # Absolute anchors for the percent: API-equivalent cost and total
        # tokens, straight off the session row. A raw total, so it ships as a
        # plain label with no severity attached.
        ui._PRESSURE_TRANSCRIPT_CACHE.clear()
        session = LocalSession(
            session_id="w1",
            tool="claude-code",
            project_path="/repo/aiwatcher-local",
            raw_cwd="/repo/aiwatcher-local",
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=20),
            source_path="/tmp/w-stats.jsonl",
            cost_usd=12.34,
            tokens_in=4_000_000,
            tokens_out=1_000_000,
        )
        with patch.object(ui.statusline, "read_transcript", return_value={
            "available": True, "latest_context": 42_000, "peak_context": 42_000,
            "model": "claude-sonnet-5",
        }):
            state = self._state(self._summary(), sessions=[session])
        pressure = state["pressure"]
        self.assertEqual(pressure["stats_label"], f"$12.34 · {ui.compact_int(5_000_000)}")
        self.assertIn("API-equivalent", pressure["stats_detail"])

    def test_pressure_is_cached_on_the_sessions_write_stamp(self):
        # A transcript only changes when the session writes, and writing moves
        # updated_at -- so two polls between writes must not parse it twice.
        ui._PRESSURE_TRANSCRIPT_CACHE.clear()
        session = self._working_session(source_path="/tmp/w-cache.jsonl")
        with patch.object(ui.statusline, "read_transcript", return_value={
            "available": True, "latest_context": 42_000,
        }) as read:
            self._state(self._summary(), sessions=[session])
            self._state(self._summary(), sessions=[session])
        read.assert_called_once()

    def test_no_working_session_means_no_meter_not_a_zero(self):
        state = self._state(self._summary(), sessions=[self._session("q", idle_minutes=12.0)])
        self.assertFalse(state["pressure"]["available"])
        self.assertTrue(state["pressure"]["reason"])

    def test_cumulative_total_sources_refuse_the_per_turn_label(self):
        # Instance 1 and 2 of the recurring defect: a cumulative number under a
        # per-turn label. The Codex-DB path reports running totals, so the
        # meter must decline rather than divide the wrong number.
        ui._PRESSURE_TRANSCRIPT_CACHE.clear()
        session = self._working_session(notes=["cumulative totals from thread"])
        with patch.object(ui.statusline, "read_transcript") as read:
            state = self._state(self._summary(), sessions=[session])
        self.assertFalse(state["pressure"]["available"])
        self.assertIn("cumulative", state["pressure"]["reason"])
        read.assert_not_called()

    def _signal_record(self, kind="loop", *, minutes_ago=8.0, severity="warning"):
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        return {
            "signal_kind": kind, "updated_at": stamp, "severity": severity,
            "session_id": "sess-loop", "urls": {"dashboard": "/?session=sess-loop"},
        }

    def test_a_recent_overlay_only_signal_reaches_the_bar_as_a_chip(self):
        with patch.object(ui, "recent_ambient_interventions", return_value=[self._signal_record()]):
            state = self._state(self._summary(), sessions=[])
        chip = state["recent_signal"]
        self.assertEqual(chip["kind"], "loop")
        self.assertEqual(chip["chip"], "loop 8m")
        self.assertEqual(chip["url"], "/?session=sess-loop")

    def test_a_reachable_single_session_gets_return_as_primary(self):
        state = self._state(
            self._summary(), sessions=[self._session("sess-1")],
            signals=self._signal("sess-1"), return_available=True,
        )
        self.assertEqual(state["primary_label"], "Return")
        self.assertEqual(state["primary_action"], "runtime_return")
        # The dashboard stays one failure away: primary_url is the fallback
        # the widgets open when the return reports it did not happen.
        self.assertIn("sess-1", state["primary_url"])
        self.assertTrue(state["waiting_sessions"][0]["return_available"])

    def test_an_unreachable_session_keeps_open_session(self):
        state = self._state(
            self._summary(), sessions=[self._session("sess-1")],
            signals=self._signal("sess-1"), return_available=False,
        )
        self.assertEqual(state["primary_label"], "Open session")
        self.assertEqual(state["primary_action"], "open_url")
        self.assertFalse(state["waiting_sessions"][0]["return_available"])

    def test_return_availability_is_the_endpoints_own_gate(self):
        # The row must never promise a jump /api/runtime-return would refuse,
        # so the helper reads the same attachment.available the endpoint does.
        ui._RUNTIME_PROCESS_CACHE = None
        session = self._session("sess-1")
        # The app tier stays offered on purpose -- the owner's call was that
        # Return should bring the desktop app forward even when it is already
        # frontmost, rather than detour through the dashboard.
        for available, level in ((True, "workspace"), (False, "unavailable"), (True, "app")):
            with self.subTest(available=available, level=level), (
                patch.object(ui, "safe_runtime_processes", return_value=[])
            ), patch.object(
                ui, "runtime_attachment_for_session",
                return_value=SimpleNamespace(available=available, level=level, app_name="Claude"),
            ):
                self.assertEqual(
                    ui._waiting_row_return_available("sess-1", [session]), available,
                )
        self.assertFalse(ui._waiting_row_return_available("missing", [session]))

    def test_a_blocked_prompt_reaches_the_bar_as_a_chip(self):
        # A blocked prompt is never shown by AIWatcher itself -- only inline
        # in the tool's own chat -- so this chip is the only place the
        # companion bar can surface it at all.
        with patch.object(
            ui, "recent_ambient_interventions",
            return_value=[self._signal_record("prompt_blocked", severity="critical")],
        ):
            state = self._state(self._summary(), sessions=[])
        chip = state["recent_signal"]
        self.assertEqual(chip["kind"], "prompt_blocked")
        self.assertEqual(chip["label"], "Prompt blocked")
        self.assertEqual(chip["severity"], "critical")

    def test_stale_and_bar_native_signals_produce_no_chip(self):
        # Older than the live window: the session is presumed gone, and a chip
        # would be an alarm about nothing actionable. Bar-native kinds already
        # have their own states and must not double-report.
        records = [
            self._signal_record("session_blocked", minutes_ago=1.0),
            self._signal_record("loop", minutes_ago=45.0),
        ]
        with patch.object(ui, "recent_ambient_interventions", return_value=records):
            state = self._state(self._summary(), sessions=[])
        self.assertIsNone(state["recent_signal"])


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
                self.assertIn("session_waiting", block[:600])
