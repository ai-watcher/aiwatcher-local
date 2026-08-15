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
        self.assertIn('skipButton', source)
        self.assertIn('skipCurrent', source)
        self.assertIn('/api/handoff-decision', source)
        self.assertIn('/api/companion-skip', source)
        self.assertIn('/api/handoff-receipts-viewed', source)
        self.assertIn('"prompt_gate"', source)
        self.assertIn('0.88, green: 0.36, blue: 0.12', source)
        self.assertIn('"/api/companion-state"', source)
        self.assertIn('"Plan"', source)
        self.assertIn('"Console"', source)
        self.assertIn('":: AIW"', source)
        self.assertIn('labelWithString: "AIW"', source)
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
        self.assertIn("#df5c1e", source)
        self.assertIn("PresenceAttention.TButton", source)
        self.assertIn("continue_here", source)
        self.assertIn("skip_current", source)
        self.assertIn("/api/handoff-decision", source)
        self.assertIn("/api/companion-skip", source)
        self.assertIn("/api/handoff-receipts-viewed", source)
        self.assertIn("cursor=\"fleur\"", source)
        self.assertIn("webbrowser.open(url)", source)
        self.assertIn("webbrowser.open(prompt_url or url)", source)
        self.assertIn("/api/companion-state", source)
        self.assertIn("text=\"Plan\"", source)
        self.assertIn("text=\"Console\"", source)
        self.assertIn("text=\":: AIW\"", source)
        self.assertIn("text=\"AIW\"", source)
        self.assertIn("set_collapsed(True)", source)
        self.assertIn("root.after(3000, refresh_state)", source)
        self.assertIn("has_primary_action", source)
        self.assertIn("schedule_auto_collapse", source)
        self.assertIn("textvariable=primary_label_var", source)
        self.assertNotIn("runtime-return", source)


if __name__ == "__main__":
    unittest.main()
