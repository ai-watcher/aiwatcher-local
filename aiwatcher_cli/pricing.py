"""Local pricing helpers for AIWatcher Local.

Prices are best-effort estimates used for personal visibility. Subscription-only
tools intentionally return zero API cost and are labeled by the CLI.
"""

from __future__ import annotations


MODEL_PRICING: dict[str, dict[str, float | bool]] = {
    # Sonnet 5 has a $2.00/$10.00 intro rate through 2026-08-31; using the
    # standard post-intro rate here so this table doesn't silently under-price
    # once the intro window closes -- nothing in this file is date-aware.
    "claude-sonnet-5": {"in": 3.00, "out": 15.00, "subscription": False},
    "claude-sonnet-4-20250514": {"in": 3.00, "out": 15.00, "subscription": False},
    "claude-sonnet-4-5-20250514": {"in": 3.00, "out": 15.00, "subscription": False},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00, "subscription": False},
    "claude-sonnet-4-5": {"in": 3.00, "out": 15.00, "subscription": False},
    "claude-fable-5": {"in": 10.00, "out": 50.00, "subscription": False},
    "claude-opus-4-8": {"in": 5.00, "out": 25.00, "subscription": False},
    "claude-opus-4-20250514": {"in": 5.00, "out": 25.00, "subscription": False},
    "claude-opus-4-7": {"in": 5.00, "out": 25.00, "subscription": False},
    "claude-opus-4-6": {"in": 5.00, "out": 25.00, "subscription": False},
    "claude-opus-4": {"in": 5.00, "out": 25.00, "subscription": False},
    "claude-haiku-4-5-20251001": {"in": 0.80, "out": 4.00, "subscription": False},
    "claude-haiku-4-5": {"in": 0.80, "out": 4.00, "subscription": False},
    "gpt-4o": {"in": 2.50, "out": 10.00, "subscription": False},
    "gpt-4o-mini": {"in": 0.15, "out": 0.60, "subscription": False},
    "gpt-5.5": {"in": 0.0, "out": 0.0, "subscription": True},
    "gpt-5.2-codex": {"in": 0.0, "out": 0.0, "subscription": True},
    "gpt-5.3-codex": {"in": 0.0, "out": 0.0, "subscription": True},
}


def lookup(model: str | None) -> dict[str, float | bool] | None:
    if not model:
        return None
    normalized = model.lower()
    if normalized in MODEL_PRICING:
        return MODEL_PRICING[normalized]
    for key, pricing in MODEL_PRICING.items():
        if normalized.startswith(key):
            return pricing
    return None


def estimate_cost(model: str | None, tokens_in: int, tokens_out: int) -> float:
    pricing = lookup(model)
    if not pricing or pricing.get("subscription"):
        return 0.0
    return (
        tokens_in * float(pricing["in"]) +
        tokens_out * float(pricing["out"])
    ) / 1_000_000


def is_subscription_model(model: str | None) -> bool:
    return bool(lookup(model) and lookup(model).get("subscription"))
