from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from aiwatcher_cli.processes import RuntimeProcess
from aiwatcher_cli.runtime_attachment import (
    format_resume_command,
    launch_resume_command,
    perform_runtime_return,
    resume_command_for_session,
    runtime_attachment_for_session,
)
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


class SessionResumeTests(unittest.TestCase):
    """Resume is the return path that does not depend on a live runtime."""

    def test_resume_command_uses_each_tool_s_own_session_id(self) -> None:
        self.assertEqual(
            resume_command_for_session("claude-code", "7da4ef23-2861-4431-be0d-fcb2a852cc6c"),
            ["claude", "--resume", "7da4ef23-2861-4431-be0d-fcb2a852cc6c"],
        )
        self.assertEqual(
            resume_command_for_session("codex-cli", "01a02a55-4450-7450-b0a6-d987ca454245"),
            ["codex", "resume", "01a02a55-4450-7450-b0a6-d987ca454245"],
        )

    def test_cursor_has_no_resume_because_its_ids_are_synthesised(self) -> None:
        # scanner builds these as f"cursor-{log_dir.name}" -- not an id Cursor
        # would recognise, so claiming a resume would be a lie.
        self.assertIsNone(resume_command_for_session("cursor", "cursor-abc123"))
        self.assertIsNone(resume_command_for_session("claude-code", ""))
        self.assertIsNone(resume_command_for_session("claude-code", "old-session"))
        self.assertIsNone(resume_command_for_session("codex-cli", "abc; echo bad"))

    def test_historical_session_still_offers_resume(self) -> None:
        """The regression that matters: resume must not inherit the live gate.

        A session whose terminal has since been closed lands on the
        'historical' tier with available=False. That is correct for opening a
        workspace and wrong for resuming a conversation.
        """
        session_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        session = LocalSession(
            session_id=session_id,
            tool="claude-code",
            project_path="/repo",
            updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )

        attachment = runtime_attachment_for_session(session, state={"status": "stale"}, processes=[])

        self.assertEqual(attachment.level, "historical")
        self.assertFalse(attachment.available)
        payload = attachment.to_json()
        self.assertTrue(payload["resume_available"])
        self.assertEqual(payload["resume_command"], f"claude --resume {session_id}")

    def test_resume_label_does_not_claim_to_return_you_to_the_original_window(self) -> None:
        session = LocalSession(
            session_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            tool="claude-code",
            project_path="/repo",
            updated_at=datetime.now(timezone.utc),
        )
        payload = runtime_attachment_for_session(session, state={"status": "active"}, processes=[]).to_json()
        self.assertEqual(payload["resume_label"], "Resume in terminal")
        self.assertIn("does not", str(payload["resume_reason"]).lower())

    def test_launch_reports_failure_instead_of_claiming_a_window_opened(self) -> None:
        with patch("aiwatcher_cli.runtime_attachment.shutil.which", return_value=None):
            result = launch_resume_command(["claude", "--resume", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"])
        self.assertFalse(result["ok"])
        self.assertIn("PATH", str(result["message"]))

    def test_launch_falls_back_when_the_platform_has_no_terminal_path(self) -> None:
        with (
            patch("aiwatcher_cli.runtime_attachment.shutil.which", return_value="/usr/bin/claude"),
            patch("aiwatcher_cli.runtime_attachment.sys.platform", "linux"),
        ):
            result = launch_resume_command(["claude", "--resume", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"])
        self.assertFalse(result["ok"])
        self.assertIn("Copy the command", str(result["message"]))

    def test_resume_command_display_quotes_shell_values_and_cwd(self) -> None:
        session_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.assertEqual(
            format_resume_command(["claude", "--resume", session_id], cwd="/tmp/My Repo", platform="darwin"),
            f"cd '/tmp/My Repo' && claude --resume {session_id}",
        )
        self.assertEqual(
            format_resume_command(["codex", "resume", session_id], cwd=r"C:\Users\Me\My Repo", platform="win32"),
            f'cd /d "C:\\Users\\Me\\My Repo" && codex resume {session_id}',
        )

    def test_macos_launch_quotes_malicious_argument_before_applescript(self) -> None:
        with (
            patch("aiwatcher_cli.runtime_attachment.shutil.which", return_value="/usr/bin/claude"),
            patch("aiwatcher_cli.runtime_attachment.sys.platform", "darwin"),
            patch("aiwatcher_cli.runtime_attachment.subprocess.Popen") as popen,
        ):
            result = launch_resume_command(["claude", "--resume", "abc; echo bad"])

        self.assertTrue(result["ok"])
        script = popen.call_args.args[0][2]
        self.assertIn("claude --resume 'abc; echo bad'", script)
        self.assertNotIn("claude --resume abc; echo bad", script)

    def test_windows_launch_uses_argv_and_recorded_workspace(self) -> None:
        session_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        with (
            patch("aiwatcher_cli.runtime_attachment.os.path.isdir", return_value=True),
            patch("aiwatcher_cli.runtime_attachment.shutil.which", return_value=r"C:\Tools\codex.exe"),
            patch("aiwatcher_cli.runtime_attachment.sys.platform", "win32"),
            patch("aiwatcher_cli.runtime_attachment.subprocess.Popen") as popen,
        ):
            result = launch_resume_command(["codex", "resume", session_id], cwd=r"C:\Users\Me\My Repo")

        self.assertTrue(result["ok"])
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["codex", "resume", session_id])
        self.assertEqual(popen.call_args.kwargs["cwd"], r"C:\Users\Me\My Repo")


if __name__ == "__main__":
    unittest.main()
