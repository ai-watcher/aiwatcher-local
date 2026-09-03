"""Optional AI Assist provider status for AIWatcher Local.

The OSS product must work without model calls. This module only describes what
is configured or locally available, so the UI can explain the tiered path before
any workflow spends tokens.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import urllib.error
import urllib.request
from typing import Any


LOCAL_PROVIDER_PORTS = {
    "lmstudio": ("LM Studio", "127.0.0.1", 1234, "http://127.0.0.1:1234/v1"),
    "llama_cpp": ("llama.cpp", "127.0.0.1", 8080, "http://127.0.0.1:8080/v1"),
    "ollama": ("Ollama", "127.0.0.1", 11434, "http://127.0.0.1:11434/v1"),
}

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-latest",
    "openai_compatible": "gpt-oss:20b",
    "ollama": "llama3.2",
    "lmstudio": "local-model",
    "llama_cpp": "local-model",
}

MAX_FRESH_START_INPUT_CHARS = 9000
MAX_FRESH_START_OUTPUT_TOKENS = 700


class AiAssistUnavailable(RuntimeError):
    """Raised when a workflow asks for AI Assist before it is ready."""


def _port_open(host: str, port: int, *, timeout: float = 0.06) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def detect_local_providers() -> list[dict[str, object]]:
    providers: list[dict[str, object]] = []
    for key, (label, host, port, base_url) in LOCAL_PROVIDER_PORTS.items():
        installed = bool(shutil.which("ollama")) if key == "ollama" else False
        running = _port_open(host, port)
        env_url = os.environ.get({
            "lmstudio": "LMSTUDIO_BASE_URL",
            "llama_cpp": "LLAMA_CPP_BASE_URL",
            "ollama": "OLLAMA_HOST",
        }[key])
        providers.append({
            "id": key,
            "label": label,
            "available": bool(running or installed or env_url),
            "running": running,
            "installed": installed,
            "base_url": env_url or base_url,
            "detail": (
                "running locally" if running else
                "installed, not running" if installed else
                "configured by environment" if env_url else
                "not detected"
            ),
        })
    providers.sort(key=lambda row: (not bool(row["available"]), str(row["label"])))
    return providers


def cloud_provider_status(stored_keys: dict[str, bool] | None = None) -> list[dict[str, object]]:
    stored = stored_keys or {}
    return [
        {
            "id": "openai",
            "label": "OpenAI",
            "available": bool(os.environ.get("OPENAI_API_KEY") or stored.get("openai")),
            "stored": bool(stored.get("openai")),
            "secret_env": "OPENAI_API_KEY",
            "detail": (
                "key saved locally" if stored.get("openai") else
                "key available in environment" if os.environ.get("OPENAI_API_KEY") else
                "paste key below or use OPENAI_API_KEY"
            ),
        },
        {
            "id": "anthropic",
            "label": "Claude",
            "available": bool(os.environ.get("ANTHROPIC_API_KEY") or stored.get("anthropic")),
            "stored": bool(stored.get("anthropic")),
            "secret_env": "ANTHROPIC_API_KEY",
            "detail": (
                "key saved locally" if stored.get("anthropic") else
                "key available in environment" if os.environ.get("ANTHROPIC_API_KEY") else
                "paste key below or use ANTHROPIC_API_KEY"
            ),
        },
        {
            "id": "openai_compatible",
            "label": "OpenAI-compatible",
            "available": bool(os.environ.get("AIWATCHER_AI_API_KEY") or stored.get("openai_compatible")),
            "stored": bool(stored.get("openai_compatible")),
            "secret_env": "AIWATCHER_AI_API_KEY",
            "detail": (
                "key saved locally"
                if stored.get("openai_compatible")
                else
                "key available in environment"
                if os.environ.get("AIWATCHER_AI_API_KEY")
                else "paste key below and add endpoint URL"
            ),
        },
    ]


def build_ai_assist_status(config: dict[str, Any]) -> dict[str, object]:
    mode = str(config.get("mode") or "off")
    provider = str(config.get("provider") or "none")
    configured_base_url = str(config.get("base_url") or "").strip()
    saved_key_values = config.get("api_keys") if isinstance(config.get("api_keys"), dict) else {}
    stored_keys = {
        "openai": bool(saved_key_values.get("openai")),
        "anthropic": bool(saved_key_values.get("anthropic")),
        "openai_compatible": bool(saved_key_values.get("openai_compatible")),
    }
    public_config = dict(config)
    public_config.pop("api_keys", None)
    public_config["stored_keys"] = stored_keys
    local = detect_local_providers()
    cloud = cloud_provider_status(stored_keys)
    def selected_available(rows: list[dict[str, object]], provider_ids: set[str]) -> bool:
        if provider in {"none", "auto"}:
            return any(row.get("available") for row in rows)
        if provider not in provider_ids:
            return False
        return any(row.get("id") == provider and row.get("available") for row in rows)

    local_ready = (
        bool(configured_base_url and provider in {"auto", "openai_compatible"})
        or selected_available(local, {"ollama", "lmstudio", "llama_cpp"})
    )
    custom_cloud_ready = (
        provider in {"auto", "openai_compatible"}
        and bool(configured_base_url)
        and bool(os.environ.get("AIWATCHER_AI_API_KEY") or stored_keys.get("openai_compatible"))
    )
    cloud_ready = custom_cloud_ready or selected_available(cloud, {"openai", "anthropic"})
    active_label = "Local rules only"
    if mode == "local":
        active_label = "Local AI Assist"
    elif mode == "cloud":
        active_label = "Cloud AI Assist"
    if mode == "off":
        status_label = "Recommended default"
        setup_hint = "No setup needed. AIWatcher uses local deterministic rules."
    elif mode == "local" and local_ready:
        status_label = "Ready"
        setup_hint = "Local model assist can be used for enabled workflows after confirmation."
    elif mode == "local":
        status_label = "Start or configure a local model"
        setup_hint = "Start Ollama, LM Studio, llama.cpp, or enter a local OpenAI-compatible base URL."
    elif mode == "cloud" and cloud_ready:
        status_label = "Ready"
        setup_hint = "Cloud Assist can run only after confirmation and within the daily cap."
    elif mode == "cloud" and provider == "openai_compatible" and not configured_base_url:
        status_label = "Base URL required"
        setup_hint = "Enter the custom endpoint URL and add its API key."
    elif mode == "cloud":
        status_label = "API key required"
        setup_hint = "Paste the selected provider key or start AIWatcher with it in the environment."
    else:
        status_label = "Provider not detected"
        setup_hint = "Choose a detected provider or return to local rules only."
    return {
        "config": public_config,
        "mode": mode,
        "provider": provider,
        "configured_base_url": configured_base_url or None,
        "active_label": active_label,
        "ready": (
            mode == "off"
            or (mode == "local" and local_ready)
            or (mode == "cloud" and cloud_ready)
        ),
        "status_label": status_label,
        "setup_hint": setup_hint,
        "local_providers": local,
        "cloud_providers": cloud,
        "stored_keys": stored_keys,
        "recommended_default": "Local rules only",
        "workflows": [
            {
                "id": "fresh_start",
                "label": "Fresh Start brief improvement",
                "priority": "first",
                "reason": "Highest chance to save more context than it spends.",
            },
            {
                "id": "prompt_plan",
                "label": "Prompt Plan rewrite",
                "priority": "second",
                "reason": "Useful when local rules can see risk but cannot understand the task deeply.",
            },
            {
                "id": "session_summary",
                "label": "Session evidence summary",
                "priority": "later",
                "reason": "Helpful after the core handoff loop is proven.",
            },
            {
                "id": "receipt_explanation",
                "label": "Receipt explanation",
                "priority": "later",
                "reason": "Polish only; receipts must remain evidence-backed without AI.",
            },
        ],
        "privacy": (
            "AI Assist is optional. Cloud keys can be saved locally or read from the environment. "
            "Source/prompt text requires explicit opt-in."
        ),
    }


def _selected_local_provider(config: dict[str, Any]) -> dict[str, object] | None:
    provider = str(config.get("provider") or "auto")
    rows = detect_local_providers()
    if provider == "auto":
        return next((row for row in rows if row.get("running") or row.get("available")), None)
    return next((row for row in rows if row.get("id") == provider), None)


def _secret_for_provider(config: dict[str, Any], provider: str) -> str:
    keys = config.get("api_keys") if isinstance(config.get("api_keys"), dict) else {}
    if provider == "openai":
        return str(keys.get("openai") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if provider == "anthropic":
        return str(keys.get("anthropic") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if provider == "openai_compatible":
        return str(keys.get("openai_compatible") or os.environ.get("AIWATCHER_AI_API_KEY") or "").strip()
    return ""


def _configured_model(config: dict[str, Any], provider: str) -> str:
    model = str(config.get("model") or os.environ.get("AIWATCHER_AI_MODEL") or "").strip()
    if model:
        return model[:120]
    return DEFAULT_MODELS.get(provider, "local-model")


def _join_url(base_url: str, suffix: str) -> str:
    return f"{base_url.rstrip('/')}/{suffix.lstrip('/')}"


def _post_json(url: str, payload: dict[str, object], headers: dict[str, str], *, timeout: float) -> dict[str, object]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AiAssistUnavailable(f"AI Assist provider returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise AiAssistUnavailable(f"AI Assist provider call failed: {exc}") from exc


def _openai_compatible_chat(
    *,
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    api_key: str = "",
    max_tokens: int = MAX_FRESH_START_OUTPUT_TOKENS,
    timeout: float = 35,
) -> dict[str, object]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = _post_json(
        _join_url(base_url, "/chat/completions"),
        {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        },
        headers,
        timeout=timeout,
    )
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = str(message.get("content") or "").strip()
    if not content:
        raise AiAssistUnavailable("AI Assist provider returned no text.")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {"text": content, "usage": usage}


def _anthropic_chat(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = MAX_FRESH_START_OUTPUT_TOKENS,
    timeout: float = 35,
) -> dict[str, object]:
    system = "\n\n".join(row["content"] for row in messages if row.get("role") == "system")
    user_messages = [{"role": row.get("role") or "user", "content": row.get("content") or ""} for row in messages if row.get("role") != "system"]
    data = _post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "system": system,
            "messages": user_messages,
        },
        {
            "Accept": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout=timeout,
    )
    parts = data.get("content") if isinstance(data.get("content"), list) else []
    text = "\n".join(str(part.get("text") or "") for part in parts if isinstance(part, dict)).strip()
    if not text:
        raise AiAssistUnavailable("AI Assist provider returned no text.")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {"text": text, "usage": usage}


def _call_configured_chat(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    max_tokens: int = MAX_FRESH_START_OUTPUT_TOKENS,
    timeout: float = 35,
) -> dict[str, object]:
    mode = str(config.get("mode") or "off")
    provider = str(config.get("provider") or "none")
    if mode == "off":
        raise AiAssistUnavailable("AI Assist is set to Local rules only.")
    if mode == "local":
        local = _selected_local_provider(config)
        if not local:
            raise AiAssistUnavailable("No local AI runtime is detected.")
        provider = str(local.get("id") or "openai_compatible")
        base_url = str(config.get("base_url") or local.get("base_url") or "").strip()
        if not base_url:
            raise AiAssistUnavailable("Local AI Assist needs a local OpenAI-compatible endpoint.")
        model = _configured_model(config, provider)
        response = _openai_compatible_chat(
            base_url=base_url,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        return {**response, "provider": provider, "model": model, "mode": mode}
    if mode != "cloud":
        raise AiAssistUnavailable("Unsupported AI Assist mode.")
    if provider == "auto":
        for candidate in ("openai", "anthropic", "openai_compatible"):
            if _secret_for_provider(config, candidate):
                provider = candidate
                break
    if provider == "openai":
        key = _secret_for_provider(config, "openai")
        if not key:
            raise AiAssistUnavailable("OpenAI API key is not configured.")
        model = _configured_model(config, "openai")
        response = _openai_compatible_chat(
            base_url="https://api.openai.com/v1",
            model=model,
            messages=messages,
            api_key=key,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        return {**response, "provider": "openai", "model": model, "mode": mode}
    if provider == "anthropic":
        key = _secret_for_provider(config, "anthropic")
        if not key:
            raise AiAssistUnavailable("Claude API key is not configured.")
        model = _configured_model(config, "anthropic")
        response = _anthropic_chat(api_key=key, model=model, messages=messages, max_tokens=max_tokens, timeout=timeout)
        return {**response, "provider": "anthropic", "model": model, "mode": mode}
    if provider == "openai_compatible":
        key = _secret_for_provider(config, "openai_compatible")
        base_url = str(config.get("base_url") or "").strip()
        if not base_url:
            raise AiAssistUnavailable("Custom endpoint URL is required.")
        model = _configured_model(config, "openai_compatible")
        response = _openai_compatible_chat(
            base_url=base_url,
            model=model,
            messages=messages,
            api_key=key,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        return {**response, "provider": "openai_compatible", "model": model, "mode": mode}
    raise AiAssistUnavailable("Choose OpenAI, Claude, Custom endpoint, or a local runtime.")


def improve_fresh_start_brief(
    config: dict[str, Any],
    *,
    local_brief: str,
    timeout: float = 35,
) -> dict[str, object]:
    """Return a bounded AI-written refinement for a Fresh Start handoff.

    Deterministic evidence remains in the original brief. The model gets only
    the already-generated metadata handoff and writes one small inferred block
    that the UI appends below it after an explicit user action.
    """
    normalized_brief = str(local_brief or "").strip()
    if not normalized_brief:
        raise AiAssistUnavailable("Fresh Start brief is empty.")
    workflows = config.get("enabled_workflows")
    if isinstance(workflows, list) and "fresh_start" not in workflows:
        raise AiAssistUnavailable("Fresh Start AI Assist is disabled in settings.")
    status = build_ai_assist_status(config)
    if not status.get("ready") or status.get("mode") == "off":
        raise AiAssistUnavailable(str(status.get("setup_hint") or "AI Assist is not ready."))
    trimmed = normalized_brief[:MAX_FRESH_START_INPUT_CHARS]
    messages = [
        {
            "role": "system",
            "content": (
                "You improve AIWatcher Fresh Start handoffs. Preserve all deterministic evidence, "
                "session identity, token/cost claims, and privacy boundaries exactly. Do not invent "
                "saved tokens, commits, tests, files, or outcomes. Write concise bullets only."
            ),
        },
        {
            "role": "user",
            "content": (
                "Rewrite only the human guidance as a short inferred refinement for the next AI session. "
                "Use this exact format and no preamble:\n\n"
                "AI Assist refinement\n"
                "- Likely objective: ...\n"
                "- Inspect first: ...\n"
                "- Smallest next checkpoint: ...\n"
                "- Ask the user if: ...\n"
                "- Acceptance check: ...\n\n"
                "Local Fresh Start brief:\n"
                f"{trimmed}"
            ),
        },
    ]
    response = _call_configured_chat(
        config,
        messages,
        max_tokens=MAX_FRESH_START_OUTPUT_TOKENS,
        timeout=timeout,
    )
    text = str(response.get("text") or "").strip()
    if "AI Assist refinement" not in text.splitlines()[0:2]:
        text = "AI Assist refinement\n" + text
    return {
        "workflow": "fresh_start",
        "status": "used",
        "mode": response.get("mode"),
        "provider": response.get("provider"),
        "model": response.get("model"),
        "input_chars": len(trimmed),
        "output_chars": len(text),
        "source_access": config.get("source_access") or "metadata_only",
        "text": text[:3000],
        "usage": response.get("usage") if isinstance(response.get("usage"), dict) else {},
    }
