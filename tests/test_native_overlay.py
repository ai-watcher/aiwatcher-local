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

    def test_tk_overlay_records_handoff_only_for_critical_context_copy(self) -> None:
        source = inspect.getsource(native_overlay.run_native_overlay)

        self.assertIn('_normalize_signal_kind(signal_kind) == "critical_context"', source)
        self.assertIn('_record_decision(base, session_id, "copy_handoff"', source)


if __name__ == "__main__":
    unittest.main()
