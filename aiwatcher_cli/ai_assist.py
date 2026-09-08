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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


LOCAL_PROVIDER_PORTS = {
    "lmstudio": ("LM Studio", "127.0.0.1", 1234, "http://127.0.0.1:1234/v1"),
    "llama_cpp": ("llama.cpp", "127.0.0.1", 8080, "http://127.0.0.1:8080/v1"),
    "ollama": ("Ollama", "127.0.0.1", 11434, "http://127.0.0.1:11434/v1"),
}

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "openai_compatible": "gpt-oss:20b",
    "ollama": "llama3.2",
    "lmstudio": "local-model",
    "llama_cpp": "local-model",
}

MAX_FRESH_START_INPUT_CHARS = 9000
MAX_FRESH_START_OUTPUT_TOKENS = 700
MAX_FRESH_START_BRIEF_CHARS = 6000
MAX_OPTIMIZE_CLEANUP_INPUT_CHARS = 7000
MAX_OPTIMIZE_CLEANUP_OUTPUT_TOKENS = 600
MAX_OPTIMIZE_CLEANUP_PROMPT_CHARS = 7000


class AiAssistUnavailable(RuntimeError):
    """Raised when a workflow asks for AI Assist before it is ready."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider_code: str | None = None,
        provider: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code
        # The concrete provider that answered (or refused). Set by
        # _call_configured_chat so an "auto" config can still record which
        # key was rejected.
        self.provider = provider


def _port_open(host: str, port: int, *, timeout: float = 0.06) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


LOCAL_PROVIDER_ENV = {
    "lmstudio": "LMSTUDIO_BASE_URL",
    "llama_cpp": "LLAMA_CPP_BASE_URL",
    "ollama": "OLLAMA_HOST",
}

# Detection opens three loopback sockets and walks PATH. The dashboard asks
# for it on every poll, so the answer is kept for a short while; a runtime that
# starts or stops shows up within this window.
LOCAL_PROVIDER_CACHE_SECONDS = 20.0
_LOCAL_PROVIDER_CACHE: tuple[float, tuple[str, ...], list[dict[str, object]]] | None = None


def _local_provider_env_signature() -> tuple[str, ...]:
    return tuple(os.environ.get(name, "") for name in (*LOCAL_PROVIDER_ENV.values(), "PATH"))


def _probe_local_providers() -> list[dict[str, object]]:
    providers: list[dict[str, object]] = []
    for key, (label, host, port, base_url) in LOCAL_PROVIDER_PORTS.items():
        installed = bool(shutil.which("ollama")) if key == "ollama" else False
        running = _port_open(host, port)
        env_url = os.environ.get(LOCAL_PROVIDER_ENV[key])
        providers.append({
            "id": key,
            "label": label,
            # "available" means a call can be made now: the port answers, or
            # the user pointed at it by environment. Installed-but-stopped is
            # reported separately; counting it as available made Settings say
            # Ready and then the call was refused.
            "available": bool(running or env_url),
            "running": running,
            "installed": installed,
            "base_url": env_url or base_url,
            "detail": (
                "running locally" if running else
                "configured by environment" if env_url else
                "installed, not running" if installed else
                "not detected"
            ),
        })
    providers.sort(key=lambda row: (not bool(row["running"]), not bool(row["available"]), str(row["label"])))
    return providers


def detect_local_providers(*, max_age_seconds: float = LOCAL_PROVIDER_CACHE_SECONDS) -> list[dict[str, object]]:
    """Return the local runtimes AIWatcher can see, memoised briefly.

    Pass max_age_seconds=0 to force a fresh probe.
    """
    global _LOCAL_PROVIDER_CACHE
    now = time.monotonic()
    signature = _local_provider_env_signature()
    cached = _LOCAL_PROVIDER_CACHE
    if cached and max_age_seconds > 0 and cached[1] == signature and now - cached[0] <= max_age_seconds:
        return [dict(row) for row in cached[2]]
    rows = _probe_local_providers()
    _LOCAL_PROVIDER_CACHE = (now, signature, rows)
    return [dict(row) for row in rows]


def _provider_check(provider: str, checks: dict[str, object] | None) -> dict[str, str]:
    row = (checks or {}).get(provider)
    if not isinstance(row, dict):
        return {}
    return {
        "status": str(row.get("status") or "").strip().lower(),
        "checked_at": str(row.get("checked_at") or "").strip(),
        "message": str(row.get("message") or "").strip(),
        "code": str(row.get("code") or "").strip(),
    }


def _cloud_row(
    *,
    provider: str,
    label: str,
    secret_env: str,
    stored: bool,
    env_present: bool,
    setup_detail: str,
    checks: dict[str, object] | None,
) -> dict[str, object]:
    check = _provider_check(provider, checks)
    check_status = check.get("status") or ("untested" if stored or env_present else "missing")
    configured = bool(stored or env_present)
    rejected = check_status == "failed"
    if rejected:
        detail = "key rejected; paste a replacement"
    elif check_status == "verified":
        detail = "key verified"
    elif stored:
        detail = "key saved locally, not tested yet"
    elif env_present:
        detail = f"key available in {secret_env}, not tested yet"
    else:
        detail = setup_detail
    return {
        "id": provider,
        "label": label,
        "available": bool(configured and not rejected),
        "configured": configured,
        "stored": stored,
        "verified": check_status == "verified",
        "check_status": check_status,
        "check_message": check.get("message") or "",
        "secret_env": secret_env,
        "detail": detail,
    }


def cloud_provider_status(
    stored_keys: dict[str, bool] | None = None,
    provider_checks: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    stored = stored_keys or {}
    return [
        _cloud_row(
            provider="openai",
            label="OpenAI",
            secret_env="OPENAI_API_KEY",
            stored=bool(stored.get("openai")),
            env_present=bool(os.environ.get("OPENAI_API_KEY")),
            setup_detail="paste key below or use OPENAI_API_KEY",
            checks=provider_checks,
        ),
        _cloud_row(
            provider="anthropic",
            label="Claude",
            secret_env="ANTHROPIC_API_KEY",
            stored=bool(stored.get("anthropic")),
            env_present=bool(os.environ.get("ANTHROPIC_API_KEY")),
            setup_detail="paste key below or use ANTHROPIC_API_KEY",
            checks=provider_checks,
        ),
        _cloud_row(
            provider="openai_compatible",
            label="OpenAI-compatible",
            secret_env="AIWATCHER_AI_API_KEY",
            stored=bool(stored.get("openai_compatible")),
            env_present=bool(os.environ.get("AIWATCHER_AI_API_KEY")),
            setup_detail="paste key below and add endpoint URL",
            checks=provider_checks,
        ),
    ]


def build_ai_assist_status(config: dict[str, Any]) -> dict[str, object]:
    mode = str(config.get("mode") or "off")
    provider = str(config.get("provider") or "none")
    configured_base_url = str(config.get("base_url") or "").strip()
    if isinstance(config.get("api_keys"), dict):
        saved_key_values = config["api_keys"]
    elif isinstance(config.get("stored_keys"), dict):
        # The redacted view from local_state.ai_assist_config() carries only
        # the booleans; raw keys never need to reach this function.
        saved_key_values = config["stored_keys"]
    else:
        saved_key_values = {}
    stored_keys = {
        "openai": bool(saved_key_values.get("openai")),
        "anthropic": bool(saved_key_values.get("anthropic")),
        "openai_compatible": bool(saved_key_values.get("openai_compatible")),
    }
    public_config = dict(config)
    public_config.pop("api_keys", None)
    public_config["stored_keys"] = stored_keys
    provider_checks = config.get("provider_checks") if isinstance(config.get("provider_checks"), dict) else {}
    public_config["provider_checks"] = provider_checks
    local = detect_local_providers()
    cloud = cloud_provider_status(stored_keys, provider_checks)
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
        and _provider_check("openai_compatible", provider_checks).get("status") != "failed"
    )
    cloud_ready = custom_cloud_ready or selected_available(cloud, {"openai", "anthropic"})
    selected_cloud = next((row for row in cloud if row.get("id") == provider), None)
    if provider == "auto":
        # Mirror _call_configured_chat, which tries providers in this order and
        # uses the first one with a key; otherwise a rejected key under "auto"
        # could never surface as "Key rejected".
        selected_cloud = next((row for row in cloud if row.get("configured")), None)
    selected_check_status = str((selected_cloud or {}).get("check_status") or "")
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
    elif mode == "cloud" and selected_check_status == "failed":
        status_label = "Key rejected"
        setup_hint = "The provider rejected this key. Paste a replacement in Settings -> AI Assist."
    elif mode == "cloud" and cloud_ready and selected_check_status == "verified":
        status_label = "Ready"
        setup_hint = "Cloud Assist can run only after confirmation and within the daily cap."
    elif mode == "cloud" and cloud_ready:
        status_label = "Configured, not tested"
        setup_hint = "AIWatcher will test this key on the next confirmed AI Assist run."
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
                "id": "optimize_cleanup",
                "label": "Optimize cleanup prompt",
                "priority": "second",
                "reason": "Turns stale chat, worktree, and runtime evidence into a safe review prompt.",
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


def _selected_local_endpoint(config: dict[str, Any]) -> dict[str, object] | None:
    """Return the local runtime a call should go to, or None.

    Mirrors the readiness rule in build_ai_assist_status: a typed base URL
    wins for "auto" and the custom provider, otherwise a running runtime is
    preferred over one that is merely configured, and an installed-but-stopped
    one is never chosen.
    """
    provider = str(config.get("provider") or "auto")
    configured_base_url = str(config.get("base_url") or "").strip()
    if configured_base_url and provider in {"auto", "openai_compatible"}:
        return {"id": "openai_compatible", "label": "Local endpoint", "base_url": configured_base_url}
    rows = detect_local_providers()
    if provider == "auto":
        running = next((row for row in rows if row.get("running")), None)
        return running or next((row for row in rows if row.get("available")), None)
    row = next((row for row in rows if row.get("id") == provider), None)
    if row is None or not row.get("available"):
        return None
    if configured_base_url:
        return {**row, "base_url": configured_base_url}
    return row


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
    """POST JSON and return the decoded object.

    Every failure surfaces as AiAssistUnavailable so the HTTP handler above
    can record a receipt and answer the browser. That includes a base URL
    urllib cannot parse (ValueError at Request construction), a body that is
    not UTF-8 (UnicodeDecodeError is a ValueError, not a JSONDecodeError),
    and a body that decodes to something other than an object.
    """
    body = json.dumps(payload).encode("utf-8")
    try:
        request = urllib.request.Request(
            url,
            data=body,
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1500]
        message, provider_code = _safe_provider_error(exc.code, detail)
        raise AiAssistUnavailable(message, status_code=exc.code, provider_code=provider_code) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise AiAssistUnavailable(f"AI Assist provider call failed: {exc}") from exc
    if not isinstance(data, dict):
        raise AiAssistUnavailable(
            f"AI Assist provider returned {type(data).__name__} instead of a JSON object."
        )
    return data


def _safe_provider_error(status_code: int, detail: str) -> tuple[str, str | None]:
    provider_code = None
    message = ""
    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        parsed = {}
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            provider_code = str(error.get("code") or error.get("type") or "").strip() or None
            message = str(error.get("message") or "").strip()
    if status_code in {401, 403}:
        return (
            f"AI Assist provider rejected the API key or credentials (HTTP {status_code}). "
            "Paste a valid replacement key in Settings -> AI Assist, then try again.",
            provider_code,
        )
    if provider_code:
        return f"AI Assist provider returned HTTP {status_code} ({provider_code}).", provider_code
    return f"AI Assist provider returned HTTP {status_code}.", provider_code


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
        local = _selected_local_endpoint(config)
        if not local:
            raise AiAssistUnavailable(
                "No local AI runtime is running. Start Ollama, LM Studio, or llama.cpp, "
                "or enter a local OpenAI-compatible base URL in Settings -> AI Assist."
            )
        provider = str(local.get("id") or "openai_compatible")
        base_url = str(local.get("base_url") or "").strip()
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
    try:
        return _cloud_chat(config, provider, messages, max_tokens=max_tokens, timeout=timeout)
    except AiAssistUnavailable as exc:
        if exc.provider is None and provider in {"openai", "anthropic", "openai_compatible"}:
            exc.provider = provider
        raise


def _cloud_chat(
    config: dict[str, Any],
    provider: str,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    timeout: float,
) -> dict[str, object]:
    mode = "cloud"
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


def _json_object_from_text(text: str) -> dict[str, object] | None:
    value = str(text or "").strip()
    if not value:
        return None
    if value.startswith("```"):
        value = value.strip("`")
        if value.lower().startswith("json"):
            value = value[4:].strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(value[start:end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _clean_line(value: object, *, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()


def _clean_list(value: object, *, limit: int = 6) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str) and value.strip():
        items = [value]
    else:
        items = []
    cleaned: list[str] = []
    for item in items:
        line = _clean_line(item)
        if line:
            cleaned.append(line)
        if len(cleaned) >= limit:
            break
    return cleaned


def _section(title: str, lines: list[str]) -> list[str]:
    if not lines:
        return []
    return ["", title, *[f"- {line}" for line in lines]]


def _structured_handoff_text(parsed: dict[str, object]) -> str:
    goal = _clean_line(parsed.get("goal"), limit=360)
    next_ask = _clean_line(parsed.get("next_ask"), limit=420)
    what_done = _clean_list(parsed.get("what_is_done") or parsed.get("done"), limit=7)
    context = _clean_list(parsed.get("context_to_preserve") or parsed.get("context"), limit=7)
    inspect = _clean_list(parsed.get("inspect_first"), limit=7)
    avoid = _clean_list(parsed.get("do_not_redo") or parsed.get("avoid"), limit=5)
    uncertainties = _clean_list(parsed.get("uncertainties"), limit=5)
    acceptance = _clean_list(parsed.get("acceptance_check") or parsed.get("acceptance"), limit=5)

    lines = [
        "AIWatcher AI-assisted Fresh Start brief",
        "",
        "You are starting a fresh AI work session from an AIWatcher handoff.",
        "Do not assume access to the previous chat, hidden memory, or unstated decisions.",
        "Continue from the repository/workspace state and AIWatcher evidence below.",
        "",
        "Goal",
        f"- {goal or 'Continue the same user goal from the source workspace after verifying the evidence.'}",
        *_section("What appears done", what_done or ["Reconstruct the prior work from the changed files, recent commits, and source-session evidence before editing."]),
        *_section("Context to preserve", context or ["Preserve the source workspace, constraints, and small next checkpoint rather than replaying the whole prior chat."]),
        *_section("Inspect first", inspect or ["Run `git status --short` and inspect the changed files or source-of-truth docs listed in the handoff evidence."]),
        *_section("Do not redo", avoid or ["Do not repeat broad discovery from the bloated session unless the evidence is insufficient."]),
        "",
        "Next ask",
        f"- {next_ask or 'State what appears done, what remains uncertain, and the smallest safe checkpoint before editing.'}",
        *_section("Acceptance check", acceptance or ["Report changed files, verification run, remaining uncertainty, and whether the result looks useful."]),
        *_section("Uncertainty to verify", uncertainties),
        "",
        "Guardrails",
        "- Preserve unrelated changes.",
        "- Do not expose secrets.",
        "- Stop before destructive changes, force pushes, broad refactors, production writes, or unrelated cleanup.",
    ]
    return "\n".join(lines).strip()


def _structured_optimize_cleanup_text(parsed: dict[str, object], *, local_prompt: str) -> str:
    safe = _clean_list(parsed.get("safe_to_archive_or_review") or parsed.get("safe_to_review"), limit=6)
    keep = _clean_list(parsed.get("keep_active"), limit=6)
    unknown = _clean_list(parsed.get("unknown"), limit=6)
    next_action = _clean_list(parsed.get("next_action"), limit=5)
    guardrails = _clean_list(parsed.get("guardrails"), limit=6)
    lines = [
        "AIWatcher AI-assisted Optimize cleanup prompt",
        "",
        "Use this in a focused review session. The job is to classify local AI work safely, not to clean it up automatically.",
        "Do not delete files, kill processes, archive chats, or rewrite history from this prompt.",
        *_section("Safe to archive/review", safe or ["Only mark something safe after verifying it in the owning AI app, git worktree, or runtime tool."]),
        *_section("Keep active", keep or ["Keep any session, worktree, or process that may still be connected to live work."]),
        *_section("Unknown", unknown or ["Treat missing identity, stale metadata, and ambiguous ownership as unknown until verified."]),
        *_section("Next action", next_action or ["Review the candidate below, choose one bucket, and report the evidence for that choice without performing cleanup."]),
        *_section("Guardrails", guardrails or [
            "Do not delete files or folders.",
            "Do not kill processes.",
            "Do not archive chats or sessions automatically.",
            "Ask before any destructive or irreversible action.",
        ]),
        "",
        "Local evidence to verify",
        str(local_prompt or "").strip()[:MAX_OPTIMIZE_CLEANUP_PROMPT_CHARS],
    ]
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class _WorkflowSpec:
    """Everything that differs between the model-backed workflows.

    The call itself, the readiness gate, the JSON parse, and the bounded
    result dict are shared; a third workflow is a new entry here rather than
    a third copy of that pipeline.
    """

    id: str
    label: str
    max_input_chars: int
    max_output_tokens: int
    max_result_chars: int
    system_prompt: str
    instructions: str
    evidence_heading: str
    # Turns the parsed JSON (or the raw text under fallback_key) into the
    # paste-ready result; receives the trimmed local text for workflows that
    # quote it back.
    structure: Callable[[dict[str, object], str], str]
    fallback_key: str


_FRESH_START_SPEC = _WorkflowSpec(
    id="fresh_start",
    label="Fresh Start",
    max_input_chars=MAX_FRESH_START_INPUT_CHARS,
    max_output_tokens=MAX_FRESH_START_OUTPUT_TOKENS,
    max_result_chars=MAX_FRESH_START_BRIEF_CHARS,
    system_prompt=(
        "You are AIWatcher's Fresh Start handoff composer. Your job is to turn local handoff "
        "evidence into a useful continuation prompt for a new AI work session. Be concrete, "
        "specific, and operational: extract the likely work done, user intent when it is actually "
        "present in the evidence, context worth preserving, files or commands to inspect first, "
        "what the next agent should avoid redoing, and the smallest next ask. "
        "Preserve deterministic evidence boundaries: do not invent saved tokens, commits, tests, "
        "files, outcomes, exact chat links, secrets, or prior conversation content. If prompt text "
        "or transcript content is not present, say the task must be reconstructed from repo state "
        "and local evidence. Prefer specific evidence from the handoff over generic advice. Every "
        "useful bullet should carry a concrete path, file, session id, count, decision, command, "
        "or explicit uncertainty from the evidence when one exists."
    ),
    instructions=(
        "Return JSON only with these keys:\n"
        "goal: string\n"
        "what_is_done: string[]\n"
        "context_to_preserve: string[]\n"
        "inspect_first: string[]\n"
        "do_not_redo: string[]\n"
        "next_ask: string\n"
        "acceptance_check: string[]\n"
        "uncertainties: string[]\n\n"
        "Make the result useful for a fresh chat, forked chat, or subagent. The next_ask should "
        "tell the new AI session exactly what to do first. Avoid echoing the section names and "
        "boilerplate from the local handoff unless the evidence is genuinely missing. Keep it short "
        "enough to paste without carrying the whole old conversation. Do not use vague goals like "
        "\"reconstruct the current work\" unless no stronger objective is present; tie the goal to "
        "the observed workspace/tool/path/evidence instead."
    ),
    evidence_heading="Local AIWatcher handoff evidence:",
    structure=lambda parsed, _trimmed: _structured_handoff_text(parsed),
    fallback_key="next_ask",
)

_OPTIMIZE_CLEANUP_SPEC = _WorkflowSpec(
    id="optimize_cleanup",
    label="Optimize cleanup",
    max_input_chars=MAX_OPTIMIZE_CLEANUP_INPUT_CHARS,
    max_output_tokens=MAX_OPTIMIZE_CLEANUP_OUTPUT_TOKENS,
    max_result_chars=MAX_OPTIMIZE_CLEANUP_PROMPT_CHARS,
    system_prompt=(
        "You are AIWatcher's Optimize cleanup prompt composer. Create a compact, paste-ready "
        "review prompt for stale AI chats, worktrees, or runtimes. Preserve deterministic evidence "
        "boundaries: do not invent paths, sessions, costs, outcomes, or source text. Never authorize "
        "deleting files, killing processes, archiving chats, force pushing, or other destructive cleanup."
    ),
    instructions=(
        "Return JSON only with these keys:\n"
        "safe_to_archive_or_review: string[]\n"
        "keep_active: string[]\n"
        "unknown: string[]\n"
        "next_action: string[]\n"
        "guardrails: string[]\n\n"
        "The final prompt must help another AI session classify the candidate into those buckets, "
        "but the AI session must only recommend; the user performs any action later in the owning app/tool. "
        "Keep this short and concrete."
    ),
    evidence_heading="Local AIWatcher cleanup evidence:",
    structure=lambda parsed, trimmed: _structured_optimize_cleanup_text(parsed, local_prompt=trimmed),
    fallback_key="next_action",
)


def _compose(
    config: dict[str, Any],
    spec: _WorkflowSpec,
    local_text: str,
    *,
    timeout: float,
) -> dict[str, object]:
    normalized = str(local_text or "").strip()
    if not normalized:
        raise AiAssistUnavailable(f"{spec.label} evidence is empty.")
    workflows = config.get("enabled_workflows")
    if isinstance(workflows, list) and spec.id not in workflows:
        raise AiAssistUnavailable(f"{spec.label} AI Assist is disabled in settings.")
    status = build_ai_assist_status(config)
    if not status.get("ready") or status.get("mode") == "off":
        raise AiAssistUnavailable(str(status.get("setup_hint") or "AI Assist is not ready."))
    trimmed = normalized[:spec.max_input_chars]
    messages = [
        {"role": "system", "content": spec.system_prompt},
        {"role": "user", "content": f"{spec.instructions}\n\n{spec.evidence_heading}\n{trimmed}"},
    ]
    response = _call_configured_chat(config, messages, max_tokens=spec.max_output_tokens, timeout=timeout)
    text = str(response.get("text") or "").strip()
    parsed = _json_object_from_text(text)
    final_text = spec.structure(parsed if parsed else {spec.fallback_key: text}, trimmed)
    return {
        "workflow": spec.id,
        "status": "used",
        "mode": response.get("mode"),
        "provider": response.get("provider"),
        "model": response.get("model"),
        "input_chars": len(trimmed),
        "output_chars": len(final_text),
        "source_access": config.get("source_access") or "metadata_only",
        "text": final_text[:spec.max_result_chars],
        "structured": parsed or {},
        "usage": response.get("usage") if isinstance(response.get("usage"), dict) else {},
    }


def improve_fresh_start_brief(
    config: dict[str, Any],
    *,
    local_brief: str,
    timeout: float = 35,
) -> dict[str, object]:
    """Return a bounded AI-composed Fresh Start handoff.

    Deterministic evidence remains authoritative. The model gets only the
    already-generated handoff and composes a clearer paste-ready continuation
    brief after an explicit user action.
    """
    return _compose(config, _FRESH_START_SPEC, local_brief, timeout=timeout)


def compose_optimize_cleanup_prompt(
    config: dict[str, Any],
    *,
    local_prompt: str,
    timeout: float = 35,
) -> dict[str, object]:
    """Return a bounded AI-composed Optimize cleanup review prompt.

    The deterministic local prompt remains the evidence boundary. The model is
    only allowed to make the review more useful; it cannot authorize cleanup.
    """
    return _compose(config, _OPTIMIZE_CLEANUP_SPEC, local_prompt, timeout=timeout)
