from __future__ import annotations

import unittest
from unittest.mock import patch

from aiwatcher_cli import ai_assist


class AiAssistTests(unittest.TestCase):
    def test_cloud_keys_are_detected_without_storing_secret_values(self) -> None:
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=True):
            rows = ai_assist.cloud_provider_status()

        openai = next(row for row in rows if row["id"] == "openai")
        anthropic = next(row for row in rows if row["id"] == "anthropic")
        self.assertTrue(openai["available"])
        self.assertEqual(openai["secret_env"], "OPENAI_API_KEY")
        self.assertNotIn("sk-test", repr(rows))
        self.assertFalse(anthropic["available"])

    def test_status_keeps_off_mode_ready_without_any_provider(self) -> None:
        with patch.object(ai_assist, "detect_local_providers", return_value=[]), patch.object(
            ai_assist, "cloud_provider_status", return_value=[]
        ):
            status = ai_assist.build_ai_assist_status({"mode": "off", "provider": "none"})

        self.assertEqual(status["active_label"], "Local rules only")
        self.assertEqual(status["status_label"], "Off by default")
        self.assertTrue(status["ready"])

    def test_local_mode_needs_a_detected_local_provider(self) -> None:
        with patch.object(
            ai_assist, "detect_local_providers", return_value=[{"id": "ollama", "available": False}]
        ), patch.object(ai_assist, "cloud_provider_status", return_value=[]):
            unavailable = ai_assist.build_ai_assist_status({"mode": "local", "provider": "ollama"})
        with patch.object(
            ai_assist, "detect_local_providers", return_value=[{"id": "ollama", "available": True}]
        ), patch.object(ai_assist, "cloud_provider_status", return_value=[]):
            available = ai_assist.build_ai_assist_status({"mode": "local", "provider": "ollama"})

        self.assertFalse(unavailable["ready"])
        self.assertEqual(unavailable["status_label"], "Provider not detected")
        self.assertTrue(available["ready"])
        self.assertEqual(available["status_label"], "Ready")

    def test_specific_cloud_provider_must_have_its_own_key(self) -> None:
        cloud = [
            {"id": "openai", "available": True},
            {"id": "anthropic", "available": False},
        ]
        with patch.object(ai_assist, "detect_local_providers", return_value=[]), patch.object(
            ai_assist, "cloud_provider_status", return_value=cloud
        ):
            openai = ai_assist.build_ai_assist_status({"mode": "cloud", "provider": "openai"})
            anthropic = ai_assist.build_ai_assist_status({"mode": "cloud", "provider": "anthropic"})

        self.assertTrue(openai["ready"])
        self.assertFalse(anthropic["ready"])


if __name__ == "__main__":
    unittest.main()
