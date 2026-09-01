from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from datetime import datetime, timedelta, timezone

from aiwatcher_cli import companion, ui
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

    def _state(self, summary, *, sessions=(), signals=None, gate=None, return_available=False):
        with (
            patch.object(ui, "build_summary_cached", return_value=summary),
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
        self.assertIn("2 sessions", state["subtitle"])
        self.assertIn("15m", state["subtitle"])

    def test_nothing_waiting_leaves_the_companion_alone(self):
        state = self._state(self._summary(), sessions=[self._session("busy", idle_minutes=0.1)], signals={})
        self.assertNotEqual(state["state"], "session_waiting")

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
                self._session("w1", idle_minutes=0.5),
                self._session("w2", idle_minutes=1.0),
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
        self.assertIn("4 sessions", state["subtitle"])
        self.assertEqual([row["session_id"] for row in queue], ["s-long", "s-mid", "s-short"])
        first = queue[0]
        self.assertEqual(first["tool"], ui.tool_label("claude-code"))
        self.assertEqual(first["project"], "myapp")
        self.assertEqual(first["waited_label"], "15m")
        self.assertEqual(first["url"], "/?session=s-long")

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
        self.assertEqual(state["subtitle"], "1 quiet session")

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
            "available": True, "latest_context": 158_000,
        }) as read:
            state = self._state(self._summary(), sessions=[self._working_session()])
        pressure = state["pressure"]
        self.assertTrue(pressure["available"])
        self.assertEqual(pressure["latest_turn_tokens"], 158_000)
        self.assertEqual(pressure["severity"], "warning")
        self.assertEqual(pressure["pct_of_turn_limit"], 79)
        read.assert_called_once()

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
                self.assertIn("session_waiting", block[:400])
