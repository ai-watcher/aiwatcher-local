"""Comparison metrics for AIWatcher Local.

Every function here answers "compared to what?" -- a model against another
model, this week against your own history, tokens re-sent against tokens
actually needed. A number without a comparison is a metric, not an insight,
and belongs in a table rather than in the insights feed.

Deliberately free of presentation: these return numbers and sample counts so
the dashboard and (later) the commit receipt can consume the same code instead
of each re-deriving it. Every function is honesty-gated the same way
_cost_per_surviving_change is -- `available: False` with a reason, rather than
a confident-looking number computed from three data points.

Nothing here depends on outcome or survival data. That is deliberate: as of
this writing the stored survival signal measures git reachability, which
reports ~100% survival where line-level measurement finds ~32%, so any metric
built on it would inherit that error.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any, Sequence

from .pricing import CACHE_READ_MULTIPLIER, is_subscription_model, lookup
from .scanner import LocalEvent, LocalSession, display_model_name

# A ratio needs both arms to be real. Below this, the "comparison" is one
# outlier session divided by another.
MIN_SESSIONS_PER_MODEL = 4
MIN_BASELINE_WEEKS = 2
MIN_REPLAYED_TOKENS = 50_000  # under this the dollar figure rounds to noise


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    return {"available": False, "reason": reason, **extra}


def _has_cumulative_totals(session: LocalSession) -> bool:
    """Tools that report running totals rather than per-session deltas.

    Duplicated from ui.py/cli.py rather than imported: ui.py imports this
    module, so importing back would cycle.
    """
    return any("cumulative" in note.lower() for note in session.notes)


def model_cost_comparison(
    rows: Sequence[LocalSession],
    *,
    min_sessions: int = MIN_SESSIONS_PER_MODEL,
    driver_threshold: float = 1.5,
) -> dict[str, Any]:
    """How models compare on cost per session AND cost per token.

    Reporting only cost-per-session is actively misleading, and this repo's own
    data shows why: Sonnet sessions cost ~20x an Opus session while being the
    *cheapest* model per token, because they run ~78x more tokens. A card built
    on the session figure alone would tell you to switch to the pricier model.

    So both are returned, plus `driver` naming which one explains the gap:
      - "volume" -- the dear model just gets used for bigger sessions
      - "rate"   -- it genuinely costs more per token
      - "mixed"  -- both contribute
    `rankings_disagree` flags the specific trap above: dearest per session is
    also cheapest per token.

    Medians for the per-session figures (session cost is heavily right-skewed,
    so one huge session would drag a mean), but the effective rate is
    total-over-total -- a median of per-session rates would weight a tiny
    session the same as a long one.

    Subscription-priced models are excluded rather than counted as free: their
    zero is a pricing-table artifact, not an observation.
    """
    by_model: dict[str, list[tuple[float, int]]] = {}
    for row in rows:
        if is_subscription_model(row.model) or not lookup(row.model):
            continue
        if _has_cumulative_totals(row) or row.cost_usd <= 0:
            continue
        tokens = row.tokens_in + row.tokens_out
        if tokens <= 0:
            continue
        by_model.setdefault(row.model or "unknown", []).append((row.cost_usd, tokens))

    eligible = {model: rows_ for model, rows_ in by_model.items() if len(rows_) >= min_sessions}
    if len(eligible) < 2:
        return _unavailable(
            f"Need {min_sessions}+ API-priced sessions on at least two models to compare.",
            models_seen=len(by_model),
            models_eligible=len(eligible),
        )

    stats: dict[str, dict[str, Any]] = {}
    for model, pairs in eligible.items():
        total_cost = sum(cost for cost, _ in pairs)
        total_tokens = sum(tokens for _, tokens in pairs)
        stats[model] = {
            "model": model,
            "label": display_model_name(model),
            "sessions": len(pairs),
            "median_session_usd": round(median(cost for cost, _ in pairs), 6),
            "median_session_tokens": int(median(tokens for _, tokens in pairs)),
            "usd_per_mtok": round(total_cost / total_tokens * 1_000_000, 4),
        }

    dear_session = max(stats.values(), key=lambda s: s["median_session_usd"])
    cheap_session = min(stats.values(), key=lambda s: s["median_session_usd"])
    dear_rate = max(stats.values(), key=lambda s: s["usd_per_mtok"])
    cheap_rate = min(stats.values(), key=lambda s: s["usd_per_mtok"])
    if cheap_session["median_session_usd"] <= 0 or cheap_rate["usd_per_mtok"] <= 0:
        return _unavailable("A model's cheapest figure is zero, so no ratio is meaningful.")

    session_ratio = dear_session["median_session_usd"] / cheap_session["median_session_usd"]
    rate_ratio = dear_rate["usd_per_mtok"] / cheap_rate["usd_per_mtok"]

    # `driver` must compare the SAME pair the session ratio does, or the two
    # factors describe different comparisons and cannot be weighed against each
    # other. Roughly: session_ratio ~= volume_factor * rate_factor, so whichever
    # factor is further from 1 is what actually explains the gap.
    volume_factor = (
        dear_session["median_session_tokens"] / cheap_session["median_session_tokens"]
        if cheap_session["median_session_tokens"] > 0 else 1.0
    )
    rate_factor = (
        dear_session["usd_per_mtok"] / cheap_session["usd_per_mtok"]
        if cheap_session["usd_per_mtok"] > 0 else 1.0
    )
    volume_pull = max(volume_factor, 1 / volume_factor) if volume_factor > 0 else 1.0
    rate_pull = max(rate_factor, 1 / rate_factor) if rate_factor > 0 else 1.0
    if volume_pull >= rate_pull * driver_threshold:
        driver = "volume"
    elif rate_pull >= volume_pull * driver_threshold:
        driver = "rate"
    else:
        driver = "mixed"

    return {
        "available": True,
        "reason": None,
        "models": sorted(stats.values(), key=lambda s: s["median_session_usd"], reverse=True),
        "by_session": {
            "dearest": dear_session,
            "cheapest": cheap_session,
            "ratio": round(session_ratio, 2),
        },
        "by_token": {
            "dearest": dear_rate,
            "cheapest": cheap_rate,
            "ratio": round(rate_ratio, 2),
        },
        # Both factors are for the by_session pair: how much bigger the dearer
        # model's sessions run, and how its per-token rate compares. A
        # rate_factor below 1 means the dearer-per-session model is actually
        # cheaper per token.
        "volume_factor": round(volume_factor, 2),
        "rate_factor": round(rate_factor, 2),
        "driver": driver,
        "rankings_disagree": dear_session["model"] == cheap_rate["model"],
    }


def pace_vs_baseline(
    events: Sequence[LocalEvent],
    *,
    days: int = 7,
    baseline_weeks: int = 4,
    min_weeks: int = MIN_BASELINE_WEEKS,
) -> dict[str, Any]:
    """This window's spend against the same-length windows before it.

    Compares you to yourself. Provider quota is not visible locally, so an
    honest "are you running hot" has to be self-referential -- and a personal
    baseline is what makes a raw dollar figure mean anything.

    Buckets *events*, not sessions. Bucketing whole sessions by their last-touch
    time credits every window with spend that happened in earlier ones, which
    overstates the current window and understates the baseline -- enough that
    the reported "excess" could exceed the window's entire spend.

    Windows with no recorded activity are dropped rather than counted as zero:
    a fortnight away from the keyboard would otherwise halve the baseline and
    make the return week look like a spike.
    """
    now = datetime.now().astimezone()
    window = timedelta(days=days)
    current_start = now - window

    current = 0.0
    buckets: dict[int, float] = {}
    for event in events:
        if event.cost_usd <= 0 or not event.timestamp:
            continue
        stamp = event.timestamp.astimezone()
        if stamp >= current_start:
            current += event.cost_usd
            continue
        index = int((current_start - stamp) / window)
        if index < baseline_weeks:
            buckets[index] = buckets.get(index, 0.0) + event.cost_usd

    active = [value for value in buckets.values() if value > 0]
    if len(active) < min_weeks:
        return _unavailable(
            f"Need {min_weeks}+ prior {days}-day windows with activity to set a baseline.",
            baseline_windows=len(active),
        )

    baseline = sum(active) / len(active)
    if baseline <= 0:
        return _unavailable("Baseline spend is zero.")

    return {
        "available": True,
        "reason": None,
        "window_days": days,
        "current_usd": round(current, 6),
        "baseline_usd": round(baseline, 6),
        "baseline_windows": len(active),
        "ratio": round(current / baseline, 2),
        "delta_pct": round((current / baseline - 1) * 100, 1),
        "direction": "above" if current > baseline else "below",
    }


def replayed_context_cost(
    sessions: Sequence[LocalSession],
    *,
    limit: int = 5,
    min_tokens: int = MIN_REPLAYED_TOKENS,
) -> dict[str, Any]:
    """What each session paid to re-send conversation history it already sent.

    Every turn re-sends the whole conversation, so billed input far exceeds the
    context a session ever actually held. `cache_read_tokens` is exactly that
    re-sent portion as the provider counted it -- a measurement, not an
    estimate, so this deliberately does NOT go through session_health's
    bloat_ratio (an output-vs-input proxy that is degenerate now that billed
    input includes cache reads).

    Priced at the cache-read rate, not the full input rate: replayed context is
    discounted about tenfold, and charging it at face value overstates the cost
    by that factor.
    """
    rows: list[dict[str, Any]] = []
    for session in sessions:
        replayed = session.cache_read_tokens
        if replayed < min_tokens or session.tokens_in <= 0:
            continue
        pricing = lookup(session.model, session.updated_at)
        if not pricing or pricing.get("subscription"):
            continue
        cost = replayed * float(pricing["in"]) * CACHE_READ_MULTIPLIER / 1_000_000
        rows.append({
            "session_id": session.session_id,
            "tool": session.tool,
            "project_path": session.project_path,
            "model": session.model,
            "model_label": display_model_name(session.model),
            "replayed_tokens": replayed,
            "billed_input_tokens": session.tokens_in,
            "replayed_pct": round(100.0 * replayed / session.tokens_in, 1),
            "replayed_usd": round(cost, 6),
            "session_usd": round(session.cost_usd, 6),
            "share_of_session_pct": (
                round(100.0 * cost / session.cost_usd, 1) if session.cost_usd > 0 else None
            ),
        })

    if not rows:
        return _unavailable(
            "No session replayed enough context to price reliably.",
            sessions_analyzed=len(sessions),
        )

    rows.sort(key=lambda row: row["replayed_usd"], reverse=True)
    return {
        "available": True,
        "reason": None,
        "total_replayed_usd": round(sum(row["replayed_usd"] for row in rows), 6),
        "sessions": rows[:limit],
    }
