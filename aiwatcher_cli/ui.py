"""Local-only dashboard for AIWatcher Local."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from . import __version__
from .cli import (
    usable_survival_summary,
    _loop_signal,
    _velocity_signal,
    analyze_prompt,
    filter_sessions,
    session_insights,
    setup_checklist,
    timeline_analysis,
)
from .correlate import link_recent_interventions_to_sessions
from .handoff import build_handoff_capsule
from .metrics import model_cost_comparison, pace_vs_baseline, replayed_context_cost
from .local_state import (
    COMMAND_GATE_BLOCKED_DECISIONS,
    MAX_COMMAND_DECISIONS_STORED,
    PROMPT_MODIFIED_DECISIONS,
    VALID_OUTCOMES,
    evidence_snapshots_for_sessions,
    get_outcome,
    outcome_counts,
    outcomes_for_sessions,
    recent_command_decisions,
    recent_handoff_decisions,
    recent_interventions,
    record_handoff_decision,
    record_evidence_snapshot,
    record_outcome,
    record_ui_server,
)
from .outcome_evidence import VALID_EVIDENCE_OUTCOMES, build_outcome_evidence, evidence_for_sessions
from .ledger import Ledger, build_ledger, unbanked_summary
from .pricing import is_subscription_model
from .session_health import ContextHealth, analyze_all_sessions, gate_health_warning
from .scanner import (
    clip_sessions_to_window,
    LocalEvent,
    LocalSession,
    discover_tools,
    display_model_name,
    extract_opening_prompt,
    model_usage_totals,
    scan_all,
    scan_all_events,
    segment_session_by_prompt,
    surface_coverage,
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
    # Billions became reachable once replayed cache tokens were counted: a long
    # session re-sends its whole context every turn, so totals run far past the
    # millions this used to top out at ("1332.1M" instead of "1.3B").
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
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


def _tool_surface_key(row: LocalSession) -> str:
    """Group key that separates CLI from Desktop usage of the same tool, when known."""
    if row.surface:
        return f"{row.tool} ({row.surface})"
    return row.tool


def group_by_model_breakdown(rows: list[LocalSession]) -> list[dict[str, object]]:
    """Aggregate model usage from each session's model_breakdown.

    A session that used more than one model (e.g. Fable then Sonnet in the same
    conversation) contributes to every model's bucket here, instead of only the
    single last-used model that row.model records for backward compatibility.
    """
    totals = model_usage_totals(rows)
    result = []
    for key, bucket in totals.items():
        tokens = bucket["tokens_in"] + bucket["tokens_out"]
        display_name = display_model_name(key)
        result.append({
            "name": display_name,
            "id": key,
            "short_name": short_path(display_name),
            "sessions": int(bucket["sessions"]),
            "tokens": int(tokens),
            "tokens_label": compact_int(int(tokens)),
            "api_value_usd": round(bucket["cost_usd"], 6),
            "api_value_label": money(bucket["cost_usd"]),
            "calls": int(bucket["agent_calls"]),
            "tool_calls": int(bucket["tool_calls"]),
        })
    result.sort(key=lambda item: (float(item["api_value_usd"]), int(item["tokens"])), reverse=True)
    return result


def rows_for_window(days: int) -> list[LocalSession]:
    """Sessions clipped to the window -- see clip_sessions_to_window for why the
    old `updated_at`-only rule overstated every total."""
    since = datetime.now().astimezone() - timedelta(days=days)
    try:
        events = scan_all_events()
    except OSError:
        events = []
    return clip_sessions_to_window(scan_all(), events, since)


def _session_row_json(
    row: LocalSession,
    window_outcomes: dict[str, dict[str, object]],
    evidence_by_session: dict[str, object],
) -> dict[str, object]:
    return {
        "tool": row.tool,
        "session_id": row.session_id,
        "project": short_path(row.project_path),
        "project_full": row.project_path or "unknown",
        "model": display_model_name(row.model),
        "tokens": compact_int(row.tokens_in + row.tokens_out),
        "api_value": money(row.cost_usd),
        "outcome": (window_outcomes.get(row.session_id) or {}).get("outcome"),
        "inferred_outcome": evidence_by_session.get(row.session_id).inferred_outcome if evidence_by_session.get(row.session_id) else None,
        "updated_at": (row.updated_at or row.started_at).isoformat() if (row.updated_at or row.started_at) else None,
    }


SESSION_SEARCH_RESULT_LIMIT = 50


def build_session_search(
    days: int = 30,
    *,
    search: str | None = None,
    outcome: str | None = None,
    evidence: str | None = None,
) -> dict[str, object]:
    """S-27: UI-facing search/filter over local sessions, reusing filter_sessions()
    (cli.py) rather than re-implementing matching here."""
    rows = rows_for_window(days)
    matched = filter_sessions(rows, search=search, outcome=outcome, evidence=evidence)
    matched = sorted(matched, key=lambda row: row.updated_at or row.started_at or MIN_DT, reverse=True)
    total_matched = len(matched)
    matched = matched[:SESSION_SEARCH_RESULT_LIMIT]
    window_outcomes = outcomes_for_sessions({row.session_id for row in matched})
    # Every git-backed evidence lookup shells out per session with no cache, so
    # this is the dominant cost of a search request -- only pay it when the
    # caller actually asked for evidence (an `evidence` filter already implies
    # every returned row has that exact inferred_outcome, so label it directly
    # instead of recomputing what filter_sessions() just computed internally).
    evidence_by_session = (
        {row.session_id: SimpleNamespace(inferred_outcome=evidence) for row in matched} if evidence else {}
    )
    return {
        "query": {"search": search or "", "outcome": outcome or "", "evidence": evidence or ""},
        "total_scanned": len(rows),
        "total_matched": total_matched,
        "sessions": [_session_row_json(row, window_outcomes, evidence_by_session) for row in matched],
    }


def _survival_for_session(session_id: str) -> dict[str, str] | None:
    """Flatten a stored evidence_snapshot's survival history to {bucket: status}
    for build_outcome_evidence(), which only needs the status, not checked_at."""
    row = evidence_snapshots_for_sessions({session_id}).get(session_id)
    survival = row.get("survival") if row and isinstance(row.get("survival"), dict) else None
    if not survival:
        return None
    return {
        bucket: entry.get("status")
        for bucket, entry in survival.items()
        if isinstance(entry, dict) and entry.get("status")
    }


def survival_by_session(sessions: list[LocalSession]) -> dict[str, dict[str, str]]:
    snapshots = evidence_snapshots_for_sessions({row.session_id for row in sessions})
    result: dict[str, dict[str, str]] = {}
    for session_id, row in snapshots.items():
        survival = row.get("survival") if isinstance(row.get("survival"), dict) else None
        if not survival:
            continue
        flattened = {
            bucket: entry.get("status")
            for bucket, entry in survival.items()
            if isinstance(entry, dict) and entry.get("status")
        }
        if flattened:
            result[session_id] = flattened
    return result


def session_json(row: LocalSession) -> dict[str, object]:
    started = row.started_at.isoformat() if row.started_at else None
    updated = row.updated_at.isoformat() if row.updated_at else None
    outcome = get_outcome(row.session_id)
    evidence = build_outcome_evidence(row, survival=_survival_for_session(row.session_id))
    return {
        "session_id": row.session_id,
        "tool": row.tool,
        "project": row.project_path or "unknown",
        "project_short": short_path(row.project_path),
        "model": display_model_name(row.model),
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
        "model": display_model_name(row.model),
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
        "models": group_by_model_breakdown(rows),
        "tools": group_rows(rows, _tool_surface_key),
        "sessions": [session_json(row) for row in sessions[:20]],
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
    evidence = build_outcome_evidence(row, survival=_survival_for_session(session_id))
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
    tools = group_rows(rows, _tool_surface_key)
    models = group_by_model_breakdown(rows)
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
        "digest": build_weekly_digest(days),
    }


DIGEST_EVIDENCE_SAMPLE_SIZE = 30
DIGEST_CANDIDATE_LIMIT = 5


def _events_by_session(rows: list[LocalSession]) -> dict[str, list[LocalEvent]]:
    session_ids = {row.session_id for row in rows}
    grouped: dict[str, list[LocalEvent]] = defaultdict(list)
    for event in scan_all_events():
        if event.session_id in session_ids:
            grouped[event.session_id].append(event)
    return grouped


def _recommend_weekly_improvement(
    *,
    commands_blocked: int,
    loop_candidates: list[dict[str, object]],
    velocity_candidates: list[dict[str, object]],
    inferred_churned: int,
    outcomes: dict[str, int],
    survival: dict[str, object],
) -> str:
    if commands_blocked > 0:
        plural = "s were" if commands_blocked != 1 else " was"
        return (
            f"{commands_blocked} dangerous command{plural} blocked this window -- "
            "run `aiwatcher hook-status` to review what was caught."
        )
    if loop_candidates:
        return (
            f"{len(loop_candidates)} session(s) show repeated identical content -- "
            "narrow scope before re-running the same step."
        )
    if velocity_candidates:
        return (
            f"{len(velocity_candidates)} session(s) ran well above their tool's typical pace -- "
            "check for a runaway loop before it burns more budget."
        )
    survival_pct = survival.get("survival_pct")
    if survival.get("available") and isinstance(survival_pct, (int, float)) and survival_pct < 50:
        return (
            f"Only {survival_pct:.0f}% of the lines you paid for are still in the code -- "
            "review the costliest recent changes before repeating that approach."
        )
    if inferred_churned > 0:
        plural = "s" if inferred_churned != 1 else ""
        return f"{inferred_churned} session{plural} looked useful but the commit didn't survive -- review before trusting similar work."
    if outcomes["abandoned"] > outcomes["useful"]:
        return "More sessions were marked abandoned than useful this window -- review scoping before the next batch."
    return "No urgent signal this window -- local usage looks healthy."


def build_weekly_digest(days: int = 7) -> dict[str, object]:
    """P1-5 (S-26): richer weekly signals layered onto build_report's plain totals --
    outcome breakdown, highest-cost useful session, top sessions, loop/runaway
    candidates (P1-3), command-gate activity (P1-3), prompt-preflight activity
    (P1-1), survival economics (P1-4), and one recommendation.
    """
    all_rows = scan_all()
    rows = rows_for_window(days)
    window_session_ids = {row.session_id for row in rows}
    try:
        window_outcomes = outcomes_for_sessions(window_session_ids)
        outcomes = outcome_counts(window_session_ids)
    except OSError:
        window_outcomes = {}
        outcomes = {key: 0 for key in ("abandoned", "rework", "useful")}

    sample_rows = rows[:DIGEST_EVIDENCE_SAMPLE_SIZE]
    try:
        evidence_by_session = evidence_for_sessions(sample_rows, survival_by_session=survival_by_session(sample_rows))
    except OSError:
        evidence_by_session = {}
    inferred_useful = sum(
        1
        for session_id, evidence in evidence_by_session.items()
        if session_id not in window_outcomes and evidence.inferred_outcome == "useful"
    )
    inferred_churned = sum(1 for evidence in evidence_by_session.values() if evidence.inferred_outcome == "churned")

    useful_rows = [row for row in rows if (window_outcomes.get(row.session_id) or {}).get("outcome") == "useful"]
    highest_cost_useful = max(useful_rows, key=lambda row: row.cost_usd, default=None)

    top_sessions = sorted(rows, key=lambda row: row.cost_usd, reverse=True)[:DIGEST_CANDIDATE_LIMIT]

    events_by_session = _events_by_session(rows)
    loop_candidates: list[dict[str, object]] = []
    velocity_candidates: list[dict[str, object]] = []
    for row in rows:
        events = events_by_session.get(row.session_id, [])
        loop = _loop_signal(events)
        if loop is not None:
            loop_candidates.append({
                "project": short_path(row.project_path),
                "tool": row.tool,
                "diagnosis": loop["diagnosis"],
            })
        velocity = _velocity_signal(row.tool, events)
        if velocity is not None:
            velocity_candidates.append({
                "project": short_path(row.project_path),
                "tool": row.tool,
                "ratio_label": f"{float(velocity['ratio']):.1f}x baseline pace",
            })

    try:
        gate_decisions = recent_command_decisions(limit=MAX_COMMAND_DECISIONS_STORED, days=days)
    except OSError:
        gate_decisions = []
    blocked = [row for row in gate_decisions if row.get("decision") in COMMAND_GATE_BLOCKED_DECISIONS]

    try:
        prompt_interventions = recent_interventions(limit=200, days=days)
    except OSError:
        prompt_interventions = []
    prompts_modified = [row for row in prompt_interventions if row.get("decision") in PROMPT_MODIFIED_DECISIONS]

    try:
        survival = _survival_summary()
    except OSError:
        survival = {"available": False, "sample_count": 0, "required_samples": MIN_SURVIVAL_SAMPLES}

    recommendation = _recommend_weekly_improvement(
        commands_blocked=len(blocked),
        loop_candidates=loop_candidates,
        velocity_candidates=velocity_candidates,
        inferred_churned=inferred_churned,
        outcomes=outcomes,
        survival=survival,
    )

    return {
        "outcomes": {
            "useful": outcomes["useful"],
            "rework": outcomes["rework"],
            "abandoned": outcomes["abandoned"],
            "inferred_useful": inferred_useful,
            "inferred_churned": inferred_churned,
        },
        "highest_cost_useful_session": (
            {
                "project": short_path(highest_cost_useful.project_path),
                "tool": highest_cost_useful.tool,
                "model": highest_cost_useful.model or "unknown",
                "api_value_label": money(highest_cost_useful.cost_usd),
            }
            if highest_cost_useful is not None
            else None
        ),
        "top_sessions": [
            {
                "session_id": row.session_id,
                "project": short_path(row.project_path),
                "tool": row.tool,
                "model": row.model or "unknown",
                "api_value_label": money(row.cost_usd),
                "outcome": (window_outcomes.get(row.session_id) or {}).get("outcome"),
            }
            for row in top_sessions
        ],
        "loop_candidates": loop_candidates[:DIGEST_CANDIDATE_LIMIT],
        "velocity_candidates": velocity_candidates[:DIGEST_CANDIDATE_LIMIT],
        "command_gate": {
            "gates_fired": len(gate_decisions),
            "commands_blocked": len(blocked),
        },
        "prompt_gate": {
            "flagged": len(prompt_interventions),
            "modified": len(prompts_modified),
        },
        "survival": survival,
        "recommendation": recommendation,
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


def _context_action(health: ContextHealth) -> dict[str, str]:
    if health.severity == "critical":
        return {
            "label": "Start fresh",
            "secondary_label": "Copy handoff",
            "reason": "Critical context pressure is likely to waste turns or miss details.",
        }
    if health.is_context_pressure or health.is_high_bloat:
        return {
            "label": "Compact",
            "secondary_label": "Prepare handoff",
            "reason": "Context is growing; compact before it compounds further.",
        }
    if health.is_stale:
        return {
            "label": "Review",
            "secondary_label": "Fresh session",
            "reason": "The session is old enough that a focused restart may be cleaner.",
        }
    return {
        "label": "Keep going",
        "secondary_label": "Review",
        "reason": "Context looks healthy.",
    }


def _context_health_cards(rows: list[LocalSession], events: list[LocalEvent]) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    sessions_by_id = {row.session_id: row for row in rows}
    for health in analyze_all_sessions(rows, events)[:5]:
        session = sessions_by_id.get(health.session_id)
        action = _context_action(health)
        cards.append({
            "session_id": health.session_id,
            "tool": health.tool,
            "project": short_path(health.project_path),
            "severity": health.severity,
            "latest_turn_tokens": compact_int(health.latest_turn_tokens),
            "peak_turn_tokens": compact_int(health.peak_turn_tokens),
            # Measured cache reads on the latest turn, not a ratio applied to
            # the turn size — the provider counts the replayed portion for us.
            "estimated_replayed_context_tokens": health.latest_turn_replayed_tokens,
            "estimated_replayed_context_label": compact_int(health.latest_turn_replayed_tokens),
            "bloat_measurable": health.bloat_measurable,
            "efficiency_label": f"{health.efficiency_pct:.0f}%" if health.bloat_measurable else "n/a",
            "bloat_label": f"{health.bloat_ratio * 100:.0f}%" if health.bloat_measurable else "n/a",
            "replayed_cost_label": f"${health.replayed_cost_usd:.2f}" if health.bloat_measurable else "n/a",
            "analyzed_cost_label": f"${health.analyzed_cost_usd:.2f}" if health.bloat_measurable else "n/a",
            "age_label": f"{health.age_days:.1f}d" if health.age_days >= 1 else f"{health.age_hours:.0f}h",
            "recommendation": health.recommendations[0] if health.recommendations else "Context is healthy.",
            "action": action,
            "can_handoff": bool(session),
            "compact_prompt": _build_compact_prompt(health),
        })
    return cards


def _handoff_bubble(context_health: list[dict[str, object]]) -> dict[str, object] | None:
    """Pick the single highest-value handoff prompt for Today.

    The full health list remains available below; this bubble is the
    developer-facing intervention: one timely choice, like "start fresh" or
    "continue here", with an honest estimate of context pressure avoided.
    """
    candidate = next(
        (row for row in context_health if row.get("severity") in {"critical", "warning"} and row.get("can_handoff")),
        None,
    )
    if not candidate:
        return None
    severity = str(candidate.get("severity") or "warning")
    saved_label = str(candidate.get("estimated_replayed_context_label") or candidate.get("latest_turn_tokens") or "context")
    project = str(candidate.get("project") or "this session")
    if severity == "critical":
        title = f"Start a new chat to save ~{saved_label} tokens of context"
        body = (
            f"{project} is at critical context pressure. Create a fresh-session handoff brief so the next agent "
            "keeps the goal, repo, files, and guardrails without replaying the bloated history."
        )
        primary_label = "New chat"
    else:
        title = f"This session is getting heavy: ~{saved_label} tokens are replayed context"
        body = (
            f"{project} is showing context pressure. Compact or start fresh before the next broad task so usage "
            "does not compound."
        )
        primary_label = "Prepare handoff"
    reason = str(candidate.get("recommendation") or candidate.get("action", {}).get("reason") or body)
    return {
        "session_id": candidate.get("session_id"),
        "project": project,
        "tool": candidate.get("tool"),
        "severity": severity,
        "title": title,
        "body": body,
        "reason": reason,
        "primary_label": primary_label,
        "continue_label": "Continue here",
        "saved_context_label": saved_label,
        "expected_saved_context_tokens": candidate.get("estimated_replayed_context_tokens"),
        "tags": [
            f"{candidate.get('latest_turn_tokens')} tokens/turn",
        ] + ([
            f"{candidate.get('bloat_label')} of spend replayed",
            f"{candidate.get('replayed_cost_label')} on replayed context",
        ] if candidate.get("bloat_measurable") else []),
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


MIN_SURVIVAL_SAMPLES = 5


def _cost_per_surviving_change(all_rows: list[LocalSession]) -> dict[str, object]:
    """S-23: cost of sessions whose commit survived vs. churned, at the earliest checked bucket.

    Deliberately scans the whole local history (all_rows), not just the
    current days-window: survival needs >=7 days of age by definition, so a
    short display window would almost always show zero samples even once
    survival data exists. Honesty-gated the same way baselines are --
    "available: False" until there's enough history to say anything real.
    """
    snapshots = evidence_snapshots_for_sessions()
    rows_by_id = {row.session_id: row for row in all_rows}
    survived_costs: list[float] = []
    churned_costs: list[float] = []
    for session_id, snapshot in snapshots.items():
        survival = snapshot.get("survival") if isinstance(snapshot.get("survival"), dict) else {}
        status = None
        for bucket in ("7", "14", "30"):
            entry = survival.get(bucket)
            if isinstance(entry, dict) and entry.get("status") in {"survived", "churned"}:
                status = entry["status"]
                break
        if status is None:
            continue
        row = rows_by_id.get(session_id)
        if row is None:
            continue
        (survived_costs if status == "survived" else churned_costs).append(row.cost_usd)

    sample_count = len(survived_costs) + len(churned_costs)
    if sample_count < MIN_SURVIVAL_SAMPLES:
        return {"available": False, "sample_count": sample_count, "required_samples": MIN_SURVIVAL_SAMPLES}
    return {
        "available": True,
        "sample_count": sample_count,
        "surviving_count": len(survived_costs),
        "churned_count": len(churned_costs),
        "cost_per_surviving_change": money(sum(survived_costs) / len(survived_costs)) if survived_costs else "—",
        "cost_per_churned_change": money(sum(churned_costs) / len(churned_costs)) if churned_costs else "—",
    }


def _survival_summary() -> dict[str, object]:
    """Cost per surviving line, read from cache.

    Never computed here. Measuring survival runs a git blame pass per file --
    ~23s for a month of history -- so it is refreshed off the hot path by
    cli.get_or_refresh_survival() and only read on a request.

    Replaces the reachability-based survived/churned split, which asked whether
    a commit was still in git history. That stays true after a revert or a full
    rewrite, so it reported 16 of 16 changes surviving locally and its "cost per
    surviving change" was cost-per-change with a different name.
    """
    cached = usable_survival_summary()
    if not cached or not cached.get("available"):
        return {
            "available": False,
            "reason": (cached or {}).get("reason")
            or "Not measured yet. Run `aiwatcher today` or reopen the dashboard to compute it.",
        }
    summary = dict(cached)
    summary["cost_per_surviving_line_label"] = money(float(summary.get("usd_per_surviving_line") or 0))
    summary["cost_per_line_label"] = money(float(summary.get("usd_per_line") or 0))
    summary["measured_cost_label"] = money(float(summary.get("cost_usd") or 0))
    summary["too_recent_label"] = money(float(summary.get("too_recent_usd") or 0))
    return summary


def _window_ledger(events: list[LocalEvent], days: int) -> Ledger | None:
    """The change ledger for this window, or None if git could not be read.

    Computed on the request rather than cached, unlike survival: this is one
    `git log --numstat` per repo that had spend (~0.3s for a week locally),
    where survival is a blame pass per file (~23s). Caching it would also pin
    it to one window, and the whole point is that it moves with the day
    selector alongside every other number on the page.

    Built once per request and shared: the unbanked card and the change table
    are two views of the same ledger, and running git twice for them would
    double the only real cost on this path.
    """
    try:
        return build_ledger(events, days=days)
    except OSError:
        return None


def _unbanked_card(ledger: Ledger | None) -> dict[str, object]:
    """Spend in this window with no commit behind it."""
    if ledger is None:
        return {"available": False, "reason": "Could not read git history for the active repos."}

    card = dict(unbanked_summary(ledger))
    card["unbanked_label"] = money(float(card.get("unbanked_usd") or 0))
    card["banked_label"] = money(float(card.get("banked_usd") or 0))
    card["unresolved_label"] = money(float(card.get("unresolved_usd") or 0))
    card["outside_repo_label"] = money(float(card.get("outside_repo_usd") or 0))
    card["top_repos"] = [
        {**entry, "short_name": short_path(str(entry.get("repo"))),
         "unbanked_label": money(float(entry.get("unbanked_usd") or 0))}
        for entry in (card.get("top_repos") or [])
    ]
    if card.get("available"):
        share = float(card.get("unbanked_pct") or 0)
        card["headline"] = (
            f"{card['unbanked_label']} of the last {card.get('window_days')} days "
            f"({share:.0f}%) has no commit behind it"
        )
        # Says what it is and what it is not. Uncommitted work in progress looks
        # exactly like exploration that went nowhere, and the card must not
        # claim to tell them apart.
        card["caption"] = (
            f"{card['banked_label']} reached a commit. The rest is exploration that "
            "went nowhere or work still uncommitted — this cannot tell them apart."
        )
    return card


def _change_rows(
    ledger: Ledger | None,
    survival: dict[str, object],
    *,
    limit: int = 50,
) -> list[dict[str, object]]:
    """One row per commit: what it cost, how much it wrote, and $/line.

    The aggregate was all that reached the screen -- "$X per surviving line"
    with no way to see which commits drove it. Ranked by cost, because the
    question this answers is "where did the money go", not "what happened
    recently".

    Survival is joined in from the cached summary where it exists. It covers
    only the changes that pass survival's own age gate and cost-coverage walk,
    so most rows have none, and a missing entry means "not measured", never
    "did not survive". Nothing here triggers a blame pass.
    """
    if ledger is None:
        return []
    by_change = survival.get("by_change") if isinstance(survival, dict) else None
    by_change = by_change if isinstance(by_change, dict) else {}

    rows: list[dict[str, object]] = []
    for change in ledger.changes[:limit]:
        measured = by_change.get(change.sha)
        measured = measured if isinstance(measured, dict) else {}
        survived = measured.get("survival_pct") if measured.get("measurable") else None
        rows.append({
            "sha": change.sha,
            "short_sha": change.sha[:8],
            "repo": change.repo,
            "project": short_path(change.repo),
            "subject": change.subject,
            # landed_at is the author date, which is what attribution keys off
            # and the honest answer to "when was this work done".
            "committed_at": change.landed_at.isoformat(),
            "rewritten_at": change.committed_at.isoformat() if change.was_rewritten else None,
            "was_rewritten": change.was_rewritten,
            "cost_usd": round(change.cost_usd, 6),
            "cost_label": money(change.cost_usd),
            "lines_added": change.lines_added,
            "lines_removed": change.lines_removed,
            "lines_changed": change.lines_changed,
            "files_changed": change.files_changed,
            "event_count": change.event_count,
            "tools": change.tools,
            "models": [display_model_name(model) for model in change.models],
            "usd_per_line": (
                round(change.usd_per_line, 6) if change.usd_per_line is not None else None
            ),
            "usd_per_line_label": (
                money(change.usd_per_line) if change.usd_per_line is not None else "—"
            ),
            "survival_pct": survived,
            "survival_label": f"{survived:.0f}%" if survived is not None else "—",
            "usd_per_surviving_line_label": (
                money(float(measured["usd_per_surviving_line"]))
                if measured.get("usd_per_surviving_line") is not None else "—"
            ),
            # A commit with no spend behind it is not free work -- it is work
            # AIWatcher did not observe (hand-written, or from an untracked
            # surface). Saying "$0.00" would read as the opposite.
            "unattributed": change.cost_usd <= 0,
        })
    return rows


def _handoff_decision_rows(limit: int = 10) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in recent_handoff_decisions(limit=limit):
        expected = row.get("expected_saved_context_tokens")
        expected_int = expected if isinstance(expected, int) and expected > 0 else None
        rows.append({
            "id": row.get("id"),
            "created_at": row.get("created_at"),
            "session_id": row.get("session_id"),
            "decision": row.get("decision"),
            "reason": row.get("reason"),
            "expected_saved_context_tokens": expected_int,
            "expected_saved_context_label": compact_int(expected_int) if expected_int else None,
        })
    return rows


def _recent_handoff_decision_session_ids(rows: list[dict[str, object]]) -> set[str]:
    session_ids: set[str] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for row in rows:
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        created_at = row.get("created_at")
        if isinstance(created_at, str):
            try:
                if datetime.fromisoformat(created_at).astimezone(timezone.utc) < cutoff:
                    continue
            except ValueError:
                pass
        session_ids.add(session_id)
    return session_ids
def _insight_feed(
    rows: list[LocalSession],
    all_rows: list[LocalSession],
    all_events: list[LocalEvent],
    *,
    days: int,
    inferred_useful: int,
    needs_review: int,
    churned: int,
) -> list[dict[str, object]]:
    """One ranked list, ordered by how much money each finding is about.

    Replaces three panels that were all built from the same handful of max()
    calls -- Weekly Digest, Local Insights and Daily Journal each restated the
    same top project, costliest session and loop count.

    Two rules decide what earns a place here:
      1. Every card names a comparison. "1.1M tokens in one session" gives the
         reader nothing to do; "97% of it was replayed history, costing $196"
         does. A number with no "versus" is a metric and belongs in a table.
      2. Cards are ranked by dollars, not insertion order, so the biggest
         finding is the one the eye lands on.

    Coverage gaps (tools detected but not scanned) deliberately do NOT appear
    here -- they are a setup concern, they never change, and mixing them in is
    what made the old list read as noise. They live on the Coverage tab.
    """
    cards: list[dict[str, object]] = []

    replay = replayed_context_cost(rows)
    if replay["available"] and replay["sessions"]:
        top = replay["sessions"][0]
        window_cost = sum(row.cost_usd for row in rows)
        share = (replay["total_replayed_usd"] / window_cost * 100) if window_cost > 0 else 0
        cards.append({
            "id": "replayed-context",
            "title": f"{share:.0f}% of your spend went on re-sending conversation history",
            "body": (
                f"{money(replay['total_replayed_usd'])} of {money(window_cost)} this window. The worst session replayed "
                f"{top['replayed_pct']:.0f}% of its context, {money(top['replayed_usd'])} of its "
                f"{money(top['session_usd'])}. Compacting or starting fresh earlier is what this buys back."
            ),
            "impact_usd": replay["total_replayed_usd"],
            "session_id": top["session_id"],
            "severity": "high" if share >= 40 else "medium",
        })

    pace = pace_vs_baseline(all_events, days=days)
    if pace["available"] and pace["ratio"] >= 1.25:
        excess = max(0.0, pace["current_usd"] - pace["baseline_usd"])
        cards.append({
            "id": "pace",
            "title": f"You are {pace['ratio']:.1f}x your usual pace",
            "body": (
                f"{money(pace['current_usd'])} in the last {days} days against a "
                f"{money(pace['baseline_usd'])} average over your previous "
                f"{pace['baseline_windows']} windows. Local logs cannot see your plan's quota, so this "
                f"compares you to yourself rather than to a limit."
            ),
            "impact_usd": excess,
            "session_id": None,
            "severity": "medium" if pace["ratio"] < 2 else "high",
        })

    models = model_cost_comparison(all_rows)
    if models["available"]:
        dear = models["by_session"]["dearest"]
        cheap = models["by_session"]["cheapest"]
        if models["driver"] == "volume":
            body = (
                f"{dear['label']} sessions cost {models['by_session']['ratio']:.1f}x a {cheap['label']} "
                f"session, but they run {models['volume_factor']:.0f}x more tokens -- per token it is "
                f"{models['rate_factor']:.2f}x the rate. The gap is how you use it, not what it charges."
            )
        elif models["driver"] == "rate":
            body = (
                f"{dear['label']} costs {models['rate_factor']:.1f}x more per token than {cheap['label']} "
                f"on comparably sized sessions. Worth checking which tasks genuinely need it."
            )
        else:
            body = (
                f"{dear['label']} sessions cost {models['by_session']['ratio']:.1f}x a {cheap['label']} "
                f"session -- {models['volume_factor']:.1f}x from size and {models['rate_factor']:.2f}x "
                f"from rate."
            )
        cards.append({
            "id": "model-mix",
            "title": f"{dear['label']} is {models['by_session']['ratio']:.1f}x your {cheap['label']} sessions",
            "body": body,
            # No dollar figure: the models are not interchangeable for every
            # task, so quoting a "saving" would promise something untestable.
            "impact_usd": None,
            "session_id": None,
            "severity": "info",
        })

    if churned:
        cards.append({
            "id": "churned",
            "title": f"{churned} session{'s' if churned != 1 else ''} looked useful but did not stick",
            "body": "The commit was later reverted or rewritten. Worth a look before repeating the approach.",
            "impact_usd": None,
            "session_id": None,
            "severity": "medium",
        })
    if inferred_useful or needs_review:
        total = inferred_useful + needs_review
        cards.append({
            "id": "outcome-review",
            "title": f"{total} session{'s' if total != 1 else ''} still need an outcome",
            "body": (
                f"{inferred_useful} have a nearby commit or test; {needs_review} changed files without one. "
                "Confirming them sharpens every cost-per-outcome number on this page."
            ),
            "impact_usd": None,
            "session_id": None,
            "severity": "info",
        })

    # Dollar-weighted findings first, biggest first; everything else after, in
    # the order it was added.
    with_impact = [card for card in cards if card["impact_usd"] is not None]
    without_impact = [card for card in cards if card["impact_usd"] is None]
    with_impact.sort(key=lambda card: float(card["impact_usd"] or 0), reverse=True)
    for card in with_impact:
        card["impact_label"] = money(float(card["impact_usd"] or 0))
    for card in without_impact:
        card["impact_label"] = ""
    return [*with_impact, *without_impact]


def build_summary(days: int = 7) -> dict[str, object]:
    now = datetime.now().astimezone()
    since = now - timedelta(days=days)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    all_rows = scan_all()
    try:
        link_recent_interventions_to_sessions(all_rows)
    except OSError:
        pass
    # Events are loaded up front because the windows are clipped by them: a
    # session merely *touched* inside a window used to contribute every dollar
    # it had ever cost, which on long-running sessions roughly doubled the
    # reported total.
    try:
        all_events = scan_all_events()
    except OSError:
        all_events = []
    rows = clip_sessions_to_window(all_rows, all_events, since)
    month_rows = clip_sessions_to_window(all_rows, all_events, month_start)

    stats = summarize(rows)
    month_stats = summarize(month_rows)
    split = token_split(rows)
    day_of_month = max(1, now.day)
    projected_month = float(month_stats["api_value_usd"]) / day_of_month * 30

    projects = group_rows(rows, lambda row: row.project_path or "unknown")
    tools = group_rows(rows, _tool_surface_key)
    models = group_by_model_breakdown(rows)

    recent = sorted(rows, key=lambda row: row.updated_at or row.started_at or MIN_DT, reverse=True)[:12]
    detected = discover_tools()
    notes = sorted({note for row in rows for note in row.notes})
    context_health = _context_health_cards(rows, all_events)
    handoff_decisions = _handoff_decision_rows(limit=10)
    suppressed_handoff_sessions = _recent_handoff_decision_session_ids(handoff_decisions)
    handoff_bubble = _handoff_bubble([
        row for row in context_health
        if str(row.get("session_id", "")) not in suppressed_handoff_sessions
    ])

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
    if detected.get("cline"):
        insights.append({
            "title": "Cline detected, not scanned yet",
            "body": "AIWatcher can see Cline is present, but does not claim session, token, or cost coverage for it yet.",
        })
    if detected.get("windsurf"):
        insights.append({
            "title": "Windsurf detected, not scanned yet",
            "body": "AIWatcher can see Windsurf is present, but does not claim session, token, or cost coverage for it yet.",
        })
    if context_health:
        top_health = context_health[0]
        if top_health["severity"] in {"warning", "critical"}:
            insights.append({
                "title": "Context health needs attention",
                "body": (
                    f"{top_health['project']} is {top_health['severity']} at "
                    f"{top_health['latest_turn_tokens']} tokens/turn. "
                    f"Suggested action: {top_health['action']['label']}."
                ),
            })

    window_session_ids = {row.session_id for row in rows}
    window_outcomes = outcomes_for_sessions(window_session_ids)
    outcomes = outcome_counts(window_session_ids)
    sample_rows = rows[:30]
    evidence_by_session = evidence_for_sessions(sample_rows, survival_by_session=survival_by_session(sample_rows))
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
    churned = sum(1 for evidence in evidence_by_session.values() if evidence.inferred_outcome == "churned")
    replayed_tokens = sum(row.cache_read_tokens for row in rows)
    insights = _insight_feed(
        rows,
        all_rows,
        all_events,
        days=days,
        inferred_useful=inferred_useful,
        needs_review=needs_review,
        churned=churned,
    )
    survival_summary = _survival_summary()
    window_ledger = _window_ledger(all_events, days)
    unbanked = _unbanked_card(window_ledger)
    changes = _change_rows(window_ledger, survival_summary)
    changes_meta = {
        # Named rather than silently dropped: a window can legitimately hold a
        # teammate's commits, and a table that just omitted them would look
        # like it had lost work.
        "foreign_changes": window_ledger.foreign_changes if window_ledger else 0,
        "repos": len(window_ledger.repos) if window_ledger else 0,
    }
    interventions = recent_interventions(limit=200, days=days)
    receipt_events = all_events if interventions else []
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
            # A single token total is misleading once cache reads are counted:
            # replayed history is the same content billed again on every turn,
            # so the combined figure runs into the billions and says nothing
            # about how much was actually written. Split it.
            "new_tokens_label": compact_int(max(0, int(stats["tokens"]) - replayed_tokens)),
            "replayed_tokens_label": compact_int(replayed_tokens),
            "replayed_share_pct": (
                round(100.0 * replayed_tokens / int(stats["tokens"]), 1)
                if int(stats["tokens"]) > 0 else 0.0
            ),
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
        "survival": survival_summary,
        "unbanked": unbanked,
        "changes": changes,
        "changes_meta": changes_meta,
        "projects": projects[:10],
        "tools": tools,
        "models": models[:10],
        "insights": insights,
        "notes": notes[:5],
        "coverage": [row.to_json() for row in surface_coverage(all_rows)],
        "setup": setup_checklist(),
        "context_health": context_health,
        "handoff_bubble": handoff_bubble,
        "handoff_decisions": handoff_decisions,
        "recent_sessions": [
            {
                "tool": row.tool,
                "session_id": row.session_id,
                "project": short_path(row.project_path),
                "project_full": row.project_path or "unknown",
                "model": display_model_name(row.model),
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
    ctx_k = round(health.latest_turn_tokens / 1000)
    if health.bloat_measurable:
        headline = (
            f"This session is at {ctx_k}K tokens/turn, and "
            f"{int(health.bloat_ratio * 100)}% of its ${health.analyzed_cost_usd:.2f} "
            "so far went on re-sending history."
        )
    else:
        headline = f"This session is at {ctx_k}K tokens/turn."
    lines = [
        headline,
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
    td.num, th.num { text-align: right; white-space: nowrap; }
    .muted { color: var(--muted); }
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
    .headline { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin: 4px 0 16px; }
    .headline-figure { font-size: 30px; font-weight: 600; letter-spacing: -.5px; }
    .headline-sub { color: var(--muted); font-size: 14px; }
    .feed-row { display: flex; align-items: flex-start; gap: 14px; padding: 14px 0; border-top: 1px solid var(--line); }
    .feed-row:first-child { border-top: none; }
    .feed-row.clickable { cursor: pointer; }
    .feed-row.clickable:hover { background: var(--surface-hover); }
    .feed-main { flex: 1; min-width: 0; border-left: 3px solid var(--line-strong); padding-left: 13px; }
    .feed-row.high .feed-main { border-left-color: var(--red); }
    .feed-row.medium .feed-main { border-left-color: var(--amber); }
    .feed-row.info .feed-main { border-left-color: var(--line-strong); }
    .feed-main strong { display: block; color: white; margin-bottom: 4px; }
    .feed-main p { margin: 0; color: var(--muted); line-height: 1.5; }
    .feed-impact { font-size: 16px; white-space: nowrap; padding-top: 1px; }
    .insight strong { display: block; margin-bottom: 3px; color: white; }
    .pill-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
    .pill { border: 1px solid var(--line); background: #0b1118; border-radius: 999px; padding: 5px 9px; color: #bdc9d9; font-size: 11px; }
    .coverage-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .coverage-card, .health-card { border: 1px solid var(--line); border-radius: 8px; background: #0b1118; padding: 14px; }
    .coverage-head, .health-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
    .coverage-status, .health-severity { border-radius: 999px; border: 1px solid var(--line); padding: 4px 8px; font-size: 11px; font-weight: 800; white-space: nowrap; }
    .coverage-status.automatic, .health-severity.healthy { color: #bff5df; border-color: rgba(53,211,153,.45); background: var(--green-soft); }
    .coverage-status.limited, .coverage-status.unverified, .health-severity.warning { color: #ffe2a4; border-color: rgba(246,189,96,.45); background: var(--amber-soft); }
    .coverage-status.companion { color: #dceaff; border-color: rgba(112,167,255,.45); background: var(--blue-soft); }
    .coverage-status.unsupported, .coverage-status.not_detected, .health-severity.critical { color: #ffc4ce; border-color: rgba(242,125,143,.45); background: var(--red-soft); }
    .coverage-detail, .health-detail { display: grid; gap: 6px; color: var(--muted); font-size: 12px; line-height: 1.45; }
    .health-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .handoff-bubble {
      border-color: rgba(112,167,255,.5);
      background: #eaf2ff;
      color: #172237;
      box-shadow: 0 10px 26px rgba(7,18,32,.18);
    }
    .handoff-bubble h2 { color: #1f5fa8; }
    .handoff-bubble p, .handoff-bubble .sub { color: #4b5870; }
    .handoff-bubble .pill { background: rgba(255,255,255,.72); border-color: #bfd2ec; color: #37445a; }
    .handoff-bubble .btn-primary { background: #ffffff; border-color: #c5d7ee; color: #172237; }
    .handoff-bubble .btn-primary:hover { background: #f8fbff; border-color: #8fb6e8; }
    .handoff-bubble .btn-quiet { background: transparent; border-color: transparent; color: #4b5870; }
    .handoff-bubble .btn-quiet:hover { background: rgba(255,255,255,.6); border-color: #c5d7ee; color: #172237; }
    .outcome-pill.useful { color: #bff5df; border-color: rgba(53,211,153,.38); background: var(--green-soft); }
    .outcome-pill.rework { color: #ffe2a4; border-color: rgba(246,189,96,.38); background: var(--amber-soft); }
    .outcome-pill.abandoned { color: #ffc4ce; border-color: rgba(242,125,143,.38); background: var(--red-soft); }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid var(--line); padding: 11px 8px; text-align: left; }
    th { color: var(--muted); font-weight: 600; }
    td:last-child, th:last-child { text-align: right; }
    tr.clickable:hover { background: rgba(112,167,255,.05); }
    .session-filters { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin: 14px 0 4px; }
    .session-filters input[type="text"] {
      flex: 1; min-width: 220px; border: 1px solid var(--line-strong); background: var(--surface-raised);
      color: var(--text); border-radius: 8px; min-height: 38px; padding: 8px 12px; font: inherit;
    }
    .session-filters input[type="text"]:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
    .row-action { min-height: 30px; padding: 5px 9px; color: #ddecff; background: var(--blue-soft); border-color: #3d6594; font-size: 12px; }
    .empty { color: var(--muted); padding: 16px; border: 1px dashed var(--line); border-radius: 8px; }
    .digest-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--line); }
    .digest-row:last-child { border-bottom: 0; }
    .digest-row.clickable { cursor: pointer; }
    .digest-row.clickable:hover { background: rgba(255,255,255,.03); }
    .digest-row .digest-row-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #dce6f6; }
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
    .outcome-button.selected { font-weight: 700; }
    .outcome-button.selected.useful { background: rgba(53,211,153,.28); box-shadow: inset 0 0 0 2px var(--green); color: #eafff5; }
    .outcome-button.selected.rework { background: rgba(246,189,96,.28); box-shadow: inset 0 0 0 2px var(--amber); color: #fff3dc; }
    .outcome-button.selected.abandoned { background: rgba(242,125,143,.28); box-shadow: inset 0 0 0 2px var(--red); color: #ffe3e8; }
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
      .coverage-grid { grid-template-columns: 1fr; }
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
    <button class="nav-tab" data-view="changes" onclick="showView('changes')">Changes</button>
    <button class="nav-tab" data-view="receipts" onclick="showView('receipts')">Receipts</button>
    <button class="nav-tab" data-view="insights" onclick="showView('insights')">Insights</button>
    <button class="nav-tab" data-view="coverage" onclick="showView('coverage')">Coverage</button>
    <button class="nav-tab" data-view="setup" onclick="showView('setup')">Setup</button>
  </nav>

  <section id="view-today" class="view">
    <section id="handoffBubble" class="card handoff-bubble" style="margin-bottom:14px" hidden></section>

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

    <section class="card" style="margin-bottom:14px">
      <div class="section-title">
        <div><h2>This week's digest</h2><p>A quick pulse on cost, outcomes, and what to change next.</p></div>
        <button class="btn-quiet" onclick="showView('insights')">View full digest</button>
      </div>
      <div id="todayDigest"></div>
    </section>

    <section class="grid kpis">
      <div class="card metric-card metric-green"><div class="label">Useful outcomes</div><div class="value" id="usefulOutcomes">-</div><div class="sub">Value per useful change: <span id="costPerUseful">-</span></div><div class="sub" id="costPerSurvivingRow" hidden>Cost per surviving line: <span id="costPerSurviving">-</span></div></div>
      <div class="card metric-card metric-amber"><div class="label">Preflight decisions</div><div class="value" id="preflightDecisions">-</div><div class="sub"><span id="windowLabel">-</span></div></div>
      <div class="card metric-card metric-blue"><div class="label">Sessions observed</div><div class="value" id="sessions">-</div><div class="sub">This machine only</div></div>
      <div class="card metric-card metric-red"><div class="label">API-equivalent value</div><div class="value" id="apiValue">-</div><div class="sub">Excludes subscription allocation</div></div>
    </section>

    <section class="card" style="margin-bottom:14px">
      <div class="section-title"><div><h2>Unbanked spend</h2><p>AI spend in this window with no commit behind it.</p></div></div>
      <div id="unbanked"></div>
    </section>

    <section class="card" style="margin-bottom:14px">
      <div class="section-title"><div><h2>Latest intervention</h2><p>What AIWatcher changed before execution and what happened afterward.</p></div></div>
      <div id="latestIntervention"></div>
    </section>

    <section class="card" style="margin-bottom:14px">
      <div class="section-title"><div><h2>Latest handoff decision</h2><p>Fresh-session handoff choices and expected context avoided.</p></div></div>
      <div id="latestHandoffDecision"></div>
    </section>

    <section class="card" style="margin-bottom:14px">
      <div class="section-title"><div><h2>Session health</h2><p>Context bloat, runway pressure, and handoff actions for active local work.</p></div></div>
      <div id="contextHealth"></div>
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
        <div><h2>Sessions</h2><p>Search and resume prior local AI work. Click any row to inspect locally — prompt text is shown for your own review only, never uploaded.</p></div>
        <span class="pill">Local machine only</span>
      </div>
      <div class="session-filters">
        <input id="sessionSearch" type="text" placeholder="Search project, tool, model, session id, or a file/topic keyword" oninput="debounceSessionSearch()">
        <select id="sessionOutcomeFilter" onchange="loadSessions()">
          <option value="">Any outcome</option>
          <option value="useful">Useful</option>
          <option value="rework">Rework</option>
          <option value="abandoned">Abandoned</option>
        </select>
        <select id="sessionEvidenceFilter" onchange="loadSessions()">
          <option value="">Any evidence</option>
          <option value="useful">Evidence: likely useful</option>
          <option value="needs_review">Evidence: needs review</option>
          <option value="churned">Evidence: reverted/rewritten</option>
        </select>
        <button class="btn-quiet" onclick="clearSessionFilters()">Clear</button>
      </div>
      <p class="receipt-note" id="sessionResultsNote"></p>
      <div class="table-wrap"><table>
        <thead><tr><th>Tool</th><th>Project</th><th>Model</th><th>Tokens</th><th></th></tr></thead>
        <tbody id="sessionRows"></tbody>
      </table></div>
    </div>
  </section>

  <section id="view-changes" class="view" hidden>
    <div class="card">
      <div class="section-title">
        <div><h2>Cost per change</h2><p>What each commit cost in AI spend, and what that works out to per line.</p></div>
        <span class="pill">Ranked by cost</span>
      </div>
      <div id="changeTotals"></div>
      <div class="table-wrap"><table>
        <thead><tr>
          <th>Commit</th><th>Project</th><th class="num">Cost</th><th class="num">Lines</th>
          <th class="num">$/line</th><th class="num">Still standing</th><th class="num">$/surviving line</th>
        </tr></thead>
        <tbody id="changeRows"></tbody>
      </table></div>
      <p class="receipt-note">A change's cost is the AI spend in that repo since the previous change, attributed per
        model call rather than per session, and capped at a 12h lookback from when the work was <em>authored</em> —
        rebasing rewrites a commit's date, and keying off that stranded the spend behind it. Survival is only measured
        for changes old enough to judge and costly enough to reach the sampling budget — a blank means not measured,
        not "did not survive". It is a floor either way: reformatting moves attribution away from the original change.</p>
    </div>
  </section>

  <section id="view-receipts" class="view" hidden>
    <div class="card" style="margin-bottom:14px">
      <div class="section-title">
        <div><h2>Handoff decisions</h2><p>When AIWatcher suggested a fresh session, what you chose, and expected context avoided.</p></div>
        <span class="pill">Metadata only</span>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Time</th><th>Decision</th><th>Expected context avoided</th><th>Session</th><th></th></tr></thead>
        <tbody id="handoffDecisionRows"></tbody>
      </table></div>
    </div>
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
    <section class="card" style="margin-bottom:14px">
      <div class="section-title">
        <div><h2>What your week cost</h2><p>Ranked by how much money each finding is about.</p></div>
        <span class="pill">Local logs only</span>
      </div>
      <div id="insightHeadline"></div>
      <div id="insightFeed"></div>
    </section>
    <section class="card" style="margin-bottom:14px">
      <div class="section-title">
        <div><h2>Outcomes and guardrails</h2><p>What stuck, and what was caught before it ran.</p></div>
      </div>
      <div id="report"></div>
    </section>
    <p class="receipt-note" style="margin-top:4px">
      Read-only local scan &middot; no LLM calls &middot; no source or prompt content in summaries &middot;
      nothing leaves this machine unless you connect Cloud.
    </p>
  </section>

  <section id="view-coverage" class="view" hidden>
    <div class="card">
      <div class="section-title">
        <div><h2>Surface Coverage</h2><p>What AIWatcher can gate, scan, or only help with manually on this machine.</p></div>
        <span class="pill">Verified locally</span>
      </div>
      <div id="coverageRows" class="coverage-grid"></div>
    </div>
  </section>

  <section id="view-setup" class="view" hidden>
    <div class="card">
      <div class="section-title">
        <div><h2>Setup Checklist</h2><p>Get to first value without overclaiming which surfaces are protected.</p></div>
        <span class="pill">Local only</span>
      </div>
      <p class="receipt-note">For ambient warnings while you work, run <code>aiwatcher watch --notify --interval 60</code>.</p>
      <div id="setupRows" class="coverage-grid"></div>
    </div>
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
function handoffDecisionLabel(value) {
  const labels = {
    new_chat: 'Prepared fresh-session handoff',
    continue_here: 'Continued in current session',
    copy_handoff: 'Copied handoff brief',
    dismissed: 'Dismissed'
  };
  return labels[value] || value || 'unknown';
}
function renderLatestHandoffDecision(decisions) {
  const decision = (decisions || [])[0];
  if (!decision) return '<div class="empty">No handoff bubble decisions recorded yet.</div>';
  const saved = decision.expected_saved_context_label
    ? `<span class="pill">~${esc(decision.expected_saved_context_label)} context avoided</span>`
    : '';
  return `<div class="receipt-summary"><div>
    <div class="session-title">${esc(handoffDecisionLabel(decision.decision))}</div>
    <div class="session-meta">${esc(dateLabel(decision.created_at))} · session ${esc(decision.session_id || 'unknown')}</div>
    <p>${esc(decision.reason || 'AIWatcher recommended a fresh-session handoff because local context health crossed a threshold.')}</p>
    <div class="pill-row"><span class="pill">handoff bubble</span>${saved}</div>
  </div>${decision.session_id ? `<button class="btn-quiet" onclick="selectSession('${esc(decision.session_id)}')">Inspect session</button>` : ''}</div>`;
}
function renderHandoffDecisionRows(decisions) {
  if (!decisions.length) return '<tr><td colspan="5"><div class="empty">No handoff decisions recorded yet.</div></td></tr>';
  return decisions.map(decision => `<tr>
    <td>${esc(dateLabel(decision.created_at))}</td>
    <td>${esc(handoffDecisionLabel(decision.decision))}</td>
    <td>${esc(decision.expected_saved_context_label ? `~${decision.expected_saved_context_label}` : '—')}</td>
    <td>${esc(decision.session_id || 'unknown')}</td>
    <td>${decision.session_id ? `<button class="row-action" onclick="selectSession('${esc(decision.session_id)}')">Inspect</button>` : ''}</td>
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
    return true;
  } catch (error) {
    showToast('Copy failed. Select the text manually.', 'error');
    return false;
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
  if (session.inferred_outcome === 'churned') return '<span class="pill outcome-pill rework">Evidence: reverted/rewritten</span>';
  if (session.inferred_outcome === 'useful') return '<span class="pill outcome-pill useful">Evidence: likely useful</span>';
  if (session.inferred_outcome === 'needs_review') return '<span class="pill outcome-pill rework">Evidence: review changes</span>';
  return '';
}
function survivalLabel(survival) {
  // evidence.survival is {bucket: "survived"|"churned"|"unknown"} -- already
  // flattened server-side (see ui.py's _survival_for_session), not the raw
  // {status, checked_at} shape local_state.py stores it in.
  if (!survival) return null;
  for (const bucket of ['7', '14', '30']) {
    const status = survival[bucket];
    if (status) return `${status} (${bucket}-day check)`;
  }
  return null;
}
function renderEvidence(evidence) {
  if (!evidence) return '';
  const commits = evidence.commits || [];
  const files = evidence.changed_files || [];
  const tests = evidence.tests || [];
  const reasons = evidence.reasons || [];
  const survival = survivalLabel(evidence.survival);
  return `<section class="detail-section"><h3>Outcome evidence</h3>
    <p>Local git/test signals. AIWatcher stores metadata, not source diffs.</p>
    <div class="mini-grid">
      <div class="mini"><span class="label">Inferred outcome</span><strong>${esc(evidence.inferred_outcome || 'not enough evidence')}</strong></div>
      <div class="mini"><span class="label">Confidence</span><strong>${esc(evidence.confidence || 'low')}</strong></div>
      <div class="mini"><span class="label">Nearby commits</span><strong>${esc(commits.length)}</strong></div>
      <div class="mini"><span class="label">Changed files</span><strong>${esc(files.length)}</strong></div>
    </div>
    ${survival ? `<div class="pill-row"><span class="pill">Survival: ${esc(survival)}</span></div>` : ''}
    ${evidence.same_file_reprompt ? '<div class="pill-row"><span class="pill rework">A later session touched the same file(s) again soon after</span></div>' : ''}
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
  const churned = !s.outcome && evidence.inferred_outcome === 'churned';
  const likelyUseful = !s.outcome && evidence.inferred_outcome === 'useful';
  const highCost = cost >= 5 || tokens >= 500000 || toolCalls >= 250;
  let title = 'Review this AI work';
  if (s.outcome) title = `Marked ${s.outcome}`;
  else if (churned) title = "Looked useful, but didn't stick";
  else if (likelyUseful && highCost) title = 'Likely useful, but expensive';
  else if (likelyUseful) title = 'Likely useful, needs confirmation';
  else if (highCost) title = 'High-cost session, needs review';
  const bullets = [];
  if (churned) bullets.push('The commit this session produced was later reverted or rewritten -- it did not survive on the current branch.');
  if (likelyUseful) bullets.push('A nearby commit or test signal suggests this produced useful work.');
  if (evidence.same_file_reprompt) bullets.push('A later session touched the same file(s) again soon after -- this attempt may not have fully resolved the task.');
  if (cost >= 5) bullets.push(`${s.api_value} API-equivalent value is high for one local session.`);
  if (tokens >= 500000) bullets.push(`${s.tokens_label} tokens indicates heavy context pressure.`);
  if (toolCalls >= 250) bullets.push(`${s.tool_calls} tool calls suggests broad search, retries, or loop-like work.`);
  if (!bullets.length) bullets.push('No urgent cost or outcome signal was detected.');
  return { title, tone: churned ? 'high' : likelyUseful ? 'useful' : highCost ? 'high' : '', bullets };
}
function renderVerdict(s) {
  const verdict = sessionVerdict(s);
  const subtitle = s.outcome
    ? 'Saved locally. Pick a different button below anytime to change it.'
    : 'Confirm the outcome, then use the expensive asks below to improve the next run.';
  return `<div class="verdict-card ${esc(verdict.tone)}"><h3>${esc(verdict.title)}</h3>
    <p>${esc(subtitle)}</p>
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
async function copyHandoffFromBubble(sessionId) {
  if (window.currentHandoffBubble) await recordHandoffDecision(window.currentHandoffBubble, 'copy_handoff');
  const res = await fetch(`/api/handoff?id=${encodeURIComponent(sessionId)}&target=generic&prompt=0`);
  const capsule = await res.json();
  if (capsule.error) {
    showToast(capsule.error, 'error');
    return;
  }
  const copied = await copyText(capsule.next_brief || '', 'Handoff copied — paste it into a fresh AI chat');
  if (copied) renderHandoffCopied(window.currentHandoffBubble, sessionId);
}
async function recordHandoffDecision(bubble, decision) {
  if (!bubble || !bubble.session_id) return;
  try {
    await fetch('/api/handoff-decision', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: bubble.session_id,
        decision,
        reason: bubble.reason || bubble.body || '',
        expected_saved_context_tokens: bubble.expected_saved_context_tokens || null,
      })
    });
  } catch (error) {
    // Decision receipts should never block the user's flow.
  }
}
async function startFreshFromBubble(sessionId) {
  if (window.currentHandoffBubble) await recordHandoffDecision(window.currentHandoffBubble, 'new_chat');
  await openHandoff(sessionId);
}
async function continueFromBubble() {
  if (window.currentHandoffBubble) await recordHandoffDecision(window.currentHandoffBubble, 'continue_here');
  document.getElementById('handoffBubble').hidden = true;
  showToast('Handoff decision saved: continue here');
}
function renderHandoffCopied(bubble, sessionId) {
  const node = document.getElementById('handoffBubble');
  if (!node || !bubble) return;
  node.hidden = false;
  node.innerHTML = `<div class="section-title">
      <div>
        <h2>Handoff copied. Start a fresh chat now.</h2>
        <p>Paste the copied brief into Claude, Codex, Cursor, or your next AI tool. AIWatcher saved this decision locally and will stop nudging this session for now.</p>
      </div>
      <span class="pill">saved</span>
    </div>
    <div class="pill-row">
      <span class="pill">${esc(bubble.expected_saved_context_label || 'fresh context')}</span>
      <span class="pill">privacy-safe metadata</span>
      <span class="pill">decision receipt saved</span>
    </div>
    <div class="actions" style="margin-top:14px">
      <button class="btn-primary" data-session="${esc(sessionId)}" onclick="openHandoff(this.dataset.session)">Open capsule</button>
      <button class="btn-quiet" onclick="showView('receipts')">View receipt</button>
      <button class="btn-quiet" onclick="document.getElementById('handoffBubble').hidden = true">Dismiss</button>
    </div>`;
}
function renderHandoffBubble(bubble) {
  const node = document.getElementById('handoffBubble');
  window.currentHandoffBubble = bubble || null;
  if (!bubble) {
    node.hidden = true;
    node.innerHTML = '';
    return;
  }
  node.hidden = false;
  node.innerHTML = `<div class="section-title">
      <div>
        <h2>${esc(bubble.title)}</h2>
        <p>${esc(bubble.body)}</p>
      </div>
      <span class="pill">${esc(bubble.severity)}</span>
    </div>
    <div class="pill-row">${(bubble.tags || []).map(tag => `<span class="pill">${esc(tag)}</span>`).join('')}</div>
    <div class="actions" style="margin-top:14px">
      <button class="btn-primary" data-session="${esc(bubble.session_id)}" onclick="startFreshFromBubble(this.dataset.session)">${esc(bubble.primary_label || 'New chat')}</button>
      <button class="btn-quiet" data-session="${esc(bubble.session_id)}" onclick="copyHandoffFromBubble(this.dataset.session)">Copy handoff</button>
      <button class="btn-quiet" onclick="continueFromBubble()">${esc(bubble.continue_label || 'Continue here')}</button>
      <button class="btn-quiet" data-session="${esc(bubble.session_id)}" onclick="selectSession(this.dataset.session)">Inspect session</button>
    </div>`;
}
function dateLabel(value) {
  if (!value) return 'unknown';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
function renderChangeRows(rows) {
  if (!rows.length) {
    return `<tr><td colspan="7" class="empty">No commits in this window, or git history could not be read.</td></tr>`;
  }
  return rows.map(row => `<tr>
    <td><code>${esc(row.short_sha)}</code> ${esc(row.subject)}
      <div class="session-meta">${esc(dateLabel(row.committed_at))}${row.tools.length ? ' &middot; ' + esc(row.tools.join(', ')) : ''}${row.event_count ? ' &middot; ' + esc(row.event_count) + ' model calls' : ''}${row.was_rewritten ? ' &middot; <span class="muted" title="Rebased or amended on ' + esc(dateLabel(row.rewritten_at)) + '. Cost is attributed by when the work was authored, not when it was rewritten.">rewritten</span>' : ''}</div></td>
    <td>${esc(row.project)}</td>
    <td class="num">${row.unattributed ? '<span class="muted">no spend observed</span>' : esc(row.cost_label)}</td>
    <td class="num">+${esc(row.lines_added)} / -${esc(row.lines_removed)}
      <div class="session-meta">${esc(row.files_changed)} file(s)</div></td>
    <td class="num">${row.unattributed ? '—' : esc(row.usd_per_line_label)}</td>
    <td class="num">${esc(row.survival_label)}</td>
    <td class="num">${esc(row.usd_per_surviving_line_label)}</td>
  </tr>`).join('');
}
function renderChangeTotals(rows, meta) {
  if (!rows.length) return '';
  const foreign = (meta && meta.foreign_changes) || 0;
  const note = foreign
    ? `<p class="receipt-note" style="margin-top:0">${esc(foreign)} commit(s) in this window were written by someone else and
       arrived by fetch — excluded, because no spend on this machine can belong to them.</p>` : '';
  const attributed = rows.filter(row => !row.unattributed);
  const cost = attributed.reduce((sum, row) => sum + row.cost_usd, 0);
  const lines = attributed.reduce((sum, row) => sum + row.lines_changed, 0);
  const measured = rows.filter(row => row.survival_pct !== null).length;
  return `<div class="mini-grid" style="margin-bottom:12px">
    <div class="mini"><span class="label">Commits</span><strong>${esc(rows.length)}</strong></div>
    <div class="mini"><span class="label">Attributed spend</span><strong>${esc(fmtMoney(cost))}</strong></div>
    <div class="mini"><span class="label">Lines changed</span><strong>${esc(lines.toLocaleString())}</strong></div>
    <div class="mini"><span class="label">Survival measured</span><strong>${esc(measured)} of ${esc(rows.length)}</strong></div>
  </div>${note}`;
}
function fmtMoney(value) {
  return '$' + (Math.round(value * 100) / 100).toFixed(2);
}
function renderUnbanked(card) {
  if (!card || !card.available) {
    return `<div class="empty">${esc((card && card.reason) || 'Not measured for this window.')}</div>`;
  }
  const repos = (card.top_repos || []).map(entry =>
    `<li>${esc(entry.short_name)} &middot; <strong>${esc(entry.unbanked_label)}</strong></li>`).join('');
  const outside = card.outside_repo_usd > 0
    ? `<span class="pill">${esc(card.outside_repo_label)} outside any repo</span>` : '';
  // Surfaced, not hidden: spend git could not answer for is excluded from the
  // headline, so the headline would otherwise silently shrink without saying why.
  const unresolved = card.unresolved_usd > 0
    ? `<span class="pill">${esc(card.unresolved_label)} unresolved (git could not read ${esc((card.unresolved_repos || []).length)} repo(s))</span>` : '';
  return `<div class="headline">
      <span class="headline-figure">${esc(card.unbanked_label)}</span>
      <span class="headline-sub">${esc(card.unbanked_pct)}% of the last ${esc(card.window_days)} days had no commit behind it</span>
    </div>
    <div class="mini-grid" style="margin-top:12px">
      <div class="mini"><span class="label">Reached a commit</span><strong>${esc(card.banked_label)}</strong></div>
      <div class="mini"><span class="label">Never did</span><strong>${esc(card.unbanked_label)}</strong></div>
      <div class="mini"><span class="label">Commits in window</span><strong>${esc(card.changes)}</strong></div>
      <div class="mini"><span class="label">Model calls unbanked</span><strong>${esc(card.unbanked_events)}</strong></div>
    </div>
    ${repos ? `<p class="receipt-note" style="margin-bottom:4px">Where it went:</p>
      <ul style="margin:0 0 10px 18px;padding:0">${repos}</ul>` : ''}
    <div class="pill-row">${outside}${unresolved}</div>
    <p class="receipt-note">${esc(card.caption)}
      Spend banks against the next commit in the same repo within
      ${esc(card.max_lookback_hours)}h; anything older stays unbanked rather than being misattributed.</p>`;
}
function renderContextHealth(rows) {
  if (!rows.length) return '<div class="empty">No active context-health warnings. AIWatcher will surface bloat, stale sessions, and handoff opportunities here.</div>';
  return `<div class="coverage-grid">${rows.map(row => `<div class="health-card">
    <div class="health-head">
      <div><h3>${esc(row.project)}</h3><p>${esc(row.tool)} · ${esc(row.age_label)}</p></div>
      <span class="health-severity ${esc(row.severity)}">${esc(row.severity)}</span>
    </div>
    <div class="mini-grid">
      <div class="mini"><span class="label">Latest turn</span><strong>${esc(row.latest_turn_tokens)}</strong></div>
      <div class="mini"><span class="label">Peak turn</span><strong>${esc(row.peak_turn_tokens)}</strong></div>
      <div class="mini"><span class="label">Spend on replay</span><strong>${esc(row.bloat_label)}</strong></div>
      <div class="mini"><span class="label">Replay cost</span><strong>${esc(row.replayed_cost_label)}</strong></div>
    </div>
    <p>${esc(row.recommendation)}</p>
    <div class="health-actions">
      <button class="btn-primary" data-session="${esc(row.session_id)}" onclick="selectSession(this.dataset.session)">${esc(row.action.label)}</button>
      ${row.can_handoff ? `<button class="btn-quiet" data-session="${esc(row.session_id)}" onclick="openHandoff(this.dataset.session)">${esc(row.action.secondary_label)}</button>` : ''}
      <button class="btn-quiet" data-compact="${esc(row.compact_prompt || '/compact')}" onclick="copyText(this.dataset.compact, 'Compact prompt copied')">Copy compact prompt</button>
    </div>
    <p class="receipt-note">${esc(row.action.reason)}</p>
  </div>`).join('')}</div>`;
}
function renderCoverage(rows) {
  if (!rows.length) return '<div class="empty">Coverage could not be determined on this machine.</div>';
  return rows.map(row => `<div class="coverage-card">
    <div class="coverage-head">
      <h3>${esc(row.label)}</h3>
      <span class="coverage-status ${esc(row.status)}">${esc(row.status_label)}</span>
    </div>
    <div class="coverage-detail">
      <div><strong>Gate:</strong> ${esc(row.automatic_gate)}</div>
      <div><strong>History:</strong> ${esc(row.history)}</div>
      <div><strong>Sessions:</strong> ${esc(row.session_count)}</div>
      <div><strong>Next:</strong> ${esc(row.action)}</div>
      <div>${esc(row.detail)}</div>
    </div>
  </div>`).join('');
}
function renderSetup(rows) {
  if (!rows.length) return '<div class="empty">Setup checklist unavailable.</div>';
  return rows.map((row, index) => `<div class="coverage-card">
    <div class="coverage-head">
      <h3>${index + 1}. ${esc(row.title)}</h3>
      <span class="coverage-status ${esc(row.status)}">${esc(row.status)}</span>
    </div>
    <div class="coverage-detail">
      <div>${esc(row.why)}</div>
      <code>${esc(row.command)}</code>
      <button class="btn-quiet" data-command="${esc(row.command)}" onclick="copyText(this.dataset.command, 'Command copied')">Copy command</button>
    </div>
  </div>`).join('');
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
      <button data-testid="outcome-useful" class="outcome-button useful ${s.outcome === 'useful' ? 'selected' : ''}" onclick="markOutcome('${esc(s.session_id)}','useful')">${s.outcome === 'useful' ? '✓ ' : ''}Useful</button>
      <button data-testid="outcome-rework" class="outcome-button rework ${s.outcome === 'rework' ? 'selected' : ''}" onclick="markOutcome('${esc(s.session_id)}','rework')">${s.outcome === 'rework' ? '✓ ' : ''}Needs rework</button>
      <button data-testid="outcome-abandoned" class="outcome-button abandoned ${s.outcome === 'abandoned' ? 'selected' : ''}" onclick="markOutcome('${esc(s.session_id)}','abandoned')">${s.outcome === 'abandoned' ? '✓ ' : ''}Abandoned</button>
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
function renderTodayDigest(digest) {
  if (!digest) return '<div class="empty">Not enough local history yet to build a weekly digest.</div>';
  const o = digest.outcomes;
  const tally = [
    o.useful ? `${o.useful} useful` : '',
    o.rework ? `${o.rework} rework` : '',
    o.abandoned ? `${o.abandoned} abandoned` : '',
  ].filter(Boolean).join(', ');
  const survival = digest.survival && digest.survival.available
    ? `<span class="pill">${esc(digest.survival.cost_per_surviving_line_label)} per surviving line &middot; ${esc(digest.survival.survival_pct)}% still standing</span>`
    : '';
  return `<div class="insight"><strong>${esc(digest.recommendation)}</strong></div>
    <div class="pill-row">
      ${tally ? `<span class="pill">${esc(tally)}</span>` : ''}
      ${digest.command_gate.commands_blocked ? `<span class="pill">${esc(digest.command_gate.commands_blocked)} dangerous commands blocked</span>` : ''}
      ${digest.prompt_gate.modified ? `<span class="pill">${esc(digest.prompt_gate.modified)} risky prompts modified</span>` : ''}
      ${survival}
    </div>`;
}
function renderReport(report) {
  const digest = report.digest;
  if (!digest) {
    return `<p>${esc(report.title)}</p>
      <div class="pill-row">${report.summary.map(item => `<span class="pill">${esc(item)}</span>`).join('')}</div>
      ${report.highlights.map(item => `<div class="insight"><strong>${esc(item)}</strong></div>`).join('')}
      <p>${esc(report.next_checks.join(' '))}</p>`;
  }
  const sections = [];
  const o = digest.outcomes;
  const outcomeTotal = o.useful + o.rework + o.abandoned + o.inferred_useful + o.inferred_churned;
  if (outcomeTotal > 0) {
    sections.push(`<div class="detail-section">
      <h2>Outcomes</h2>
      <div class="mini-grid">
        ${o.useful ? `<div class="mini"><span class="label">Useful</span><strong>${esc(o.useful)}</strong></div>` : ''}
        ${o.rework ? `<div class="mini"><span class="label">Rework</span><strong>${esc(o.rework)}</strong></div>` : ''}
        ${o.abandoned ? `<div class="mini"><span class="label">Abandoned</span><strong>${esc(o.abandoned)}</strong></div>` : ''}
        ${o.inferred_useful ? `<div class="mini"><span class="label">Inferred useful</span><strong>${esc(o.inferred_useful)}</strong></div>` : ''}
        ${o.inferred_churned ? `<div class="mini"><span class="label">Inferred churned</span><strong>${esc(o.inferred_churned)}</strong></div>` : ''}
      </div>
    </div>`);
  }
  if (digest.highest_cost_useful_session) {
    const h = digest.highest_cost_useful_session;
    sections.push(`<div class="detail-section">
      <h2>Highest-cost useful session</h2>
      <p>${esc(h.project)} &middot; ${esc(h.tool)} &middot; ${esc(h.model)} &mdash; <span class="mono">${esc(h.api_value_label)}</span></p>
    </div>`);
  }
  if (digest.top_sessions && digest.top_sessions.length) {
    sections.push(`<div class="detail-section">
      <h2>Costliest sessions</h2>
      ${digest.top_sessions.map(s => `<div class="digest-row${s.session_id ? ' clickable' : ''}" ${s.session_id ? `onclick="selectSession('${esc(s.session_id)}')"` : ''}>
        <span class="digest-row-label">${esc(s.project)} &middot; ${esc(s.tool)} &middot; ${esc(s.model)}</span>
        <span class="mono">${esc(s.api_value_label)}</span>
        ${s.outcome ? `<span class="outcome-pill ${esc(s.outcome)}">${esc(s.outcome)}</span>` : '<span class="pill">unreviewed</span>'}
      </div>`).join('')}
    </div>`);
  }
  const candidates = [
    ...digest.loop_candidates.map(c => ({ ...c, kind: 'Loop' })),
    ...digest.velocity_candidates.map(c => ({ ...c, kind: 'Runaway pace' })),
  ];
  if (candidates.length) {
    sections.push(`<div class="detail-section">
      <h2>Loop &amp; runaway signals</h2>
      ${candidates.map(c => `<div class="insight"><strong>${esc(c.kind)} &middot; ${esc(c.project)} (${esc(c.tool)})</strong><p>${esc(c.diagnosis || c.ratio_label)}</p></div>`).join('')}
    </div>`);
  }
  if (digest.command_gate.gates_fired > 0 || digest.prompt_gate.flagged > 0) {
    sections.push(`<div class="detail-section">
      <h2>Guardrails this window</h2>
      <div class="pill-row">
        ${digest.command_gate.gates_fired ? `<span class="pill">${esc(digest.command_gate.commands_blocked)} of ${esc(digest.command_gate.gates_fired)} dangerous commands blocked</span>` : ''}
        ${digest.prompt_gate.flagged ? `<span class="pill">${esc(digest.prompt_gate.modified)} of ${esc(digest.prompt_gate.flagged)} risky prompts modified</span>` : ''}
      </div>
    </div>`);
  }
  if (digest.survival && digest.survival.available) {
    const s = digest.survival;
    sections.push(`<div class="detail-section">
      <h2>Cost per surviving line</h2>
      <div class="mini-grid">
        <div class="mini"><span class="label">Still standing</span><strong>${esc(s.survival_pct)}%</strong></div>
        <div class="mini"><span class="label">$/line written</span><strong>${esc(s.cost_per_line_label)}</strong></div>
        <div class="mini"><span class="label">$/surviving line</span><strong>${esc(s.cost_per_surviving_line_label)}</strong></div>
        <div class="mini"><span class="label">Changes measured</span><strong>${esc(s.changes_measured)}</strong></div>
      </div>
      <p class="receipt-note">${esc(s.lines_intact)} of ${esc(s.lines_touched)} lines still in the code, across
        ${esc(s.cost_coverage_pct)}% of spend in the last ${esc(s.window_days)} days.
        ${s.changes_too_recent ? `${esc(s.changes_too_recent)} newer change(s) worth ${esc(s.too_recent_label)} are not old enough to judge yet.` : ''}
        A floor, not a verdict: reformatting and refactoring move attribution away from the original change.</p>
    </div>`);
  }
  return `<div class="verdict-card"><h3>${esc(digest.recommendation)}</h3></div>
    <div class="pill-row" style="margin-top:14px">${report.summary.map(item => `<span class="pill">${esc(item)}</span>`).join('')}</div>
    ${sections.join('')}
    <p class="receipt-note">API-equivalent value, not invoice spend. Outcomes are inferred from local signals, not guaranteed truth. Based on local logs only, not live provider quota.</p>`;
}
function renderInsightHeadline(totals) {
  const split = totals.replayed_tokens_label && totals.replayed_share_pct
    ? `<span class="pill">${esc(totals.new_tokens_label)} new &middot; ${esc(totals.replayed_tokens_label)} replayed (${esc(totals.replayed_share_pct)}%)</span>`
    : `<span class="pill">${esc(totals.tokens_label)} tokens</span>`;
  return `<div class="headline">
    <span class="headline-figure">${esc(totals.api_value_label)}</span>
    <span class="headline-sub">${esc(totals.window_label)} &middot; ${esc(totals.sessions)} sessions</span>
    ${split}
  </div>`;
}
function renderInsightFeed(insights) {
  if (!insights || !insights.length) {
    return '<div class="empty">No notable local signals yet. Keep using AI tools and check back after a few sessions.</div>';
  }
  return insights.map(card => `<div class="feed-row ${esc(card.severity || 'info')}${card.session_id ? ' clickable' : ''}"
      ${card.session_id ? `onclick="selectSession('${esc(card.session_id)}')"` : ''}>
      <div class="feed-main">
        <strong>${esc(card.title)}</strong>
        <p>${esc(card.body)}</p>
      </div>
      ${card.impact_label ? `<span class="feed-impact mono">${esc(card.impact_label)}</span>` : ''}
    </div>`).join('');
}
function showView(view) {
  document.querySelectorAll('.view').forEach(node => {
    node.hidden = node.id !== `view-${view}`;
  });
  document.querySelectorAll('.nav-tab').forEach(node => {
    node.classList.toggle('active', node.dataset.view === view);
  });
}
let sessionSearchTimer = null;
function debounceSessionSearch() {
  clearTimeout(sessionSearchTimer);
  sessionSearchTimer = setTimeout(loadSessions, 250);
}
function clearSessionFilters() {
  document.getElementById('sessionSearch').value = '';
  document.getElementById('sessionOutcomeFilter').value = '';
  document.getElementById('sessionEvidenceFilter').value = '';
  loadSessions();
}
let sessionSearchToken = 0;
async function loadSessions() {
  const days = document.getElementById('days').value;
  const search = document.getElementById('sessionSearch').value.trim();
  const outcome = document.getElementById('sessionOutcomeFilter').value;
  const evidence = document.getElementById('sessionEvidenceFilter').value;
  const params = new URLSearchParams({ days });
  if (search) params.set('search', search);
  if (outcome) params.set('outcome', outcome);
  if (evidence) params.set('evidence', evidence);
  // A search that doesn't field-match every session in the window falls back
  // to an uncached per-session git evidence lookup (filter_sessions()'s rough
  // topic match) -- that can take several seconds, so show a visible pending
  // state, and drop this response if a newer search has since been fired.
  const token = ++sessionSearchToken;
  document.getElementById('sessionResultsNote').textContent = 'Searching local sessions...';
  const res = await fetch(`/api/sessions?${params.toString()}`);
  const data = await res.json();
  if (token !== sessionSearchToken) return;
  const filtered = Boolean(search || outcome || evidence);
  document.getElementById('sessionResultsNote').textContent = filtered
    ? `${data.total_matched} matching session${data.total_matched === 1 ? '' : 's'} of ${data.total_scanned} in this window.`
    : `${data.total_scanned} session${data.total_scanned === 1 ? '' : 's'} in this window.`;
  document.getElementById('sessionRows').innerHTML = data.sessions.length
    ? data.sessions.map(s => `<tr class="clickable" onclick="selectSession('${esc(s.session_id)}')">
        <td>${esc(s.tool)}</td>
        <td>${esc(s.project)}<br>${s.outcome ? outcomePill(s.outcome) : outcomeEvidencePill(s)}</td>
        <td>${esc(s.model)}</td>
        <td class="mono">${esc(s.tokens)}</td>
        <td><button class="row-action">Review</button></td>
      </tr>`).join('')
    : `<tr><td colspan="5"><div class="empty">${filtered
        ? 'No sessions match those filters. Try clearing the search or a different outcome/evidence filter.'
        : 'No local sessions found for this window.'}</div></td></tr>`;
}
async function load(resetDetail = true) {
  const days = document.getElementById('days').value;
  // /api/journal is still served for the CLI's `aiwatcher journal`, but the
  // dashboard no longer renders it: every line it produced was a restatement
  // of something already in the insight feed.
  const [summaryRes, reportRes] = await Promise.all([
    fetch(`/api/summary?days=${days}`),
    fetch(`/api/report?days=${days}`)
  ]);
  const data = await summaryRes.json();
  const report = await reportRes.json();
  renderHandoffBubble(data.handoff_bubble || null);
  document.getElementById('todayDigest').innerHTML = renderTodayDigest(report.digest);
  const totals = data.totals;
  document.getElementById('apiValue').textContent = totals.api_value_label;
  document.getElementById('windowLabel').textContent = totals.window_label;
  document.getElementById('sessions').textContent = totals.sessions;
  document.getElementById('usefulOutcomes').textContent = totals.useful_outcomes;
  document.getElementById('costPerUseful').textContent = `${totals.cost_per_useful_change}${totals.inferred_useful_outcomes ? ` · ${totals.inferred_useful_outcomes} to confirm` : ''}`;
  const survival = data.survival || {};
  const survivalRow = document.getElementById('costPerSurvivingRow');
  if (survival.available) {
    survivalRow.hidden = false;
    document.getElementById('costPerSurviving').textContent =
      `${survival.cost_per_surviving_line_label} per surviving line (${survival.survival_pct}% of ${survival.lines_touched} lines still standing)`;
  } else {
    survivalRow.hidden = true;
  }
  document.getElementById('preflightDecisions').textContent = totals.preflight_decisions;
  receiptCache = data.intervention_receipts || [];
  const handoffDecisions = data.handoff_decisions || [];
  document.getElementById('latestIntervention').innerHTML = renderLatestReceipt(receiptCache[0]);
  document.getElementById('latestHandoffDecision').innerHTML = renderLatestHandoffDecision(handoffDecisions);
  document.getElementById('receiptRows').innerHTML = renderReceiptRows(receiptCache);
  document.getElementById('handoffDecisionRows').innerHTML = renderHandoffDecisionRows(handoffDecisions);
  document.getElementById('contextHealth').innerHTML = renderContextHealth(data.context_health || []);
  document.getElementById('unbanked').innerHTML = renderUnbanked(data.unbanked);
  const changeRows = data.changes || [];
  document.getElementById('changeRows').innerHTML = renderChangeRows(changeRows);
  document.getElementById('changeTotals').innerHTML = renderChangeTotals(changeRows, data.changes_meta);
  document.getElementById('coverageRows').innerHTML = renderCoverage(data.coverage || []);
  document.getElementById('setupRows').innerHTML = renderSetup(data.setup || []);
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
  document.getElementById('privacy').innerHTML = data.privacy.map(p => `<div class="privacy-item"><span class="privacy-check">&#10003;</span><span>${esc(p)}</span></div>`).join('');
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
  loadSessions();
  document.getElementById('report').innerHTML = renderReport(report);
  document.getElementById('insightHeadline').innerHTML = renderInsightHeadline(data.totals);
  document.getElementById('insightFeed').innerHTML = renderInsightFeed(data.insights);
  if (resetDetail && document.getElementById('detailDrawer').classList.contains('open')) closeDrawer();
}
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeDrawer(); });
(async () => {
  await load();
  // Deep link from `aiwatcher watch --notify` (issue #31): ?session=<id>
  // opens straight to that session's review instead of the overview.
  const deepLinkSession = new URLSearchParams(location.search).get('session');
  if (deepLinkSession) selectSession(deepLinkSession);
})();
</script>
</body>
</html>
"""


OVERLAY_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AIWatcher Handoff</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: transparent;
      color: #edf6ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: end center;
      padding: 18px;
      background:
        radial-gradient(circle at 12% 0%, rgba(79, 209, 197, 0.16), transparent 34%),
        rgba(4, 9, 18, 0.78);
    }
    .bubble {
      width: min(780px, 100%);
      border: 1px solid rgba(126, 172, 255, 0.45);
      background: rgba(16, 25, 40, 0.96);
      box-shadow: 0 22px 70px rgba(0, 0, 0, 0.36);
      border-radius: 18px;
      overflow: hidden;
    }
    .top {
      padding: 20px 22px 16px;
      border-bottom: 1px solid rgba(126, 172, 255, 0.22);
      display: flex;
      justify-content: space-between;
      gap: 16px;
    }
    h1 { margin: 0 0 8px; font-size: clamp(22px, 4vw, 30px); letter-spacing: 0; }
    p { margin: 0; color: #a9b6c8; line-height: 1.45; }
    .badge {
      align-self: flex-start;
      border: 1px solid rgba(255, 119, 150, 0.52);
      color: #ff9bad;
      padding: 8px 12px;
      border-radius: 999px;
      white-space: nowrap;
      font-weight: 800;
    }
    .body { padding: 16px 22px 18px; }
    .tags { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 14px; }
    .tag {
      border: 1px solid rgba(126, 172, 255, 0.24);
      background: rgba(255, 255, 255, 0.04);
      color: #d9e5f7;
      padding: 7px 10px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 13px;
    }
    .actions {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 16px;
    }
    button, a {
      border: 1px solid rgba(126, 172, 255, 0.3);
      border-radius: 12px;
      color: #edf6ff;
      background: rgba(15, 23, 42, 0.92);
      padding: 12px 14px;
      min-height: 46px;
      font-size: 15px;
      font-weight: 850;
      text-align: center;
      text-decoration: none;
      cursor: pointer;
    }
    .primary {
      border: 0;
      background: linear-gradient(135deg, #44d7b6, #68a8ff);
      color: #06111f;
    }
    .foot {
      padding: 0 22px 18px;
      color: #7f8da3;
      font-size: 13px;
    }
    .empty { padding: 24px; color: #a9b6c8; }
    @media (max-width: 660px) {
      body { padding: 10px; }
      .top { display: block; }
      .badge { display: inline-block; margin-top: 12px; }
      .actions { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
  <main class="bubble" id="bubble">
    <div class="empty">Loading AIWatcher handoff recommendation...</div>
  </main>
<script>
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}
function queryParam(name) {
  return new URLSearchParams(window.location.search).get(name);
}
async function copyText(value, label = 'Copied') {
  try {
    await navigator.clipboard.writeText(value || '');
    renderSaved(label);
  } catch (error) {
    renderSaved('Copy failed. Open dashboard and copy from the handoff drawer.');
  }
}
async function recordDecision(decision, bubble) {
  if (!bubble || !bubble.session_id) return;
  try {
    await fetch('/api/handoff-decision', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: bubble.session_id,
        decision,
        reason: bubble.reason || bubble.body || '',
        expected_saved_context_tokens: bubble.expected_saved_context_tokens || null,
      })
    });
  } catch (error) {}
}
function renderSaved(message) {
  document.getElementById('bubble').innerHTML = `<div class="top"><div><h1>${esc(message)}</h1><p>You can close this AIWatcher companion and return to your AI tool.</p></div><span class="badge">saved</span></div>
    <div class="body"><div class="actions"><button class="primary" onclick="window.close()">Close</button><a href="/">Open dashboard</a></div></div>`;
}
async function copyHandoff(bubble, decision) {
  await recordDecision(decision, bubble);
  const res = await fetch(`/api/handoff?id=${encodeURIComponent(bubble.session_id)}&target=generic&prompt=0`);
  const capsule = await res.json();
  if (capsule.error) {
    renderSaved(capsule.error);
    return;
  }
  await copyText(capsule.next_brief || '', decision === 'new_chat' ? 'Fresh-session handoff copied' : 'Handoff brief copied');
}
async function continueHere(bubble) {
  await recordDecision('continue_here', bubble);
  renderSaved('Decision saved: continue here');
}
function renderBubble(bubble) {
  const tags = (bubble.tags || []).map(tag => `<span class="tag">${esc(tag)}</span>`).join('');
  document.getElementById('bubble').innerHTML = `<div class="top">
    <div><h1>${esc(bubble.title || 'Start a fresh AI session')}</h1><p>${esc(bubble.body || 'AIWatcher found context pressure that may waste your next turns.')}</p></div>
    <span class="badge">${esc(bubble.severity || 'warning')}</span>
  </div>
  <div class="body">
    <div class="tags">${tags}</div>
    <p>${esc(bubble.reason || 'Use a handoff brief to preserve the outcome without carrying the full chat history.')}</p>
    <div class="actions">
      <button class="primary" id="newChat">New chat</button>
      <button id="copyBrief">Copy handoff</button>
      <button id="continueHere">Continue here</button>
      <a href="/?session=${encodeURIComponent(bubble.session_id || '')}">Inspect</a>
    </div>
  </div>
  <div class="foot">Local-only. Prompt/source content is not stored in this decision.</div>`;
  document.getElementById('newChat').onclick = () => copyHandoff(bubble, 'new_chat');
  document.getElementById('copyBrief').onclick = () => copyHandoff(bubble, 'copy_handoff');
  document.getElementById('continueHere').onclick = () => continueHere(bubble);
}
async function load() {
  const wanted = queryParam('session');
  const res = await fetch('/api/summary?days=7');
  const data = await res.json();
  let bubble = data.handoff_bubble;
  if (wanted && (!bubble || bubble.session_id !== wanted)) {
    const health = (data.context_health || []).find(row => row.session_id === wanted);
    if (health) {
      const saved = health.estimated_replayed_context_label || 'context';
      bubble = {
        session_id: health.session_id,
        project: health.project,
        tool: health.tool,
        severity: health.severity,
        title: `Start a new chat to save ~${saved} tokens of context`,
        body: health.recommendation || 'This session is getting heavy. Use a handoff brief before continuing.',
        reason: health.recommendation || 'Context pressure is elevated.',
        expected_saved_context_tokens: health.estimated_replayed_context_tokens || null,
        tags: [`${health.latest_turn_tokens} tokens/turn`, `${saved} replayed`].concat(
          health.bloat_measurable ? [`${health.bloat_label} of spend replayed`] : []),
      };
    }
  }
  if (!bubble) {
    document.getElementById('bubble').innerHTML = `<div class="top"><div><h1>No handoff needed right now</h1><p>AIWatcher did not find warning or critical context pressure in the current local window.</p></div><span class="badge">healthy</span></div>
      <div class="body"><div class="actions"><a class="primary" href="/">Open dashboard</a><button onclick="window.close()">Close</button></div></div>`;
    return;
  }
  renderBubble(bubble);
}
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
        if parsed.path == "/overlay":
            self._send(200, OVERLAY_HTML, "text/html; charset=utf-8")
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
        if parsed.path == "/api/sessions":
            params = parse_qs(parsed.query)
            try:
                days = max(1, min(90, int(params.get("days", ["30"])[0])))
            except ValueError:
                days = 30
            search = params.get("search", [""])[0].strip() or None
            outcome = params.get("outcome", [""])[0].strip() or None
            if outcome not in VALID_OUTCOMES:
                outcome = None
            evidence = params.get("evidence", [""])[0].strip() or None
            if evidence not in VALID_EVIDENCE_OUTCOMES:
                evidence = None
            self._send(
                200,
                json.dumps(build_session_search(days, search=search, outcome=outcome, evidence=evidence)),
                "application/json; charset=utf-8",
            )
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
                        "bloat_measurable": match.bloat_measurable,
                        "replayed_cost_usd": round(match.replayed_cost_usd, 6),
                        "analyzed_cost_usd": round(match.analyzed_cost_usd, 6),
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
        if parsed.path not in {"/api/outcome", "/api/preflight", "/api/handoff-decision"}:
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
        if parsed.path == "/api/handoff-decision":
            session_id = str(payload.get("session_id", "")).strip()
            decision = str(payload.get("decision", "")).strip()
            reason = str(payload.get("reason", "")).strip()
            expected = payload.get("expected_saved_context_tokens")
            if not session_id:
                self._send(400, json.dumps({"error": "session_id is required"}), "application/json; charset=utf-8")
                return
            try:
                record = record_handoff_decision(
                    session_id=session_id,
                    decision=decision,
                    reason=reason,
                    expected_saved_context_tokens=expected if isinstance(expected, int) else None,
                )
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}), "application/json; charset=utf-8")
                return
            except OSError as exc:
                self._send(
                    500,
                    json.dumps({"error": f"Could not save handoff decision: {exc}"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send(200, json.dumps(record), "application/json; charset=utf-8")
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
    record_ui_server(host, selected_port)
    print(f"AIWatcher Local UI running at http://{host}:{selected_port}")
    print("Local-only. No data leaves this machine. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped AIWatcher Local UI.")
    finally:
        server.server_close()
