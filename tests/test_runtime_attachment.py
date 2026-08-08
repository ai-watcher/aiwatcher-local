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
        self.assertIn("does not expose a stable deep link", attachment.reason)

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
        self.assertIn("Exact terminal/chat focus needs", attachment.reason)

    def test_stale_session_uses_handoff_instead_of_live_return(self) -> None:
        session = LocalSession(session_id="old", tool="claude-code", project_path="/repo")
        attachment = runtime_attachment_for_session(
            session,
            state={"status": "stale"},
            processes=[],
        )

        self.assertFalse(attachment.available)
        self.assertEqual(attachment.level, "historical")
        self.assertEqual(attachment.action_label, "Copy handoff")

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


if __name__ == "__main__":
    unittest.main()
