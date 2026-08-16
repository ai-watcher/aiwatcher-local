from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from aiwatcher_cli import cli
from aiwatcher_cli.runtime_attachment import RuntimeAttachment
from aiwatcher_cli.runtime_nudge import build_runtime_nudge, presentation_for_signal
from aiwatcher_cli.scanner import LocalSession


def session(*, surface: str = "cli", age_seconds: int = 30, tool: str = "codex-cli") -> LocalSession:
    now = datetime.now(timezone.utc)
    return LocalSession(
        session_id="session-live",
        tool=tool,
        surface=surface,
        project_path="/repo",
        started_at=now - timedelta(minutes=20),
        updated_at=now - timedelta(seconds=age_seconds),
    )


def attachment(*, level: str = "active_process") -> RuntimeAttachment:
    return RuntimeAttachment(
        session_id="session-live",
        level=level,
        mode="vscode",
        label="Attached runtime",
        action_label="Open workspace",
        available=True,
        confidence="high",
        reason="Matched a local runtime.",
        tool="codex-cli",
        surface="cli",
        project_path="/repo",
    )


STATUS = {
    "signal_kind": "critical_context",
    "reason": "The latest turn is replaying substantial context.",
}


class RuntimeNudgeTests(unittest.TestCase):
    def test_active_process_is_eligible_after_a_short_pause(self) -> None:
        nudge = build_runtime_nudge(
            session(),
            STATUS,
            attachment(),
            active_foreground_tool="codex",
        )

        self.assertTrue(nudge.eligible)
        self.assertEqual(nudge.required_observations, 2)
        self.assertEqual(nudge.primary_label, "Copy Fresh Start brief")

    def test_nudge_waits_until_the_user_reaches_a_pause(self) -> None:
        nudge = build_runtime_nudge(
            session(age_seconds=2),
            STATUS,
            attachment(),
            active_foreground_tool="codex",
        )

        self.assertFalse(nudge.eligible)
        self.assertIn("waiting for a pause", nudge.hold_reason or "")

    def test_active_process_does_not_interrupt_an_unrelated_foreground_app(self) -> None:
        nudge = build_runtime_nudge(
            session(),
            STATUS,
            attachment(),
            active_foreground_tool="chrome",
        )

        self.assertFalse(nudge.eligible)
        self.assertIn("chrome is foreground", nudge.hold_reason or "")

    def test_stale_session_never_interrupts_the_desktop(self) -> None:
        nudge = build_runtime_nudge(
            session(age_seconds=16 * 60),
            STATUS,
            attachment(),
            active_foreground_tool="codex",
        )

        self.assertFalse(nudge.eligible)
        self.assertIn("too old", nudge.hold_reason or "")

    def test_desktop_session_requires_the_matching_foreground_tool(self) -> None:
        matching = build_runtime_nudge(
            session(surface="desktop"),
            STATUS,
            attachment(level="app"),
            active_foreground_tool="codex",
        )
        unrelated = build_runtime_nudge(
            session(surface="desktop"),
            STATUS,
            attachment(level="app"),
            active_foreground_tool="claude",
        )

        self.assertTrue(matching.eligible)
        self.assertFalse(unrelated.eligible)
        self.assertIn("not codex", unrelated.hold_reason or "")

    def test_signal_copy_does_not_call_every_warning_a_fresh_start(self) -> None:
        velocity = presentation_for_signal("velocity", "Fast local activity.")

        self.assertEqual(velocity["action_mode"], "continue_focused")
        self.assertNotIn("Fresh Start", velocity["primary_label"])

    def test_coordinator_selects_the_matching_foreground_session_not_the_newest_log(self) -> None:
        now = datetime.now(timezone.utc)
        newest_background = LocalSession(
            session_id="claude-background",
            tool="claude-code",
            surface="desktop",
            project_path="/repo-a",
            updated_at=now - timedelta(seconds=20),
        )
        foreground = LocalSession(
            session_id="codex-foreground",
            tool="codex-cli",
            surface="desktop",
            project_path="/repo-b",
            updated_at=now - timedelta(seconds=30),
        )
        args = SimpleNamespace(cost_threshold=5.0, calls_threshold=250, tokens_threshold=500_000)

        def app_attachment(row: LocalSession, **_: object) -> RuntimeAttachment:
            return RuntimeAttachment(
                session_id=row.session_id,
                level="app",
                mode="app",
                label="Detected app",
                action_label="Open app",
                available=True,
                confidence="medium",
                reason="Recent desktop session.",
                tool=row.tool,
                surface=row.surface,
                project_path=row.project_path,
            )

        with (
            patch.object(cli, "_watch_status", return_value={
                "action": "create handoff capsule now",
                "signal_kind": "critical_context",
                "reason": "Context pressure.",
                "health": None,
            }),
            patch.object(cli, "runtime_attachment_for_session", side_effect=app_attachment),
        ):
            selected = cli._select_runtime_nudge_session(
                [newest_background, foreground],
                {},
                args,
                processes=[],
                active_foreground_tool="codex",
            )

        self.assertEqual(selected, "codex-foreground")


if __name__ == "__main__":
    unittest.main()
