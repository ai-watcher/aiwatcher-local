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

    def test_saved_cloud_key_makes_status_ready_without_echoing_secret(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            status = ai_assist.build_ai_assist_status({
                "mode": "cloud",
                "provider": "openai",
                "api_keys": {"openai": "sk-local-test"},
            })

        self.assertTrue(status["ready"])
        self.assertTrue(status["stored_keys"]["openai"])
        self.assertTrue(status["config"]["stored_keys"]["openai"])
        self.assertNotIn("sk-local-test", repr(status))

    def test_status_keeps_off_mode_ready_without_any_provider(self) -> None:
        with (
            patch.object(ai_assist, "detect_local_providers", return_value=[]),
            patch.object(ai_assist, "cloud_provider_status", return_value=[]),
        ):
            status = ai_assist.build_ai_assist_status({"mode": "off", "provider": "none"})

        self.assertEqual(status["active_label"], "Local rules only")
        self.assertEqual(status["status_label"], "Recommended default")
        self.assertTrue(status["ready"])

    def test_local_mode_needs_a_detected_local_provider(self) -> None:
        with (
            patch.object(ai_assist, "detect_local_providers", return_value=[{"id": "ollama", "available": False}]),
            patch.object(ai_assist, "cloud_provider_status", return_value=[]),
        ):
            unavailable = ai_assist.build_ai_assist_status({"mode": "local", "provider": "ollama"})
        with (
            patch.object(ai_assist, "detect_local_providers", return_value=[{"id": "ollama", "available": True}]),
            patch.object(ai_assist, "cloud_provider_status", return_value=[]),
        ):
            available = ai_assist.build_ai_assist_status({"mode": "local", "provider": "ollama"})

        self.assertFalse(unavailable["ready"])
        self.assertEqual(unavailable["status_label"], "Start or configure a local model")
        self.assertTrue(available["ready"])
        self.assertEqual(available["status_label"], "Ready")

    def test_local_mode_accepts_explicit_local_base_url(self) -> None:
        with (
            patch.object(ai_assist, "detect_local_providers", return_value=[]),
            patch.object(ai_assist, "cloud_provider_status", return_value=[]),
        ):
            status = ai_assist.build_ai_assist_status({
                "mode": "local",
                "provider": "auto",
                "base_url": "http://127.0.0.1:9999/v1",
            })

        self.assertTrue(status["ready"])
        self.assertEqual(status["configured_base_url"], "http://127.0.0.1:9999/v1")
        self.assertEqual(status["status_label"], "Ready")

    def test_custom_cloud_endpoint_needs_base_url_and_its_own_key(self) -> None:
        cloud = [
            {"id": "openai_compatible", "available": True},
        ]
        with (
            patch.dict("os.environ", {"AIWATCHER_AI_API_KEY": "custom-key"}, clear=True),
            patch.object(ai_assist, "detect_local_providers", return_value=[]),
            patch.object(ai_assist, "cloud_provider_status", return_value=cloud),
        ):
            missing_url = ai_assist.build_ai_assist_status({
                "mode": "cloud",
                "provider": "openai_compatible",
            })
            ready = ai_assist.build_ai_assist_status({
                "mode": "cloud",
                "provider": "openai_compatible",
                "base_url": "https://llm.example.com/v1",
            })
            auto_ready = ai_assist.build_ai_assist_status({
                "mode": "cloud",
                "provider": "auto",
                "base_url": "https://llm.example.com/v1",
            })

        self.assertFalse(missing_url["ready"])
        self.assertEqual(missing_url["status_label"], "Base URL required")
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["status_label"], "Ready")
        self.assertTrue(auto_ready["ready"])

    def test_specific_cloud_provider_must_have_its_own_key(self) -> None:
        cloud = [
            {"id": "openai", "available": True},
            {"id": "anthropic", "available": False},
        ]
        with (
            patch.object(ai_assist, "detect_local_providers", return_value=[]),
            patch.object(ai_assist, "cloud_provider_status", return_value=cloud),
        ):
            openai = ai_assist.build_ai_assist_status({"mode": "cloud", "provider": "openai"})
            anthropic = ai_assist.build_ai_assist_status({"mode": "cloud", "provider": "anthropic"})

        self.assertTrue(openai["ready"])
        self.assertFalse(anthropic["ready"])

    def test_fresh_start_improvement_composes_bounded_handoff(self) -> None:
        with (
            patch.object(ai_assist, "build_ai_assist_status", return_value={
                "ready": True,
                "mode": "cloud",
                "setup_hint": "Ready",
            }),
            patch.object(ai_assist, "_call_configured_chat", return_value={
                "mode": "cloud",
                "provider": "openai",
                "model": "gpt-test",
                "text": (
                    '{"goal":"Finish the smallest checkpoint.",'
                    '"what_is_done":["Settings page exists"],'
                    '"context_to_preserve":["AI Assist is optional"],'
                    '"inspect_first":["git status --short"],'
                    '"do_not_redo":["Do not rerun broad discovery"],'
                    '"next_ask":"Inspect settings files, then patch only the AI Assist config UX.",'
                    '"acceptance_check":["node --check passes"],'
                    '"uncertainties":["Confirm user-selected provider persists"]}'
                ),
                "usage": {"prompt_tokens": 200, "completion_tokens": 50},
            }) as call,
        ):
            result = ai_assist.improve_fresh_start_brief(
                {
                    "mode": "cloud",
                    "provider": "openai",
                    "source_access": "metadata_only",
                    "enabled_workflows": ["fresh_start"],
                    "api_keys": {"openai": "sk-secret"},
                },
                local_brief="AIWatcher Fresh Start brief\n" + ("x" * 20_000),
            )

        payload = call.call_args.args[1][1]["content"]
        self.assertLessEqual(result["input_chars"], ai_assist.MAX_FRESH_START_INPUT_CHARS)
        self.assertIn("AIWatcher AI-assisted Fresh Start brief", result["text"])
        self.assertIn("What appears done", result["text"])
        self.assertIn("Settings page exists", result["text"])
        self.assertIn("Next ask", result["text"])
        self.assertEqual(result["structured"]["goal"], "Finish the smallest checkpoint.")
        self.assertNotIn("sk-secret", payload)
        self.assertLess(len(payload), 10_000)

    def test_optimize_cleanup_prompt_composes_buckets_and_guardrails(self) -> None:
        with (
            patch.object(ai_assist, "build_ai_assist_status", return_value={
                "ready": True,
                "mode": "cloud",
                "setup_hint": "Ready",
            }),
            patch.object(ai_assist, "_call_configured_chat", return_value={
                "mode": "cloud",
                "provider": "openai",
                "model": "gpt-test",
                "text": (
                    '{"safe_to_archive_or_review":["Old Codex chat can be reviewed in the app"],'
                    '"keep_active":["Keep sessions with recent activity"],'
                    '"unknown":["Process ownership is not proven"],'
                    '"next_action":["Open the owning app and verify the chat is done"],'
                    '"guardrails":["Do not delete files","Do not kill processes"]}'
                ),
                "usage": {"prompt_tokens": 180, "completion_tokens": 70},
            }) as call,
        ):
            result = ai_assist.compose_optimize_cleanup_prompt(
                {
                    "mode": "cloud",
                    "provider": "openai",
                    "source_access": "metadata_only",
                    "enabled_workflows": ["optimize_cleanup"],
                    "api_keys": {"openai": "sk-secret"},
                },
                local_prompt="AIWatcher Optimize cleanup prompt\nFull path: /repo/app\n" + ("x" * 20_000),
            )

        payload = call.call_args.args[1][1]["content"]
        self.assertLessEqual(result["input_chars"], ai_assist.MAX_OPTIMIZE_CLEANUP_INPUT_CHARS)
        self.assertIn("AIWatcher AI-assisted Optimize cleanup prompt", result["text"])
        self.assertIn("Safe to archive/review", result["text"])
        self.assertIn("Keep active", result["text"])
        self.assertIn("Unknown", result["text"])
        self.assertIn("Next action", result["text"])
        self.assertIn("Do not delete files", result["text"])
        self.assertIn("Full path: /repo/app", result["text"])
        self.assertEqual(result["structured"]["next_action"], ["Open the owning app and verify the chat is done"])
        self.assertNotIn("sk-secret", payload)
        self.assertLess(len(payload), 8_000)


if __name__ == "__main__":
    unittest.main()
