"""Local-only dashboard for AIWatcher Local."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .cli import analyze_prompt, session_insights
from .correlate import link_recent_interventions_to_sessions
from .handoff import build_handoff_capsule
from .local_state import (
    VALID_OUTCOMES,
    get_outcome,
    outcome_counts,
    outcomes_for_sessions,
    recent_interventions,
    record_evidence_snapshot,
    record_outcome,
)
from .outcome_evidence import build_outcome_evidence, evidence_for_sessions
from .pricing import is_subscription_model
from .session_health import ContextHealth, analyze_all_sessions, gate_health_warning
from .scanner import (
    LocalEvent,
    LocalSession,
    discover_tools,
    extract_opening_prompt,
    scan_all,
    scan_all_events,
    segment_session_by_prompt,
)


MAX_REQUEST_BYTES = 64 * 1024

MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def money(value: float) -> str:
    if value == 0:
        return "$0.00"
    if abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def compact_int(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def short_path(path: str | None, max_len: int = 54) -> str:
    if not path:
        return "unknown"
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


def in_window(session: LocalSession, since: datetime) -> bool:
    stamp = session.updated_at or session.started_at
    return bool(stamp and stamp.astimezone() >= since)


def summarize(rows: list[LocalSession]) -> dict[str, float | int]:
    return {
        "sessions": len(rows),
        "tokens": sum(row.tokens_in + row.tokens_out for row in rows),
        "tokens_in": sum(row.tokens_in for row in rows),
        "tokens_out": sum(row.tokens_out for row in rows),
        "api_value_usd": sum(row.cost_usd for row in rows),
        "calls": sum(row.agent_calls for row in rows),
        "tool_calls": sum(row.tool_calls for row in rows),
    }


def token_split(rows: list[LocalSession]) -> dict[str, int]:
    api_priced = sum(
        row.tokens_in + row.tokens_out
        for row in rows
        if row.cost_usd > 0 and not is_subscription_model(row.model)
    )
    total = sum(row.tokens_in + row.tokens_out for row in rows)
    return {"api_priced": api_priced, "plan_limited": max(0, total - api_priced)}


def has_cumulative_totals(session: LocalSession) -> bool:
    return any("cumulative" in note.lower() for note in session.notes)


def group_rows(rows: list[LocalSession], key_fn) -> list[dict[str, object]]:
    grouped: dict[str, list[LocalSession]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)

    result = []
    for key, items in grouped.items():
        stats = summarize(items)
        result.append({
            "name": key,
            "id": key,
            "short_name": short_path(key),
            "sessions": stats["sessions"],
            "tokens": stats["tokens"],
            "tokens_label": compact_int(int(stats["tokens"])),
            "api_value_usd": round(float(stats["api_value_usd"]), 6),
            "api_value_label": money(float(stats["api_value_usd"])),
            "calls": stats["calls"],
            "tool_calls": stats["tool_calls"],
        })
    result.sort(key=lambda item: (float(item["api_value_usd"]), int(item["tokens"])), reverse=True)
    return result


def rows_for_window(days: int) -> list[LocalSession]:
    since = datetime.now().astimezone() - timedelta(days=days)
    return [row for row in scan_all() if in_window(row, since)]


def session_json(row: LocalSession) -> dict[str, object]:
    started = row.started_at.isoformat() if row.started_at else None
    updated = row.updated_at.isoformat() if row.updated_at else None
    outcome = get_outcome(row.session_id)
    evidence = build_outcome_evidence(row)
    return {
        "session_id": row.session_id,
        "tool": row.tool,
        "project": row.project_path or "unknown",
        "project_short": short_path(row.project_path),
        "model": row.model or "unknown",
        "tokens": row.tokens_in + row.tokens_out,
        "tokens_label": compact_int(row.tokens_in + row.tokens_out),
        "tokens_in_label": compact_int(row.tokens_in),
        "tokens_out_label": compact_int(row.tokens_out),
        "api_value_usd": round(row.cost_usd, 6),
        "api_value": money(row.cost_usd),
        "calls": row.agent_calls,
        "tool_calls": row.tool_calls,
        "started_at": started,
        "updated_at": updated,
        "source_path": row.source_path,
        "notes": row.notes,
        "outcome": outcome["outcome"] if outcome else None,
        "outcome_note": outcome.get("note") if outcome else None,
        "evidence": evidence.to_json(),
    }


def event_json(row: LocalEvent) -> dict[str, object]:
    return {
        "event_id": row.event_id,
        "event_type": row.event_type,
        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "model": row.model or "unknown",
        "tokens": row.tokens_in + row.tokens_out,
        "tokens_label": compact_int(row.tokens_in + row.tokens_out),
        "api_value": money(row.cost_usd),
        "api_value_usd": round(row.cost_usd, 6),
        "content_hash": row.content_hash,
        "turn": row.turn,
    }


def build_project_detail(project: str, days: int = 7) -> dict[str, object]:
    rows = [row for row in rows_for_window(days) if (row.project_path or "unknown") == project]
    stats = summarize(rows)
    sessions = sorted(rows, key=lambda row: row.updated_at or row.started_at or MIN_DT, reverse=True)
    return {
        "project": project,
        "project_short": short_path(project, 72),
        "totals": {
            "sessions": stats["sessions"],
            "api_value": money(float(stats["api_value_usd"])),
            "tokens": compact_int(int(stats["tokens"])),
            "calls": stats["calls"],
            "tool_calls": stats["tool_calls"],
        },
        "models": group_rows(rows, lambda row: row.model or "unknown"),
        "tools": group_rows(rows, lambda row: row.tool),
        "sessions": [session_json(row) for row in sessions[:20]],
    }


def timeline_analysis(events: list[LocalEvent]) -> dict[str, object]:
    """Aggregate session events by type and detect duplicated content (loop/waste signal)."""
    buckets: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "cost": 0.0, "tokens": 0})
    total_cost = 0.0
    hash_counts: Counter[str] = Counter()
    for event in events:
        bucket = buckets[event.event_type]
        bucket["count"] += 1
        bucket["cost"] += event.cost_usd
        bucket["tokens"] += event.tokens_in + event.tokens_out
        total_cost += event.cost_usd
        if event.content_hash:
            hash_counts[event.content_hash] += 1

    cost_by_type = sorted(
        (
            {
                "event_type": event_type,
                "count": int(data["count"]),
                "tokens_label": compact_int(int(data["tokens"])),
                "api_value": money(data["cost"]),
                "api_value_usd": round(data["cost"], 6),
                "label": f"{money(data['cost'])} · {int(data['count'])}x",
                "share_pct": round(data["cost"] / total_cost * 100) if total_cost else 0,
            }
            for event_type, data in buckets.items()
        ),
        key=lambda row: row["api_value_usd"],
        reverse=True,
    )

    repeated = [count for count in hash_counts.values() if count > 1]
    duplicate_events = sum(count - 1 for count in repeated)
    return {
        "cost_by_type": cost_by_type,
        "repeats": {
            "distinct_repeated": len(repeated),
            "duplicate_events": duplicate_events,
            "max_repeat": max(repeated, default=0),
        },
    }


def build_prompt_analysis(
    row: LocalSession, segments: list[dict[str, object]] | None = None
) -> dict[str, object] | None:
    """Attribute session cost to the prompts that drove it, and coach the costliest ask worth tightening.

    Returns None for sessions with no readable prompts (e.g. non-Claude sources).
    """
    if segments is None:
        segments = segment_session_by_prompt(row.source_path)
    if not segments:
        return None
    total = sum(float(s["cost_usd"]) for s in segments)
    by_cost = sorted(segments, key=lambda s: float(s["cost_usd"]), reverse=True)

    expensive = [
        {
            "prompt": seg["prompt"],
            "turn": seg["turn"],
            "tool_calls": seg["tool_calls"],
            "api_value": money(float(seg["cost_usd"])),
            "api_value_usd": round(float(seg["cost_usd"]), 6),
            "share_pct": round(float(seg["cost_usd"]) / total * 100) if total else 0,
        }
        for seg in by_cost[:5]
        if float(seg["cost_usd"]) > 0
    ]

    # Coach the costliest prompt that actually has something to tighten (analyze_prompt score > 0).
    # If none qualifies, cost accumulated across turns rather than from any single weak ask.
    coaching = None
    for seg in by_cost:
        analysis = analyze_prompt(str(seg["prompt"]), tool=row.tool, cwd=row.project_path)
        if int(analysis["score"]) > 0:
            coaching = {
                "prompt": seg["prompt"],
                "turn": seg["turn"],
                "api_value": money(float(seg["cost_usd"])),
                "risk": analysis["risk"],
                "findings": analysis["findings"],
                "suggestions": analysis["suggestions"],
                "suggested_prompt": analysis["suggested_prompt"],
            }
            break

    return {
        "opening_prompt": segments[0]["prompt"],
        "turns": len(segments),
        "expensive_asks": expensive,
        "coaching": coaching,
    }


# High backstop so a full session (and thus every turn) renders; only pathological
# sessions truncate, and the timeline note reports it when they do.
EVENT_DISPLAY_LIMIT = 5000


def build_session_detail(session_id: str, days: int = 30) -> dict[str, object]:
    rows = [row for row in rows_for_window(days) if row.session_id == session_id]
    if not rows:
        return {"error": "session not found"}
    row = rows[0]
    # A single-session view shows the whole session, not just the last `days` — otherwise
    # early turns of a long-running session are hidden. We only filter by session id here.
    events = sorted(
        [event for event in scan_all_events() if event.session_id == session_id],
        key=lambda event: event.timestamp or MIN_DT,
    )
    costliest = max(events, key=lambda event: (event.cost_usd, event.tokens_in + event.tokens_out), default=None)
    segments = segment_session_by_prompt(row.source_path)
    turn_prompts = {int(seg["turn"]): str(seg["prompt"])[:240] for seg in segments}
    evidence = build_outcome_evidence(row)
    try:
        record_evidence_snapshot(session_id, evidence.to_json())
    except OSError:
        pass
    return {
        **session_json(row),
        "privacy": "Prompt text is shown only when you inspect this local session; it is not uploaded or persisted in summaries.",
        "insights": session_insights(row),
        "prompt_analysis": build_prompt_analysis(row, segments),
        "outcome_evidence": evidence.to_json(),
        "turn_prompts": turn_prompts,
        "timeline_summary": {
            "events": len(events),
            "shown": min(len(events), EVENT_DISPLAY_LIMIT),
            "tokens": compact_int(sum(event.tokens_in + event.tokens_out for event in events)),
            "api_value": money(sum(event.cost_usd for event in events)),
            "costliest": event_json(costliest) if costliest else None,
            **timeline_analysis(events),
        },
        "events": [event_json(event) for event in events[:EVENT_DISPLAY_LIMIT]],
    }


def build_handoff_detail(
    session_id: str,
    days: int = 30,
    target: str = "generic",
    include_prompt_excerpt: bool = False,
) -> dict[str, object]:
    rows = [row for row in rows_for_window(days) if row.session_id == session_id]
    if not rows:
        return {"error": "session not found"}
    row = rows[0]
    events = sorted(
        [event for event in scan_all_events() if event.session_id == session_id],
        key=lambda event: event.timestamp or MIN_DT,
    )
    outcome = get_outcome(session_id)
    return build_handoff_capsule(
        row,
        events,
        outcome=outcome.get("outcome") if outcome else None,
        include_prompt_excerpt=include_prompt_excerpt,
        target=target if target in {"generic", "claude", "codex", "cursor", "vscode"} else "generic",
    )


def build_report(days: int = 7) -> dict[str, object]:
    rows = rows_for_window(days)
    stats = summarize(rows)
    projects = group_rows(rows, lambda row: row.project_path or "unknown")
    tools = group_rows(rows, lambda row: row.tool)
    models = group_rows(rows, lambda row: row.model or "unknown")
    return {
        "title": f"Your AI coding week" if days == 7 else f"Your last {days} days",
        "summary": [
            f"{stats['sessions']} local sessions",
            f"{money(float(stats['api_value_usd']))} API-equivalent value",
            f"{compact_int(int(stats['tokens']))} tokens observed",
            f"{stats['tool_calls']} tool results/calls observed",
        ],
        "highlights": [
            f"Top project: {projects[0]['short_name']} ({projects[0]['api_value_label']})" if projects else "No project activity found.",
            f"Top model: {models[0]['name']} ({models[0]['tokens_label']} tokens)" if models else "No model activity found.",
            f"Top tool: {tools[0]['name']} ({tools[0]['sessions']} sessions)" if tools else "No tool activity found.",
        ],
        "next_checks": [
            "Review the top project for long-running or accidental sessions.",
            "Treat API-equivalent value as usage pressure, not subscription invoice spend.",
            "Export event hashes if you want privacy-safe local evidence.",
        ],
    }


def build_journal(days: int = 1) -> dict[str, object]:
    rows = rows_for_window(days)
    if not rows:
        return {
            "title": f"Your last {days} day{'s' if days != 1 else ''}",
            "summary": "No local AI work detected.",
            "items": [],
            "improvement": "Use AIWatcher after a few sessions to spot cost, scope, and loop patterns.",
        }

    stats = summarize(rows)
    projects = group_rows(rows, lambda row: row.project_path or "unknown")
    costliest = max(rows, key=lambda row: (row.cost_usd, row.tokens_in + row.tokens_out))
    pressure_rows = [row for row in rows if not has_cumulative_totals(row)]
    largest_context = max(pressure_rows, key=lambda row: row.tokens_in + row.tokens_out, default=None)
    loop_candidate = max(pressure_rows, key=lambda row: row.agent_calls, default=None)
    improvement = "Keep prompts scoped and ask for a short plan before large edits."
    if loop_candidate and loop_candidate.agent_calls >= 250:
        improvement = "Add explicit stop conditions to broad prompts to avoid repeated model/tool loops."
    elif largest_context and largest_context.tokens_in + largest_context.tokens_out >= 500_000:
        improvement = "Use smaller file scopes or checkpoints before asking for implementation."
    elif costliest.cost_usd >= 1:
        improvement = "Review the costliest session before repeating a similar prompt."

    return {
        "title": f"Your last {days} day{'s' if days != 1 else ''}",
        "summary": f"{stats['sessions']} sessions · {money(float(stats['api_value_usd']))} API-equivalent · {compact_int(int(stats['tokens']))} tokens",
        "items": [
            f"Top project: {projects[0]['short_name']} ({projects[0]['api_value_label']})" if projects else "No project attribution yet.",
            f"Most expensive session: {short_path(costliest.project_path)} · {costliest.tool} · {money(costliest.cost_usd)}",
            (
                f"Largest reliable context: {short_path(largest_context.project_path)} · "
                f"{compact_int(largest_context.tokens_in + largest_context.tokens_out)} tokens"
                if largest_context else "Largest reliable context: unavailable from local logs"
            ),
            (
                f"Loop signal: {loop_candidate.agent_calls} model calls in {short_path(loop_candidate.project_path)}"
                if loop_candidate else "Loop signal: unavailable from local logs"
            ),
        ],
        "improvement": improvement,
    }


def build_prompt_preflight(prompt: str, *, tool: str = "agent", cwd: str | None = None) -> dict[str, object]:
    text = prompt.strip()
    if not text:
        return {"error": "prompt is required"}
    result = analyze_prompt(text, tool=tool or "agent", cwd=cwd)
    impact = result.get("estimated_impact") if isinstance(result.get("estimated_impact"), dict) else {}
    impact_label = "AIWatcher needs local history before it can estimate savings."
    if impact:
        if impact.get("available"):
            savings = impact.get("savings", {}) if isinstance(impact.get("savings"), dict) else {}
            api_value = savings.get("api_value_usd", []) if isinstance(savings, dict) else []
            tokens = savings.get("tokens", []) if isinstance(savings, dict) else []
            if len(api_value) == 2 and len(tokens) == 2:
                impact_label = (
                    f"Possible avoidable pressure: {compact_int(int(tokens[0]))}-{compact_int(int(tokens[1]))} tokens "
                    f"and {money(float(api_value[0]))}-{money(float(api_value[1]))} API-equivalent."
                )
            else:
                impact_label = "Comparable local sessions found, but savings could not be summarized."
        else:
            impact_label = str(impact.get("direction") or impact.get("basis") or impact_label)
    return {
        "risk": result["risk"],
        "score": result["score"],
        "tool": result["tool"],
        "findings": result["findings"],
        "suggestions": result["suggestions"],
        "suggested_prompt": result["suggested_prompt"],
        "impact_label": impact_label,
        "privacy": "Prompt text is analyzed locally for this response and is not persisted by the Prompt Companion.",
    }


def _impact_range_label(values: object, *, currency: bool = False) -> str | None:
    if not isinstance(values, list) or len(values) != 2:
        return None
    low, high = values
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    formatter = money if currency else lambda value: compact_int(int(value))
    return formatter(float(low)) if low == high else f"{formatter(float(low))}-{formatter(float(high))}"


def _build_intervention_receipts(
    interventions: list[dict[str, object]],
    sessions: list[LocalSession],
    outcomes: dict[str, dict[str, object]],
    events: list[LocalEvent] | None = None,
) -> list[dict[str, object]]:
    sessions_by_id = {row.session_id: row for row in sessions}
    events = events or []
    receipts: list[dict[str, object]] = []
    decision_labels = {
        "brief_accepted": "Used safer brief",
        "brief_edited": "Used edited brief",
        "context_added": "Added safer context",
        "allowed_original": "Ran original",
        "original": "Ran original",
        "suggested": "Used safer prompt",
        "edited": "Used edited prompt",
        "blocked": "Blocked",
        "cancelled": "Cancelled",
    }
    for intervention in interventions:
        decision = str(intervention.get("decision") or "")
        session_id = str(intervention.get("session_id") or "")
        if decision in {"blocked", "cancelled"}:
            session_id = ""
        session = sessions_by_id.get(session_id)
        prediction = intervention.get("predicted_impact")
        prediction = prediction if isinstance(prediction, dict) else {}
        savings = prediction.get("savings") if isinstance(prediction.get("savings"), dict) else {}
        original = prediction.get("original") if isinstance(prediction.get("original"), dict) else {}
        actual_reliable = bool(session and not has_cumulative_totals(session))
        actual = None
        inferred = None
        created_at = None
        try:
            created_at = datetime.fromisoformat(str(intervention.get("created_at") or "").replace("Z", "+00:00"))
        except ValueError:
            pass
        observed_events = [
            event for event in events
            if event.session_id == session_id
            and event.timestamp
            and created_at
            and event.timestamp.astimezone(timezone.utc) >= created_at.astimezone(timezone.utc) - timedelta(seconds=5)
        ]
        if session and observed_events:
            event_tokens_in = sum(event.tokens_in for event in observed_events)
            event_tokens_out = sum(event.tokens_out for event in observed_events)
            event_cost = sum(event.cost_usd for event in observed_events)
            event_model_calls = sum(1 for event in observed_events if event.tokens_in or event.tokens_out)
            event_tool_calls = sum(1 for event in observed_events if event.event_type == "tool_result")
            actual_reliable = True
            actual = {
                "tokens": event_tokens_in + event_tokens_out,
                "tokens_label": compact_int(event_tokens_in + event_tokens_out),
                "model_calls": event_model_calls,
                "tool_calls": event_tool_calls,
                "api_value_usd": round(event_cost, 6),
                "api_value_label": money(event_cost),
                "reliable": True,
                "reason": "Measured from local events recorded after this intervention.",
            }
        if session:
            session_started = session.started_at.astimezone(timezone.utc) if session.started_at else None
            intervention_time = created_at.astimezone(timezone.utc) if created_at else None
            session_predates_intervention = bool(session_started and intervention_time and session_started < intervention_time - timedelta(minutes=2))
            if actual is None:
                actual_reliable = actual_reliable and not session_predates_intervention
                reason = None
                if has_cumulative_totals(session):
                    reason = "This tool exposes cumulative thread totals, not one-prompt usage."
                elif session_predates_intervention:
                    reason = "The linked conversation predates this intervention and no post-intervention event delta is available."
                actual = {
                    "tokens": session.tokens_in + session.tokens_out,
                    "tokens_label": compact_int(session.tokens_in + session.tokens_out),
                    "model_calls": session.agent_calls,
                    "tool_calls": session.tool_calls,
                    "api_value_usd": round(session.cost_usd, 6),
                    "api_value_label": money(session.cost_usd),
                    "reliable": actual_reliable,
                    "reason": reason,
                }
        if actual_reliable and original:
            def avoided(metric: str, observed: float) -> float | None:
                values = original.get(metric)
                if not isinstance(values, list) or len(values) != 2:
                    return None
                midpoint = (float(values[0]) + float(values[1])) / 2
                return max(0.0, midpoint - observed)

            inferred_tokens = avoided("tokens", float(actual["tokens"]))
            inferred_calls = avoided("model_calls", float(actual["model_calls"]))
            inferred_tools = avoided("tool_calls", float(actual["tool_calls"]))
            inferred_cost = avoided("api_value_usd", float(actual["api_value_usd"]))
            inferred = {
                "tokens_label": compact_int(int(inferred_tokens)) if inferred_tokens is not None else None,
                "model_calls": int(inferred_calls) if inferred_calls is not None else None,
                "tool_calls": int(inferred_tools) if inferred_tools is not None else None,
                "api_value_label": money(inferred_cost) if inferred_cost is not None else None,
                "label": "Observed below historical baseline",
                "disclaimer": "An inferred comparison, not a guaranteed counterfactual saving.",
            }
        outcome = outcomes.get(session_id) if session_id else None
        receipts.append({
            "id": intervention.get("id"),
            "created_at": intervention.get("created_at"),
            "tool": intervention.get("tool") or "agent",
            "project": short_path(str(intervention.get("cwd") or "unknown"), 72),
            "decision": decision,
            "decision_label": decision_labels.get(decision, decision or "Recorded"),
            "original_risk": intervention.get("risk") or "unknown",
            "original_score": intervention.get("score"),
            "selected_risk": intervention.get("selected_risk"),
            "selected_score": intervention.get("selected_score"),
            "risk_points_reduced": intervention.get("risk_points_reduced"),
            "predicted": {
                "available": bool(prediction.get("available")),
                "confidence": prediction.get("confidence"),
                "basis": prediction.get("basis"),
                "tokens_label": _impact_range_label(savings.get("tokens")),
                "model_calls_label": _impact_range_label(savings.get("model_calls")),
                "tool_calls_label": _impact_range_label(savings.get("tool_calls")),
                "api_value_label": _impact_range_label(savings.get("api_value_usd"), currency=True),
            },
            "session_id": session_id or None,
            "session_status": (
                "No session expected"
                if decision in {"blocked", "cancelled"}
                else "Observed" if session else "Waiting for resulting session"
            ),
            "actual": actual,
            "inferred": inferred,
            "outcome": outcome.get("outcome") if outcome else None,
        })
    return receipts


def build_summary(days: int = 7) -> dict[str, object]:
    now = datetime.now().astimezone()
    since = now - timedelta(days=days)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    all_rows = scan_all()
    try:
        link_recent_interventions_to_sessions(all_rows)
    except OSError:
        pass
    rows = [row for row in all_rows if in_window(row, since)]
    month_rows = [row for row in all_rows if in_window(row, month_start)]

    stats = summarize(rows)
    month_stats = summarize(month_rows)
    split = token_split(rows)
    day_of_month = max(1, now.day)
    projected_month = float(month_stats["api_value_usd"]) / day_of_month * 30

    projects = group_rows(rows, lambda row: row.project_path or "unknown")
    tools = group_rows(rows, lambda row: row.tool)
    models = group_rows(rows, lambda row: row.model or "unknown")

    recent = sorted(rows, key=lambda row: row.updated_at or row.started_at or MIN_DT, reverse=True)[:12]
    detected = discover_tools()
    notes = sorted({note for row in rows for note in row.notes})

    insights = []
    if projects:
        top = projects[0]
        insights.append({
            "title": "Top project",
            "body": f"{top['short_name']} accounts for {top['api_value_label']} API-equivalent value.",
        })
        if float(top["api_value_usd"]) > 0 and float(top["api_value_usd"]) >= float(stats["api_value_usd"]) * 0.6:
            insights.append({
                "title": "Usage is concentrated",
                "body": f"Most API-equivalent value is coming from {top['short_name']}. Review recent sessions there before optimizing anything else.",
            })
    costliest = max(rows, key=lambda row: (row.cost_usd, row.tokens_in + row.tokens_out), default=None)
    if costliest and costliest.cost_usd >= 1:
        insights.append({
            "title": "Session worth reviewing",
            "body": f"{short_path(costliest.project_path)} used {money(costliest.cost_usd)} API-equivalent value on {costliest.model or costliest.tool}. Open the session before repeating similar work.",
        })
    pressure_rows = [row for row in rows if not has_cumulative_totals(row)]
    highest_context = max(pressure_rows, key=lambda row: row.tokens_in + row.tokens_out, default=None)
    if highest_context and highest_context.tokens_in + highest_context.tokens_out >= 500_000:
        insights.append({
            "title": "Large-context session",
            "body": f"{compact_int(highest_context.tokens_in + highest_context.tokens_out)} tokens were observed in one session. Try smaller file scopes or checkpoints for similar tasks.",
        })
    loop_candidate = max(pressure_rows, key=lambda row: row.agent_calls, default=None)
    if loop_candidate and loop_candidate.agent_calls >= 250:
        insights.append({
            "title": "Possible iterative loop",
            "body": f"{loop_candidate.agent_calls} model calls were observed in one {loop_candidate.tool} session. Add explicit stop conditions or narrower acceptance criteria.",
        })
    if split["plan_limited"] > 0:
        insights.append({
            "title": "Subscription/limited usage detected",
            "body": f"{compact_int(split['plan_limited'])} tokens came from plan-based or limited-cost sources. Treat them as observed usage, not invoice spend.",
        })
    if detected.get("cursor") and not any(row.tool == "cursor" for row in rows):
        insights.append({
            "title": "Cursor detected, but usage is limited",
            "body": "Cursor is installed, but local token/cost history is not reliably exposed yet.",
        })

    window_session_ids = {row.session_id for row in rows}
    window_outcomes = outcomes_for_sessions(window_session_ids)
    outcomes = outcome_counts(window_session_ids)
    evidence_by_session = evidence_for_sessions(rows[:30])
    inferred_useful = sum(
        1
        for session_id, evidence in evidence_by_session.items()
        if session_id not in window_outcomes and evidence.inferred_outcome == "useful"
    )
    needs_review = sum(
        1
        for session_id, evidence in evidence_by_session.items()
        if session_id not in window_outcomes and evidence.inferred_outcome == "needs_review"
    )
    if inferred_useful:
        insights.append({
            "title": "Outcome evidence found",
            "body": f"{inferred_useful} unmarked session{'s' if inferred_useful != 1 else ''} have nearby commit or test evidence. Review and confirm the outcome.",
        })
    if needs_review:
        insights.append({
            "title": "Work needs outcome review",
            "body": f"{needs_review} unmarked session{'s' if needs_review != 1 else ''} changed files without a confirmed useful outcome.",
        })
    interventions = recent_interventions(limit=200, days=days)
    receipt_events = scan_all_events() if interventions else []
    receipts = _build_intervention_receipts(interventions, all_rows, outcomes_for_sessions(), receipt_events)
    useful_rows = [
        row for row in rows
        if (window_outcomes.get(row.session_id) or {}).get("outcome") == "useful"
    ]
    useful_cost = sum(row.cost_usd for row in useful_rows)
    cost_per_useful = useful_cost / len(useful_rows) if useful_rows else None
    return {
        "generated_at": now.isoformat(),
        "days": days,
        "privacy": [
            "Read-only local scan",
            "No LLM calls",
            "No source or prompt content in summaries",
            "No cloud upload unless you connect Cloud",
        ],
        "totals": {
            "window_label": "Last 24 hours" if days == 1 else f"Last {days} days",
            "sessions": stats["sessions"],
            "api_value_usd": round(float(stats["api_value_usd"]), 6),
            "api_value_label": money(float(stats["api_value_usd"])),
            "projected_month_label": money(projected_month),
            "tokens_label": compact_int(int(stats["tokens"])),
            "api_priced_tokens_label": compact_int(split["api_priced"]),
            "plan_limited_tokens_label": compact_int(split["plan_limited"]),
            "calls": stats["calls"],
            "tool_calls": stats["tool_calls"],
            "useful_outcomes": outcomes["useful"],
            "inferred_useful_outcomes": inferred_useful,
            "needs_review_outcomes": needs_review,
            "rework_outcomes": outcomes["rework"],
            "abandoned_outcomes": outcomes["abandoned"],
            "preflight_decisions": len(interventions),
            "cost_per_useful_change": money(cost_per_useful) if cost_per_useful is not None else "—",
        },
        "projects": projects[:10],
        "tools": tools,
        "models": models[:10],
        "insights": insights,
        "notes": notes[:5],
        "recent_sessions": [
            {
                "tool": row.tool,
                "session_id": row.session_id,
                "project": short_path(row.project_path),
                "project_full": row.project_path or "unknown",
                "model": row.model or "unknown",
                "tokens": compact_int(row.tokens_in + row.tokens_out),
                "api_value": money(row.cost_usd),
                "outcome": (window_outcomes.get(row.session_id) or {}).get("outcome"),
                "inferred_outcome": evidence_by_session.get(row.session_id).inferred_outcome if evidence_by_session.get(row.session_id) else None,
                "updated_at": (row.updated_at or row.started_at).isoformat() if (row.updated_at or row.started_at) else None,
            }
            for row in recent
        ],
        "intervention_receipts": receipts[:30],
    }


def _build_compact_prompt(health: object) -> str:
    """Generate a /compact-style smart compaction prompt for a session."""
    if not isinstance(health, ContextHealth):
        return "/compact"
    eff = health.efficiency_pct
    bloat = int(health.bloat_ratio * 100)
    ctx_k = round(health.latest_turn_tokens / 1000)
    lines = [
        f"This session is at {ctx_k}K tokens/turn ({eff:.0f}% efficient — {bloat}% replayed history).",
        "",
        "Please compact this conversation by producing a structured summary that preserves:",
        "1. Active task: what we are building right now and why",
        "2. Key decisions already made (architecture, approach, rejected alternatives)",
        "3. Files we have modified or created in this session",
        "4. Hard constraints and invariants I must not violate",
        "5. Open questions / next steps I still need to take",
        "",
        "Discard: full tool outputs, completed debug traces, superseded code snippets.",
        "Format: concise bullet points per section. I will paste this into a fresh session.",
    ]
    return "\n".join(lines)


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AIWatcher Local</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #080b10;
      --surface: #10151d;
      --surface-raised: #161d27;
      --surface-hover: #1b2430;
      --text: #f5f7fa;
      --muted: #9ba8b8;
      --faint: #6f7d8f;
      --line: #293443;
      --line-strong: #3a485b;
      --green: #35d399;
      --green-soft: rgba(53, 211, 153, .12);
      --blue: #70a7ff;
      --blue-soft: rgba(112, 167, 255, .14);
      --amber: #f6bd60;
      --amber-soft: rgba(246, 189, 96, .13);
      --red: #f27d8f;
      --red-soft: rgba(242, 125, 143, .13);
    }
    * { box-sizing: border-box; }
    html { min-width: 320px; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
    }
    body.drawer-open { overflow: hidden; }
    main { max-width: 1360px; margin: 0 auto; padding: 24px 28px 48px; }
    header { display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 20px; }
    h1 { font-size: 22px; margin: 0; letter-spacing: 0; }
    h2 { margin: 0; font-size: 16px; }
    h3 { margin: 0; font-size: 14px; }
    p { color: var(--muted); line-height: 1.5; margin: 0; }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .brand-mark {
      display: grid; place-items: center; width: 36px; height: 36px;
      border: 1px solid #4b86cf; border-radius: 8px; background: var(--blue-soft);
      color: #dceaff; font-weight: 800;
    }
    .brand-copy { min-width: 0; }
    .brand-copy p { margin-top: 2px; font-size: 12px; }
    .status-badge {
      display: inline-flex; align-items: center; gap: 7px; color: #c9f8e5;
      border: 1px solid rgba(53,211,153,.32); background: var(--green-soft);
      border-radius: 999px; padding: 5px 9px; font-size: 11px; font-weight: 700;
    }
    .status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); }
    button, select {
      border: 1px solid var(--line-strong);
      background: var(--surface-raised);
      color: var(--text);
      border-radius: 8px;
      min-height: 38px;
      padding: 8px 12px;
      font: inherit;
      font-weight: 650;
    }
    button { cursor: pointer; transition: background .15s ease, border-color .15s ease, color .15s ease; }
    button:hover { background: var(--surface-hover); border-color: #53647a; }
    button:focus-visible, select:focus-visible, a:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
    button:disabled { cursor: wait; opacity: .62; }
    .actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .btn-primary { background: #2f6fbd; border-color: #5594df; color: white; }
    .btn-primary:hover { background: #397dce; border-color: #76adf0; }
    .btn-quiet { background: transparent; border-color: var(--line); color: #d5deea; }
    .link-button {
      text-decoration: none;
      border: 1px solid #4377b5;
      background: var(--blue-soft);
      color: #dcebff;
      border-radius: 8px;
      min-height: 38px;
      padding: 9px 12px;
      font-weight: 650;
    }
    .grid { display: grid; gap: 14px; }
    .product-nav {
      display: flex; gap: 4px; width: fit-content; max-width: 100%; overflow-x: auto;
      margin: 0 0 22px; padding: 4px; border: 1px solid var(--line);
      border-radius: 8px; background: #0c1118;
    }
    .nav-tab {
      min-height: 34px; border: 0; background: transparent; border-radius: 6px;
      padding: 7px 14px; color: #aebaca; font-size: 13px;
    }
    .nav-tab:hover { background: #151d27; border-color: transparent; }
    .nav-tab.active { background: #273449; color: white; }
    .view[hidden] { display: none; }
    .kpis { grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 14px 0; }
    .two { grid-template-columns: minmax(0, 1.15fr) minmax(340px, .85fr); }
    .card {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      min-width: 0;
    }
    .hero-card { border-color: #345b8b; background: #101925; }
    .attention-card { border-color: #615233; background: #181710; }
    .metric-card { min-height: 116px; position: relative; overflow: hidden; }
    .metric-card::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--metric, var(--blue)); }
    .metric-green { --metric: var(--green); }
    .metric-blue { --metric: var(--blue); }
    .metric-amber { --metric: var(--amber); }
    .metric-red { --metric: var(--red); }
    .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; font-weight: 700; }
    .value { font-size: 30px; font-weight: 780; margin-top: 10px; font-variant-numeric: tabular-nums; }
    .sub { color: var(--muted); font-size: 13px; margin-top: 4px; }
    .session-summary { margin-top: 16px; }
    .session-title { font-size: 18px; color: white; font-weight: 720; overflow-wrap: anywhere; }
    .session-meta { margin-top: 5px; color: var(--muted); }
    .session-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 18px; }
    .receipt-summary { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; align-items: center; }
    .risk-flow { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
    .risk-chip { border: 1px solid var(--line); border-radius: 6px; padding: 6px 9px; font-size: 12px; font-weight: 750; text-transform: capitalize; }
    .risk-chip.high { color: #ffc4ce; border-color: rgba(242,125,143,.45); background: var(--red-soft); }
    .risk-chip.medium { color: #ffe2a4; border-color: rgba(246,189,96,.45); background: var(--amber-soft); }
    .risk-chip.low { color: #bff5df; border-color: rgba(53,211,153,.45); background: var(--green-soft); }
    .risk-arrow { color: var(--muted); font-weight: 800; }
    .receipt-note { color: var(--muted); font-size: 12px; margin-top: 10px; }
    .bar-row { display: grid; grid-template-columns: minmax(140px, 1fr) minmax(120px, 1.5fr) 88px; gap: 12px; align-items: center; margin: 11px 0; padding: 5px 0; }
    .bar-row.clickable, tr.clickable { cursor: pointer; }
    .bar-row.clickable { border-radius: 6px; }
    .bar-row.clickable:hover { background: rgba(255,255,255,.03); }
    .bar-row.clickable:hover .bar-label, tr.clickable:hover td { color: white; }
    .bar-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #dce6f6; }
    .bar-shell { height: 8px; background: #080d13; border-radius: 99px; overflow: hidden; border: 1px solid #222d3b; }
    .bar { height: 100%; background: var(--blue); border-radius: 99px; min-width: 2px; }
    .amount { text-align: right; color: #dce6f6; font-variant-numeric: tabular-nums; }
    .insight { border-left: 3px solid var(--amber); padding: 8px 0 8px 13px; margin: 12px 0; }
    .insight strong { display: block; margin-bottom: 3px; color: white; }
    .pill-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
    .pill { border: 1px solid var(--line); background: #0b1118; border-radius: 999px; padding: 5px 9px; color: #bdc9d9; font-size: 11px; }
    .outcome-pill.useful { color: #bff5df; border-color: rgba(53,211,153,.38); background: var(--green-soft); }
    .outcome-pill.rework { color: #ffe2a4; border-color: rgba(246,189,96,.38); background: var(--amber-soft); }
    .outcome-pill.abandoned { color: #ffc4ce; border-color: rgba(242,125,143,.38); background: var(--red-soft); }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid var(--line); padding: 11px 8px; text-align: left; }
    th { color: var(--muted); font-weight: 600; }
    td:last-child, th:last-child { text-align: right; }
    tr.clickable:hover { background: rgba(112,167,255,.05); }
    .row-action { min-height: 30px; padding: 5px 9px; color: #ddecff; background: var(--blue-soft); border-color: #3d6594; font-size: 12px; }
    .empty { color: var(--muted); padding: 16px; border: 1px dashed var(--line); border-radius: 8px; }
    .detail-section { padding: 20px 0; border-bottom: 1px solid var(--line); }
    .detail-section:last-child { border-bottom: 0; }
    .verdict-card { border: 1px solid var(--line-strong); border-left: 4px solid var(--blue); border-radius: 8px; padding: 16px; background: #101925; margin-top: 14px; }
    .verdict-card.high { border-left-color: var(--amber); background: #181710; }
    .verdict-card.useful { border-left-color: var(--green); background: #101b17; }
    .verdict-card h3 { font-size: 17px; margin: 0 0 8px; }
    .verdict-card ul { margin: 10px 0 0; padding-left: 18px; color: #d9e4f2; line-height: 1.45; }
    details.aiw-details { border: 1px solid var(--line); border-radius: 8px; background: #0b1118; margin-top: 12px; }
    details.aiw-details summary { cursor: pointer; padding: 12px 14px; color: #dce6f6; font-weight: 750; }
    details.aiw-details[open] summary { border-bottom: 1px solid var(--line); }
    .details-body { padding: 14px; }
    .insight-list { margin: 8px 0 0; padding-left: 18px; }
    .insight-list li { margin: 6px 0; line-height: 1.45; }
    .costliest-step { margin: 12px 0; padding: 12px 14px; border: 1px solid var(--line-strong); border-left: 3px solid var(--metric-red, #d9534f); border-radius: 8px; background: var(--surface-raised); }
    .costliest-head { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
    .costliest-share { text-transform: none; letter-spacing: 0; font-weight: 600; color: var(--text, inherit); }
    .costliest-body { margin-top: 6px; font-size: 14px; overflow-wrap: anywhere; }
    .waste-note { margin: 12px 0; padding: 10px 14px; border: 1px solid var(--line-strong); border-left: 3px solid #e0a800; border-radius: 8px; background: var(--surface-raised); font-size: 13px; line-height: 1.45; }
    .prompt-text, .prompt-suggested { margin: 8px 0 4px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface-raised); font-size: 13px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 240px; overflow-y: auto; }
    .prompt-suggested { border-left: 3px solid var(--metric-green, #2e9e5b); }
    .risk-tag { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; padding: 2px 8px; border-radius: 999px; vertical-align: middle; margin-left: 8px; border: 1px solid var(--line-strong); }
    .risk-high { color: #d9534f; border-color: #d9534f; }
    .risk-medium { color: #e0a800; border-color: #e0a800; }
    .risk-low { color: var(--muted); }
    .prompt-opener { margin: 6px 0 0; line-height: 1.5; }
    .prompt-opener-label { display: inline-block; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin-right: 6px; }
    .asks-table td { vertical-align: top; }
    .asks-table .ask-turn { color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
    .asks-table .ask-prompt { overflow-wrap: anywhere; white-space: pre-wrap; }
    .asks-table .ask-tools { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
    .asks-table .ask-cost { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
    .asks-table .ask-share { display: block; font-size: 11px; color: var(--muted); }
    .evt-turn { color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
    .timeline-note { margin: 4px 0 8px; font-size: 12px; color: var(--muted); }
    .mini-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 10px 0 12px; }
    .mini { border-left: 2px solid var(--line-strong); padding: 5px 10px; min-width: 0; }
    .mini strong { display: block; font-size: 16px; margin-top: 4px; overflow-wrap: anywhere; }
    .section-title { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; margin: 0 0 16px; }
    .section-title h2 { margin: 0; }
    .section-title p { font-size: 12px; }
    .mono { font-variant-numeric: tabular-nums; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .privacy-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .privacy-item { display: flex; align-items: center; gap: 9px; color: #c8d3e1; padding: 10px 0; }
    .privacy-check { color: var(--green); font-weight: 900; }
    .prompt-shell { display: grid; grid-template-columns: minmax(0, 1fr) minmax(360px, .86fr); gap: 14px; align-items: start; }
    .prompt-box, .brief-box {
      width: 100%;
      min-height: 260px;
      resize: vertical;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: #090f16;
      color: var(--text);
      padding: 14px;
      font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .prompt-box:focus, .brief-box:focus { outline: 2px solid var(--blue); outline-offset: 2px; }
    .prompt-form-row { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 10px; margin: 12px 0; }
    .prompt-result { margin-top: 14px; }
    .risk-card { border-left: 3px solid var(--amber); padding: 14px; border-radius: 8px; background: #111923; }
    .risk-card.high { border-left-color: var(--red); background: #191116; }
    .risk-card.low { border-left-color: var(--green); background: #101b17; }
    .risk-card h3 { font-size: 16px; margin-bottom: 8px; }
    .prompt-list { margin: 10px 0 0; padding-left: 18px; color: #d9e4f2; line-height: 1.55; }
    .copy-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
    .handoff-cta {
      margin-top: 16px;
      padding: 14px;
      border: 1px solid rgba(86,157,231,.36);
      border-radius: 8px;
      background: linear-gradient(135deg, rgba(47,111,189,.18), rgba(53,211,153,.1));
      display: grid;
      gap: 10px;
    }
    .handoff-cta h4 { margin: 0; font-size: 13px; }
    .handoff-cta p { font-size: 12px; }
    .handoff-cta .btn-primary { width: fit-content; }
    .prompt-opt-in { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 14px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface-raised); font-size: 13px; color: #d5deea; cursor: pointer; }
    .prompt-opt-in input { width: 15px; height: 15px; accent-color: var(--blue, #4f8cff); }
    .prompt-opt-in .hint { flex-basis: 100%; color: #93a2b8; font-size: 12px; }
    .drawer-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.62); opacity: 0; pointer-events: none; transition: opacity .18s ease; z-index: 20; }
    .drawer-backdrop.open { opacity: 1; pointer-events: auto; }
    .drawer {
      position: fixed; z-index: 21; top: 0; right: 0; bottom: 0; width: min(620px, 94vw);
      background: #0d1219; border-left: 1px solid var(--line-strong); box-shadow: -18px 0 46px rgba(0,0,0,.42);
      transform: translateX(102%); visibility: hidden; transition: transform .2s ease, visibility .2s ease; display: flex; flex-direction: column;
    }
    .drawer.open { transform: translateX(0); visibility: visible; }
    .drawer-header { display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 18px 22px; border-bottom: 1px solid var(--line); }
    .drawer-header p { font-size: 12px; margin-top: 2px; }
    .drawer-content { padding: 0 22px 30px; overflow-y: auto; }
    .outcome-control { margin-top: 16px; padding: 18px; border: 1px solid var(--line-strong); border-radius: 8px; background: var(--surface-raised); }
    .outcome-control h3 { font-size: 16px; }
    .outcome-options { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 14px; }
    .outcome-button { min-height: 44px; background: #0d131b; }
    .outcome-button.useful { color: #bff5df; border-color: rgba(53,211,153,.45); }
    .outcome-button.rework { color: #ffe2a4; border-color: rgba(246,189,96,.45); }
    .outcome-button.abandoned { color: #ffc4ce; border-color: rgba(242,125,143,.45); }
    .outcome-button.selected.useful { background: var(--green-soft); box-shadow: inset 0 0 0 1px var(--green); }
    .outcome-button.selected.rework { background: var(--amber-soft); box-shadow: inset 0 0 0 1px var(--amber); }
    .outcome-button.selected.abandoned { background: var(--red-soft); box-shadow: inset 0 0 0 1px var(--red); }
    .toast { position: fixed; right: 20px; bottom: 20px; z-index: 30; max-width: 420px; padding: 12px 14px; border: 1px solid var(--line-strong); border-radius: 8px; background: #18212c; color: white; box-shadow: 0 12px 32px rgba(0,0,0,.35); opacity: 0; transform: translateY(12px); pointer-events: none; transition: opacity .18s ease, transform .18s ease; }
    .toast.show { opacity: 1; transform: translateY(0); }
    .toast.error { border-color: rgba(242,125,143,.55); background: #2a171d; }
    .loading { color: var(--muted); padding: 18px 0; }
    @media (max-width: 860px) {
      main { padding: 18px; }
      header { flex-direction: column; }
      .actions { width: 100%; }
      .actions select { flex: 1; }
      .kpis { grid-template-columns: 1fr 1fr; }
      .two { grid-template-columns: 1fr; }
      .prompt-shell { grid-template-columns: 1fr; }
      .receipt-summary { grid-template-columns: 1fr; }
      .bar-row { grid-template-columns: 1fr; gap: 6px; }
      .amount { text-align: left; }
    }
    @media (max-width: 560px) {
      main { padding: 14px 12px 36px; }
      .brand-copy p, .link-button { display: none; }
      .kpis { grid-template-columns: 1fr; }
      .metric-card { min-height: 104px; }
      .mini-grid, .outcome-options, .privacy-grid { grid-template-columns: 1fr; }
      .drawer { width: 100vw; }
      .drawer-header, .drawer-content { padding-left: 16px; padding-right: 16px; }
      table { min-width: 620px; }
      .table-wrap { overflow-x: auto; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">AW</div>
      <div class="brand-copy">
        <div class="actions">
          <h1>AIWatcher Local</h1>
          <span class="status-badge"><span class="status-dot"></span>Private and local</span>
        </div>
        <p>Your AI coding work, outcomes, and improvement signals.</p>
      </div>
    </div>
    <div class="actions">
      <select id="days" onchange="load()">
        <option value="1">Last 24 hours</option>
        <option value="7" selected>Last 7 days</option>
        <option value="30">Last 30 days</option>
      </select>
      <button class="btn-quiet" onclick="load()">Refresh data</button>
      <a class="link-button" href="https://www.getaiwatcher.com" target="_blank" rel="noreferrer">Enterprise</a>
    </div>
  </header>

  <nav class="product-nav" aria-label="AIWatcher Local sections">
    <button class="nav-tab active" data-view="today" onclick="showView('today')">Today</button>
    <button class="nav-tab" data-view="prompt" onclick="showView('prompt')">Prompt</button>
    <button class="nav-tab" data-view="projects" onclick="showView('projects')">Projects</button>
    <button class="nav-tab" data-view="sessions" onclick="showView('sessions')">Sessions</button>
    <button class="nav-tab" data-view="receipts" onclick="showView('receipts')">Receipts</button>
    <button class="nav-tab" data-view="insights" onclick="showView('insights')">Insights</button>
  </nav>

  <section id="view-today" class="view">
    <section class="grid two" style="margin-bottom:14px">
      <div class="card hero-card">
        <div class="section-title"><div><h2>Latest AI work</h2><p>Your most recent local session and its outcome.</p></div></div>
        <div id="latestSession"></div>
      </div>
      <div class="card attention-card">
        <div class="section-title"><div><h2>One thing worth changing</h2><p>The highest-signal local recommendation for your next run.</p></div></div>
        <div id="todayRecommendation"></div>
      </div>
    </section>
    <section class="grid kpis">
      <div class="card metric-card metric-green"><div class="label">Useful outcomes</div><div class="value" id="usefulOutcomes">-</div><div class="sub">Value per useful change: <span id="costPerUseful">-</span></div></div>
      <div class="card metric-card metric-amber"><div class="label">Preflight decisions</div><div class="value" id="preflightDecisions">-</div><div class="sub"><span id="windowLabel">-</span></div></div>
      <div class="card metric-card metric-blue"><div class="label">Sessions observed</div><div class="value" id="sessions">-</div><div class="sub">This machine only</div></div>
      <div class="card metric-card metric-red"><div class="label">API-equivalent value</div><div class="value" id="apiValue">-</div><div class="sub">Excludes subscription allocation</div></div>
    </section>

    <section class="card" style="margin-bottom:14px">
      <div class="section-title"><div><h2>Latest intervention</h2><p>What AIWatcher changed before execution and what happened afterward.</p></div></div>
      <div id="latestIntervention"></div>
    </section>

    <section class="grid two">
      <div class="card">
        <div class="section-title"><div><h2>Projects Driving AI Usage</h2><p>Click a project to inspect local sessions, models, and tools.</p></div></div>
        <div id="projects"></div>
      </div>
      <div class="card">
        <div class="section-title"><div><h2>Recent sessions</h2><p>Review work and record whether it was useful.</p></div></div>
        <div class="table-wrap"><table>
          <thead><tr><th>Tool</th><th>Project</th><th>Tokens</th><th></th></tr></thead>
          <tbody id="recent"></tbody>
        </table></div>
      </div>
    </section>

    <section class="grid two" style="margin-top:14px">
      <div class="card">
        <h2>Models and Tools</h2>
        <div id="models"></div>
      </div>
      <div class="card">
        <div class="section-title"><div><h2>Privacy at a glance</h2><p>Your local trust boundary stays visible.</p></div></div>
        <div class="privacy-grid" id="privacy"></div>
        <div id="insights" hidden></div>
      </div>
    </section>
  </section>

  <section id="view-prompt" class="view" hidden>
    <div class="prompt-shell">
      <div class="card">
        <div class="section-title"><div><h2>Prompt Companion</h2><p>For surfaces AIWatcher cannot hook directly — Claude Desktop general chat, Codex Desktop chat, claude.ai/other browser chat. Draft here, then copy the result over yourself.</p></div></div>
        <textarea id="promptInput" class="prompt-box" placeholder="Paste or draft a prompt here. Example: Refactor the entire codebase and delete old auth secrets"></textarea>
        <div class="prompt-form-row">
          <select id="promptTool">
            <option value="codex">Codex</option>
            <option value="claude">Claude</option>
            <option value="cursor">Cursor</option>
            <option value="agent">Generic agent</option>
          </select>
          <input id="promptCwd" class="prompt-box" style="min-height:38px;resize:none" placeholder="Working directory, optional">
        </div>
        <div class="actions">
          <button class="btn-primary" onclick="preflightPrompt()">Preflight prompt</button>
          <button class="btn-quiet" onclick="clearPromptCompanion()">Clear</button>
        </div>
        <p style="margin-top:12px">Claude Code CLI, Codex CLI/TUI, and Cursor already get this automatically via an installed hook — you don't need this tab for them. This is manual: nothing is sent anywhere or intercepted on your behalf, and prompt text is analyzed locally and not persisted.</p>
      </div>
      <div class="card">
        <div class="section-title"><div><h2>Decision</h2><p>Use the brief, edit it, or paste the original unchanged.</p></div></div>
        <div id="promptResult" class="prompt-result"><div class="empty">Run a preflight to see risk, reasoning, and a safer execution brief.</div></div>
      </div>
    </div>
  </section>

  <section id="view-projects" class="view" hidden>
    <div class="card">
      <div class="section-title">
        <div><h2>Projects</h2><p>Local repos and folders absorbing AI coding work.</p></div>
        <span class="pill" id="projectWindow">-</span>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Project</th><th>Sessions</th><th>Tokens</th><th>Model calls</th><th>API value</th></tr></thead>
        <tbody id="projectRows"></tbody>
      </table></div>
    </div>
  </section>

  <section id="view-sessions" class="view" hidden>
    <div class="card">
      <div class="section-title">
        <div><h2>Sessions</h2><p>Recent local AI runs. Click any row to inspect locally — prompt text is shown for your own review only, never uploaded.</p></div>
        <span class="pill">Local machine only</span>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Tool</th><th>Project</th><th>Model</th><th>Tokens</th><th></th></tr></thead>
        <tbody id="sessionRows"></tbody>
      </table></div>
    </div>
  </section>

  <section id="view-receipts" class="view" hidden>
    <div class="card">
      <div class="section-title">
        <div><h2>Intervention receipts</h2><p>Risk decisions, predicted impact, resulting usage, and developer outcomes.</p></div>
        <span class="pill">Prompt text stays private</span>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Time</th><th>Tool / project</th><th>Decision</th><th>Risk change</th><th>Result</th><th></th></tr></thead>
        <tbody id="receiptRows"></tbody>
      </table></div>
    </div>
  </section>

  <section id="view-insights" class="view" hidden>
    <section class="grid two">
      <div class="card">
        <div class="section-title"><div><h2>Local Insights</h2><p>Suggestions to reduce waste without uploading prompts.</p></div></div>
        <div id="insightRows"></div>
      </div>
      <div class="card">
        <h2>Daily Journal</h2>
        <div id="journal"></div>
        <div class="detail-section">
          <h2>Local Weekly Report</h2>
          <div id="report"></div>
        </div>
      </div>
    </section>
    <section class="grid two" style="margin-top:14px">
      <div class="card">
        <h2>Privacy Contract</h2>
        <p>AIWatcher Local is read-only and local-first. Summaries avoid source and prompt content by default.</p>
        <div class="pill-row" id="privacyLarge"></div>
      </div>
      <div class="card">
        <h2>Enterprise handoff</h2>
        <p>Enterprise adds team history, policy controls, HITL approvals, evidence packs, and integrations.</p>
        <div class="pill-row">
          <span class="pill">Team visibility</span>
          <span class="pill">Budget guardrails</span>
          <span class="pill">Audit evidence</span>
          <span class="pill">SSO/RBAC</span>
        </div>
      </div>
    </section>
  </section>
</main>
<div class="drawer-backdrop" id="drawerBackdrop" onclick="closeDrawer()"></div>
<aside class="drawer" id="detailDrawer" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="drawerTitle">
  <div class="drawer-header">
    <div><h2 id="drawerTitle">Work detail</h2><p>Local metadata only</p></div>
    <button class="btn-quiet" onclick="closeDrawer()" aria-label="Close work detail">Close</button>
  </div>
  <div class="drawer-content" id="detailContent"><div class="loading">Select a project or session to inspect.</div></div>
</aside>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script>
function maxValue(rows) {
  return Math.max(0.000001, ...rows.map(r => Number(r.api_value_usd || 0)));
}
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[ch]));
}
let receiptCache = [];
function riskFlow(receipt) {
  const original = `<span class="risk-chip ${esc(receipt.original_risk)}">${esc(receipt.original_risk)} · ${esc(receipt.original_score ?? '—')}</span>`;
  if (receipt.selected_score === null || receipt.selected_score === undefined) return `<div class="risk-flow">${original}</div>`;
  return `<div class="risk-flow">${original}<span class="risk-arrow">→</span><span class="risk-chip ${esc(receipt.selected_risk || 'low')}">${esc(receipt.selected_risk || 'unknown')} · ${esc(receipt.selected_score)}</span><span class="pill">${esc(receipt.risk_points_reduced || 0)} points reduced</span></div>`;
}
function predictedStats(receipt) {
  const p = receipt.predicted || {};
  if (!p.available) return `<div class="empty">Savings estimate unavailable. ${esc(p.basis || 'More comparable local history is required.')}</div>`;
  return `<div class="mini-grid">
    <div class="mini"><span class="label">Predicted token savings</span><strong>${esc(p.tokens_label || '—')}</strong></div>
    <div class="mini"><span class="label">Model calls avoided</span><strong>${esc(p.model_calls_label || '—')}</strong></div>
    <div class="mini"><span class="label">Tool calls avoided</span><strong>${esc(p.tool_calls_label || '—')}</strong></div>
    <div class="mini"><span class="label">API-equivalent savings</span><strong>${esc(p.api_value_label || '—')}</strong></div>
  </div><p class="receipt-note">${esc(p.confidence || 'unknown')} confidence · ${esc(p.basis || '')}</p>`;
}
function renderLatestReceipt(receipt) {
  if (!receipt) return '<div class="empty">No prompt intervention recorded in this window.</div>';
  return `<div class="receipt-summary"><div>
    <div class="session-title">${esc(receipt.decision_label)}</div>
    <div class="session-meta">${esc(receipt.tool)} · ${esc(receipt.project)} · ${esc(dateLabel(receipt.created_at))}</div>
    ${riskFlow(receipt)}
    ${predictedStats(receipt)}
    <div class="pill-row"><span class="pill">${esc(receipt.session_status)}</span>${outcomePill(receipt.outcome)}</div>
  </div><button class="btn-primary" onclick="openReceipt('${esc(receipt.id)}')">Review receipt</button></div>`;
}
function renderReceiptRows(receipts) {
  if (!receipts.length) return '<tr><td colspan="6"><div class="empty">No interventions recorded in this window.</div></td></tr>';
  return receipts.map(receipt => `<tr class="clickable" onclick="openReceipt('${esc(receipt.id)}')">
    <td>${esc(dateLabel(receipt.created_at))}</td>
    <td><strong>${esc(receipt.tool)}</strong><br><span class="sub">${esc(receipt.project)}</span></td>
    <td>${esc(receipt.decision_label)}</td>
    <td>${esc(receipt.original_score ?? '—')} → ${esc(receipt.selected_score ?? '—')}</td>
    <td>${esc(receipt.outcome || receipt.session_status)}</td>
    <td><button class="row-action">Review</button></td>
  </tr>`).join('');
}
function openReceipt(receiptId) {
  const receipt = receiptCache.find(item => item.id === receiptId);
  if (!receipt) return;
  openDrawer('Intervention receipt');
  const actual = receipt.actual
    ? `<section class="detail-section"><h3>Observed session</h3>
       <div class="mini-grid">
         <div class="mini"><span class="label">Tokens</span><strong>${esc(receipt.actual.tokens_label)}</strong></div>
         <div class="mini"><span class="label">Model calls</span><strong>${esc(receipt.actual.model_calls)}</strong></div>
         <div class="mini"><span class="label">Tool calls</span><strong>${esc(receipt.actual.tool_calls)}</strong></div>
         <div class="mini"><span class="label">API value</span><strong>${esc(receipt.actual.api_value_label)}</strong></div>
       </div>${receipt.actual.reliable ? '' : `<p>${esc(receipt.actual.reason)}</p>`}
       ${receipt.session_id ? `<button class="btn-quiet" onclick="selectSession('${esc(receipt.session_id)}')">Open resulting session</button>` : ''}
      </section>`
    : `<section class="detail-section"><h3>Observed session</h3><div class="empty">Waiting for a matching local session. Refresh after the agent finishes.</div></section>`;
  const inferred = receipt.inferred
    ? `<section class="detail-section"><h3>${esc(receipt.inferred.label)}</h3>
       <div class="mini-grid">
         <div class="mini"><span class="label">Tokens below baseline</span><strong>${esc(receipt.inferred.tokens_label || '—')}</strong></div>
         <div class="mini"><span class="label">Model calls</span><strong>${esc(receipt.inferred.model_calls ?? '—')}</strong></div>
         <div class="mini"><span class="label">Tool calls</span><strong>${esc(receipt.inferred.tool_calls ?? '—')}</strong></div>
         <div class="mini"><span class="label">API-equivalent</span><strong>${esc(receipt.inferred.api_value_label || '—')}</strong></div>
       </div><p>${esc(receipt.inferred.disclaimer)}</p></section>`
    : '';
  document.getElementById('detailContent').innerHTML = `<section class="detail-section">
    <h2>${esc(receipt.decision_label)}</h2>
    <p>${esc(receipt.tool)} · ${esc(receipt.project)} · ${esc(dateLabel(receipt.created_at))}</p>
    ${riskFlow(receipt)}
    <div class="pill-row"><span class="pill">${esc(receipt.session_status)}</span>${outcomePill(receipt.outcome)}</div>
  </section><section class="detail-section"><h3>Predicted before execution</h3>${predictedStats(receipt)}</section>${actual}${inferred}
  <section class="detail-section"><h3>Privacy evidence</h3><p>Prompt text is not stored. This receipt contains hashes, policy findings, aggregate usage, and your outcome only.</p></section>`;
}
async function copyText(value, label = 'Copied') {
  try {
    await navigator.clipboard.writeText(value || '');
    showToast(label);
  } catch (error) {
    showToast('Copy failed. Select the text manually.', 'error');
  }
}
function clearPromptCompanion() {
  document.getElementById('promptInput').value = '';
  document.getElementById('promptResult').innerHTML = '<div class="empty">Run a preflight to see risk, reasoning, and a safer execution brief.</div>';
}
async function preflightPrompt() {
  const prompt = document.getElementById('promptInput').value;
  const tool = document.getElementById('promptTool').value;
  const cwd = document.getElementById('promptCwd').value;
  const resultNode = document.getElementById('promptResult');
  if (!prompt.trim()) {
    resultNode.innerHTML = '<div class="empty">Write or paste a prompt first.</div>';
    return;
  }
  resultNode.innerHTML = '<div class="loading">Checking cost, scope, and safety pressure...</div>';
  try {
    const res = await fetch('/api/preflight', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, tool, cwd })
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      resultNode.innerHTML = `<div class="empty">${esc(data.error || 'Could not preflight prompt.')}</div>`;
      return;
    }
    const riskTone = data.risk || 'low';
    resultNode.innerHTML = `<div class="risk-card ${esc(riskTone)}">
      <h3>Risk: ${esc(data.risk)} · score ${esc(data.score)}</h3>
      <p>${esc(data.impact_label)}</p>
      <h3 style="margin-top:14px">Findings</h3>
      <ul class="prompt-list">${data.findings.map(item => `<li>${esc(item)}</li>`).join('')}</ul>
      <h3 style="margin-top:14px">Suggestions</h3>
      <ul class="prompt-list">${data.suggestions.map(item => `<li>${esc(item)}</li>`).join('')}</ul>
    </div>
    <div class="detail-section">
      <h3>Execution brief</h3>
      <textarea id="promptBrief" class="brief-box">${esc(data.suggested_prompt)}</textarea>
      <div class="copy-row">
        <button class="btn-primary" onclick="copyText(document.getElementById('promptBrief').value, 'Execution brief copied — paste it into your AI tool now')">Copy brief</button>
        <button class="btn-quiet" onclick="copyText(document.getElementById('promptInput').value, 'Original prompt copied — paste it into your AI tool now')">Copy original</button>
      </div>
      <p style="margin-top:10px">Paste whichever you choose as the first message in Claude Desktop, Codex Desktop, or your browser chat. AIWatcher cannot submit it for you on these surfaces.</p>
      <p style="margin-top:6px">${esc(data.privacy)}</p>
    </div>`;
  } catch (error) {
    resultNode.innerHTML = '<div class="empty">Could not reach the local AIWatcher server.</div>';
  }
}
let toastTimer;
function showToast(message, kind = 'success') {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.className = `toast ${kind === 'error' ? 'error' : ''} show`;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { toast.className = 'toast'; }, 3200);
}
function openDrawer(title) {
  document.getElementById('drawerTitle').textContent = title;
  document.getElementById('drawerBackdrop').classList.add('open');
  document.getElementById('detailDrawer').classList.add('open');
  document.getElementById('detailDrawer').setAttribute('aria-hidden', 'false');
  document.body.classList.add('drawer-open');
}
function closeDrawer() {
  document.getElementById('drawerBackdrop').classList.remove('open');
  document.getElementById('detailDrawer').classList.remove('open');
  document.getElementById('detailDrawer').setAttribute('aria-hidden', 'true');
  document.body.classList.remove('drawer-open');
}
function outcomePill(outcome) {
  const value = outcome || 'not marked';
  const tone = outcome || '';
  return `<span class="pill outcome-pill ${tone}">Outcome: ${esc(value)}</span>`;
}
function outcomeEvidencePill(session) {
  if (!session || session.outcome) return '';
  if (session.inferred_outcome === 'useful') return '<span class="pill outcome-pill useful">Evidence: likely useful</span>';
  if (session.inferred_outcome === 'needs_review') return '<span class="pill outcome-pill rework">Evidence: review changes</span>';
  return '';
}
function renderEvidence(evidence) {
  if (!evidence) return '';
  const commits = evidence.commits || [];
  const files = evidence.changed_files || [];
  const tests = evidence.tests || [];
  const reasons = evidence.reasons || [];
  return `<section class="detail-section"><h3>Outcome evidence</h3>
    <p>Local git/test signals. AIWatcher stores metadata, not source diffs.</p>
    <div class="mini-grid">
      <div class="mini"><span class="label">Inferred outcome</span><strong>${esc(evidence.inferred_outcome || 'not enough evidence')}</strong></div>
      <div class="mini"><span class="label">Confidence</span><strong>${esc(evidence.confidence || 'low')}</strong></div>
      <div class="mini"><span class="label">Nearby commits</span><strong>${esc(commits.length)}</strong></div>
      <div class="mini"><span class="label">Changed files</span><strong>${esc(files.length)}</strong></div>
    </div>
    ${tests.length ? `<div class="pill-row"><span class="pill">${esc(tests.length)} recent test artifact${tests.length === 1 ? '' : 's'}</span></div>` : ''}
    ${reasons.length ? `<ul class="insight-list">${reasons.map(reason => `<li>${esc(reason)}</li>`).join('')}</ul>` : ''}
    ${commits.length ? `<div class="pill-row">${commits.slice(0, 4).map(commit => `<span class="pill">commit ${esc(commit.sha)}</span>`).join('')}</div>` : ''}
    ${files.length ? `<details class="aiw-details"><summary>${esc(files.length)} changed file${files.length === 1 ? '' : 's'}</summary><div class="details-body"><div class="pill-row">${files.slice(0, 12).map(file => `<span class="pill">${esc(file)}</span>`).join('')}</div></div></details>` : ''}
  </section>`;
}
function eventTypeLabel(type) {
  const labels = {
    assistant: 'Model reasoning',
    assistant_tool_use: 'Tool-driven work',
    tool_result: 'Tool results',
    user: 'User turns',
    'last-prompt': 'Prompt metadata',
    mode: 'Mode metadata'
  };
  return labels[type] || type;
}
function compactText(value, limit = 420) {
  const text = String(value || '');
  return text.length > limit ? `${text.slice(0, limit)}...` : text;
}
function sessionVerdict(s) {
  const evidence = s.outcome_evidence || s.evidence || {};
  const tokens = Number(s.tokens || 0);
  const cost = Number(s.api_value_usd || 0);
  const toolCalls = Number(s.tool_calls || 0);
  const likelyUseful = !s.outcome && evidence.inferred_outcome === 'useful';
  const highCost = cost >= 5 || tokens >= 500000 || toolCalls >= 250;
  let title = 'Review this AI work';
  if (s.outcome) title = `Marked ${s.outcome}`;
  else if (likelyUseful && highCost) title = 'Likely useful, but expensive';
  else if (likelyUseful) title = 'Likely useful, needs confirmation';
  else if (highCost) title = 'High-cost session, needs review';
  const bullets = [];
  if (likelyUseful) bullets.push('A nearby commit or test signal suggests this produced useful work.');
  if (cost >= 5) bullets.push(`${s.api_value} API-equivalent value is high for one local session.`);
  if (tokens >= 500000) bullets.push(`${s.tokens_label} tokens indicates heavy context pressure.`);
  if (toolCalls >= 250) bullets.push(`${s.tool_calls} tool calls suggests broad search, retries, or loop-like work.`);
  if (!bullets.length) bullets.push('No urgent cost or outcome signal was detected.');
  return { title, tone: likelyUseful ? 'useful' : highCost ? 'high' : '', bullets };
}
function renderVerdict(s) {
  const verdict = sessionVerdict(s);
  return `<div class="verdict-card ${esc(verdict.tone)}"><h3>${esc(verdict.title)}</h3>
    <p>Confirm the outcome, then use the expensive asks below to improve the next run.</p>
    <ul>${verdict.bullets.map(item => `<li>${esc(item)}</li>`).join('')}</ul>
  </div>`;
}
function renderHandoff(capsule) {
  const usage = capsule.usage || {};
  const evidence = capsule.evidence || {};
  const changedFiles = evidence.changed_files || [];
  const target = capsule.target || 'generic';
  const includePrompt = !!capsule.include_prompt_excerpt;
  return `<section class="detail-section">
    <h2>Fresh-session handoff</h2>
    <p>Use this when a session gets expensive, stale, or hard to continue. It keeps the next ${esc(capsule.target_label || 'AI tool')} focused without carrying the whole chat history.</p>
    <div class="mini-grid">
      <div class="mini"><span class="label">Previous usage</span><strong>${esc(usage.tokens_label || '—')}</strong></div>
      <div class="mini"><span class="label">API value</span><strong>${esc(usage.api_value_label || '—')}</strong></div>
      <div class="mini"><span class="label">Model calls</span><strong>${esc(usage.model_calls ?? '—')}</strong></div>
      <div class="mini"><span class="label">Evidence</span><strong>${esc((evidence.commits || []).length)} commits</strong></div>
    </div>
    <div class="copy-row">
      ${['generic','claude','codex','cursor','vscode'].map(item => `<button class="${item === target ? 'btn-primary' : 'btn-quiet'}" onclick="openHandoff('${esc(capsule.session_id)}','${item}', ${includePrompt})">${esc(item === 'generic' ? 'Generic' : item)}</button>`).join('')}
    </div>
    <label class="prompt-opt-in">
      <input type="checkbox" ${includePrompt ? 'checked' : ''} onchange="openHandoff('${esc(capsule.session_id)}','${target}', this.checked)">
      <span class="prompt-opt-in-label">Include prompt excerpt <span class="pill">Privacy opt-in</span></span>
      <span class="hint">Off by default: everything else in this brief is metadata (counts, hashes, file paths). This adds your actual prompt text from the costliest turn, so review it before pasting into another tool.</span>
    </label>
  </section>
  <section class="detail-section"><h3>Why hand off now</h3>
    <ul class="insight-list">${(capsule.warnings || []).map(item => `<li>${esc(item)}</li>`).join('')}</ul>
  </section>
  <section class="detail-section"><h3>Paste into the next AI tool</h3>
    <textarea id="handoffBrief" class="brief-box">${esc(capsule.next_brief || '')}</textarea>
    <div class="copy-row"><button class="btn-primary" onclick="copyText(document.getElementById('handoffBrief').value, 'Handoff brief copied')">Copy handoff brief</button></div>
    ${changedFiles.length ? `<details class="aiw-details"><summary>${esc(changedFiles.length)} changed file${changedFiles.length === 1 ? '' : 's'} to inspect</summary><div class="details-body"><div class="pill-row">${changedFiles.slice(0, 12).map(file => `<span class="pill">${esc(file)}</span>`).join('')}</div></div></details>` : ''}
  </section>`;
}
async function openHandoff(sessionId, target = 'generic', includePrompt = false) {
  openDrawer('Handoff capsule');
  document.getElementById('detailContent').innerHTML = '<div class="loading">Building local handoff capsule...</div>';
  const res = await fetch(`/api/handoff?id=${encodeURIComponent(sessionId)}&target=${encodeURIComponent(target)}&prompt=${includePrompt ? '1' : '0'}`);
  const capsule = await res.json();
  if (capsule.error) {
    document.getElementById('detailContent').innerHTML = `<div class="empty">${esc(capsule.error)}</div>`;
    return;
  }
  document.getElementById('detailContent').innerHTML = renderHandoff(capsule);
}
function dateLabel(value) {
  if (!value) return 'unknown';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
function costliestShare(event, session) {
  const total = Number(session.api_value_usd || 0);
  const part = Number(event.api_value_usd || 0);
  if (total <= 0 || part <= 0) return '';
  const pct = Math.round(part / total * 100);
  return pct >= 1 ? ` · ${pct}% of session cost` : '';
}
function bars(rows, valueKey = "api_value_label", kind = "project") {
  if (!rows.length) return '<div class="empty">No local usage found for this window.</div>';
  const max = maxValue(rows);
  return rows.map(row => {
    const width = Math.max(2, Math.round(Number(row.api_value_usd || 0) / max * 100));
    const id = encodeURIComponent(row.id || row.name);
    const click = kind === "project" ? `onclick="selectProject(decodeURIComponent(this.dataset.id))" data-id="${id}"` : "";
    return `<div class="bar-row ${kind === "project" ? "clickable" : ""}" title="${esc(row.name)}" ${click}>
      <div class="bar-label">${esc(row.short_name || row.name)}</div>
      <div class="bar-shell"><div class="bar" style="width:${width}%"></div></div>
      <div class="amount">${esc(row[valueKey])}</div>
    </div>`;
  }).join('');
}
function miniStats(totals) {
  return `<div class="mini-grid">
    <div class="mini"><span class="label">Sessions</span><strong>${esc(totals.sessions)}</strong></div>
    <div class="mini"><span class="label">API value</span><strong>${esc(totals.api_value)}</strong></div>
    <div class="mini"><span class="label">Tokens</span><strong>${esc(totals.tokens)}</strong></div>
    <div class="mini"><span class="label">Tool calls</span><strong>${esc(totals.tool_calls)}</strong></div>
  </div>`;
}
async function selectProject(project) {
  openDrawer('Project detail');
  document.getElementById('detailContent').innerHTML = '<div class="loading">Loading project activity...</div>';
  const days = document.getElementById('days').value;
  const res = await fetch(`/api/project?days=${days}&project=${encodeURIComponent(project)}`);
  const data = await res.json();
  document.getElementById('drawerTitle').textContent = data.project_short || 'Project detail';
  document.getElementById('detailContent').innerHTML = `<section class="detail-section"><h2>${esc(data.project_short)}</h2>
    ${miniStats(data.totals)}
    </section><section class="detail-section"><h3>Models used</h3>
    ${bars(data.models, "api_value_label", "model")}
    </section><section class="detail-section"><h3>Recent sessions</h3>
    <div class="table-wrap"><table><thead><tr><th>Tool</th><th>Model</th><th>Tokens</th><th></th></tr></thead>
      <tbody>${data.sessions.map(s => `<tr class="clickable" onclick="selectSession('${s.session_id}')">
        <td>${esc(s.tool)}</td><td>${esc(s.model)}</td><td>${esc(s.tokens_label)}</td><td><button class="row-action">Review</button></td>
      </tr>`).join('')}</tbody></table></div></section>`;
}
async function selectSession(sessionId) {
  openDrawer('Session review');
  document.getElementById('detailContent').innerHTML = '<div class="loading">Loading session details...</div>';
  const res = await fetch(`/api/session?id=${encodeURIComponent(sessionId)}`);
  const s = await res.json();
  if (s.error) {
    document.getElementById('detailContent').innerHTML = `<div class="empty">${esc(s.error)}</div>`;
    return;
  }
  document.getElementById('drawerTitle').textContent = 'Session review';
  const summary = s.timeline_summary || {};
  const costliest = summary.costliest;
  const costliestCallout = costliest
    ? `<div class="costliest-step">
        <div class="costliest-head">Costliest step<span class="costliest-share">${costliestShare(costliest, s)}</span></div>
        <div class="costliest-body">${esc(eventTypeLabel(costliest.event_type))} · ${esc(costliest.model)} · ${esc(costliest.tokens_label)} tokens · ${esc(costliest.api_value)}</div>
      </div>`
    : '';
  const costRows = (summary.cost_by_type || []).filter(r => r.api_value_usd > 0)
    .map(r => ({ ...r, name: eventTypeLabel(r.event_type), short_name: eventTypeLabel(r.event_type) }));
  const costBreakdown = costRows.length
    ? `<section class="detail-section"><h3>Cost by event type</h3>
        <p>Where this session's API-equivalent value actually went.</p>
        ${bars(costRows, "label", "type")}
      </section>`
    : '';
  const repeats = summary.repeats || {};
  const wasteNote = repeats.duplicate_events > 0
    ? `<div class="waste-note">Possible rework: ${esc(repeats.duplicate_events)} event(s) repeated identical content${repeats.max_repeat > 2 ? ` (one appeared ${esc(repeats.max_repeat)}x)` : ''}. This often signals a retry loop or the agent re-doing work.</div>`
    : '';
  const turnPrompts = s.turn_prompts || {};
  const meaningfulEvents = (s.events || []).filter(e => Number(e.tokens || 0) > 0 || Number(e.api_value_usd || 0) > 0);
  const hiddenMetadata = Math.max(0, (s.events || []).length - meaningfulEvents.length);
  const shownEvents = meaningfulEvents.slice(0, 80);
  const truncated = meaningfulEvents.length > shownEvents.length
    ? `<p class="timeline-note">Showing first ${esc(shownEvents.length)} meaningful events of ${esc(meaningfulEvents.length)}. ${esc(hiddenMetadata)} zero-value metadata events hidden.</p>`
    : hiddenMetadata
      ? `<p class="timeline-note">${esc(hiddenMetadata)} zero-value metadata events hidden.</p>`
      : '';
  const timeline = meaningfulEvents.length
    ? `<section class="detail-section"><details class="aiw-details"><summary>Advanced timeline (${esc(meaningfulEvents.length)} meaningful events)</summary><div class="details-body">
        <p>${esc(summary.events)} total events · ${esc(summary.tokens)} tokens · ${esc(summary.api_value)} API-equivalent value</p>
        ${costliestCallout}
        ${wasteNote}
        ${truncated}
        <div class="table-wrap"><table><thead><tr><th>Turn</th><th>Event</th><th>Model</th><th>Tokens</th><th>API value</th></tr></thead>
          <tbody>${shownEvents.map(e => `<tr title="${esc(e.turn && turnPrompts[e.turn] ? 'Turn #' + e.turn + ': ' + compactText(turnPrompts[e.turn], 220) : (e.content_hash || ''))}">
            <td class="evt-turn">${e.turn ? '#' + esc(e.turn) : '—'}</td><td>${esc(eventTypeLabel(e.event_type))}</td><td>${esc(e.model)}</td><td>${esc(e.tokens_label)}</td><td>${esc(e.api_value)}</td>
          </tr>`).join('')}</tbody></table></div>
      </div></details></section>`
    : `<section class="detail-section"><details class="aiw-details"><summary>Advanced timeline</summary><div class="details-body"><p>No meaningful token/cost events are available for this tool yet.</p>${hiddenMetadata ? `<p>${esc(hiddenMetadata)} zero-value metadata events hidden.</p>` : ''}</div></details></section>`;
  const outcomeActions = `<div class="outcome-control"><h3>Was this work useful?</h3>
    <p>Mark the result so AIWatcher can measure value instead of tokens alone.</p>
    <div class="outcome-options">
      <button data-testid="outcome-useful" class="outcome-button useful ${s.outcome === 'useful' ? 'selected' : ''}" onclick="markOutcome('${esc(s.session_id)}','useful')">Useful</button>
      <button data-testid="outcome-rework" class="outcome-button rework ${s.outcome === 'rework' ? 'selected' : ''}" onclick="markOutcome('${esc(s.session_id)}','rework')">Needs rework</button>
      <button data-testid="outcome-abandoned" class="outcome-button abandoned ${s.outcome === 'abandoned' ? 'selected' : ''}" onclick="markOutcome('${esc(s.session_id)}','abandoned')">Abandoned</button>
    </div>
    <div class="handoff-cta">
      <div>
        <h4>Continue in a fresh session</h4>
        <p>Create a paste-ready brief with local evidence, recent commits, and guardrails for Claude, Codex, Cursor, or VS Code.</p>
      </div>
      <button class="btn-primary" onclick="openHandoff('${esc(s.session_id)}')">Create handoff capsule</button>
    </div>
    </div>`;
  const insights = s.insights && s.insights.length
    ? `<section class="detail-section"><h3>What to check next</h3>
        <ul class="insight-list">${s.insights.map(i => `<li>${esc(i)}</li>`).join('')}</ul>
      </section>`
    : `<section class="detail-section"><h3>What to check next</h3>
        <p>Nothing unusual in this session summary.</p></section>`;
  const pa = s.prompt_analysis;
  let promptReview = '';
  if (pa) {
    const opener = `<details class="aiw-details"><summary>Session opening prompt</summary><div class="details-body"><p class="prompt-opener">${esc(pa.opening_prompt)}</p></div></details>`;
    const asksRows = (pa.expensive_asks || []).map(a => `<tr>
        <td class="ask-turn">#${esc(a.turn)}</td>
        <td class="ask-prompt" title="${esc(a.prompt)}">${esc(a.prompt.length > 110 ? a.prompt.slice(0, 110) + '…' : a.prompt)}</td>
        <td class="ask-tools">${esc(a.tool_calls)}</td>
        <td class="ask-cost">${esc(a.api_value)}<span class="ask-share">${esc(a.share_pct)}%</span></td>
      </tr>`).join('');
    const expensiveAsks = (pa.expensive_asks && pa.expensive_asks.length)
      ? `<section class="detail-section"><h3>Expensive asks</h3>
          <p>Which prompts drove the cost, by turn. Cost is cumulative — later turns re-send the whole conversation, so a short prompt late in a long session can still be expensive.</p>
          <div class="table-wrap"><table class="asks-table"><thead><tr><th>Turn</th><th>Prompt</th><th>Tools</th><th>Cost</th></tr></thead>
            <tbody>${asksRows}</tbody></table></div>
        </section>`
      : '';
    const c = pa.coaching;
    const coaching = c
      ? `<section class="detail-section"><h3>Prompt worth tightening <span class="risk-tag risk-${esc(c.risk)}">${esc(c.risk)} risk</span></h3>
          <p>Turn #${esc(c.turn)} (${esc(c.api_value)}) — the costliest ask with something to tighten.</p>
          <div class="prompt-text">${esc(compactText(c.prompt, 700))}</div>
          ${String(c.prompt || '').length > 700 ? `<details class="aiw-details"><summary>Show full prompt</summary><div class="details-body"><div class="prompt-text">${esc(c.prompt)}</div></div></details>` : ''}
          ${c.findings && c.findings.length ? `<h4>Findings</h4><ul class="insight-list">${c.findings.map(f => `<li>${esc(f)}</li>`).join('')}</ul>` : ''}
          ${c.suggestions && c.suggestions.length ? `<h4>Suggestions</h4><ul class="insight-list">${c.suggestions.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
          <h4>Tighter prompt for next time</h4>
          <div class="prompt-suggested">${esc(compactText(c.suggested_prompt, 900))}</div>
          ${String(c.suggested_prompt || '').length > 900 ? `<details class="aiw-details"><summary>Show full tighter prompt</summary><div class="details-body"><div class="prompt-suggested">${esc(c.suggested_prompt)}</div></div></details>` : ''}
        </section>`
      : `<section class="detail-section"><h3>Prompt worth tightening</h3>
          <p>No single prompt stood out as under-specified — cost accumulated across ${esc(pa.turns)} turns. For work this long, checkpoint or start a fresh session between chunks to keep context (and cost) from compounding.</p>
        </section>`;
    promptReview = `${expensiveAsks}${coaching}<section class="detail-section"><h3>Prompt context</h3>${opener}</section>`;
  }
  document.getElementById('detailContent').innerHTML = `<section class="detail-section">
    <h2 class="session-title">${esc(s.project_short)}</h2>
    <p class="session-meta">${esc(s.tool)} · ${esc(s.model)}</p>
    ${miniStats({ sessions: 1, api_value: s.api_value, tokens: s.tokens_label, tool_calls: s.tool_calls })}
    ${outcomePill(s.outcome)}
    ${renderVerdict(s)}
    ${outcomeActions}
    </section>
    ${promptReview}
    ${renderEvidence(s.outcome_evidence)}
    ${insights}
    ${costBreakdown}
    <section class="detail-section"><details class="aiw-details"><summary>Session metadata</summary><div class="details-body">
      <table><tbody>
        <tr><th>Started</th><td>${esc(dateLabel(s.started_at))}</td></tr>
        <tr><th>Updated</th><td>${esc(dateLabel(s.updated_at))}</td></tr>
        <tr><th>Source</th><td>${esc(s.source_path || 'unknown')}</td></tr>
        <tr><th>Privacy</th><td>${esc(s.privacy)}</td></tr>
      </tbody></table>
    </div></details></section>
    ${timeline}`;
}
async function markOutcome(sessionId, outcome) {
  const buttons = document.querySelectorAll('.outcome-button');
  buttons.forEach(button => { button.disabled = true; });
  try {
    const res = await fetch('/api/outcome', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, outcome })
    });
    if (!res.ok) {
      const error = await res.json().catch(() => ({ error: 'Could not save outcome' }));
      showToast(error.error || 'Could not save outcome', 'error');
      return;
    }
    await Promise.all([selectSession(sessionId), load(false)]);
    const labels = { useful: 'Useful', rework: 'Needs rework', abandoned: 'Abandoned' };
    showToast(`Outcome saved: ${labels[outcome]}`);
  } catch (error) {
    showToast('Could not reach the local AIWatcher server.', 'error');
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}
function renderReport(report) {
  return `<p>${esc(report.title)}</p>
    <div class="pill-row">${report.summary.map(item => `<span class="pill">${esc(item)}</span>`).join('')}</div>
    ${report.highlights.map(item => `<div class="insight"><strong>${esc(item)}</strong></div>`).join('')}
    <p>${esc(report.next_checks.join(' '))}</p>`;
}
function renderJournal(journal) {
  return `<p>${esc(journal.title)}</p>
    <div class="pill-row"><span class="pill">${esc(journal.summary)}</span></div>
    ${journal.items.map(item => `<div class="insight"><strong>${esc(item)}</strong></div>`).join('')}
    <p><strong>One thing to change next time:</strong> ${esc(journal.improvement)}</p>`;
}
function showView(view) {
  document.querySelectorAll('.view').forEach(node => {
    node.hidden = node.id !== `view-${view}`;
  });
  document.querySelectorAll('.nav-tab').forEach(node => {
    node.classList.toggle('active', node.dataset.view === view);
  });
}
async function load(resetDetail = true) {
  const days = document.getElementById('days').value;
  const [summaryRes, reportRes, journalRes] = await Promise.all([
    fetch(`/api/summary?days=${days}`),
    fetch(`/api/report?days=${days}`),
    fetch(`/api/journal?days=${Math.min(Number(days), 30)}`)
  ]);
  const data = await summaryRes.json();
  const report = await reportRes.json();
  const journal = await journalRes.json();
  const totals = data.totals;
  document.getElementById('apiValue').textContent = totals.api_value_label;
  document.getElementById('windowLabel').textContent = totals.window_label;
  document.getElementById('sessions').textContent = totals.sessions;
  document.getElementById('usefulOutcomes').textContent = totals.useful_outcomes;
  document.getElementById('costPerUseful').textContent = `${totals.cost_per_useful_change}${totals.inferred_useful_outcomes ? ` · ${totals.inferred_useful_outcomes} to confirm` : ''}`;
  document.getElementById('preflightDecisions').textContent = totals.preflight_decisions;
  receiptCache = data.intervention_receipts || [];
  document.getElementById('latestIntervention').innerHTML = renderLatestReceipt(receiptCache[0]);
  document.getElementById('receiptRows').innerHTML = renderReceiptRows(receiptCache);
  const latest = data.recent_sessions[0];
  document.getElementById('latestSession').innerHTML = latest
    ? `<div class="session-summary"><div class="session-title">${esc(latest.project)}</div>
       <div class="session-meta">${esc(latest.tool)} · ${esc(latest.model)} · ${esc(latest.tokens)} tokens · ${esc(latest.api_value)}</div>
       <div class="session-actions">${outcomePill(latest.outcome)}${outcomeEvidencePill(latest)}
       <button data-testid="review-latest" class="btn-primary" onclick="selectSession('${esc(latest.session_id)}')">Review outcome</button></div></div>`
    : '<div class="empty">No local AI session detected yet.</div>';
  const recommendation = data.insights[0];
  document.getElementById('todayRecommendation').innerHTML = recommendation
    ? `<div class="insight"><strong>${esc(recommendation.title)}</strong><p>${esc(recommendation.body)}</p></div>`
    : '<div class="empty">Nothing unusual yet. Keep the next task scoped and define a stop condition.</div>';
  document.getElementById('projects').innerHTML = bars(data.projects, "api_value_label", "project");
  document.getElementById('models').innerHTML = bars(data.models, "api_value_label", "model");
  document.getElementById('insights').innerHTML = data.insights.length
    ? data.insights.map(i => `<div class="insight"><strong>${esc(i.title)}</strong><p>${esc(i.body)}</p></div>`).join('')
    : '<div class="empty">No notable local signals yet.</div>';
  document.getElementById('insightRows').innerHTML = data.insights.length
    ? data.insights.map(i => `<div class="insight"><strong>${esc(i.title)}</strong><p>${esc(i.body)}</p></div>`).join('')
    : '<div class="empty">No notable local signals yet. Keep using AI tools and check back after a few sessions.</div>';
  document.getElementById('privacy').innerHTML = data.privacy.map(p => `<div class="privacy-item"><span class="privacy-check">&#10003;</span><span>${esc(p)}</span></div>`).join('');
  document.getElementById('privacyLarge').innerHTML = data.privacy.map(p => `<span class="pill">${esc(p)}</span>`).join('');
  document.getElementById('recent').innerHTML = data.recent_sessions.slice(0, 6).map(s => `<tr class="clickable" onclick="selectSession('${s.session_id}')">
    <td>${esc(s.tool)}</td><td>${esc(s.project)}<br>${outcomeEvidencePill(s)}</td><td>${esc(s.tokens)}</td><td><button class="row-action">Review</button></td>
  </tr>`).join('');
  document.getElementById('projectWindow').textContent = totals.window_label;
  document.getElementById('projectRows').innerHTML = data.projects.length
    ? data.projects.map(p => `<tr class="clickable" onclick="selectProject(decodeURIComponent(this.dataset.id))" data-id="${encodeURIComponent(p.id)}">
        <td>${esc(p.short_name || p.name)}</td>
        <td class="mono">${esc(p.sessions)}</td>
        <td class="mono">${esc(p.tokens_label)}</td>
        <td class="mono">${esc(p.calls)}</td>
        <td class="mono">${esc(p.api_value_label)}</td>
      </tr>`).join('')
    : '<tr><td colspan="5"><div class="empty">No local project usage found for this window.</div></td></tr>';
  document.getElementById('sessionRows').innerHTML = data.recent_sessions.length
    ? data.recent_sessions.map(s => `<tr class="clickable" onclick="selectSession('${s.session_id}')">
        <td>${esc(s.tool)}</td>
        <td>${esc(s.project)}</td>
        <td>${esc(s.model)}</td>
        <td class="mono">${esc(s.tokens)}</td>
        <td><button class="row-action">Review</button></td>
      </tr>`).join('')
    : '<tr><td colspan="5"><div class="empty">No local sessions found for this window.</div></td></tr>';
  document.getElementById('report').innerHTML = renderReport(report);
  document.getElementById('journal').innerHTML = renderJournal(journal);
  if (resetDetail && document.getElementById('detailDrawer').classList.contains('open')) closeDrawer();
}
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeDrawer(); });
load();
</script>
</body>
</html>
"""


class UIHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def handle_error(self, request: object, client_address: object) -> None:
        # A browser tab closing or refreshing mid-fetch (the dashboard fires
        # several concurrent requests via Promise.all on every load()) causes a
        # BrokenPipeError/ConnectionResetError when we try to write the response
        # to an already-closed socket. That's expected client behavior, not a
        # server bug — suppress the noisy traceback but still log anything else.
        import sys
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)

    def _trusted_origin(self) -> str | None:
        """Echo back Origin only for the AIWatcher extension or local dev pages.

        Never returns "*" — a wildcard would let any open tab in the browser
        read this loopback server's responses (prompt risk, cost, session
        metadata), not just the AIWatcher extension.

        Compares the parsed hostname exactly rather than using str.startswith()
        prefix matching — a prefix check would also match attacker-registerable
        domains like http://127.0.0.1.evil.com or http://localhost.evil.com,
        which reintroduces the same cross-origin exposure a wildcard would have.
        """
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            return origin
        parsed = urlparse(origin)
        if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}:
            return origin
        return None

    def _send(self, status: int, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            trusted = self._trusted_origin()
            if trusted:
                self.send_header("Access-Control-Allow-Origin", trusted)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected before the response finished — nothing to do.
            pass

    def do_OPTIONS(self) -> None:
        trusted = self._trusted_origin()
        if not trusted:
            self._send(405, "Cross-origin requests are not allowed", "text/plain; charset=utf-8")
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", trusted)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/health":
            self._send(200, json.dumps({
                "service": "aiwatcher-local",
                "version": __version__,
                "capabilities": ["preflight"],
            }), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/summary":
            params = parse_qs(parsed.query)
            try:
                days = max(1, min(90, int(params.get("days", ["7"])[0])))
            except ValueError:
                days = 7
            self._send(200, json.dumps(build_summary(days)), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/project":
            params = parse_qs(parsed.query)
            try:
                days = max(1, min(90, int(params.get("days", ["7"])[0])))
            except ValueError:
                days = 7
            project = params.get("project", ["unknown"])[0]
            self._send(200, json.dumps(build_project_detail(project, days)), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/session":
            params = parse_qs(parsed.query)
            session_id = params.get("id", [""])[0]
            self._send(200, json.dumps(build_session_detail(session_id)), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/handoff":
            params = parse_qs(parsed.query)
            session_id = params.get("id", [""])[0]
            target = params.get("target", ["generic"])[0]
            include_prompt_excerpt = params.get("prompt", ["0"])[0] == "1"
            try:
                days = max(1, min(90, int(params.get("days", ["30"])[0])))
            except ValueError:
                days = 30
            self._send(
                200,
                json.dumps(build_handoff_detail(session_id, days, target, include_prompt_excerpt)),
                "application/json; charset=utf-8",
            )
            return
        if parsed.path == "/api/report":
            params = parse_qs(parsed.query)
            try:
                days = max(1, min(90, int(params.get("days", ["7"])[0])))
            except ValueError:
                days = 7
            self._send(200, json.dumps(build_report(days)), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/journal":
            params = parse_qs(parsed.query)
            try:
                days = max(1, min(30, int(params.get("days", ["1"])[0])))
            except ValueError:
                days = 1
            self._send(200, json.dumps(build_journal(days)), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/context-health":
            params = parse_qs(parsed.query)
            tool = params.get("tool", ["claude"])[0].strip() or "claude"
            cwd = params.get("cwd", [""])[0].strip() or None
            try:
                all_sessions = scan_all()
                all_events = scan_all_events()
                healths = analyze_all_sessions(all_sessions, all_events)
                warn = gate_health_warning(all_sessions, all_events, tool=tool, cwd=cwd)
                tool_lower = tool.lower()
                tool_aliases = {"claude": {"claude", "claude-code"}, "codex": {"codex", "codex-cli"}}
                allowed = tool_aliases.get(tool_lower, {tool_lower})
                match = next((h for h in healths if h.tool.lower() in allowed), None)
                if match:
                    payload = {
                        "session_id": match.session_id,
                        "tool": match.tool,
                        "severity": match.severity,
                        "age_hours": match.age_hours,
                        "age_days": match.age_days,
                        "latest_turn_tokens": match.latest_turn_tokens,
                        "peak_turn_tokens": match.peak_turn_tokens,
                        "efficiency_pct": match.efficiency_pct,
                        "bloat_ratio": match.bloat_ratio,
                        "growth_rate": match.growth_rate,
                        "is_context_critical": match.is_context_critical,
                        "is_context_pressure": match.is_context_pressure,
                        "is_extreme_bloat": match.is_extreme_bloat,
                        "is_high_bloat": match.is_high_bloat,
                        "is_stale": match.is_stale,
                        "is_critical_stale": match.is_critical_stale,
                        "recommendations": match.recommendations,
                        "warning_text": warn,
                        "compact_prompt": _build_compact_prompt(match),
                    }
                else:
                    payload = {"severity": "healthy", "warning_text": None, "compact_prompt": None}
            except OSError:
                payload = {"severity": "healthy", "warning_text": None, "compact_prompt": None}
            self._send(200, json.dumps(payload), "application/json; charset=utf-8")
            return
        self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/outcome", "/api/preflight"}:
            self._send(404, "Not found", "text/plain; charset=utf-8")
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send(415, json.dumps({"error": "Content-Type must be application/json"}), "application/json; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_REQUEST_BYTES:
                self._send(413, json.dumps({"error": "Request body is too large"}), "application/json; charset=utf-8")
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, json.JSONDecodeError):
            self._send(400, json.dumps({"error": "Invalid JSON body"}), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/preflight":
            prompt = str(payload.get("prompt", ""))
            tool = str(payload.get("tool", "agent")).strip() or "agent"
            cwd = str(payload.get("cwd", "")).strip() or None
            response = build_prompt_preflight(prompt, tool=tool, cwd=cwd)
            status = 400 if response.get("error") else 200
            self._send(status, json.dumps(response), "application/json; charset=utf-8")
            return
        session_id = str(payload.get("session_id", "")).strip()
        outcome = str(payload.get("outcome", "")).strip()
        note = str(payload.get("note", "")).strip() or None
        if not session_id:
            self._send(400, json.dumps({"error": "session_id is required"}), "application/json; charset=utf-8")
            return
        if outcome not in VALID_OUTCOMES:
            self._send(400, json.dumps({"error": "outcome must be useful, rework, or abandoned"}), "application/json; charset=utf-8")
            return
        if not any(row.session_id == session_id for row in scan_all()):
            self._send(404, json.dumps({"error": "session not found"}), "application/json; charset=utf-8")
            return
        try:
            record = record_outcome(session_id, outcome, note)
        except OSError as exc:
            self._send(
                500,
                json.dumps({"error": f"Could not save outcome: {exc}"}),
                "application/json; charset=utf-8",
            )
            return
        self._send(200, json.dumps(record), "application/json; charset=utf-8")


def is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_available_port(host: str, preferred_port: int, attempts: int = 20) -> int:
    for candidate in range(preferred_port, preferred_port + max(1, attempts)):
        if is_port_available(host, candidate):
            return candidate
    raise OSError(f"No available port found from {preferred_port} to {preferred_port + max(1, attempts) - 1}.")


def _pids_for_port(port: int) -> list[str]:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return []

        pids: set[str] = set()
        suffix = f":{port}"
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local_address, state, pid = parts[1], parts[3], parts[-1]
            if local_address.endswith(suffix) and state.upper() == "LISTENING" and pid.isdigit():
                pids.add(pid)
        return sorted(pids)

    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []

    return [pid.strip() for pid in result.stdout.splitlines() if pid.strip()]


def restart_local_server(port: int) -> bool:
    pids = _pids_for_port(port)
    if not pids:
        return False

    stopped = False
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", pid, "/F"], check=False, capture_output=True, text=True)
            else:
                subprocess.run(["kill", pid], check=False, capture_output=True, text=True)
            stopped = True
        except OSError:
            continue
    return stopped


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    auto_port: bool = True,
    port_attempts: int = 20,
    restart: bool = False,
) -> None:
    if restart:
        stopped = restart_local_server(port)
        if stopped:
            print(f"Stopped existing process on port {port}.")

    selected_port = port
    if auto_port:
        selected_port = find_available_port(host, port, port_attempts)
        if selected_port != port:
            print(f"Port {port} is busy. Using {selected_port} instead.")

    server = ThreadingHTTPServer((host, selected_port), UIHandler)
    print(f"AIWatcher Local UI running at http://{host}:{selected_port}")
    print("Local-only. No data leaves this machine. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped AIWatcher Local UI.")
    finally:
        server.server_close()
