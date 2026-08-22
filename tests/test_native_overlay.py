from __future__ import annotations

import unittest
from unittest import mock
import inspect

from aiwatcher_cli import native_overlay
from aiwatcher_cli.native_overlay import overlay_config


class NativeOverlayConfigTests(unittest.TestCase):
    def test_velocity_recommends_focus_not_a_fresh_chat(self) -> None:
        config = overlay_config("velocity")

        self.assertEqual(config.primary_mode, "copy")
        self.assertEqual(config.primary_label, "Copy focused brief")
        self.assertNotIn("fresh", config.title.lower())

    def test_critical_context_offers_fresh_session_brief(self) -> None:
        config = overlay_config("critical_context")

        self.assertEqual(config.primary_mode, "copy")
        self.assertEqual(config.primary_label, "Copy fresh-session brief")

    def test_loop_prioritizes_inspection(self) -> None:
        config = overlay_config("loop", action_endpoint_available=True)

        self.assertEqual(config.primary_mode, "inspect")
        self.assertEqual(config.primary_label, "Stop and inspect")

    def test_primary_label_can_be_overridden_by_watch_presentation(self) -> None:
        config = overlay_config("runway", primary_label="Copy cross-tool Fresh Start")

        self.assertEqual(config.primary_label, "Copy cross-tool Fresh Start")

    def test_generic_fresh_start_title_infers_critical_context(self) -> None:
        self.assertEqual(
            native_overlay._infer_signal_kind_from_title("generic", "AIWatcher: start a fresh chat"),
            "critical_context",
        )

    def test_runtime_return_uses_post_body(self) -> None:
        with mock.patch.object(native_overlay, "_request_json", return_value={"ok": True}) as request_json:
            self.assertTrue(native_overlay._request_runtime_return("http://127.0.0.1:8765", "sess-1"))

        request_json.assert_called_once_with("http://127.0.0.1:8765/api/runtime-return", {"session_id": "sess-1"})

    def test_macos_overlay_records_handoff_only_for_critical_context_copy(self) -> None:
        self.assertIn('if signalKind == "critical_context"', native_overlay.MACOS_SWIFT_OVERLAY)
        self.assertIn("/api/handoff-basic", native_overlay.MACOS_SWIFT_OVERLAY)
        self.assertIn('postDecision("copy_handoff")', native_overlay.MACOS_SWIFT_OVERLAY)
        self.assertIn('if action == "snooze"', native_overlay.MACOS_SWIFT_OVERLAY)
        self.assertNotIn('action == "dismiss" ? "dismissed" : "copy_handoff"', native_overlay.MACOS_SWIFT_OVERLAY)

    def test_macos_overlay_is_nonactivating_and_has_one_primary_action(self) -> None:
        source = native_overlay.MACOS_SWIFT_OVERLAY

        self.assertIn(".nonactivatingPanel", source)
        self.assertNotIn("NSApp.activate", source)
        self.assertIn('NSButton(title: "Continue here"', source)
        self.assertIn('NSButton(title: "..."', source)

    def test_tk_overlay_records_handoff_only_for_critical_context_copy(self) -> None:
        source = inspect.getsource(native_overlay.run_native_overlay)

        self.assertIn('_normalize_signal_kind(signal_kind) == "critical_context"', source)
        self.assertIn('_record_decision(base, session_id, "copy_handoff"', source)

    def test_macos_presence_is_collapsed_nonactivating_companion(self) -> None:
        source = native_overlay.MACOS_SWIFT_PRESENCE

        self.assertIn(".nonactivatingPanel", source)
        self.assertIn(".canJoinAllSpaces", source)
        self.assertIn('"AIWatcher"', source)
        self.assertIn('"Watching quietly"', source)
        self.assertIn('@objc func openDashboard', source)
        self.assertIn('@objc func openPrompt', source)
        self.assertIn('@objc func scanNow', source)
        self.assertIn('@objc func openPrimary', source)
        self.assertIn('@objc func toggleCollapsed', source)
        self.assertIn('final class DragView', source)
        self.assertIn('window.isMovableByWindowBackground = true', source)
        self.assertIn('dotLabel', source)
        self.assertIn('stateName', source)
        self.assertIn('pulseOn', source)
        self.assertIn('updateAppearance()', source)
        self.assertIn('schedulePulse()', source)
        self.assertIn('primaryButton.layer?.backgroundColor', source)
        self.assertIn('continueButton', source)
        self.assertIn('continueHere', source)
        self.assertIn('continuePromptGate', source)
        self.assertIn('"run_original_prompt"', source)
        self.assertIn('"decision": "run_original"', source)
        self.assertIn('skipButton', source)
        self.assertIn('skipCurrent', source)
        self.assertIn('/api/handoff-decision', source)
        self.assertIn('/api/companion-skip', source)
        self.assertIn('/api/companion-scan', source)
        self.assertIn('/api/handoff-receipts-viewed', source)
        self.assertIn('"prompt_gate"', source)
        self.assertIn('"control_review"', source)
        self.assertNotIn('"proof_pending", "needs_review"', source)
        self.assertIn('0.93, green: 0.42, blue: 0.14', source)
        self.assertIn('0.91, green: 0.96, blue: 1.00', source)
        self.assertIn('0.95, green: 0.99, blue: 1.00', source)
        self.assertIn('"/api/companion-state"', source)
        self.assertIn('"Plan"', source)
        self.assertIn('"Scan"', source)
        self.assertIn('"UI"', source)
        self.assertIn('"AI"', source)
        self.assertIn("targetHeight / 2", source)
        self.assertIn('labelWithString: "AI"', source)
        self.assertIn("expandButton.layer?.borderColor", source)
        self.assertIn("setCollapsed(true)", source)
        self.assertIn("current.maxX - targetWidth", source)
        self.assertIn("hasPrimaryAction", source)
        self.assertIn("scheduleAutoCollapse", source)

    def test_tk_presence_opens_dashboard_and_prompt_without_session_claim(self) -> None:
        source = inspect.getsource(native_overlay.run_native_presence)

        self.assertIn("Watching quietly", source)
        self.assertIn("toggle_collapsed", source)
        self.assertIn("state_var", source)
        self.assertIn("pulse_var", source)
        self.assertIn("#ed6a24", source)
        self.assertIn("#eef8fb", source)
        self.assertIn("PresenceCollapsed.TButton", source)
        self.assertIn("PresenceAttention.TButton", source)
        self.assertNotIn('"proof_pending", "needs_review"', source)
        self.assertIn("continue_here", source)
        self.assertIn("run_original_prompt", source)
        self.assertIn("\"decision\": \"run_original\"", source)
        self.assertIn("skip_current", source)
        self.assertIn("/api/handoff-decision", source)
        self.assertIn("/api/companion-skip", source)
        self.assertIn("/api/handoff-receipts-viewed", source)
        self.assertIn("control_review", source)
        self.assertIn("cursor=\"fleur\"", source)
        self.assertIn("webbrowser.open(url)", source)
        self.assertIn("webbrowser.open(prompt_url or url)", source)
        self.assertIn("scan_now", source)
        self.assertIn("/api/companion-state", source)
        self.assertIn("/api/companion-scan", source)
        self.assertIn("text=\"Plan\"", source)
        self.assertIn("text=\"Scan\"", source)
        self.assertIn("text=\"UI\"", source)
        self.assertIn("text=\"AI\"", source)
        self.assertIn("set_collapsed(True)", source)
        self.assertIn("root.after(3000, refresh_state)", source)
        self.assertIn("has_primary_action", source)
        self.assertIn("schedule_auto_collapse", source)
        self.assertIn("textvariable=primary_label_var", source)
        self.assertIn("primary_runtime_available_var", source)
        self.assertIn("/api/runtime-return", source)
        self.assertIn("if primary_runtime_available_var.get()", source)
        self.assertIn("Clipboard has text", source)
        self.assertIn("Click Replace to copy Fresh Start", source)
        self.assertIn("pending_clipboard_override_session_var", source)
        self.assertIn('state_var.get() in {"prompt_gate", "control_recommended", "optimize_available", "clipboard_confirm"}', source)
        self.assertIn("visibility: str = \"always\"", source)
        self.assertIn("_foreground_looks_like_ai_work", source)
        self.assertIn("root.withdraw()", source)

        mac_source = native_overlay.MACOS_SWIFT_PRESENCE
        self.assertIn("visibilityMode", mac_source)
        self.assertIn("NSWorkspace.shared.frontmostApplication", mac_source)
        self.assertIn("foregroundLooksLikeAIWork", mac_source)
        self.assertIn('visibilityMode == "ai-apps"', mac_source)

    def test_presence_visibility_uses_windows_foreground_api(self) -> None:
        helper_source = inspect.getsource(native_overlay._windows_foreground_text)
        main_source = inspect.getsource(native_overlay.main)

        self.assertIn("GetForegroundWindow", helper_source)
        self.assertIn("GetWindowTextW", helper_source)
        self.assertIn("tasklist", helper_source)
        self.assertIn("--visibility", main_source)
        self.assertIn("visibility=args.visibility", main_source)

    def test_macos_tray_has_console_prompt_scan_and_quit(self) -> None:
        source = native_overlay.MACOS_SWIFT_TRAY

        self.assertIn("NSStatusBar.system.statusItem", source)
        self.assertIn('"AIW"', source)
        self.assertIn("Open Console", source)
        self.assertIn("Plan Prompt", source)
        self.assertIn("Scan Now", source)
        self.assertIn("Quit AIWatcher Menu", source)
        self.assertIn("/api/companion-scan", source)

    def test_windows_tray_uses_shell_notify_icon(self) -> None:
        source = inspect.getsource(native_overlay._run_windows_tray)

        self.assertIn("Shell_NotifyIconW", source)
        self.assertIn("CreatePopupMenu", source)
        self.assertIn("Open Console", source)
        self.assertIn("Plan Prompt", source)
        self.assertIn("Scan Now", source)

    def test_main_routes_tray_mode_separately_from_presence(self) -> None:
        source = inspect.getsource(native_overlay.main)

        self.assertIn("--tray", source)
        self.assertIn("run_native_tray", source)
        self.assertIn("run_native_presence", source)


if __name__ == "__main__":
    unittest.main()
