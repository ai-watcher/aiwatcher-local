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


class ForegroundProbeTests(unittest.TestCase):
    """foreground_tool promises best-effort silence. It has to keep that promise
    for the failure its own timeout creates.

    Both platform probes shell out with timeout=1 and caught only OSError.
    TimeoutExpired is a SubprocessError, so a busy machine raised straight
    through and crashed `aiwatcher watch` -- and once foreground_tool moved onto
    the watch path, one loaded local run turned ten watch tests red at once.
    """

    def test_a_busy_machine_does_not_crash_the_watch_loop(self) -> None:
        import subprocess

        from aiwatcher_cli import runtime_nudge

        def timed_out(*args: object, **kwargs: object):
            raise subprocess.TimeoutExpired(["tasklist"], 1)

        for probe in ("_windows_foreground_app", "_macos_foreground_app"):
            with self.subTest(probe=probe):
                with patch.object(runtime_nudge.subprocess, "run", timed_out):
                    self.assertIsNone(getattr(runtime_nudge, probe)())


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


class BlockedSessionNudgeTests(unittest.TestCase):
    """The one signal whose value is that you are somewhere else."""

    def _nudge(self, *, idle_seconds: float, foreground: str | None = "code", kind: str = "session_blocked"):
        now = datetime.now(timezone.utc)
        session = LocalSession(
            session_id="s1",
            tool="claude-code",
            surface="cli",
            updated_at=now - timedelta(seconds=idle_seconds),
        )
        attachment = RuntimeAttachment(
            session_id="s1", level="historical", mode="none", label="",
            action_label="", available=False, confidence="low", reason="",
            tool="claude-code",
        )
        return build_runtime_nudge(
            session,
            {"signal_kind": kind, "reason": "Waiting on you."},
            attachment,
            now=now,
            active_foreground_tool=foreground,
        )

    def test_it_interrupts_even_when_you_are_in_another_app(self):
        # Every other signal is held unless its tool is in front of you.
        # Inverted here: being elsewhere is the entire reason this exists.
        nudge = self._nudge(idle_seconds=6 * 60, foreground="code")
        self.assertTrue(nudge.eligible, nudge.hold_reason)
        self.assertEqual(nudge.title, "A session is waiting for you")

    def test_the_same_situation_still_holds_a_context_warning(self):
        # Proves the inversion is scoped to this signal and did not loosen the
        # policy for everything else.
        nudge = self._nudge(idle_seconds=6 * 60, foreground="code", kind="critical_context")
        self.assertFalse(nudge.eligible)

    def test_a_short_wait_is_held(self):
        # At the keyboard you answer in seconds. Interrupting mid-answer is how
        # this feature gets muted.
        nudge = self._nudge(idle_seconds=20)
        self.assertFalse(nudge.eligible)
        self.assertIn("still be answering", nudge.hold_reason or "")

    def test_a_long_wait_still_interrupts(self):
        # MAX_ACTIVE_IDLE_SECONDS would go quiet at fifteen minutes, which is
        # when the wait has cost the most.
        nudge = self._nudge(idle_seconds=45 * 60)
        self.assertTrue(nudge.eligible, nudge.hold_reason)

    def test_it_needs_no_second_sighting(self):
        # REQUIRED_OBSERVATIONS confirms signals inferred from noisy timestamps.
        # This one was reported by the tool; a confirmation poll is pure latency.
        self.assertEqual(self._nudge(idle_seconds=6 * 60).required_observations, 1)

    def test_it_offers_a_way_back(self):
        nudge = self._nudge(idle_seconds=6 * 60)
        self.assertEqual(nudge.primary_label, "Return to session")
        self.assertEqual(nudge.action, "return_session")
