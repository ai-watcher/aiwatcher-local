from __future__ import annotations

import io
import unittest
from unittest.mock import patch
import urllib.error

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

    def test_saved_cloud_key_is_configured_without_echoing_secret(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            status = ai_assist.build_ai_assist_status({
                "mode": "cloud",
                "provider": "openai",
                "api_keys": {"openai": "sk-local-test"},
            })

        self.assertTrue(status["ready"])
        self.assertEqual(status["status_label"], "Configured, not tested")
        self.assertTrue(status["stored_keys"]["openai"])
        self.assertTrue(status["config"]["stored_keys"]["openai"])
        self.assertNotIn("sk-local-test", repr(status))

    def test_rejected_cloud_key_is_not_ready(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            status = ai_assist.build_ai_assist_status({
                "mode": "cloud",
                "provider": "openai",
                "api_keys": {"openai": "sk-local-test"},
                "provider_checks": {
                    "openai": {
                        "status": "failed",
                        "message": "AI Assist provider rejected the API key or credentials.",
                        "code": "invalid_api_key",
                    },
                },
            })

        openai = next(row for row in status["cloud_providers"] if row["id"] == "openai")
        self.assertFalse(status["ready"])
        self.assertEqual(status["status_label"], "Key rejected")
        self.assertFalse(openai["available"])
        self.assertEqual(openai["check_status"], "failed")
        self.assertNotIn("sk-local-test", repr(status))

    def test_provider_http_error_is_sanitized(self) -> None:
        raw = (
            b'{ "error": { "message": "Incorrect API key provided: sk-secret-value. '
            b'You can find your API key at https://platform.openai.com/account/api-keys.", '
            b'"code": "invalid_api_key" } }'
        )
        error = urllib.error.HTTPError(
            "https://api.openai.com/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            io.BytesIO(raw),
        )
        with patch.object(ai_assist.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(ai_assist.AiAssistUnavailable) as raised:
                ai_assist._openai_compatible_chat(
                    base_url="https://api.openai.com/v1",
                    model="gpt-test",
                    messages=[{"role": "user", "content": "hello"}],
                    api_key="sk-secret-value",
                )

        message = str(raised.exception)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.provider_code, "invalid_api_key")
        self.assertIn("provider rejected the API key", message)
        self.assertNotIn("sk-secret-value", message)
        self.assertNotIn("Incorrect API key provided", message)

    def test_provider_error_type_is_used_when_code_is_absent(self) -> None:
        # Anthropic reports {"error": {"type": ...}}; OpenAI reports "code".
        message, code = ai_assist._safe_provider_error(
            404, '{"type":"error","error":{"type":"not_found_error","message":"model: nope"}}'
        )
        self.assertEqual(code, "not_found_error")
        self.assertIn("HTTP 404 (not_found_error)", message)

    def test_default_claude_model_is_a_current_model(self) -> None:
        # claude-3-5-haiku-latest was retired on 2026-02-19; a blank model field
        # with Claude selected must not fail on every run.
        self.assertEqual(ai_assist.DEFAULT_MODELS["anthropic"], "claude-haiku-4-5")

    def test_post_json_converts_every_failure_to_ai_assist_unavailable(self) -> None:
        messages = [{"role": "user", "content": "hello"}]
        # A base URL urllib cannot parse used to raise ValueError at Request
        # construction, outside the try, and escape the HTTP handler.
        with self.assertRaises(ai_assist.AiAssistUnavailable):
            ai_assist._openai_compatible_chat(base_url="myhost/v1", model="m", messages=messages)

        class _Body:
            def __init__(self, raw: bytes) -> None:
                self._raw = raw
            def read(self) -> bytes:
                return self._raw
            def __enter__(self):
                return self
            def __exit__(self, *args) -> None:
                return None

        # A body that decodes to a list, and one that is not UTF-8.
        for raw in (b"[1, 2]", b"\xff\xfe"):
            with self.subTest(raw=raw):
                with patch.object(ai_assist.urllib.request, "urlopen", return_value=_Body(raw)):
                    with self.assertRaises(ai_assist.AiAssistUnavailable):
                        ai_assist._openai_compatible_chat(
                            base_url="http://127.0.0.1:1234/v1", model="m", messages=messages,
                        )

    def test_cloud_failure_names_the_provider_that_answered(self) -> None:
        # Under provider "auto" the config does not say which key was tried;
        # the exception must, so the rejection is recorded against that key.
        error = urllib.error.HTTPError(
            "https://api.openai.com/v1/chat/completions", 401, "Unauthorized", {},
            io.BytesIO(b'{"error":{"code":"invalid_api_key","message":"bad"}}'),
        )
        with patch.object(ai_assist.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(ai_assist.AiAssistUnavailable) as raised:
                ai_assist._call_configured_chat(
                    {"mode": "cloud", "provider": "auto", "api_keys": {"openai": "sk-secret"}},
                    [{"role": "user", "content": "hello"}],
                )
        self.assertEqual(raised.exception.provider, "openai")
        self.assertEqual(raised.exception.status_code, 401)

    def test_auto_provider_status_shows_a_rejected_key(self) -> None:
        with (
            patch.object(ai_assist, "detect_local_providers", return_value=[]),
            patch.dict(ai_assist.os.environ, {}, clear=True),
        ):
            status = ai_assist.build_ai_assist_status({
                "mode": "cloud",
                "provider": "auto",
                "api_keys": {"openai": "sk-secret"},
                "provider_checks": {"openai": {"status": "failed", "message": "rejected", "code": "invalid_api_key"}},
            })
        self.assertEqual(status["status_label"], "Key rejected")
        self.assertFalse(status["ready"])

    def test_local_call_uses_the_typed_base_url_when_nothing_is_detected(self) -> None:
        # Settings said Ready for this config; the call used to refuse it
        # because it only consulted the detected runtimes.
        with (
            patch.object(ai_assist, "detect_local_providers", return_value=[]),
            patch.object(ai_assist, "_openai_compatible_chat", return_value={"text": "ok", "usage": {}}) as chat,
        ):
            for provider in ("auto", "openai_compatible"):
                with self.subTest(provider=provider):
                    response = ai_assist._call_configured_chat(
                        {"mode": "local", "provider": provider, "base_url": "http://127.0.0.1:5000/v1"},
                        [{"role": "user", "content": "hello"}],
                    )
                    self.assertEqual(chat.call_args.kwargs["base_url"], "http://127.0.0.1:5000/v1")
                    self.assertEqual(response["provider"], "openai_compatible")

    def test_local_auto_prefers_a_running_runtime_over_an_installed_one(self) -> None:
        rows = [
            {"id": "ollama", "label": "Ollama", "available": False, "running": False, "installed": True, "base_url": "http://127.0.0.1:11434/v1"},
            {"id": "llama_cpp", "label": "llama.cpp", "available": True, "running": True, "installed": False, "base_url": "http://127.0.0.1:8080/v1"},
        ]
        with patch.object(ai_assist, "detect_local_providers", return_value=rows):
            chosen = ai_assist._selected_local_endpoint({"mode": "local", "provider": "auto"})
            stopped = ai_assist._selected_local_endpoint({"mode": "local", "provider": "ollama"})
        self.assertEqual(chosen["id"], "llama_cpp")
        self.assertIsNone(stopped)

    def test_installed_but_stopped_runtime_is_not_available(self) -> None:
        with (
            patch.object(ai_assist.shutil, "which", return_value="/usr/local/bin/ollama"),
            patch.object(ai_assist, "_port_open", return_value=False),
            patch.dict(ai_assist.os.environ, {}, clear=True),
        ):
            rows = ai_assist.detect_local_providers(max_age_seconds=0)
        ollama = next(row for row in rows if row["id"] == "ollama")
        self.assertTrue(ollama["installed"])
        self.assertFalse(ollama["available"])
        self.assertEqual(ollama["detail"], "installed, not running")

    def test_detection_is_memoised_between_polls(self) -> None:
        with (
            patch.object(ai_assist, "_port_open", return_value=False) as probe,
            patch.dict(ai_assist.os.environ, {"PATH": "/nonexistent"}, clear=True),
        ):
            ai_assist.detect_local_providers(max_age_seconds=0)
            ai_assist.detect_local_providers()
            ai_assist.detect_local_providers()
            self.assertEqual(probe.call_count, 3)  # one probe per port, once
            ai_assist.detect_local_providers(max_age_seconds=0)
            self.assertEqual(probe.call_count, 6)

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
        self.assertEqual(ready["status_label"], "Configured, not tested")
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
        self.assertEqual(openai["status_label"], "Configured, not tested")
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
