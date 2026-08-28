from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from aiwatcher_cli.scanner import LocalSession
from aiwatcher_cli.session_presence import (
    LIVE_WINDOW_MINUTES,
    WORKING_SECONDS,
    live_presence,
    presence_by_tool,
    presence_for_session,
    presence_for_sessions,
)


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def session(
    session_id: str = "s1",
    tool: str = "claude-code",
    *,
    seconds_ago: float | None = 10,
    started_ago: float | None = None,
    project_path: str | None = "/repo",
    raw_cwd: str | None = None,
) -> LocalSession:
    return LocalSession(
        session_id=session_id,
        tool=tool,
        project_path=project_path,
        raw_cwd=raw_cwd,
        updated_at=NOW - timedelta(seconds=seconds_ago) if seconds_ago is not None else None,
        started_at=NOW - timedelta(seconds=started_ago) if started_ago is not None else None,
    )


class StateTests(unittest.TestCase):
    def test_recent_write_is_working(self) -> None:
        presence = presence_for_session(session(seconds_ago=15), now=NOW)
        self.assertEqual(presence.state, "working")
        self.assertEqual(presence.label, "working")
        self.assertTrue(presence.measurable)
        self.assertIsNone(presence.reason)
        self.assertTrue(presence.live)

    def test_working_boundary_is_inclusive(self) -> None:
        self.assertEqual(
            presence_for_session(session(seconds_ago=WORKING_SECONDS), now=NOW).state,
            "working",
        )
        self.assertEqual(
            presence_for_session(session(seconds_ago=WORKING_SECONDS + 1), now=NOW).state,
            "quiet",
        )

    def test_quiet_carries_a_coarse_duration(self) -> None:
        presence = presence_for_session(session(seconds_ago=5 * 60), now=NOW)
        self.assertEqual(presence.state, "quiet")
        self.assertEqual(presence.label, "quiet 5m")
        self.assertTrue(presence.live)

    def test_quiet_label_never_ticks_in_seconds(self) -> None:
        # 4m12s and 4m48s must read the same, or the surface animates.
        first = presence_for_session(session(seconds_ago=4 * 60 + 12), now=NOW)
        second = presence_for_session(session(seconds_ago=4 * 60 + 8), now=NOW)
        self.assertEqual(first.label, second.label)
        self.assertNotIn("s", first.label.replace("quiet", ""))

    def test_past_the_live_window_is_gone(self) -> None:
        presence = presence_for_session(
            session(seconds_ago=LIVE_WINDOW_MINUTES * 60 + 1), now=NOW
        )
        self.assertEqual(presence.state, "gone")
        self.assertFalse(presence.live)

    def test_live_window_boundary_is_still_quiet(self) -> None:
        presence = presence_for_session(
            session(seconds_ago=LIVE_WINDOW_MINUTES * 60), now=NOW
        )
        self.assertEqual(presence.state, "quiet")

    def test_started_at_is_used_when_there_is_no_update(self) -> None:
        presence = presence_for_session(
            session(seconds_ago=None, started_ago=30), now=NOW
        )
        self.assertEqual(presence.state, "working")

    def test_future_timestamp_does_not_produce_negative_idle(self) -> None:
        # Clock skew, or a machine waking from sleep. Not evidence of anything,
        # and a negative idle would sort ahead of every genuinely live session.
        presence = presence_for_session(session(seconds_ago=-90), now=NOW)
        self.assertEqual(presence.idle_seconds, 0.0)
        self.assertEqual(presence.state, "working")

    def test_naive_timestamps_are_read_as_utc(self) -> None:
        row = LocalSession(
            session_id="naive",
            tool="claude-code",
            updated_at=(NOW - timedelta(seconds=20)).replace(tzinfo=None),
        )
        self.assertEqual(presence_for_session(row, now=NOW).state, "working")


class UnmeasurableTests(unittest.TestCase):
    def test_missing_timestamp_is_unmeasurable_with_a_reason(self) -> None:
        presence = presence_for_session(session(seconds_ago=None), now=NOW)
        self.assertEqual(presence.state, "unmeasurable")
        self.assertFalse(presence.measurable)
        self.assertTrue(presence.reason)
        self.assertFalse(presence.live)

    def test_unmeasurable_is_not_counted_as_idle_or_live(self) -> None:
        presence = presence_for_session(session(seconds_ago=None), now=NOW)
        self.assertIsNone(presence.idle_seconds)
        self.assertNotIn("quiet", presence.label)

    def test_cursor_is_refused_even_though_it_has_a_timestamp(self) -> None:
        # The stamp is real; it just answers "did the editor write a log",
        # which is a different question from "is this chat working".
        presence = presence_for_session(session(tool="cursor", seconds_ago=5), now=NOW)
        self.assertEqual(presence.state, "unmeasurable")
        self.assertIn("mtime", presence.reason or "")

    def test_a_tool_with_nothing_readable_reports_why_not_zero(self) -> None:
        rows = presence_for_sessions([session(tool="cursor", seconds_ago=5)], now=NOW)
        tools = presence_by_tool(rows)
        self.assertEqual(len(tools), 1)
        self.assertFalse(tools[0]["measurable"])
        self.assertTrue(tools[0]["reason"])
        self.assertEqual(tools[0]["live"], 0)

    def test_one_readable_session_makes_the_tool_measurable(self) -> None:
        rows = presence_for_sessions(
            [session("a", seconds_ago=None), session("b", seconds_ago=10)], now=NOW
        )
        tools = presence_by_tool(rows)
        self.assertTrue(tools[0]["measurable"])
        self.assertIsNone(tools[0]["reason"])
        self.assertEqual(tools[0]["live"], 1)


class AggregateTests(unittest.TestCase):
    def test_counts_split_by_tool(self) -> None:
        payload = live_presence(
            [
                session("a", "claude-code", seconds_ago=10),
                session("b", "claude-code", seconds_ago=15),
                session("c", "claude-code", seconds_ago=8 * 60),
                session("d", "codex-cli", seconds_ago=30),
                session("e", "codex-cli", seconds_ago=3 * 3600),
            ],
            now=NOW,
        )
        self.assertEqual(payload["working"], 3)
        self.assertEqual(payload["quiet"], 1)
        self.assertEqual(payload["live"], 4)
        by_tool = {row["tool"]: row for row in payload["tools"]}
        self.assertEqual(by_tool["claude-code"]["working"], 2)
        self.assertEqual(by_tool["claude-code"]["quiet"], 1)
        self.assertEqual(by_tool["codex-cli"]["live"], 1)

    def test_ended_sessions_are_left_out_of_the_payload(self) -> None:
        payload = live_presence([session("old", seconds_ago=6 * 3600)], now=NOW)
        self.assertEqual(payload["sessions"], [])
        self.assertEqual(payload["live"], 0)

    def test_payload_states_its_scope(self) -> None:
        # Nothing here can see another machine or a cloud session, so callers
        # must never render these counts as a total.
        self.assertEqual(live_presence([], now=NOW)["scope"], "this machine")

    def test_working_sorts_ahead_of_quiet(self) -> None:
        rows = presence_for_sessions(
            [session("quiet", seconds_ago=10 * 60), session("busy", seconds_ago=5)],
            now=NOW,
        )
        self.assertEqual([row.session_id for row in rows], ["busy", "quiet"])

    def test_analyst_runs_are_counted_but_marked(self) -> None:
        # AIWatcher's own Second Opinion spawn. Reported rather than hidden --
        # but it must not pass as one of the user's own live sessions.
        rows = presence_for_sessions(
            [
                session("mine", seconds_ago=10),
                session("analyst", seconds_ago=10, raw_cwd="/repo/.aiwatcher/analyst"),
            ],
            now=NOW,
        )
        flagged = [row for row in rows if row.analyst_run]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].session_id, "analyst")
        payload = live_presence(
            [
                session("mine", seconds_ago=10),
                session("analyst", seconds_ago=10, raw_cwd="/repo/.aiwatcher/analyst"),
            ],
            now=NOW,
        )
        self.assertEqual(payload["live"], 2)
        self.assertEqual(payload["analyst_runs"], 1)

    def test_json_round_trip_keeps_the_reason(self) -> None:
        row = presence_for_session(session(tool="cursor", seconds_ago=5), now=NOW).to_json()
        self.assertEqual(row["state"], "unmeasurable")
        self.assertFalse(row["measurable"])
        self.assertTrue(row["reason"])
        self.assertIsNone(row["idle_seconds"])
        self.assertFalse(row["live"])


class SharedBoundaryTests(unittest.TestCase):
    def test_live_window_is_the_same_number_the_dashboard_uses(self) -> None:
        # Two spellings of "live" would eventually disagree on screen.
        from aiwatcher_cli import ui

        self.assertEqual(ui.ACTIVE_SESSION_MINUTES, LIVE_WINDOW_MINUTES)

    def test_working_and_quiet_nest_inside_the_active_window(self) -> None:
        from aiwatcher_cli.ui import session_state

        row = session(seconds_ago=5 * 60)
        self.assertEqual(session_state(row, now=NOW)["status"], "active")
        self.assertEqual(presence_for_session(row, now=NOW).state, "quiet")


if __name__ == "__main__":
    unittest.main()


class WorkingTreeCollisionTests(unittest.TestCase):
    """Live sessions sharing one checkout, which can overwrite each other."""

    def _collisions(self, rows):
        return live_presence(rows, now=NOW)["collisions"]

    def test_two_working_sessions_in_one_tree_are_reported(self):
        found = self._collisions([
            session("a", raw_cwd="/repo"),
            session("b", raw_cwd="/repo"),
        ])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["live"], 2)
        self.assertEqual(found[0]["working"], 2)
        self.assertEqual(sorted(found[0]["sessions"]), ["a", "b"])

    def test_different_trees_do_not_collide(self):
        self.assertEqual(
            self._collisions([
                session("a", raw_cwd="/repo-one"),
                session("b", raw_cwd="/repo-two"),
            ]),
            [],
        )

    def test_separate_worktrees_of_one_repo_do_not_collide(self):
        # A linked worktree is its own checkout with its own files. This is the
        # good practice the feature must not punish -- and the reason the key is
        # the real git root rather than project_path, which folds an agent's
        # throwaway worktree back into the repo it came from.
        self.assertEqual(
            self._collisions([
                session("a", raw_cwd="/repo"),
                session("b", raw_cwd="/repo/.claude/worktrees/agent-1"),
            ]),
            [],
        )

    def test_two_idle_sessions_are_not_a_collision(self):
        # Neither is writing. Firing on every pair of parked sessions is how a
        # warning gets ignored before it ever catches anything real.
        self.assertEqual(
            self._collisions([
                session("a", raw_cwd="/repo", seconds_ago=9 * 60),
                session("b", raw_cwd="/repo", seconds_ago=11 * 60),
            ]),
            [],
        )

    def test_one_working_beside_one_quiet_still_counts(self):
        found = self._collisions([
            session("a", raw_cwd="/repo", seconds_ago=10),
            session("b", raw_cwd="/repo", seconds_ago=9 * 60),
        ])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["working"], 1)
        self.assertEqual(found[0]["live"], 2)

    def test_an_ended_session_is_not_a_sharer(self):
        self.assertEqual(
            self._collisions([
                session("a", raw_cwd="/repo"),
                session("b", raw_cwd="/repo", seconds_ago=5 * 3600),
            ]),
            [],
        )

    def test_analyst_spawns_do_not_trigger_it(self):
        # Second Opinion runs in a sandbox under the repository, so counting it
        # would fire this on any project AIWatcher had looked at, against a
        # session the user never started.
        self.assertEqual(
            self._collisions([
                session("mine", raw_cwd="/repo"),
                session("analyst", raw_cwd="/repo/.aiwatcher/analyst"),
            ]),
            [],
        )

    def test_it_spans_tools(self):
        # Claude and Codex in one tree collide exactly as well as two Claudes.
        found = self._collisions([
            session("a", tool="claude-code", raw_cwd="/repo"),
            session("b", tool="codex-cli", raw_cwd="/repo"),
        ])
        self.assertEqual(found[0]["tools"], ["Claude", "Codex"])

    def test_a_session_with_no_recorded_directory_is_skipped(self):
        # Unknown is not the same as shared, and guessing here would accuse a
        # developer of something that is not happening.
        self.assertEqual(
            self._collisions([
                session("a", raw_cwd="/repo"),
                session("b", raw_cwd=None),
            ]),
            [],
        )

    def test_the_label_names_the_checkout(self):
        found = self._collisions([
            session("a", raw_cwd="/home/dev/aiwatcher-local"),
            session("b", raw_cwd="/home/dev/aiwatcher-local"),
        ])
        self.assertEqual(found[0]["label"], "aiwatcher-local")


class WorkingTreeCollisionOnDiskTests(unittest.TestCase):
    """The same question against a real repository on disk.

    Every other collision test compares paths that do not exist, so it never
    reaches the git lookup and passes on string equality alone. Folding a
    subdirectory into its repository is the whole reason that lookup is there.
    """

    def test_a_subdirectory_shares_its_repository(self):
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(["git", "init", "-q", root], check=True)
            nested = os.path.join(root, "src", "deep")
            os.makedirs(nested)
            found = live_presence(
                [
                    session("a", raw_cwd=root),
                    session("b", raw_cwd=nested),
                ],
                now=NOW,
            )["collisions"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["live"], 2)

    def test_two_checkouts_of_one_repository_stay_apart(self):
        with tempfile.TemporaryDirectory() as base:
            main = os.path.join(base, "main")
            subprocess.run(["git", "init", "-q", main], check=True)
            subprocess.run(
                ["git", "-C", main, "commit", "-q", "--allow-empty", "-m", "root"],
                check=True,
                env={**os.environ,
                     "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                     "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
            )
            linked = os.path.join(base, "feature")
            subprocess.run(
                ["git", "-C", main, "worktree", "add", "-q", "-b", "feature", linked],
                check=True,
            )
            found = live_presence(
                [session("a", raw_cwd=main), session("b", raw_cwd=linked)],
                now=NOW,
            )["collisions"]
        self.assertEqual(found, [])


class WaitingOnYouTests(unittest.TestCase):
    """The one state that comes from an event rather than a timestamp."""

    def _signal(self, session_id="s1", *, minutes_ago=5.0, kind="permission"):
        return {session_id: {
            "at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "tool": "claude-code",
            "kind": kind,
        }}

    def test_a_signal_makes_the_session_waiting(self):
        row = session(seconds_ago=6 * 60)
        presence = presence_for_session(
            row, now=NOW, waiting=self._signal()["s1"],
        )
        self.assertEqual(presence.state, "waiting")
        self.assertEqual(presence.label, "waiting 5m")
        self.assertTrue(presence.live)

    def test_a_wait_under_a_minute_carries_no_number(self):
        # "waiting 0m" reads as broken.
        presence = presence_for_session(
            session(seconds_ago=90), now=NOW,
            waiting=self._signal(minutes_ago=0.4)["s1"],
        )
        self.assertEqual(presence.label, "waiting on you")

    def test_a_later_write_clears_it_without_needing_an_event(self):
        # Approving a permission produces the tool result, so a write after the
        # signal means the developer already answered. No "unblocked" event
        # exists, and waiting for one would leave the alert stuck on.
        presence = presence_for_session(
            session(seconds_ago=60), now=NOW,
            waiting=self._signal(minutes_ago=5)["s1"],
        )
        self.assertEqual(presence.state, "working")

    def test_a_signal_older_than_the_live_window_is_ignored(self):
        # The session was closed or the machine slept. A hook cannot send an
        # event for a process that is gone, so nothing would ever clear this.
        presence = presence_for_session(
            session(seconds_ago=3 * 3600), now=NOW,
            waiting=self._signal(minutes_ago=90)["s1"],
        )
        self.assertNotEqual(presence.state, "waiting")

    def test_a_malformed_signal_is_ignored_rather_than_trusted(self):
        for bad in ({}, {"at": None}, {"at": "not-a-date"}, {"at": 12345}):
            with self.subTest(bad=bad):
                presence = presence_for_session(session(seconds_ago=6 * 60), now=NOW, waiting=bad)
                self.assertEqual(presence.state, "quiet")

    def test_a_signal_from_the_future_is_not_a_wait(self):
        presence = presence_for_session(
            session(seconds_ago=6 * 60), now=NOW,
            waiting=self._signal(minutes_ago=-5)["s1"],
        )
        self.assertNotEqual(presence.state, "waiting")

    def test_waiting_sorts_ahead_of_working(self):
        rows = presence_for_sessions(
            [session("busy", seconds_ago=5), session("s1", seconds_ago=6 * 60)],
            now=NOW,
            waiting=self._signal(),
        )
        self.assertEqual([row.session_id for row in rows], ["s1", "busy"])

    def test_the_payload_counts_it_separately(self):
        payload = live_presence(
            [session("s1", seconds_ago=6 * 60), session("busy", seconds_ago=5)],
            now=NOW,
            waiting=self._signal(),
        )
        self.assertEqual(payload["waiting"], 1)
        self.assertEqual(payload["working"], 1)
        self.assertEqual(payload["live"], 2)
        by_tool = {row["tool"]: row for row in payload["tools"]}
        self.assertEqual(by_tool["claude-code"]["waiting"], 1)

    def test_no_signals_means_nothing_waiting(self):
        payload = live_presence([session("s1", seconds_ago=6 * 60)], now=NOW, waiting={})
        self.assertEqual(payload["waiting"], 0)
        self.assertEqual(payload["sessions"][0]["state"], "quiet")

    def test_a_waiting_session_still_counts_as_a_tree_sharer(self):
        # It is stopped, not gone. Whatever it had half-written is still there
        # to be overwritten by the session that is working.
        found = live_presence(
            [session("s1", raw_cwd="/repo", seconds_ago=6 * 60),
             session("busy", raw_cwd="/repo", seconds_ago=5)],
            now=NOW,
            waiting=self._signal(),
        )["collisions"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["live"], 2)
        self.assertEqual(found[0]["working"], 1)
