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
    "claude-opus-5": {"in": 5.00, "out": 25.00, "subscription": False},
    "claude-mythos-5": {"in": 10.00, "out": 50.00, "subscription": False},
    "gpt-4o": {"in": 2.50, "out": 10.00, "subscription": False},
    "gpt-4o-mini": {"in": 0.15, "out": 0.60, "subscription": False},
    "gpt-5.5": {"in": 0.0, "out": 0.0, "subscription": True},
    "gpt-5.2-codex": {"in": 0.0, "out": 0.0, "subscription": True},
    "gpt-5.3-codex": {"in": 0.0, "out": 0.0, "subscription": True},
    "gpt-5.6-terra": {"in": 0.0, "out": 0.0, "subscription": True},
}

# Prompt-cache rates, as multiples of a model's base input price. Cached reads
# are heavily discounted but they are NOT free, and a long session replays its
# whole history every turn -- so for anything but a trivial session these are
# most of the real bill. Writes are charged at a premium instead of a discount
# because the tokens are being stored as well as processed; the 1h TTL costs
# more than the 5m one for the same reason.
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.00
CACHE_READ_MULTIPLIER = 0.10


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


def estimate_cost(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    *,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    cache_read: int = 0,
) -> float:
    """Estimated API-equivalent cost for one turn or session.

    The cache arguments are keyword-only and default to zero so existing
    callers that only know about plain input/output keep working unchanged.
    Pass them wherever the source log reports cache buckets *separately from*
    `tokens_in` -- Anthropic's format does, and omitting them undercounts a
    cached session's real cost by roughly an order of magnitude.

    Sources whose input count already includes cached tokens (Codex reports
    `input_tokens` inclusive of `cached_input_tokens`) must NOT also pass them
    here, or the same tokens get billed twice.
    """
    pricing = lookup(model)
    if not pricing or pricing.get("subscription"):
        return 0.0
    price_in = float(pricing["in"])
    return (
        tokens_in * price_in +
        tokens_out * float(pricing["out"]) +
        cache_write_5m * price_in * CACHE_WRITE_5M_MULTIPLIER +
        cache_write_1h * price_in * CACHE_WRITE_1H_MULTIPLIER +
        cache_read * price_in * CACHE_READ_MULTIPLIER
    ) / 1_000_000


def is_subscription_model(model: str | None) -> bool:
    return bool(lookup(model) and lookup(model).get("subscription"))
