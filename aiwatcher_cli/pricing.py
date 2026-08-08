"""Local pricing helpers for AIWatcher Local.

Prices are best-effort estimates used for personal visibility. Subscription-only
tools intentionally return zero API cost and are labeled by the CLI.
"""

from __future__ import annotations

from datetime import datetime, timezone


MODEL_PRICING: dict[str, dict[str, float | bool]] = {
    # Standard rate. Sonnet 5's introductory rate is in INTRO_PRICING below and
    # applies to spend dated before it lapses.
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


# Promotional rates that apply to spend dated before `until`, keyed the same way
# as MODEL_PRICING.
#
# Spend is priced by when it happened, not by when the report runs. That is the
# only reading that stays correct in both directions: history keeps the rate it
# was actually billed at, and the standard rate takes over on its own the moment
# the promotion lapses -- no deploy, no scheduled job, no reminder. It also means
# a future price change can be entered ahead of time and simply take effect.
#
# `until` is exclusive: the last billable moment of 2026-08-31 UTC is inside the
# promotion, 2026-09-01T00:00Z is not.
#
# What this cannot do is notice a price change nobody entered. Anthropic
# publishes no pricing API -- the Models API returns context windows and
# capabilities, not dollars -- so new rates still arrive by editing this table.
INTRO_PRICING: dict[str, dict[str, float | datetime]] = {
    # https://claude.com/pricing -- $2/$10 through 2026-08-31, $3/$15 after.
    "claude-sonnet-5": {
        "in": 2.00,
        "out": 10.00,
        "until": datetime(2026, 9, 1, tzinfo=timezone.utc),
    },
}


def lookup(model: str | None, when: datetime | None = None) -> dict[str, float | bool] | None:
    """Rates for `model` as they stood at `when` (default: the standard rate).

    Callers that price a specific event pass that event's timestamp. Callers
    asking a general question about a model ("is this subscription-only?") omit
    it and get the standard rate, which is the right default for a question that
    has no date attached.
    """
    if not model:
        return None
    normalized = model.lower()
    pricing = MODEL_PRICING.get(normalized)
    key = normalized if pricing is not None else None
    if pricing is None:
        for candidate, rates in MODEL_PRICING.items():
            if normalized.startswith(candidate):
                pricing, key = rates, candidate
                break
    if pricing is None:
        return None
    intro = INTRO_PRICING.get(key or "")
    if intro is None or when is None:
        return pricing
    stamp = when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)
    if stamp >= intro["until"]:
        return pricing
    # Only the rates move; `subscription` and anything else stays as configured.
    return {**pricing, "in": intro["in"], "out": intro["out"]}


def estimate_cost(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    *,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    cache_read: int = 0,
    when: datetime | None = None,
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

    `when` is the moment the tokens were spent, and every caller that has one
    should pass it: promotional rates lapse, and pricing a July turn at
    September's rate overstates it. Cache rates are multiples of the base input
    price, so they follow the dated rate without any extra handling.
    """
    pricing = lookup(model, when)
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


def cache_read_cost(model: str | None, cache_read: int, when: datetime | None = None) -> float:
    """What the replayed portion of a turn cost, at the discounted cache rate.

    Split out of `estimate_cost` so callers that need to say "this much of the
    bill was re-sent history" price it the same way the bill itself was priced.
    Returns 0.0 for subscription or unknown models, where there is no dollar
    figure to attribute. `when` dates the rate, as in `estimate_cost` -- this
    figure is a share of that bill, so it has to use the same rate or the share
    is computed against a different denominator than the total.
    """
    pricing = lookup(model, when)
    if not pricing or pricing.get("subscription"):
        return 0.0
    return cache_read * float(pricing["in"]) * CACHE_READ_MULTIPLIER / 1_000_000


def is_subscription_model(model: str | None) -> bool:
    return bool(lookup(model) and lookup(model).get("subscription"))
