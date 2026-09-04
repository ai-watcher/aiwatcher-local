"""Optional AI Assist provider status for AIWatcher Local.

The OSS product must work without model calls. This module only describes what
is configured or locally available, so the UI can explain the tiered path before
any workflow spends tokens.
"""

from __future__ import annotations

import os
import shutil
import socket
from typing import Any


LOCAL_PROVIDER_PORTS = {
    "lmstudio": ("LM Studio", "127.0.0.1", 1234, "http://127.0.0.1:1234/v1"),
    "llama_cpp": ("llama.cpp", "127.0.0.1", 8080, "http://127.0.0.1:8080/v1"),
    "ollama": ("Ollama", "127.0.0.1", 11434, "http://127.0.0.1:11434"),
}


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


def cloud_provider_status() -> list[dict[str, object]]:
    return [
        {
            "id": "openai",
            "label": "OpenAI",
            "available": bool(os.environ.get("OPENAI_API_KEY")),
            "secret_env": "OPENAI_API_KEY",
            "detail": "key available in environment" if os.environ.get("OPENAI_API_KEY") else "no key in environment",
        },
        {
            "id": "anthropic",
            "label": "Anthropic",
            "available": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "secret_env": "ANTHROPIC_API_KEY",
            "detail": "key available in environment" if os.environ.get("ANTHROPIC_API_KEY") else "no key in environment",
        },
    ]


def build_ai_assist_status(config: dict[str, Any]) -> dict[str, object]:
    mode = str(config.get("mode") or "off")
    provider = str(config.get("provider") or "none")
    local = detect_local_providers()
    cloud = cloud_provider_status()
    def selected_available(rows: list[dict[str, object]], local_ids: set[str]) -> bool:
        if provider in {"none", "auto"}:
            return any(row.get("available") for row in rows)
        if provider not in local_ids:
            return False
        return any(row.get("id") == provider and row.get("available") for row in rows)

    local_ready = selected_available(local, {"ollama", "lmstudio", "llama_cpp"})
    cloud_ready = selected_available(cloud, {"openai", "anthropic"})
    active_label = "Local rules only"
    if mode == "local":
        active_label = "Local AI Assist"
    elif mode == "cloud":
        active_label = "Cloud AI Assist"
    return {
        "config": config,
        "mode": mode,
        "provider": provider,
        "active_label": active_label,
        "ready": (
            mode == "off"
            or (mode == "local" and local_ready)
            or (mode == "cloud" and cloud_ready)
        ),
        "status_label": (
            "Off by default"
            if mode == "off" else
            "Ready" if (mode == "local" and local_ready) or (mode == "cloud" and cloud_ready) else
            "Provider not detected"
        ),
        "local_providers": local,
        "cloud_providers": cloud,
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
            "AI Assist is optional. Secrets are read from the environment, not stored in AIWatcher state. "
            "Source/prompt text requires explicit opt-in."
        ),
    }
