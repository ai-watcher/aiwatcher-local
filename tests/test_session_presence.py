from __future__ import annotations

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
