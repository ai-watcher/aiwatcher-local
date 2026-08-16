from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from aiwatcher_cli.processes import RuntimeProcess
from aiwatcher_cli.runtime_attachment import perform_runtime_return, runtime_attachment_for_session
from aiwatcher_cli.scanner import LocalSession


class RuntimeAttachmentTests(unittest.TestCase):
    def test_desktop_session_opens_app_but_does_not_claim_exact_chat(self) -> None:
        session = LocalSession(
            session_id="desktop-session",
            tool="codex-cli",
            surface="desktop",
            project_path="/repo",
            updated_at=datetime.now(timezone.utc),
        )

        with patch("aiwatcher_cli.runtime_attachment.sys.platform", "darwin"):
            attachment = runtime_attachment_for_session(
                session,
                state={"status": "active"},
                processes=[],
            )

        self.assertEqual(attachment.level, "app")
        self.assertEqual(attachment.mode, "app")
        self.assertEqual(attachment.action_label, "Open Codex")
        self.assertTrue(attachment.available)
        self.assertFalse(attachment.exact_return_available)
        self.assertEqual(attachment.exact_return_label, "App focus only")
        self.assertEqual(attachment.identity_level, "likely_workspace")
        self.assertEqual(attachment.identity_label, "Likely active app")
        self.assertIn("does not expose a stable deep link", attachment.reason)
        self.assertIn("exact desktop chat", attachment.exact_return_reason)

    def test_matching_process_records_active_attachment_without_overclaiming_focus(self) -> None:
        session = LocalSession(
            session_id="live-session",
            tool="claude-code",
            surface="cli",
            project_path="/repo",
            updated_at=datetime.now(timezone.utc),
        )
        process = RuntimeProcess(
            pid=123,
            ppid=1,
            age_seconds=60,
            state="S",
            tool="Claude",
            command="claude --session-id live-session --cwd /repo",
            cwd="/repo",
            session_id="live-session",
        )

        attachment = runtime_attachment_for_session(
            session,
            state={"status": "active"},
            processes=[process],
        )

        self.assertEqual(attachment.level, "active_process")
        self.assertEqual(attachment.pid, 123)
        self.assertFalse(attachment.exact_return_available)
        self.assertEqual(attachment.exact_return_label, "Needs native companion")
        self.assertEqual(attachment.identity_level, "exact_session")
        self.assertEqual(attachment.identity_label, "Exact active session")
        self.assertIn("Exact terminal/chat focus needs", attachment.reason)

    def test_matching_process_with_deep_link_allows_exact_return(self) -> None:
        session = LocalSession(
            session_id="live-session",
            tool="codex-cli",
            surface="desktop",
            project_path="/repo",
            updated_at=datetime.now(timezone.utc),
        )
        process = RuntimeProcess(
            pid=123,
            ppid=1,
            age_seconds=60,
            state="S",
            tool="Codex",
            command="codex --session-id live-session --deep-link codex://chat/live-session",
            cwd="/repo",
            session_id="live-session",
            deep_link="codex://chat/live-session",
        )

        attachment = runtime_attachment_for_session(
            session,
            state={"status": "active"},
            processes=[process],
        )

        self.assertEqual(attachment.level, "exact_deep_link")
        self.assertEqual(attachment.mode, "deep_link")
        self.assertTrue(attachment.available)
        self.assertTrue(attachment.exact_return_available)
        self.assertEqual(attachment.action_label, "Return to exact chat")
        self.assertEqual(attachment.deep_link, "codex://chat/live-session")

    def test_unsafe_deep_link_is_ignored(self) -> None:
        session = LocalSession(
            session_id="live-session",
            tool="codex-cli",
            surface="desktop",
            project_path="/repo",
            updated_at=datetime.now(timezone.utc),
        )
        process = RuntimeProcess(
            pid=123,
            ppid=1,
            age_seconds=60,
            state="S",
            tool="Codex",
            command="codex --session-id live-session --deep-link file:///etc/passwd",
            cwd="/repo",
            session_id="live-session",
            deep_link="file:///etc/passwd",
        )

        attachment = runtime_attachment_for_session(
            session,
            state={"status": "active"},
            processes=[process],
        )

        self.assertNotEqual(attachment.mode, "deep_link")
        self.assertFalse(attachment.exact_return_available)

    def test_stale_session_uses_handoff_instead_of_live_return(self) -> None:
        session = LocalSession(session_id="old", tool="claude-code", project_path="/repo")
        attachment = runtime_attachment_for_session(
            session,
            state={"status": "stale"},
            processes=[],
        )

        self.assertFalse(attachment.available)
        self.assertEqual(attachment.level, "historical")
        self.assertEqual(attachment.action_label, "No live return")
        self.assertEqual(attachment.identity_level, "historical_log")
        self.assertEqual(attachment.identity_label, "Historical log only")
        self.assertFalse(attachment.native_companion_required)

    def test_perform_runtime_return_opens_macos_app(self) -> None:
        session = LocalSession(
            session_id="desktop-session",
            tool="claude-code",
            surface="desktop",
            project_path="/repo",
            updated_at=datetime.now(timezone.utc),
        )
        with (
            patch("aiwatcher_cli.runtime_attachment.sys.platform", "darwin"),
            patch("aiwatcher_cli.runtime_attachment.subprocess.Popen") as popen,
        ):
            attachment = runtime_attachment_for_session(session, state={"status": "active"}, processes=[])
            result = perform_runtime_return(attachment)

        self.assertTrue(result["ok"])
        popen.assert_called_once()
        self.assertIn("Exact chat return is not available", result["message"])

    def test_perform_runtime_return_opens_exact_deep_link(self) -> None:
        session = LocalSession(
            session_id="desktop-session",
            tool="codex-cli",
            surface="desktop",
            project_path="/repo",
            updated_at=datetime.now(timezone.utc),
        )
        process = RuntimeProcess(
            pid=123,
            ppid=1,
            age_seconds=60,
            state="S",
            tool="Codex",
            command="codex --session-id desktop-session --deep-link codex://chat/desktop-session",
            cwd="/repo",
            session_id="desktop-session",
            deep_link="codex://chat/desktop-session",
        )
        with (
            patch("aiwatcher_cli.runtime_attachment.sys.platform", "darwin"),
            patch("aiwatcher_cli.runtime_attachment.subprocess.Popen") as popen,
        ):
            attachment = runtime_attachment_for_session(session, state={"status": "active"}, processes=[process])
            result = perform_runtime_return(attachment)

        self.assertTrue(result["ok"])
        popen.assert_called_once_with(
            ["open", "codex://chat/desktop-session"],
            stdout=-3,
            stderr=-3,
        )
        self.assertIn("exact chat", result["message"])


if __name__ == "__main__":
    unittest.main()
