"""Fresh Start before/after: what a restart did to the cost of a turn.

A Fresh Start exists because a long session gets expensive per turn. This is
the one intervention that can be measured on the same piece of work rather
than estimated against other work: the last few turns before the restart are
the "before", the first few turns of the session it restarted into are the
"after". No baseline, no "estimated" label -- but also no claim beyond what
the two windows show. It does not say the whole task got cheaper, or how many
turns were saved, and it says so when the tool had already compacted the
context on its own inside the before window.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .scanner import LocalSession, _parse_ts
from .tasks import _segments_for

WINDOW_TURNS = 5
RESTART_DECISIONS = {"new_chat", "copy_handoff"}


def _window_stats(segments: list[dict[str, object]]) -> dict[str, Any]:
    turns = len(segments)
    tokens = sum(int(seg.get("tokens") or 0) for seg in segments)
    cache = sum(int(seg.get("cache_read_tokens") or 0) for seg in segments)
    cost = sum(float(seg.get("cost_usd") or 0.0) for seg in segments)
    calls = sum(int(seg.get("tool_calls") or 0) for seg in segments)
    return {
        "turns": turns,
        "tokens_per_turn": round(tokens / turns) if turns else None,
        "cache_read_per_turn": round(cache / turns) if turns else None,
        "cost_per_turn": round(cost / turns, 4) if turns else None,
        "tool_calls_per_turn": round(calls / turns, 1) if turns else None,
        "first_at": segments[0].get("at") if segments else None,
        "last_at": segments[-1].get("at") if segments else None,
    }


def _tokens_label(value: float | int | None) -> str:
    if value is None:
        return "—"
    value = float(value)
    if value >= 1e9:
        return f"{value / 1e9:.1f}B"
    if value >= 1e6:
        return f"{value / 1e6:.1f}M"
    if value >= 1e3:
        return f"{value / 1e3:.0f}k"
    return f"{value:.0f}"


def _pct_change(before: float | None, after: float | None) -> float | None:
    """Positive means the after window is cheaper. None when the before window is empty."""
    if not before or after is None:
        return None
    return round((before - after) / before * 100, 1)


def measure_restart(
    *,
    decision: dict[str, Any],
    source: LocalSession | None,
    next_session: LocalSession | None,
    window: int = WINDOW_TURNS,
) -> dict[str, Any]:
    """Compare the last `window` turns before a Fresh Start with the first `window` after it."""
    result: dict[str, Any] = {
        "window_turns": window,
        "status": "not_a_restart",
        "reason": None,
        "before": None,
        "after": None,
        "after_turns_so_far": 0,
        "compacted_before": False,
        "tokens_per_turn_change_pct": None,
        "cost_per_turn_change_pct": None,
        "label": None,
    }
    if str(decision.get("decision") or "") not in RESTART_DECISIONS:
        result["reason"] = "This decision did not restart into a new session."
        return result
    if source is None:
        result["status"] = "unmeasurable"
        result["reason"] = "The session this Fresh Start came from is not in the current window."
        return result
    taken_at = _parse_ts(decision.get("created_at"))
    source_segments = _segments_for(source)
    if not source_segments:
        result["status"] = "unmeasurable"
        result["reason"] = "The source session has no readable per-prompt transcript."
        return result
    before_pool = [
        seg for seg in source_segments
        if not taken_at or not _parse_ts(seg.get("at")) or _parse_ts(seg.get("at")) <= taken_at
    ]
    before = before_pool[-window:]
    if not before:
        result["status"] = "unmeasurable"
        result["reason"] = "No turns were recorded before the restart."
        return result
    result["before"] = _window_stats(before)
    result["compacted_before"] = any(bool(seg.get("compacted")) for seg in before)
    correlation = decision.get("next_session_correlation") if isinstance(decision.get("next_session_correlation"), dict) else {}
    if next_session is None:
        status = str(correlation.get("status") or "waiting")
        result["status"] = "ambiguous" if status == "ambiguous" else "unlinked"
        result["reason"] = (
            "More than one later session could be the restart; nothing is measured until that is clear."
            if status == "ambiguous"
            else "No later session in this project has been linked to the restart yet."
        )
        return result
    after_all = _segments_for(next_session)
    after = after_all[:window]
    result["after_turns_so_far"] = len(after)
    if len(after) < window:
        result["status"] = "measuring"
        result["reason"] = f"{len(after)} of {window} turns after the restart so far; the comparison waits for the window to fill."
        return result
    result["after"] = _window_stats(after)
    b, a = result["before"], result["after"]
    result["tokens_per_turn_change_pct"] = _pct_change(b["tokens_per_turn"], a["tokens_per_turn"])
    result["cost_per_turn_change_pct"] = _pct_change(b["cost_per_turn"], a["cost_per_turn"])
    result["status"] = "measured"
    verb = "cut" if (result["tokens_per_turn_change_pct"] or 0) > 0 else "did not cut"
    result["label"] = (
        f"Fresh Start {verb} this work from {_tokens_label(b['tokens_per_turn'])} to {_tokens_label(a['tokens_per_turn'])} "
        f"tokens per turn (${b['cost_per_turn']:.2f} → ${a['cost_per_turn']:.2f}). "
        f"First {window} turns after the restart used {round(a['tool_calls_per_turn'] * window)} tool calls."
    )
    if result["compacted_before"]:
        result["label"] += " The tool had already compacted the context inside the before window."
    return result


def summarize_restarts(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    """The Improve figure: how many restarts were taken, how many measured, and the median cut.

    Median, not mean: one restart from a 70M-token turn would swamp everything
    else. Below one measured restart the figure is not shown at all.
    """
    taken = [m for m in measurements if m.get("status") != "not_a_restart"]
    measured = [m for m in taken if m.get("status") == "measured"]
    changes = sorted(float(m["tokens_per_turn_change_pct"]) for m in measured if m.get("tokens_per_turn_change_pct") is not None)
    cost_changes = sorted(float(m["cost_per_turn_change_pct"]) for m in measured if m.get("cost_per_turn_change_pct") is not None)

    def median(values: list[float]) -> float | None:
        if not values:
            return None
        mid = len(values) // 2
        return values[mid] if len(values) % 2 else round((values[mid - 1] + values[mid]) / 2, 1)

    return {
        "measurable": bool(measured),
        "taken": len(taken),
        "measured": len(measured),
        "measuring": sum(1 for m in taken if m.get("status") == "measuring"),
        "unlinked": sum(1 for m in taken if m.get("status") in {"unlinked", "ambiguous"}),
        "median_tokens_per_turn_change_pct": median(changes),
        "median_cost_per_turn_change_pct": median(cost_changes),
        "reason": None if measured else (
            "No Fresh Start has been taken in this window." if not taken
            else "Fresh Starts were taken but none has five measured turns on both sides yet."
        ),
    }
