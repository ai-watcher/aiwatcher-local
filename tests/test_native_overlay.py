from __future__ import annotations

import unittest

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
        config = overlay_config("runway", primary_label="Copy cross-tool handoff")

        self.assertEqual(config.primary_label, "Copy cross-tool handoff")


if __name__ == "__main__":
    unittest.main()
