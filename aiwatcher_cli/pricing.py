"""Local pricing helpers for AIWatcher Local.

Prices are best-effort API-equivalent estimates used for personal visibility:
what the tokens would cost at the provider's list price. On a subscription no
money moves per token, and the dashboard says so. Entries marked subscription
are models AIWatcher recognises but has no list price for, and return zero.
"""

from __future__ import annotations

from datetime import datetime, timezone


_1M = 1_000_000
_200K = 200_000

# `context_window` is the input the model accepts on one call. It is what every
# per-turn context figure is judged against, so it has to be per model: the
# Claude 5 family runs at 1M while Haiku and the 4.x snapshots stop at 200K, and
# a single number for all of them is how a 211K Codex turn came to be reported
# as "past the limit".
MODEL_PRICING: dict[str, dict[str, float | bool]] = {
    # Standard rate. Sonnet 5's introductory rate is in INTRO_PRICING below and
    # applies to spend dated before it lapses.
    "claude-sonnet-5": {"in": 3.00, "out": 15.00, "subscription": False, "context_window": _1M},
    "claude-sonnet-4-20250514": {"in": 3.00, "out": 15.00, "subscription": False, "context_window": _200K},
    "claude-sonnet-4-5-20250514": {"in": 3.00, "out": 15.00, "subscription": False, "context_window": _200K},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00, "subscription": False, "context_window": _1M},
    "claude-sonnet-4-5": {"in": 3.00, "out": 15.00, "subscription": False, "context_window": _200K},
    "claude-fable-5-1": {"in": 10.00, "out": 50.00, "subscription": False, "context_window": _1M},
    "claude-fable-5": {"in": 10.00, "out": 50.00, "subscription": False, "context_window": _1M},
    "claude-opus-4-8": {"in": 5.00, "out": 25.00, "subscription": False, "context_window": _1M},
    "claude-opus-4-20250514": {"in": 5.00, "out": 25.00, "subscription": False, "context_window": _200K},
    "claude-opus-4-7": {"in": 5.00, "out": 25.00, "subscription": False, "context_window": _1M},
    "claude-opus-4-6": {"in": 5.00, "out": 25.00, "subscription": False, "context_window": _1M},
    "claude-opus-4": {"in": 5.00, "out": 25.00, "subscription": False, "context_window": _200K},
    "claude-haiku-4-5-20251001": {"in": 1.00, "out": 5.00, "subscription": False, "context_window": _200K},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00, "subscription": False, "context_window": _200K},
    "claude-opus-5": {"in": 5.00, "out": 25.00, "subscription": False, "context_window": _1M},
    "claude-mythos-5-1": {"in": 10.00, "out": 50.00, "subscription": False, "context_window": _1M},
    "claude-mythos-5": {"in": 10.00, "out": 50.00, "subscription": False, "context_window": _1M},
    "gpt-4o": {"in": 2.50, "out": 10.00, "subscription": False, "context_window": 128_000},
    "gpt-4o-mini": {"in": 0.15, "out": 0.60, "subscription": False, "context_window": 128_000},
    # GPT-5 family at OpenAI's standard list prices, per 1M tokens, from
    # https://developers.openai.com/api/docs/pricing (read 2026-09-15). Until
    # then every Codex session was priced at $0 as a subscription product; Danny
    # chose API-equivalent dollars everywhere, the same treatment Claude
    # sessions get, so a Codex session's header, prompt receipts and Companion
    # bar agree. Cached input is 10% of input for every model listed with one,
    # which is CACHE_READ_MULTIPLIER; the -pro models list no cached price.
    # gpt-5-codex and gpt-5.x-codex builds not listed on their own resolve to
    # their base version by prefix (see `lookup`).
    #
    # 400K is the Codex CLI's cap (272K input + 128K reserved output), which is
    # the only place AIWatcher ever meets a GPT-5 model. The API window for the
    # newer builds is larger, and Codex can be configured to use it; a session
    # that does will exceed this figure and `session_health` reports its ceiling
    # as unknown rather than as breached.
    "gpt-5": {"in": 1.25, "out": 10.00, "subscription": False, "context_window": 400_000},
    "gpt-5-mini": {"in": 0.25, "out": 2.00, "subscription": False, "context_window": 400_000},
    "gpt-5-nano": {"in": 0.05, "out": 0.40, "subscription": False, "context_window": 400_000},
    "gpt-5-pro": {"in": 15.00, "out": 120.00, "subscription": False, "context_window": 400_000},
    "gpt-5.1": {"in": 1.25, "out": 10.00, "subscription": False, "context_window": 400_000},
    "gpt-5.2": {"in": 1.75, "out": 14.00, "subscription": False, "context_window": 400_000},
    "gpt-5.2-pro": {"in": 21.00, "out": 168.00, "subscription": False, "context_window": 400_000},
    "gpt-5.3-codex": {"in": 1.75, "out": 14.00, "subscription": False, "context_window": 400_000},
    "gpt-5.4": {"in": 2.50, "out": 15.00, "subscription": False, "context_window": 400_000},
    "gpt-5.4-mini": {"in": 0.75, "out": 4.50, "subscription": False, "context_window": 400_000},
    "gpt-5.4-nano": {"in": 0.20, "out": 1.25, "subscription": False, "context_window": 400_000},
    "gpt-5.4-pro": {"in": 30.00, "out": 180.00, "subscription": False, "context_window": 400_000},
    "gpt-5.5": {"in": 5.00, "out": 30.00, "subscription": False, "context_window": 400_000},
    "gpt-5.5-pro": {"in": 30.00, "out": 180.00, "subscription": False, "context_window": 400_000},
    "gpt-5.6-luna": {"in": 0.20, "out": 1.20, "subscription": False, "context_window": 400_000},
    "gpt-5.6-terra": {"in": 2.00, "out": 12.00, "subscription": False, "context_window": 400_000},
    "gpt-5.6-sol": {"in": 4.00, "out": 20.00, "subscription": False, "context_window": 400_000},
    # A Codex model AIWatcher cannot price: the bare "codex" the scanner falls
    # back to when a rollout names no model, and (via `lookup`) any GPT-5 build
    # newer than this table. Known, not guessed at -- None would mean "unknown
    # model", which is a different claim from "recognised, no list price here".
    "codex": {"in": 0.0, "out": 0.0, "subscription": True, "context_window": 400_000},
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


def _longest_prefix(model: str) -> str | None:
    """The table key a dated or variant model name belongs to.

    Longest match wins, so "gpt-5.4-mini-2026" is gpt-5.4-mini, not gpt-5.4,
    and "claude-sonnet-4-5-20250929" is claude-sonnet-4-5. A key never matches
    a different version of itself: "gpt-5" is a prefix of "gpt-5.9-codex" but a
    "." or digit after it continues the version number, and pricing 5.9 at 5's
    rate would be a guess. A "-" or "[" starts a variant of the same model.
    """
    best: str | None = None
    for candidate in MODEL_PRICING:
        if not model.startswith(candidate) or (best is not None and len(candidate) <= len(best)):
            continue
        following = model[len(candidate):len(candidate) + 1]
        if following and (following == "." or following.isdigit()):
            continue
        best = candidate
    return best


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
        key = _longest_prefix(normalized)
        pricing = MODEL_PRICING.get(key) if key else None
    if pricing is None and normalized.startswith(("gpt-5", "codex")):
        # A GPT-5 or Codex build newer than this table: recognised, unpriced.
        return MODEL_PRICING["codex"]
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


def context_window(model: str | None) -> int | None:
    """Input tokens `model` accepts on one call, or None when nobody knows.

    None is a real answer: a caller judging a per-turn figure against it must
    show the figure with no limit, not fall back to some other model's window.
    Claude Code names the 1M-context variant of a 200K model with a `[1m]`
    suffix, which the prefix scan would otherwise resolve to the 200K entry.
    """
    if not model:
        return None
    if "[1m]" in model.lower():
        return _1M
    pricing = lookup(model)
    if not pricing:
        return None
    window = pricing.get("context_window")
    return int(window) if window else None
