"""Local-only dashboard for AIWatcher Local."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
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
from .correlate import link_recent_fresh_start_receipts_to_sessions, link_recent_interventions_to_sessions
from .evidence_capture import record_missing_evidence_snapshots_from_evidence
from .handoff import HANDOFF_TYPE_LABELS, TARGET_LABELS, build_handoff_capsule
from .metrics import model_cost_comparison, pace_vs_baseline, replayed_context_cost
from .local_state import (
    COMMAND_GATE_BLOCKED_DECISIONS,
    MAX_COMMAND_DECISIONS_STORED,
    PROMPT_MODIFIED_DECISIONS,
    VALID_OUTCOMES,
    active_prompt_gate,
    evidence_snapshots_for_sessions,
    get_outcome,
    get_watcher_status,
    outcome_counts,
    outcomes_for_sessions,
    recent_command_decisions,
    recent_handoff_decisions,
    recent_interventions,
    get_ambient_intervention,
    record_ambient_intervention_action,
    record_handoff_decision,
    record_evidence_snapshot,
    record_outcome,
    record_ui_server,
    state_path,
)
from .outcome_evidence import VALID_EVIDENCE_OUTCOMES, build_outcome_evidence, evidence_for_sessions
from .ledger import Ledger, build_ledger, unbanked_summary
from .pricing import is_subscription_model
from .runtime_attachment import (
    RuntimeAttachment,
    perform_runtime_return,
    runtime_attachment_for_session,
    safe_runtime_processes,
)
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
SUMMARY_MEMORY_TTL_SECONDS = 45
SUMMARY_DISK_TTL_SECONDS = 6 * 60 * 60
# Bump whenever build_summary's payload shape changes, so a cache written by an
# older build is discarded instead of rendering blank sections in a newer UI.
SUMMARY_CACHE_SCHEMA_VERSION = 4
SESSION_SNAPSHOT_SCHEMA_VERSION = 1
SUMMARY_BACKGROUND_COOLDOWN_SECONDS = 8
SUMMARY_WINDOWS = (1, 7, 30)
ACTIVE_SESSION_MINUTES = 30
RECENT_SESSION_HOURS = 4
UNATTRIBUTED_PROJECT = "__unattributed__"
UNATTRIBUTED_PROJECT_LABEL = "Unattributed sessions"

_SUMMARY_CACHE: dict[int, tuple[float, dict[str, object]]] = {}
_SUMMARY_REFRESHING: set[int] = set()
_SUMMARY_REFRESHED_AT: dict[int, float] = {}
_SUMMARY_CACHE_LOCK = threading.RLock()
_SESSION_INDEX: dict[str, LocalSession] = {}
_EVENT_INDEX: dict[str, list[LocalEvent]] = {}
_EVENT_INDEX_READY = False
_SUMMARY_REFRESH_ERROR: str | None = None


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


def _usage_summary(row: LocalSession) -> dict[str, object]:
    tokens = row.tokens_in + row.tokens_out
    calls = max(0, int(row.agent_calls))
    return {
        "tokens": tokens,
        "tokens_label": compact_int(tokens),
        "model_calls": row.agent_calls,
        "tool_calls": row.tool_calls,
        "api_value_usd": round(row.cost_usd, 6),
        "api_value_label": money(row.cost_usd),
        "tokens_per_model_call_label": compact_int(round(tokens / calls)) if calls else "not measured",
        "cost_per_model_call_label": money(row.cost_usd / calls) if calls else "not measured",
    }


def short_path(path: str | None, max_len: int = 54) -> str:
    if not path:
        return "unknown"
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


def _is_inside_path(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def is_reliable_project_path(path: str | None) -> bool:
    if not path or path == "unknown" or path == UNATTRIBUTED_PROJECT:
        return False
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        resolved = Path(path).expanduser()
    temp_dirs = {
        Path("/tmp").resolve(),
        Path("/private/tmp").resolve(),
        Path(tempfile.gettempdir()).resolve(),
    }
    common_non_projects = {
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home() / "Downloads",
        *temp_dirs,
    }
    if resolved in common_non_projects:
        return False
    parts = set(resolved.parts)
    if ".claude" in parts or ".codex" in parts or ".cursor" in parts:
        return False
    if any(part.startswith("claude-") for part in resolved.parts) and any(_is_inside_path(resolved, temp_dir) for temp_dir in temp_dirs):
        return False
    return resolved.parent != resolved


def project_key(path: str | None) -> str:
    return path if is_reliable_project_path(path) else UNATTRIBUTED_PROJECT


def project_label(path: str | None, max_len: int = 54) -> str:
    if not is_reliable_project_path(path):
        return UNATTRIBUTED_PROJECT_LABEL
    return short_path(path, max_len)


def in_window(session: LocalSession, since: datetime) -> bool:
    stamp = session.updated_at or session.started_at
    return bool(stamp and stamp.astimezone() >= since)


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _session_from_json(raw: object) -> LocalSession | None:
    if not isinstance(raw, dict):
        return None
    session_id = raw.get("session_id")
    tool = raw.get("tool")
    if not isinstance(session_id, str) or not session_id or not isinstance(tool, str) or not tool:
        return None
    notes = raw.get("notes")
    model_breakdown = raw.get("model_breakdown")
    return LocalSession(
        session_id=session_id,
        tool=tool,
        project_path=raw.get("project_path") if isinstance(raw.get("project_path"), str) else None,
        started_at=_parse_dt(raw.get("started_at")),
        updated_at=_parse_dt(raw.get("updated_at")),
        model=raw.get("model") if isinstance(raw.get("model"), str) else None,
        tokens_in=int(raw.get("tokens_in") or 0),
        tokens_out=int(raw.get("tokens_out") or 0),
        cache_read_tokens=int(raw.get("cache_read_tokens") or 0),
        cache_write_tokens=int(raw.get("cache_write_tokens") or 0),
        cost_usd=float(raw.get("cost_usd") or 0.0),
        agent_calls=int(raw.get("agent_calls") or 0),
        tool_calls=int(raw.get("tool_calls") or 0),
        source_path=raw.get("source_path") if isinstance(raw.get("source_path"), str) else None,
        notes=[str(item) for item in notes] if isinstance(notes, list) else [],
        surface=raw.get("surface") if isinstance(raw.get("surface"), str) else None,
        model_breakdown=model_breakdown if isinstance(model_breakdown, dict) else {},
    )


def _index_sessions(rows: list[LocalSession]) -> None:
    with _SUMMARY_CACHE_LOCK:
        for row in rows:
            _SESSION_INDEX[row.session_id] = row


def _index_events(rows: list[LocalEvent], *, complete: bool = False) -> None:
    global _EVENT_INDEX_READY
    grouped: dict[str, list[LocalEvent]] = defaultdict(list)
    for row in rows:
        grouped[row.session_id].append(row)
    with _SUMMARY_CACHE_LOCK:
        _EVENT_INDEX.clear()
        _EVENT_INDEX.update(grouped)
        if complete:
            _EVENT_INDEX_READY = True


def _cached_events_for_session(session_id: str) -> list[LocalEvent] | None:
    with _SUMMARY_CACHE_LOCK:
        if not _EVENT_INDEX_READY:
            return None
        return list(_EVENT_INDEX.get(session_id, []))


def _session_index_payload(rows: list[LocalSession]) -> list[dict[str, object]]:
    return [row.to_json() for row in rows]


def _index_sessions_from_summary(summary: dict[str, object]) -> None:
    raw_rows = summary.get("_session_index")
    if not isinstance(raw_rows, list):
        return
    rows = [row for raw in raw_rows if (row := _session_from_json(raw)) is not None]
    if rows:
        _index_sessions(rows)


def _find_session_row(session_id: str, *, days: int = 30) -> LocalSession | None:
    with _SUMMARY_CACHE_LOCK:
        row = _SESSION_INDEX.get(session_id)
        if row:
            return row
        summaries = [cached[1] for cached in _SUMMARY_CACHE.values()]
    for summary in summaries:
        _index_sessions_from_summary(summary)
    with _SUMMARY_CACHE_LOCK:
        row = _SESSION_INDEX.get(session_id)
        if row:
            return row
    for candidate_days in (days, 1, 7, 30, 90):
        disk = _read_summary_disk_cache(candidate_days, max_age_seconds=SUMMARY_DISK_TTL_SECONDS)
        if disk:
            _index_sessions_from_summary(disk)
            with _SUMMARY_CACHE_LOCK:
                row = _SESSION_INDEX.get(session_id)
                if row:
                    return row
    for row in rows_for_window(days):
        if row.session_id == session_id:
            _index_sessions([row])
            return row
    return None


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


def _project_health(items: list[LocalSession]) -> dict[str, object]:
    stats = summarize(items)
    tokens = int(stats["tokens"])
    calls = int(stats["calls"])
    tool_calls = int(stats["tool_calls"])
    api_value = float(stats["api_value_usd"])
    plan_limited = token_split(items)["plan_limited"]
    sessions = int(stats["sessions"])
    if sessions <= 0:
        return {
            "status": "limited",
            "label": "Limited data",
            "tone": "limited",
            "reason": "No recent local sessions were found.",
            "action_label": "Review",
        }
    if tool_calls >= 1_000 or calls >= 1_000 or tokens >= 50_000_000:
        return {
            "status": "critical",
            "label": "Critical",
            "tone": "critical",
            "reason": "Heavy context or tool-call pressure. Review before continuing broad work.",
            "action_label": "Review",
        }
    if api_value >= 10 or tool_calls >= 250 or calls >= 250 or tokens >= 1_000_000:
        return {
            "status": "review",
            "label": "Review",
            "tone": "warning",
            "reason": "High usage for this window. Check whether the latest sessions produced useful outcomes.",
            "action_label": "Review",
        }
    if plan_limited >= 1_000_000:
        return {
            "status": "review",
            "label": "Review",
            "tone": "warning",
            "reason": "Plan-limited tokens are accumulating. Watch for quota pressure even when API value is low.",
            "action_label": "Review",
        }
    return {
        "status": "healthy",
        "label": "Healthy",
        "tone": "healthy",
        "reason": "No unusual local cost or context pressure in this window.",
        "action_label": "Review",
    }


def group_projects(rows: list[LocalSession]) -> list[dict[str, object]]:
    grouped: dict[str, list[LocalSession]] = defaultdict(list)
    for row in rows:
        grouped[project_key(row.project_path)].append(row)
    result = []
    for key, items in grouped.items():
        stats = summarize(items)
        attributed = key != UNATTRIBUTED_PROJECT
        name = key if attributed else UNATTRIBUTED_PROJECT_LABEL
        result.append({
            "name": name,
            "id": key,
            "short_name": project_label(key),
            "attributed": attributed,
            "sessions": stats["sessions"],
            "tokens": stats["tokens"],
            "tokens_label": compact_int(int(stats["tokens"])),
            "api_value_usd": round(float(stats["api_value_usd"]), 6),
            "api_value_label": money(float(stats["api_value_usd"])),
            "calls": stats["calls"],
            "tool_calls": stats["tool_calls"],
            "health": _project_health(items),
        })
    result.sort(key=lambda item: (
        0 if item.get("attributed") else 1,
        -float(item["api_value_usd"]),
        -int(item["tokens"]),
    ))
    return result


def session_state(row: LocalSession, *, now: datetime | None = None) -> dict[str, object]:
    now = now or datetime.now(timezone.utc)
    stamp = row.updated_at or row.started_at
    if not stamp:
        return {
            "status": "unknown",
            "label": "Unknown",
            "tone": "limited",
            "reason": "This tool did not expose a reliable session timestamp.",
        }
    stamp = stamp.astimezone(timezone.utc)
    age_seconds = max(0.0, (now - stamp).total_seconds())
    if age_seconds <= ACTIVE_SESSION_MINUTES * 60:
        return {
            "status": "active",
            "label": "Active log",
            "tone": "healthy",
            "reason": "This local session log was updated recently. Exact chat return still requires a live runtime attachment.",
            "age_seconds": round(age_seconds, 1),
        }
    if age_seconds <= RECENT_SESSION_HOURS * 3600:
        return {
            "status": "recent",
            "label": "Recent log",
            "tone": "warning",
            "reason": "Recently updated log. AIWatcher has not confirmed a live tool process or exact chat handle.",
            "age_seconds": round(age_seconds, 1),
        }
    if age_seconds >= 7 * 24 * 3600:
        return {
            "status": "stale",
            "label": "Stale log",
            "tone": "limited",
            "reason": "Old local session. Use it for evidence or Fresh Start, not live control.",
            "age_seconds": round(age_seconds, 1),
        }
    return {
        "status": "ended",
        "label": "Ended log",
        "tone": "limited",
        "reason": "No live tool connection is available for this session.",
        "age_seconds": round(age_seconds, 1),
    }


def session_actions(
    row: LocalSession,
    *,
    outcome: dict[str, object] | None = None,
    attachment: RuntimeAttachment | None = None,
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    runtime = attachment or runtime_attachment_for_session(row, state=session_state(row), processes=[])
    tokens = row.tokens_in + row.tokens_out
    needs_fresh_start = tokens >= 500_000 or row.agent_calls >= 250 or row.tool_calls >= 250
    identity_level = runtime.identity_level
    if identity_level == "exact_session":
        handoff_label = "Open Fresh Start"
        handoff_reason = "This active AI work is heavy enough that a Fresh Start brief is safer than replaying the full history."
    elif identity_level == "likely_workspace":
        handoff_label = "Open Fresh Start"
        handoff_reason = "AIWatcher can identify the workspace, but not the exact running chat. Copy a Fresh Start brief before continuing."
    else:
        handoff_label = "Inspect evidence"
        handoff_reason = "This is historical local evidence. Inspect it before using a Fresh Start brief."
    if not outcome:
        actions.append({
            "id": "review_outcome",
            "label": "Review outcome",
            "primary": not needs_fresh_start or identity_level == "historical_log",
            "reason": "Mark whether this work was useful so AIWatcher can measure value.",
        })
    if needs_fresh_start:
        actions.append({
            "id": "handoff",
            "label": handoff_label,
            "primary": identity_level != "historical_log",
            "reason": handoff_reason,
        })
    actions.append({
        "id": "optimize_next_prompt",
        "label": "Optimize next prompt",
        "primary": False,
        "reason": "Use this session's pressure signals to tighten the next ask.",
    })
    actions.append({
        "id": "open_tool",
        "label": runtime.action_label,
        "primary": False,
        "available": runtime.available,
        "reason": runtime.reason,
        "mode": runtime.mode,
        "level": runtime.level,
        "confidence": runtime.confidence,
    })
    primary_seen = False
    for action in actions:
        if not action.get("primary"):
            continue
        if primary_seen:
            action["primary"] = False
        else:
            primary_seen = True
    return actions


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


def rows_for_window(days: int, *, prefer_cache: bool = False) -> list[LocalSession]:
    """Sessions clipped to the window -- see clip_sessions_to_window for why the
    old `updated_at`-only rule overstated every total."""
    since = datetime.now().astimezone() - timedelta(days=days)
    if prefer_cache:
        cached_rows = _cached_session_rows()
        with _SUMMARY_CACHE_LOCK:
            event_index_ready = _EVENT_INDEX_READY
            cached_events = [event for events in _EVENT_INDEX.values() for event in events]
        if cached_rows:
            if event_index_ready:
                return clip_sessions_to_window(cached_rows, cached_events, since)
            # The normalized snapshot is intentionally usable before event
            # enrichment finishes. This keeps Work/Projects interactive while
            # exact window clipping catches up in the background.
            return [row for row in cached_rows if in_window(row, since)]
    try:
        events = scan_all_events(since=since)
    except OSError:
        events = []
    return clip_sessions_to_window(scan_all(since=since), events, since)


def _session_row_json(
    row: LocalSession,
    window_outcomes: dict[str, dict[str, object]],
    evidence_by_session: dict[str, object],
) -> dict[str, object]:
    state = session_state(row)
    attachment = runtime_attachment_for_session(row, state=state, processes=[])
    return {
        "tool": row.tool,
        "session_id": row.session_id,
        "project": project_label(row.project_path),
        "project_full": row.project_path if is_reliable_project_path(row.project_path) else "unknown",
        "model": display_model_name(row.model),
        "tokens": compact_int(row.tokens_in + row.tokens_out),
        "api_value": money(row.cost_usd),
        "outcome": (window_outcomes.get(row.session_id) or {}).get("outcome"),
        "inferred_outcome": evidence_by_session.get(row.session_id).inferred_outcome if evidence_by_session.get(row.session_id) else None,
        "state": state,
        "runtime_attachment": attachment.to_json(),
        "actions": session_actions(row, outcome=window_outcomes.get(row.session_id), attachment=attachment),
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
    rows = rows_for_window(days, prefer_cache=True)
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
    try:
        row = evidence_snapshots_for_sessions({session_id}).get(session_id)
    except OSError:
        return None
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
    try:
        outcome = get_outcome(row.session_id)
    except OSError:
        outcome = None
    evidence = build_outcome_evidence(row, survival=_survival_for_session(row.session_id))
    state = session_state(row)
    attachment = runtime_attachment_for_session(row, state=state, processes=safe_runtime_processes())
    return {
        "session_id": row.session_id,
        "tool": row.tool,
        "project": row.project_path if is_reliable_project_path(row.project_path) else "unknown",
        "project_short": project_label(row.project_path),
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
        "state": state,
        "runtime_attachment": attachment.to_json(),
        "actions": session_actions(row, outcome=outcome, attachment=attachment),
    }


def recent_session_json(
    row: LocalSession,
    *,
    window_outcomes: dict[str, dict[str, object]],
    evidence_by_session: dict[str, object] | None = None,
) -> dict[str, object]:
    evidence_by_session = evidence_by_session or {}
    state = session_state(row)
    attachment = runtime_attachment_for_session(row, state=state, processes=[])
    return {
        "tool": row.tool,
        "session_id": row.session_id,
        "project": project_label(row.project_path),
        "project_full": row.project_path if is_reliable_project_path(row.project_path) else "unknown",
        "model": display_model_name(row.model),
        "tokens": compact_int(row.tokens_in + row.tokens_out),
        "api_value": money(row.cost_usd),
        "outcome": (window_outcomes.get(row.session_id) or {}).get("outcome"),
        "inferred_outcome": evidence_by_session.get(row.session_id).inferred_outcome if evidence_by_session.get(row.session_id) else None,
        "updated_at": (row.updated_at or row.started_at).isoformat() if (row.updated_at or row.started_at) else None,
        "state": state,
        "runtime_attachment": attachment.to_json(),
        "actions": session_actions(row, outcome=window_outcomes.get(row.session_id), attachment=attachment),
    }


def session_summary_json(row: LocalSession) -> dict[str, object]:
    try:
        outcome = get_outcome(row.session_id)
    except OSError:
        outcome = None
    state = session_state(row)
    attachment = runtime_attachment_for_session(row, state=state, processes=safe_runtime_processes())
    return {
        "session_id": row.session_id,
        "tool": row.tool,
        "project": row.project_path if is_reliable_project_path(row.project_path) else "unknown",
        "project_short": project_label(row.project_path),
        "model": display_model_name(row.model),
        "tokens": row.tokens_in + row.tokens_out,
        "tokens_label": compact_int(row.tokens_in + row.tokens_out),
        "api_value_usd": round(row.cost_usd, 6),
        "api_value": money(row.cost_usd),
        "calls": row.agent_calls,
        "tool_calls": row.tool_calls,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "source_path": row.source_path,
        "outcome": outcome["outcome"] if outcome else None,
        "state": state,
        "runtime_attachment": attachment.to_json(),
        "actions": session_actions(row, outcome=outcome, attachment=attachment),
        "summary_only": True,
        "detail_status": "loading",
    }


def build_session_summary(session_id: str, days: int = 30) -> dict[str, object]:
    row = _find_session_row(session_id, days=days)
    if not row:
        return {"error": "session not found"}
    return session_summary_json(row)


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


def _safe_window_outcomes(session_ids: set[str]) -> tuple[dict[str, dict[str, object]], dict[str, int]]:
    try:
        return outcomes_for_sessions(session_ids), outcome_counts(session_ids)
    except OSError:
        return {}, {"useful": 0, "rework": 0, "abandoned": 0}


def build_project_detail(project: str, days: int = 7) -> dict[str, object]:
    rows = [row for row in rows_for_window(days, prefer_cache=True) if project_key(row.project_path) == project]
    stats = summarize(rows)
    sessions = sorted(rows, key=lambda row: row.updated_at or row.started_at or MIN_DT, reverse=True)
    return {
        "project": project,
        "project_short": UNATTRIBUTED_PROJECT_LABEL if project == UNATTRIBUTED_PROJECT else short_path(project, 72),
        "attributed": project != UNATTRIBUTED_PROJECT,
        "health": _project_health(rows),
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


def build_session_detail(session_id: str, days: int = 30, *, allow_pending: bool = False) -> dict[str, object]:
    row = _find_session_row(session_id, days=days)
    if not row:
        return {"error": "session not found"}
    cached_events = _cached_events_for_session(session_id)
    if cached_events is None:
        if not allow_pending:
            cached_events = [event for event in scan_all_events() if event.session_id == session_id]
        else:
            # Never rescan every transcript on the HTTP request thread. The fast
            # identity/action card remains useful while the shared summary worker
            # builds the event index once for every view.
            return {
                **session_summary_json(row),
                "detail_pending": True,
                "detail_message": "Timeline and outcome evidence are still indexing in the background.",
            }
    # A single-session view shows the whole session, not just the last `days` — otherwise
    # early turns of a long-running session are hidden. We only filter by session id here.
    events = sorted(
        cached_events,
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


def build_runtime_return(session_id: str, days: int = 30) -> dict[str, object]:
    row = _find_session_row(session_id, days=days)
    if not row:
        return {"ok": False, "error": "session not found"}
    state = session_state(row)
    attachment = runtime_attachment_for_session(row, state=state, processes=safe_runtime_processes())
    return perform_runtime_return(attachment)


def _related_active_workspaces(row: LocalSession, *, limit: int = 3) -> list[str]:
    now = datetime.now(timezone.utc)
    current = project_key(row.project_path)
    with _SUMMARY_CACHE_LOCK:
        candidates = list(_SESSION_INDEX.values())
    workspaces: list[str] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: item.updated_at or item.started_at or MIN_DT, reverse=True):
        state = session_state(candidate, now=now)
        if state.get("status") not in {"active", "recent"}:
            continue
        key = project_key(candidate.project_path)
        if key == UNATTRIBUTED_PROJECT or key == current or key in seen:
            continue
        seen.add(key)
        workspaces.append(key)
        if len(workspaces) >= limit:
            break
    return workspaces


def build_basic_handoff_detail(
    session_id: str,
    days: int = 30,
    target: str = "generic",
    handoff_type: str = "coding",
    objective: str | None = None,
    source_refs: list[str] | None = None,
    constraints: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> dict[str, object]:
    """Return a copyable Fresh Start brief without event/git enrichment.

    The full Fresh Start detail can pay for timeline parsing and evidence collection after
    first paint. This one only depends on the cached session row, so the user
    can copy a truthful continuation brief immediately.
    """
    row = _find_session_row(session_id, days=days)
    if not row:
        return {"error": "session not found"}
    target = target if target in TARGET_LABELS else "generic"
    handoff_type = handoff_type if handoff_type in HANDOFF_TYPE_LABELS else "coding"
    state = session_state(row)
    attachment = runtime_attachment_for_session(row, state=state, processes=safe_runtime_processes())
    project = row.project_path if is_reliable_project_path(row.project_path) else "unknown"
    usage = _usage_summary(row)
    warnings = [
        (
            f"Source session had {usage['tokens_label']} tokens, "
            f"{usage['model_calls']} model calls, {usage['tool_calls']} tool calls, "
            f"and {usage['api_value_label']} API-equivalent value."
        ),
        "Detailed git, timeline, and prompt evidence is still loading; inspect the repository before editing.",
    ]
    next_brief = "\n".join([
        "AIWatcher Fresh Start brief",
        "",
        "You are starting a fresh AI work session. Do not assume access to the previous chat.",
        "Continue from repository state on disk, not from hidden conversation history.",
        f"Target tool: {TARGET_LABELS[target]}.",
        f"Continuation type: {HANDOFF_TYPE_LABELS[handoff_type]}.",
        *([
            "",
            "Goal",
            f"- User objective: {objective.strip()}",
        ] if objective and objective.strip() else []),
        *([
            "",
            "Source of truth to load first",
            *[f"- {item}" for item in (source_refs or [])[:8] if item],
        ] if source_refs else []),
        *([
            "",
            "Do not lose these constraints",
            *[f"- {item}" for item in (constraints or [])[:8] if item],
        ] if constraints else []),
        *([
            "",
            "Acceptance checks",
            *[f"- {item}" for item in (acceptance_criteria or [])[:8] if item],
        ] if acceptance_criteria else []),
        "",
        "Workspace",
        f"- Project: {project}",
        f"- Source session: {session_id}",
        f"- Source tool/model: {row.tool} / {row.model or 'unknown'}",
        "",
        "Why start fresh",
        *[f"- {item}" for item in warnings],
        "",
        "Immediate next checkpoint",
        "- Run `git status --short` and inspect the files already changed in this repository.",
        "- Reconstruct what appears done, what remains uncertain, and the smallest safe next checkpoint.",
        "- Continue only after that checkpoint is clear; do not replay broad exploration from the old session.",
        "",
        "Guardrails",
        "- Preserve unrelated changes.",
        "- Do not expose secrets.",
        "- Stop before destructive changes or broad refactors.",
    ])
    return {
        "session_id": row.session_id,
        "project": project,
        "project_reliable": project != "unknown",
        "tool": row.tool,
        "model": display_model_name(row.model),
        "source_path": row.source_path,
        "target": target,
        "target_label": TARGET_LABELS[target],
        "updated_at": row.updated_at.isoformat() if row.updated_at else row.started_at.isoformat() if row.started_at else None,
        "usage": usage,
        "outcome": None,
        "evidence": {"commits": [], "changed_files": [], "tests": [], "confidence": "predicted"},
        "warnings": warnings,
        "handoff_type": handoff_type,
        "handoff_type_label": HANDOFF_TYPE_LABELS[handoff_type],
        "objective": objective.strip() if objective else None,
        "source_refs": (source_refs or [])[:8],
        "constraints": (constraints or [])[:8],
        "acceptance_criteria": (acceptance_criteria or [])[:8],
        "include_prompt_excerpt": False,
        "costliest_prompt": None,
        "decisions": [],
        "related_workspaces": [],
        "next_brief": next_brief,
        "runtime_attachment": attachment.to_json(),
        "basic": True,
        "enrichment_status": "loading",
    }


def build_handoff_detail(
    session_id: str,
    days: int = 30,
    target: str = "generic",
    include_prompt_excerpt: bool = False,
    handoff_type: str = "coding",
    objective: str | None = None,
    source_refs: list[str] | None = None,
    constraints: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> dict[str, object]:
    row = _find_session_row(session_id, days=days)
    if not row:
        return {"error": "session not found"}
    events = sorted(
        [event for event in scan_all_events() if event.session_id == session_id],
        key=lambda event: event.timestamp or MIN_DT,
    )
    try:
        outcome = get_outcome(session_id)
    except OSError:
        outcome = None
    capsule = build_handoff_capsule(
        row,
        events,
        outcome=outcome.get("outcome") if outcome else None,
        include_prompt_excerpt=include_prompt_excerpt,
        target=target if target in {"generic", "claude", "codex", "cursor", "vscode"} else "generic",
        handoff_type=handoff_type if handoff_type in HANDOFF_TYPE_LABELS else "coding",
        objective=objective,
        source_refs=source_refs or [],
        constraints=constraints or [],
        acceptance_criteria=acceptance_criteria or [],
        related_workspaces=_related_active_workspaces(row),
    )
    attachment = runtime_attachment_for_session(row, state=session_state(row), processes=safe_runtime_processes())
    capsule["runtime_attachment"] = attachment.to_json()
    return capsule


def build_demo_handoff_detail(
    target: str = "generic",
    handoff_type: str = "product",
    objective: str | None = None,
    source_refs: list[str] | None = None,
    constraints: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> dict[str, object]:
    target = target if target in TARGET_LABELS else "generic"
    handoff_type = handoff_type if handoff_type in HANDOFF_TYPE_LABELS else "product"
    now = datetime.now(timezone.utc)
    project_path = os.getcwd()
    session = LocalSession(
        session_id="demo-fresh-start",
        tool="codex-desktop",
        project_path=project_path,
        source_path="AIWatcher demo data",
        started_at=now - timedelta(hours=3),
        updated_at=now - timedelta(minutes=8),
        model="gpt-5.5",
        tokens_in=176_000,
        tokens_out=14_000,
        cost_usd=3.84,
        agent_calls=96,
        tool_calls=43,
    )
    capsule = build_handoff_capsule(
        session,
        [],
        outcome=None,
        target=target,
        handoff_type=handoff_type,
        objective=objective or "Continue the work in a fresh session without losing decisions, constraints, or acceptance criteria.",
        source_refs=source_refs or ["Current repo state", "Strategy or spec document", "Relevant PR or issue"],
        constraints=constraints or [
            "Do not assume access to the previous chat.",
            "Do not broaden scope beyond the next checkpoint.",
            "Preserve unrelated local changes and privacy boundaries.",
        ],
        acceptance_criteria=acceptance_criteria or [
            "First reply states what appears done, what remains uncertain, and the smallest next checkpoint.",
            "The next session loads source-of-truth files before editing.",
            "The result reports verification and remaining uncertainty.",
        ],
        extra_warnings=[
            "Demo context pressure: previous session had 190.0k tokens and several exploratory turns.",
            "Use this sample to verify the brief shape, copy action, and receipt flow before testing real local history.",
        ],
    )
    capsule["runtime_attachment"] = RuntimeAttachment(
        session_id=session.session_id,
        level="none",
        mode="demo",
        label="Demo data",
        action_label="Copy brief",
        available=False,
        confidence="demo",
        reason="This is seeded demo data. Copy the brief to inspect the Fresh Start flow; no live AI app will be opened.",
        tool=session.tool,
        surface="dashboard-demo",
        project_path=project_path,
        identity_level="demo",
        identity_label="Demo sample",
        identity_reason="Seeded sample for testing Fresh Start without real bloated local history.",
    ).to_json()
    capsule["demo"] = True
    capsule["basic"] = False
    capsule["enrichment_status"] = "complete"
    return capsule


def _query_items(params: dict[str, list[str]], name: str) -> list[str]:
    values: list[str] = []
    for raw in params.get(name, []):
        for line in str(raw).splitlines():
            item = " ".join(line.strip().split())
            if item and item not in values:
                values.append(item)
            if len(values) >= 8:
                return values
    return values


def _handoff_options_from_query(params: dict[str, list[str]], *, default_type: str = "coding") -> dict[str, object]:
    if default_type not in HANDOFF_TYPE_LABELS:
        default_type = "coding"
    handoff_type = params.get("type", [default_type])[0]
    if handoff_type not in HANDOFF_TYPE_LABELS:
        handoff_type = default_type
    objective = params.get("objective", [""])[0].strip() or None
    return {
        "handoff_type": handoff_type,
        "objective": objective,
        "source_refs": _query_items(params, "source"),
        "constraints": _query_items(params, "constraint"),
        "acceptance_criteria": _query_items(params, "acceptance"),
    }


def _payload_items(payload: dict[str, object], name: str) -> list[str]:
    raw = payload.get(name, [])
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [str(item) for item in raw]
    else:
        values = []
    return _query_items({name: values}, name)


def _handoff_options_from_payload(payload: dict[str, object], *, default_type: str = "coding") -> dict[str, object]:
    if default_type not in HANDOFF_TYPE_LABELS:
        default_type = "coding"
    handoff_type = str(payload.get("type", default_type)).strip() or default_type
    if handoff_type not in HANDOFF_TYPE_LABELS:
        handoff_type = default_type
    objective = str(payload.get("objective", "")).strip() or None
    return {
        "handoff_type": handoff_type,
        "objective": objective,
        "source_refs": _payload_items(payload, "source_refs"),
        "constraints": _payload_items(payload, "constraints"),
        "acceptance_criteria": _payload_items(payload, "acceptance_criteria"),
    }


def build_report(days: int = 7) -> dict[str, object]:
    rows = rows_for_window(days)
    stats = summarize(rows)
    projects = group_projects(rows)
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
        # Says only what reachability can see. `merge-base --is-ancestor` answers
        # "is this commit still on the branch" -- so it catches a rebase, reset or
        # amend, and provably cannot catch a revert (which adds a new commit and
        # leaves the original exactly where it was) or a delete of everything the
        # commit wrote. Line survival is the metric that judges whether the work
        # lasted; this one reports what happened to the commit.
        return (
            f"{inferred_churned} session{plural} looked useful but its commit is no longer on the branch "
            "-- rebased, reset or amended away. Worth a look before trusting similar work."
        )
    if outcomes["abandoned"] > outcomes["useful"]:
        return "More sessions were marked abandoned than useful this window -- review scoping before the next batch."
    return "No urgent signal this window -- local usage looks healthy."


def build_weekly_digest(days: int = 7) -> dict[str, object]:
    """P1-5 (S-26): richer weekly signals layered onto build_report's plain totals --
    outcome breakdown, highest-cost useful session, top sessions, loop/runaway
    candidates (P1-3), command-gate activity (P1-3), prompt-preflight activity
    (P1-1), survival economics (P1-4), and one recommendation.
    """
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
                "project": project_label(row.project_path),
                "tool": row.tool,
                "diagnosis": loop["diagnosis"],
            })
        velocity = _velocity_signal(row.tool, events)
        if velocity is not None:
            velocity_candidates.append({
                "project": project_label(row.project_path),
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
        # Same shape _survival_summary() returns when it has nothing, so every
        # consumer has one schema to read rather than two.
        survival = {"available": False, "reason": "Survival cache could not be read."}

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
                "project": project_label(highest_cost_useful.project_path),
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
                "project": project_label(row.project_path),
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
    projects = group_projects(rows)
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
            f"Most expensive session: {project_label(costliest.project_path)} · {costliest.tool} · {money(costliest.cost_usd)}",
            (
                f"Largest reliable context: {project_label(largest_context.project_path)} · "
                f"{compact_int(largest_context.tokens_in + largest_context.tokens_out)} tokens"
                if largest_context else "Largest reliable context: unavailable from local logs"
            ),
            (
                f"Loop signal: {loop_candidate.agent_calls} model calls in {project_label(loop_candidate.project_path)}"
                if loop_candidate else "Loop signal: unavailable from local logs"
            ),
        ],
        "improvement": improvement,
    }


def _context_action(health: ContextHealth) -> dict[str, str]:
    if health.severity == "critical":
        return {
            "label": "Start fresh",
            "secondary_label": "Fresh Start",
            "reason": "Critical context pressure is likely to waste turns or miss details.",
        }
    if health.is_context_pressure or health.is_high_bloat:
        return {
            "label": "Compact",
            "secondary_label": "Prepare Fresh Start",
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


def _context_health_card(health: ContextHealth, session: LocalSession | None, *, group: list[ContextHealth]) -> dict[str, object]:
    action = _context_action(health)
    critical_count = sum(1 for item in group if item.severity == "critical")
    warning_count = sum(1 for item in group if item.severity == "warning")
    replayed_tokens = sum(item.latest_turn_replayed_tokens for item in group)
    replayed_cost = sum(item.replayed_cost_usd for item in group if item.bloat_measurable)
    analyzed_cost = sum(item.analyzed_cost_usd for item in group if item.bloat_measurable)
    bloat_measurable = any(item.bloat_measurable for item in group)
    return {
        "session_id": health.session_id,
        "tool": health.tool,
        "project": project_label(health.project_path),
        "severity": health.severity,
        "session_count": len(group),
        "critical_sessions": critical_count,
        "warning_sessions": warning_count,
        "related_sessions": [
            {
                "session_id": item.session_id,
                "tool": item.tool,
                "severity": item.severity,
                "latest_turn_tokens": compact_int(item.latest_turn_tokens),
                "age_label": f"{item.age_days:.1f}d" if item.age_days >= 1 else f"{item.age_hours:.0f}h",
            }
            for item in group[:5]
        ],
        "latest_turn_tokens": compact_int(health.latest_turn_tokens),
        "peak_turn_tokens": compact_int(max(item.peak_turn_tokens for item in group)),
        "estimated_replayed_context_tokens": replayed_tokens,
        "estimated_replayed_context_label": compact_int(replayed_tokens),
        "bloat_measurable": bloat_measurable,
        "efficiency_label": f"{health.efficiency_pct:.0f}%" if health.bloat_measurable else "n/a",
        "bloat_label": f"{health.bloat_ratio * 100:.0f}%" if health.bloat_measurable else "n/a",
        "replayed_cost_label": f"${replayed_cost:.2f}" if bloat_measurable else "n/a",
        "analyzed_cost_label": f"${analyzed_cost:.2f}" if bloat_measurable else "n/a",
        "age_label": f"{health.age_days:.1f}d" if health.age_days >= 1 else f"{health.age_hours:.0f}h",
        "recommendation": health.recommendations[0] if health.recommendations else "Context is healthy.",
        "action": action,
        "runtime_attachment": (
            runtime_attachment_for_session(session, state=session_state(session), processes=[]).to_json()
            if session
            else None
        ),
        "updated_at": (
            (session.updated_at or session.started_at).isoformat()
            if session and (session.updated_at or session.started_at)
            else None
        ),
        "source_path": session.source_path if session else None,
        "can_handoff": bool(session),
        "compact_prompt": _build_compact_prompt(health),
        "group_note": (
            f"{len(group)} sessions need attention in this project."
            if len(group) > 1
            else "One session needs attention in this project."
        ),
    }


def _context_health_cards(rows: list[LocalSession], events: list[LocalEvent]) -> list[dict[str, object]]:
    sessions_by_id = {row.session_id: row for row in rows}
    grouped: dict[str, list[ContextHealth]] = defaultdict(list)
    for health in analyze_all_sessions(rows, events):
        grouped[project_key(health.project_path)].append(health)
    severity_order = {"critical": 0, "warning": 1, "healthy": 2}
    cards: list[dict[str, object]] = []
    for group in grouped.values():
        group.sort(key=lambda item: (
            severity_order.get(item.severity, 9),
            -int(item.latest_turn_tokens * item.bloat_ratio),
            -item.total_input_tokens,
        ))
        representative = group[0]
        cards.append(_context_health_card(representative, sessions_by_id.get(representative.session_id), group=group))
    cards.sort(key=lambda item: (
        severity_order.get(str(item.get("severity")), 9),
        -int(item.get("estimated_replayed_context_tokens") or 0),
        -int(item.get("session_count") or 0),
    ))
    cards = cards[:5]
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
    runtime = candidate.get("runtime_attachment") if isinstance(candidate.get("runtime_attachment"), dict) else {}
    runtime_available = bool(runtime.get("available")) and runtime.get("level") != "app"
    runtime_action = str(runtime.get("action_label") or "Open workspace")
    if severity == "critical":
        title = f"Fresh Start recommended before ~{saved_label} tokens of replayed context compounds"
        body = (
            f"{project} is at critical context pressure. Copy a Fresh Start brief so the next AI session keeps "
            "the goal, repo, files, and guardrails without replaying the bloated history."
        )
        primary_label = f"Copy brief + {runtime_action}" if runtime_available else "Copy Fresh Start brief"
    else:
        title = f"This session is getting heavy: ~{saved_label} tokens are replayed context"
        body = (
            f"{project} is showing context pressure. Compact or start fresh before the next broad task so usage "
            "does not compound."
        )
        primary_label = f"Copy brief + {runtime_action}" if runtime_available else "Prepare Fresh Start"
    reason = str(candidate.get("recommendation") or candidate.get("action", {}).get("reason") or body)
    return {
        "session_id": candidate.get("session_id"),
        "project": project,
        "tool": candidate.get("tool"),
        "updated_at": candidate.get("updated_at"),
        "source_path": candidate.get("source_path"),
        "severity": severity,
        "title": title,
        "body": body,
        "reason": reason,
        "primary_label": primary_label,
        "continue_label": "Continue 15 min",
        "saved_context_label": saved_label,
        "expected_saved_context_tokens": candidate.get("estimated_replayed_context_tokens"),
        "runtime_attachment": candidate.get("runtime_attachment"),
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


def _handoff_decision_rows(
    limit: int = 10,
    *,
    sessions: list[LocalSession] | None = None,
) -> list[dict[str, object]]:
    session_by_id = {row.session_id: row for row in sessions or []}
    session_ids = set(session_by_id)
    try:
        outcome_map = outcomes_for_sessions(session_ids) if session_ids else {}
    except OSError:
        outcome_map = {}
    try:
        evidence_map = evidence_snapshots_for_sessions(session_ids) if session_ids else {}
    except OSError:
        evidence_map = {}
    rows: list[dict[str, object]] = []
    for row in recent_handoff_decisions(limit=limit):
        expected = row.get("expected_saved_context_tokens")
        expected_int = expected if isinstance(expected, int) and expected > 0 else None
        decision = str(row.get("decision") or "")
        source_session_id = str(row.get("source_session_id") or row.get("session_id") or "")
        source_session = session_by_id.get(source_session_id)
        next_session_id = row.get("next_session_id") if isinstance(row.get("next_session_id"), str) else None
        next_session = session_by_id.get(next_session_id or "")
        correlation = row.get("next_session_correlation") if isinstance(row.get("next_session_correlation"), dict) else {}
        proof_status = "No fresh start claimed"
        proof_reason = "This decision did not claim a fresh follow-up session."
        if decision in {"new_chat", "copy_handoff"}:
            status = str(correlation.get("status") or "waiting")
            if next_session:
                proof_status = "Follow-up observed"
                proof_reason = str(
                    correlation.get("reason")
                    or "Observed a later local session in the same project after the Fresh Start action."
                )
            elif status == "ambiguous":
                proof_status = "Multiple possible next sessions"
                proof_reason = str(correlation.get("reason") or "AIWatcher found more than one possible follow-up.")
            else:
                proof_status = "Proof pending"
                proof_reason = str(correlation.get("reason") or "No later same-project local session has been observed yet.")
                if "saved tokens" not in proof_reason:
                    proof_reason = f"{proof_reason} AIWatcher will not claim saved tokens until one is linked."
        elif decision == "continue_here":
            proof_status = "Continued here"
            proof_reason = "The user chose to keep working in the current session."
        next_outcome = outcome_map.get(next_session.session_id) if next_session else None
        next_evidence = evidence_map.get(next_session.session_id) if next_session else None
        observed_followup = None
        source_usage = _usage_summary(source_session) if source_session else None
        next_usage = _usage_summary(next_session) if next_session else None
        proof_evidence = None
        if isinstance(next_evidence, dict):
            commit_shas = next_evidence.get("commit_shas") if isinstance(next_evidence.get("commit_shas"), list) else []
            test_artifacts = (
                next_evidence.get("test_artifact_hashes")
                if isinstance(next_evidence.get("test_artifact_hashes"), list)
                else []
            )
            proof_evidence = {
                "label": str(next_evidence.get("confidence") or correlation.get("confidence") or "observed"),
                "commits": len(commit_shas),
                "tests": len(test_artifacts),
                "inferred_outcome": next_evidence.get("inferred_outcome"),
            }
        if source_session and next_session:
            source_tokens = source_session.tokens_in + source_session.tokens_out
            next_tokens = next_session.tokens_in + next_session.tokens_out
            delta = source_tokens - next_tokens
            cost_delta = source_session.cost_usd - next_session.cost_usd
            if delta > 0 and cost_delta > 0:
                followup_label = (
                    f"Follow-up is {compact_int(delta)} tokens smaller and {money(cost_delta)} lower so far"
                )
            elif delta > 0:
                followup_label = f"Follow-up is {compact_int(delta)} tokens smaller so far; cost is not lower yet"
            elif cost_delta > 0:
                followup_label = f"Follow-up is {money(cost_delta)} lower so far; tokens are not lower yet"
            else:
                followup_label = "Follow-up is not smaller or cheaper yet"
            observed_followup = {
                "source_tokens_label": compact_int(source_tokens),
                "next_tokens_label": compact_int(next_tokens),
                "delta_tokens_label": compact_int(abs(delta)),
                "source_api_value_label": money(source_session.cost_usd),
                "next_api_value_label": money(next_session.cost_usd),
                "delta_api_value_label": money(abs(cost_delta)),
                "source_model_calls": source_session.agent_calls,
                "next_model_calls": next_session.agent_calls,
                "source_tool_calls": source_session.tool_calls,
                "next_tool_calls": next_session.tool_calls,
                "source_tokens_per_model_call_label": (
                    source_usage["tokens_per_model_call_label"] if source_usage else "not measured"
                ),
                "next_tokens_per_model_call_label": (
                    next_usage["tokens_per_model_call_label"] if next_usage else "not measured"
                ),
                "source_cost_per_model_call_label": (
                    source_usage["cost_per_model_call_label"] if source_usage else "not measured"
                ),
                "next_cost_per_model_call_label": (
                    next_usage["cost_per_model_call_label"] if next_usage else "not measured"
                ),
                "direction": "smaller" if delta > 0 else "larger_or_equal",
                "label": followup_label,
                "basis": "Observed local session totals so far; this compares the follow-up shape, but it is not a final saved-token or saved-dollar claim.",
            }
        rows.append({
            "id": row.get("id"),
            "created_at": row.get("created_at"),
            "session_id": row.get("session_id"),
            "source_session_id": source_session_id,
            "next_session_id": next_session_id,
            "decision": decision,
            "receipt_kind": row.get("receipt_kind"),
            "reason": row.get("reason"),
            "action_channel": row.get("action_channel"),
            "expected_saved_context_tokens": expected_int,
            "expected_saved_context_label": compact_int(expected_int) if expected_int else None,
            "proof_status": proof_status,
            "proof_reason": proof_reason,
            "proof_confidence": correlation.get("confidence"),
            "source_usage": source_usage,
            "next_usage": next_usage,
            "outcome": next_outcome.get("outcome") if isinstance(next_outcome, dict) else None,
            "inferred_outcome": next_evidence.get("inferred_outcome") if isinstance(next_evidence, dict) else None,
            "proof_evidence": proof_evidence,
            "observed_followup": observed_followup,
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


def _recent_handoff_decision_for_session(
    rows: list[dict[str, object]],
    session_id: str,
    *,
    max_age: timedelta = timedelta(hours=24),
) -> dict[str, object] | None:
    if not session_id:
        return None
    cutoff = datetime.now(timezone.utc) - max_age
    for row in rows:
        row_session_id = row.get("session_id") or row.get("source_session_id")
        if row_session_id != session_id:
            continue
        created_at = row.get("created_at")
        if isinstance(created_at, str):
            try:
                if datetime.fromisoformat(created_at).astimezone(timezone.utc) < cutoff:
                    continue
            except ValueError:
                pass
        return row
    return None


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
            # "reverted" was the one outcome this signal is structurally blind to:
            # `git revert` adds a new commit and leaves the original reachable, so
            # a reverted commit scores as surviving. What reachability actually
            # detects is history being rewritten out from under the commit.
            "title": f"{churned} session{'s' if churned != 1 else ''} produced a commit that is no longer on the branch",
            "body": (
                "Rebased, reset or amended away. This does not mean the work was undone -- "
                "a revert leaves the original commit in place and would not show up here. "
                "Cost per surviving line is the measure of whether the work lasted."
            ),
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


def build_summary(
    days: int = 7,
    *,
    all_rows: list[LocalSession] | None = None,
    all_events: list[LocalEvent] | None = None,
) -> dict[str, object]:
    now = datetime.now().astimezone()
    since = now - timedelta(days=days)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if all_rows is None:
        all_rows = scan_all(since=now - timedelta(days=max(32, days + 2)))
    _write_session_snapshot(all_rows)
    _index_sessions(all_rows)
    try:
        link_recent_interventions_to_sessions(all_rows)
        link_recent_fresh_start_receipts_to_sessions(all_rows)
    except OSError:
        pass
    # Events are loaded up front because the windows are clipped by them: a
    # session merely *touched* inside a window used to contribute every dollar
    # it had ever cost, which on long-running sessions roughly doubled the
    # reported total.
    if all_events is None:
        try:
            all_events = scan_all_events(since=since)
        except OSError:
            all_events = []
    _index_events(all_events, complete=True)
    rows = clip_sessions_to_window(all_rows, all_events, since)
    month_rows = clip_sessions_to_window(all_rows, all_events, month_start)

    stats = summarize(rows)
    month_stats = summarize(month_rows)
    split = token_split(rows)
    day_of_month = max(1, now.day)
    projected_month = float(month_stats["api_value_usd"]) / day_of_month * 30

    projects = group_projects(rows)
    tools = group_rows(rows, _tool_surface_key)
    models = group_by_model_breakdown(rows)

    recent = sorted(rows, key=lambda row: row.updated_at or row.started_at or MIN_DT, reverse=True)[:12]
    detected = discover_tools()
    notes = sorted({note for row in rows for note in row.notes})
    context_health = _context_health_cards(rows, all_events)
    handoff_decisions = _handoff_decision_rows(limit=10, sessions=all_rows)
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
            "body": f"{project_label(costliest.project_path)} used {money(costliest.cost_usd)} API-equivalent value on {costliest.model or costliest.tool}. Open the session before repeating similar work.",
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
    window_outcomes, outcomes = _safe_window_outcomes(window_session_ids)
    sample_rows = rows[:30]
    try:
        survival_map = survival_by_session(sample_rows)
    except OSError:
        survival_map = {}
    try:
        evidence_by_session = evidence_for_sessions(sample_rows, survival_by_session=survival_map)
    except OSError:
        evidence_by_session = {}
    try:
        record_missing_evidence_snapshots_from_evidence(sample_rows, evidence_by_session)
    except OSError:
        pass
    try:
        evidence_snapshots = evidence_snapshots_for_sessions({row.session_id for row in recent})
    except OSError:
        evidence_snapshots = {}
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
    try:
        interventions = recent_interventions(limit=200, days=days)
    except OSError:
        interventions = []
    receipt_events = all_events if interventions else []
    try:
        receipt_outcomes = outcomes_for_sessions()
    except OSError:
        receipt_outcomes = {}
    receipts = _build_intervention_receipts(interventions, all_rows, receipt_outcomes, receipt_events)
    useful_rows = [
        row for row in rows
        if (window_outcomes.get(row.session_id) or {}).get("outcome") == "useful"
    ]
    useful_cost = sum(row.cost_usd for row in useful_rows)
    cost_per_useful = useful_cost / len(useful_rows) if useful_rows else None
    return {
        "generated_at": now.isoformat(),
        "cache_schema_version": SUMMARY_CACHE_SCHEMA_VERSION,
        "summary_complete": True,
        "_session_index": _session_index_payload(all_rows),
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
        "watcher": get_watcher_status(),
        "context_health": context_health,
        "context_health_status": "ready",
        "handoff_bubble": handoff_bubble,
        "handoff_decisions": handoff_decisions,
        "recent_sessions": [
            {
                **recent_session_json(row, window_outcomes=window_outcomes, evidence_by_session=evidence_by_session),
                "evidence_captured": row.session_id in evidence_snapshots,
                "evidence_recorded_at": (evidence_snapshots.get(row.session_id) or {}).get("recorded_at"),
            }
            for row in recent
        ],
        "intervention_receipts": receipts[:30],
    }


def _summary_cache_dir() -> Path:
    return state_path().parent / "cache"


def _summary_cache_path(days: int) -> Path:
    return _summary_cache_dir() / f"ui-summary-{days}.json"


def _session_snapshot_path() -> Path:
    return _summary_cache_dir() / "session-index.json"


def _read_session_snapshot() -> list[LocalSession]:
    try:
        raw = json.loads(_session_snapshot_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict) or raw.get("schema_version") != SESSION_SNAPSHOT_SCHEMA_VERSION:
        return []
    items = raw.get("sessions")
    if not isinstance(items, list):
        return []
    return [row for item in items if (row := _session_from_json(item)) is not None]


def _write_session_snapshot(rows: list[LocalSession]) -> None:
    payload = {
        "schema_version": SESSION_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sessions": _session_index_payload(rows),
    }
    try:
        path = _session_snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        pass


def _cached_session_rows() -> list[LocalSession]:
    with _SUMMARY_CACHE_LOCK:
        if _SESSION_INDEX:
            return list(_SESSION_INDEX.values())
    rows = _read_session_snapshot()
    if rows:
        _index_sessions(rows)
        return rows

    # Upgrade path: older PR #46 builds already persisted the session index in
    # complete summary caches. Reuse that stable metadata even when the summary
    # schema itself changed, then write the dedicated snapshot for next start.
    for days in SUMMARY_WINDOWS:
        try:
            raw = json.loads(_summary_cache_path(days).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = raw.get("_session_index") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            continue
        rows = [row for item in items if (row := _session_from_json(item)) is not None]
        if rows:
            _index_sessions(rows)
            _write_session_snapshot(rows)
            return rows
    return []


def _mark_summary_cache(summary: dict[str, object], *, status: str, source: str, refreshing: bool) -> dict[str, object]:
    copy = dict(summary)
    copy.pop("_session_index", None)
    generated_at = copy.get("generated_at") if isinstance(copy.get("generated_at"), str) else None
    copy["cache_schema_version"] = SUMMARY_CACHE_SCHEMA_VERSION
    copy["cache"] = {
        "status": status,
        "source": source,
        "refreshing": refreshing,
        "generated_at": generated_at,
        "schema_version": SUMMARY_CACHE_SCHEMA_VERSION,
        "error": _SUMMARY_REFRESH_ERROR,
    }
    return copy


def _read_summary_disk_cache(days: int, *, max_age_seconds: int = SUMMARY_DISK_TTL_SECONDS) -> dict[str, object] | None:
    path = _summary_cache_path(days)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("cache_schema_version") != SUMMARY_CACHE_SCHEMA_VERSION:
        return None
    if raw.get("summary_complete") is not True:
        return None
    generated_at = raw.get("generated_at")
    try:
        generated = datetime.fromisoformat(str(generated_at)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
    if (datetime.now(timezone.utc) - generated).total_seconds() > max_age_seconds:
        return None
    return raw


def _write_summary_disk_cache(days: int, summary: dict[str, object]) -> None:
    # Only complete summaries are worth persisting. A first-paint shell is cheap
    # to rebuild but expensive to serve by mistake: it carries the same schema
    # version as a full payload, so a shell left on disk by a crashed or killed
    # background refresh would be served as a normal summary for the whole disk
    # TTL, rendering empty survival/context-health/receipt sections with no error.
    if summary.get("summary_complete") is not True:
        return
    try:
        path = _summary_cache_path(days)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(summary), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        pass


def _store_summary_cache(days: int, summary: dict[str, object], *, mark_refreshed: bool = True) -> None:
    _index_sessions_from_summary(summary)
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE[days] = (time.monotonic(), summary)
        if mark_refreshed:
            _SUMMARY_REFRESHED_AT[days] = time.monotonic()
    _write_summary_disk_cache(days, summary)


def _build_summary_shell(
    days: int = 7,
    *,
    all_rows: list[LocalSession] | None = None,
) -> dict[str, object]:
    """Build the dashboard's fast first-paint data without event/evidence scans."""
    now = datetime.now().astimezone()
    since = now - timedelta(days=days)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Never scan transcript history on the request thread. A cold Codex parse
    # can take tens of seconds; the last normalized local snapshot is enough
    # for a truthful first paint while one shared background refresh catches up.
    all_rows = list(all_rows) if all_rows is not None else _cached_session_rows()
    _index_sessions(all_rows)
    try:
        link_recent_interventions_to_sessions(all_rows)
        link_recent_fresh_start_receipts_to_sessions(all_rows)
    except OSError:
        pass
    rows = [row for row in all_rows if in_window(row, since)]
    month_rows = [row for row in all_rows if in_window(row, month_start)]
    stats = summarize(rows)
    month_stats = summarize(month_rows)
    split = token_split(rows)
    projected_month = float(month_stats["api_value_usd"]) / max(1, now.day) * 30
    projects = group_projects(rows)
    tools = group_rows(rows, _tool_surface_key)
    models = group_by_model_breakdown(rows)
    recent = sorted(rows, key=lambda row: row.updated_at or row.started_at or MIN_DT, reverse=True)[:12]
    window_session_ids = {row.session_id for row in rows}
    window_outcomes, outcomes = _safe_window_outcomes(window_session_ids)
    try:
        interventions = recent_interventions(limit=200, days=days)
    except OSError:
        interventions = []
    insights = []
    if projects:
        top = projects[0]
        insights.append({
            "title": "Top project",
            "body": f"{top['short_name']} accounts for {top['api_value_label']} API-equivalent value.",
        })
        health = top.get("health") if isinstance(top.get("health"), dict) else None
        if health and health.get("status") in {"review", "critical"}:
            insights.append({
                "title": f"{health['label']} project",
                "body": f"{top['short_name']}: {health['reason']}",
            })
    if split["plan_limited"] > 0:
        insights.append({
            "title": "Subscription/limited usage detected",
            "body": f"{compact_int(split['plan_limited'])} tokens came from plan-based or limited-cost sources. Treat them as observed usage, not invoice spend.",
        })
    useful_rows = [
        row for row in rows
        if (window_outcomes.get(row.session_id) or {}).get("outcome") == "useful"
    ]
    useful_cost = sum(row.cost_usd for row in useful_rows)
    cost_per_useful = useful_cost / len(useful_rows) if useful_rows else None
    return {
        "generated_at": now.isoformat(),
        "cache_schema_version": SUMMARY_CACHE_SCHEMA_VERSION,
        # First-paint shell: the event/evidence-backed sections below are still
        # placeholders, so this payload must never reach the disk cache.
        "summary_complete": False,
        "_session_index": _session_index_payload(all_rows),
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
            "inferred_useful_outcomes": 0,
            "needs_review_outcomes": 0,
            "rework_outcomes": outcomes["rework"],
            "abandoned_outcomes": outcomes["abandoned"],
            "preflight_decisions": len(interventions),
            "cost_per_useful_change": money(cost_per_useful) if cost_per_useful is not None else "—",
        },
        "survival": {"available": False, "reason": "Background evidence refresh pending."},
        "projects": projects[:10],
        "tools": tools,
        "models": models[:10],
        "insights": insights,
        "notes": sorted({note for row in rows for note in row.notes})[:5],
        "coverage": [row.to_json() for row in surface_coverage(all_rows)],
        "setup": setup_checklist(),
        "watcher": get_watcher_status(),
        "context_health": [],
        "context_health_status": "pending",
        "handoff_bubble": None,
        "handoff_decisions": _handoff_decision_rows(limit=10, sessions=all_rows),
        "recent_sessions": [
            recent_session_json(row, window_outcomes=window_outcomes)
            for row in recent
        ],
        "intervention_receipts": [],
    }


def _summary_refresh_windows(requested_days: int) -> list[int]:
    return [requested_days, *[days for days in SUMMARY_WINDOWS if days != requested_days]]


def _run_shared_summary_refresh(requested_days: int) -> None:
    """Scan local histories once, then materialize every dashboard window."""
    global _SUMMARY_REFRESH_ERROR
    try:
        now = datetime.now().astimezone()
        scan_days = max(32, requested_days + 2)
        all_rows = scan_all(since=now - timedelta(days=scan_days))
        # Publish session identity as soon as the comparatively slow transcript
        # scan finishes. Filters and detail headers do not need to wait for git,
        # outcome, or event enrichment.
        _write_session_snapshot(all_rows)
        _index_sessions(all_rows)
        try:
            all_events = scan_all_events(since=now - timedelta(days=scan_days))
        except OSError:
            all_events = []
        _index_events(all_events, complete=True)
        for days in _summary_refresh_windows(requested_days):
            summary = build_summary(days, all_rows=all_rows, all_events=all_events)
            _store_summary_cache(days, summary, mark_refreshed=True)
        _SUMMARY_REFRESH_ERROR = None
    except Exception as exc:  # fail soft: cached local data remains usable
        _SUMMARY_REFRESH_ERROR = f"Background refresh failed: {type(exc).__name__}"
    finally:
        with _SUMMARY_CACHE_LOCK:
            _SUMMARY_REFRESHING.clear()


def _refresh_summary_cache(days: int) -> None:
    with _SUMMARY_CACHE_LOCK:
        if _SUMMARY_REFRESHING:
            return
        _SUMMARY_REFRESHING.update(_summary_refresh_windows(days))
    _run_shared_summary_refresh(days)


def _maybe_refresh_summary_cache(days: int, *, force: bool = False) -> bool:
    now = time.monotonic()
    with _SUMMARY_CACHE_LOCK:
        if _SUMMARY_REFRESHING:
            return True
        if not force and now - _SUMMARY_REFRESHED_AT.get(days, 0) < SUMMARY_BACKGROUND_COOLDOWN_SECONDS:
            return False
        _SUMMARY_REFRESHING.update(_summary_refresh_windows(days))

    def run() -> None:
        _run_shared_summary_refresh(days)

    thread = threading.Thread(target=run, name="aiwatcher-summary-refresh", daemon=True)
    thread.start()
    return True


def build_summary_cached(days: int = 7, *, force: bool = False) -> dict[str, object]:
    if force:
        _maybe_refresh_summary_cache(days, force=True)
        shell = _build_summary_shell(days)
        return _mark_summary_cache(shell, status="refreshing", source="computed", refreshing=True)
    with _SUMMARY_CACHE_LOCK:
        cached = _SUMMARY_CACHE.get(days)
        refreshing = days in _SUMMARY_REFRESHING
        if cached and time.monotonic() - cached[0] <= SUMMARY_MEMORY_TTL_SECONDS:
            return _mark_summary_cache(cached[1], status="fresh", source="memory", refreshing=refreshing)
    disk = _read_summary_disk_cache(days)
    if disk:
        refreshing = _maybe_refresh_summary_cache(days)
        return _mark_summary_cache(disk, status="stale", source="disk", refreshing=refreshing)
    shell = _build_summary_shell(days)
    _store_summary_cache(days, shell, mark_refreshed=False)
    refreshing = _maybe_refresh_summary_cache(days)
    return _mark_summary_cache(shell, status="building", source="computed", refreshing=refreshing)


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_companion_state() -> dict[str, object]:
    """Small, fast state contract for the always-available Companion surface."""
    summary = build_summary_cached(7)
    base = {
        "state": "watching",
        "label": "Watching quietly",
        "title": "AIWatcher",
        "subtitle": "Plan, control, watch",
        "primary_label": "Watch",
        "primary_url": "/",
        "plan_url": "/?view=prompt",
        "control_url": "/?view=prompt",
        "watch_url": "/",
        "console_url": "/",
        "detail": "Local-only. No prompt or source text is shown in the Companion.",
    }
    try:
        gate = active_prompt_gate()
    except OSError:
        gate = None
    if isinstance(gate, dict):
        tool = str(gate.get("tool") or "AI tool")
        risk = str(gate.get("risk") or "risk")
        score = gate.get("score")
        score_label = f" score {score}" if isinstance(score, int) else ""
        return {
            **base,
            "state": "prompt_gate",
            "label": "Prompt Gate",
            "title": "Prompt Gate",
            "subtitle": f"{tool} {risk}-risk prompt waiting{score_label}.",
            "primary_label": "Review Gate",
            "primary_url": str(gate.get("url") or "/?view=prompt"),
            "control_url": str(gate.get("url") or "/?view=prompt"),
            "detail": "A hook paused this prompt locally. Review it before the AI tool continues.",
        }
    bubble = summary.get("handoff_bubble")
    if isinstance(bubble, dict) and bubble.get("session_id"):
        session_id = str(bubble.get("session_id"))
        try:
            direct_decisions = recent_handoff_decisions(limit=20)
        except OSError:
            direct_decisions = []
        summary_decisions = summary.get("handoff_decisions") if isinstance(summary.get("handoff_decisions"), list) else []
        recent_decision = _recent_handoff_decision_for_session(
            direct_decisions or summary_decisions,
            session_id,
        )
        if isinstance(recent_decision, dict):
            decision = str(recent_decision.get("decision") or "")
            if decision in {"continue_here", "dismissed"}:
                return {
                    **base,
                    "state": "watching",
                    "label": "Watching quietly",
                    "subtitle": "Fresh Start decision saved",
                    "primary_label": "Console",
                    "detail": "AIWatcher will stay quiet for this session unless a new intervention is justified.",
                }
            if decision in {"new_chat", "copy_handoff"}:
                return {
                    **base,
                    "state": "proof_pending",
                    "label": "Proof pending",
                    "subtitle": "Fresh Start brief copied. Waiting to observe follow-up evidence.",
                    "primary_label": "View receipt",
                    "primary_url": "/?view=receipts",
                    "detail": "AIWatcher will not claim saved tokens until a follow-up session is observed.",
                }
        severity = str(bubble.get("severity") or "warning")
        label = "Fresh Start" if severity == "critical" else "Review context"
        return {
            **base,
            "state": "control_recommended",
            "label": label,
            "subtitle": str(bubble.get("body") or bubble.get("reason") or "Context pressure needs a decision."),
            "primary_label": "Fresh Start",
            "primary_url": f"/?session={session_id}",
            "control_url": f"/?session={session_id}",
            "watch_url": f"/?session={session_id}",
            "detail": "Control recommendation is based on local context-health evidence.",
        }
    fresh_start_receipts = summary.get("handoff_decisions")
    if isinstance(fresh_start_receipts, list):
        now = datetime.now(timezone.utc)
        for receipt in fresh_start_receipts:
            if not isinstance(receipt, dict):
                continue
            if str(receipt.get("proof_status") or "").lower() != "proof pending":
                continue
            created_at = _parse_iso_datetime(receipt.get("created_at"))
            if created_at is not None and now - created_at > timedelta(minutes=45):
                continue
            return {
                **base,
                "state": "proof_pending",
                "label": "Proof pending",
                "subtitle": str(receipt.get("proof_reason") or "Waiting to observe the follow-up session."),
                "primary_label": "View receipt",
                "primary_url": "/?view=receipts",
                "detail": "AIWatcher copied or recorded an intervention and is waiting for observed outcome evidence.",
            }
    insights = summary.get("insights")
    if isinstance(insights, list) and insights:
        top = insights[0] if isinstance(insights[0], dict) else {}
        return {
            **base,
            "state": "needs_review",
            "label": "Needs review",
            "subtitle": str(top.get("title") or "Open AIWatcher for the latest local signal."),
            "primary_label": "Review",
            "primary_url": "/",
            "detail": str(top.get("body") or "AIWatcher found local usage evidence worth reviewing."),
        }
    watcher = summary.get("watcher")
    running = isinstance(watcher, dict) and bool(watcher.get("running"))
    return {
        **base,
        "state": "watching" if running else "offline",
        "label": "Watching quietly" if running else "Open AIWatcher",
        "subtitle": "Local Companion is running" if running else "Companion state is available from the Dashboard",
        "primary_label": "Console" if running else "Open",
        "detail": "AIWatcher will interrupt only when a matching active session has a justified action." if running else base["detail"],
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
    .runtime-strip {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      border: 1px solid var(--line); border-radius: 8px; background: #0c1118;
      padding: 10px 12px; margin: -4px 0 18px;
    }
    .runtime-copy { display: flex; align-items: center; gap: 9px; min-width: 0; }
    .runtime-copy strong { white-space: nowrap; }
    .runtime-copy span { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .runtime-source {
      display: grid; grid-template-columns: 110px 1fr; gap: 10px; align-items: start;
      margin-top: 12px; color: var(--muted); font-size: 13px;
    }
    .runtime-source span { overflow-wrap: anywhere; font-family: var(--mono); color: var(--text); }
    .cache-pill, .health-pill, .session-state {
      display: inline-flex; align-items: center; gap: 6px; border-radius: 999px;
      border: 1px solid var(--line); padding: 4px 8px; font-size: 11px; font-weight: 800;
      white-space: nowrap;
    }
    .cache-pill.refreshing, .health-pill.review, .health-pill.warning, .session-state.recent { color: #ffe2a4; border-color: rgba(246,189,96,.45); background: var(--amber-soft); }
    .cache-pill.fresh, .health-pill.healthy, .session-state.active { color: #bff5df; border-color: rgba(53,211,153,.45); background: var(--green-soft); }
    .cache-pill.building, .cache-pill.stale, .health-pill.limited, .session-state.ended, .session-state.stale, .session-state.unknown { color: #dceaff; border-color: rgba(112,167,255,.45); background: var(--blue-soft); }
    .health-pill.critical { color: #ffc4ce; border-color: rgba(242,125,143,.45); background: var(--red-soft); }
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
    button:focus-visible, select:focus-visible, input:focus-visible, textarea:focus-visible, a:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
    button:disabled { cursor: not-allowed; opacity: .62; }
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
    .recommended-action {
      border-color: rgba(86,157,231,.5);
      background: linear-gradient(135deg, rgba(47,111,189,.2), rgba(14,20,29,.82));
    }
    .recommended-action h3 { font-size: 21px; margin-bottom: 6px; }
    .recommended-action p { color: #b9c6d8; }
    .action-kicker { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
    .tool-link-note { margin-top: 10px; font-size: 12px; color: #94a3b8; }
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
    .action-queue { display: grid; gap: 10px; }
    .action-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 14px;
      align-items: center;
      border: 1px solid var(--line);
      border-left: 4px solid var(--line-strong);
      border-radius: 8px;
      background: #0b1118;
      padding: 14px;
    }
    .action-row.critical { border-left-color: var(--red); }
    .action-row.high { border-left-color: var(--amber); }
    .action-row.medium { border-left-color: var(--blue); }
    .action-row.low { border-left-color: var(--green); }
    .action-title { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-weight: 780; color: white; }
    .action-row p { margin: 6px 0 0; color: var(--muted); line-height: 1.45; }
    .action-meta { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .action-row .actions { justify-content: flex-end; }
    .coverage-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .coverage-card, .health-card { border: 1px solid var(--line); border-radius: 8px; background: #0b1118; padding: 14px; }
    .coverage-head, .health-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
    .coverage-status, .health-severity { border-radius: 999px; border: 1px solid var(--line); padding: 4px 8px; font-size: 11px; font-weight: 800; white-space: nowrap; }
    .coverage-status.automatic, .health-severity.healthy { color: #bff5df; border-color: rgba(53,211,153,.45); background: var(--green-soft); }
    .coverage-status.limited { color: #b8f0e0; border-color: rgba(53,211,153,.3); background: rgba(53,211,153,.1); }
    .coverage-status.unverified, .health-severity.warning { color: #ffe2a4; border-color: rgba(246,189,96,.45); background: var(--amber-soft); }
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
    .outcome-pill.evidence { color: #d4e7ff; border-color: rgba(135,168,255,.36); background: rgba(135,168,255,.12); }
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
    .action-stack { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; justify-content: flex-end; }
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
    .handoff-form {
      margin-top: 14px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0b1118;
      display: grid;
      gap: 12px;
    }
    .handoff-form-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
    .handoff-form-head h3 { margin: 0; font-size: 16px; }
    .handoff-form-head p { margin-top: 4px; color: var(--muted); font-size: 12px; }
    .handoff-form-grid { display: grid; grid-template-columns: minmax(150px, .55fr) minmax(0, 1.45fr); gap: 10px; }
    .handoff-form label { display: grid; gap: 6px; min-width: 0; }
    .handoff-form input, .handoff-form select, .handoff-form textarea {
      width: 100%;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: #090f16;
      color: var(--text);
      padding: 9px 10px;
      font: inherit;
    }
    .handoff-form textarea { min-height: 74px; resize: vertical; font-size: 12px; line-height: 1.45; }
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
      <select id="days" onchange="changeWindow()">
        <option value="1">Last 24 hours</option>
        <option value="7" selected>Last 7 days</option>
        <option value="30">Last 30 days</option>
      </select>
      <button id="refreshButton" class="btn-quiet" onclick="load(false, true)">Refresh data</button>
      <a class="link-button" href="https://www.getaiwatcher.com" target="_blank" rel="noreferrer">Enterprise</a>
    </div>
  </header>

  <section class="runtime-strip" aria-live="polite">
    <div class="runtime-copy">
      <span id="watcherPill" class="cache-pill building">Watcher unknown</span>
      <span id="cacheStatus">Loading local index...</span>
    </div>
    <button id="watcherCommandButton" class="btn-quiet" onclick="copyWatcherCommand()" hidden>Copy watcher command</button>
  </section>

  <nav class="product-nav" aria-label="AIWatcher Local sections">
    <button class="nav-tab active" data-view="today" onclick="showView('today')">Home</button>
    <button class="nav-tab" data-view="prompt" onclick="showView('prompt')">Control</button>
    <button class="nav-tab" data-view="sessions" onclick="showView('sessions')">Work</button>
    <button class="nav-tab" data-view="receipts" onclick="showView('receipts')">Evidence</button>
    <button class="nav-tab" data-view="insights" onclick="showView('insights')">Spend</button>
    <button class="nav-tab" data-view="setup" onclick="showView('setup')">Settings</button>
  </nav>

  <section id="view-today" class="view">
    <section id="handoffBubble" class="card handoff-bubble" style="margin-bottom:14px" hidden></section>

    <section class="card" style="margin-bottom:14px">
      <div class="section-title">
        <div><h2>Needs action</h2><p>The highest-confidence local actions before the charts. Every row says what AIWatcher knows and what it still cannot prove.</p></div>
        <button class="btn-quiet" onclick="showView('setup')">Check coverage</button>
      </div>
      <div id="actionQueue" class="action-queue"></div>
    </section>

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
      <div class="section-title"><div><h2>Latest Fresh Start receipt</h2><p>Fresh Start choices, expected replayed context, and follow-up proof.</p></div></div>
      <div id="latestHandoffDecision"></div>
    </section>

    <section class="card" style="margin-bottom:14px">
      <div class="section-title"><div><h2>Session health</h2><p>Context bloat, runway pressure, and Fresh Start actions for active local work.</p></div></div>
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
        <h3 style="font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 4px">By model</h3>
        <div id="models"></div>
        <h3 style="font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:14px 0 4px">By tool</h3>
        <div id="tools"></div>
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
        <thead><tr><th>Project</th><th>Status</th><th>Sessions</th><th>Tokens</th><th>Model calls</th><th>API value</th></tr></thead>
        <tbody id="projectRows"></tbody>
      </table></div>
    </div>
  </section>

  <section id="view-sessions" class="view" hidden>
    <div class="card">
      <div class="section-title">
        <div><h2>Work</h2><p>Search sessions, inspect projects, review changes, and continue prior local AI work. Prompt text is shown for your own review only, never uploaded.</p></div>
        <span class="pill">Local machine only</span>
      </div>
      <div class="actions" style="margin-bottom:12px">
        <button class="btn-quiet" data-view="projects" onclick="showView('projects')">Projects</button>
        <button class="btn-quiet" data-view="changes" onclick="showView('changes')">Changes ledger</button>
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
        <div><h2>Fresh Start receipts</h2><p>When AIWatcher suggested a fresh session, what you chose, and whether a follow-up session was observed.</p></div>
        <span class="pill">Metadata only</span>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Time</th><th>Decision</th><th>Expected context at risk</th><th>Proof</th><th>Sessions</th><th></th></tr></thead>
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
        <div><h2>Spend and improvement signals</h2><p>Ranked by money, context pressure, and what to change next.</p></div>
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
        <div><h2>Settings</h2><p>Setup, coverage, privacy, and local companion behavior.</p></div>
        <span class="pill">Local only</span>
      </div>
      <p class="receipt-note">Ambient Watch starts with the dashboard by default. For standalone mode, run <code>aiwatcher watch --notify --overlay --interval 60</code>.</p>
      <div class="actions" style="margin-bottom:12px">
        <button class="btn-quiet" data-view="coverage" onclick="showView('coverage')">Surface coverage</button>
      </div>
      <div class="handoff-cta" style="margin-bottom:14px">
        <h4>Test Fresh Start with sample data</h4>
        <p>Open a seeded context-pressure case, tune the continuation brief, and verify the copy flow without waiting for a real bloated session.</p>
        <button class="btn-primary" onclick="openDemoHandoff()">Try Fresh Start demo</button>
      </div>
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
const HANDOFF_TYPES = [
  { id: 'coding', label: 'Coding continuation' },
  { id: 'product', label: 'Product/strategy continuation' },
  { id: 'review', label: 'Review continuation' },
  { id: 'bugbash', label: 'Bug bash continuation' },
  { id: 'investigation', label: 'Investigation continuation' },
  { id: 'general', label: 'General work continuation' },
];
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
function jsArg(value) {
  return `'${String(value ?? '')
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/\r/g, '\\r')
    .replace(/\n/g, '\\n')}'`;
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
function actionRow(item) {
  const meta = (item.meta || []).map(value => `<span class="pill">${esc(value)}</span>`).join('');
  return `<div class="action-row ${esc(item.severity || 'medium')}">
    <div>
      <div class="action-title"><span>${esc(item.title)}</span><span class="pill">${esc(item.evidence || 'local evidence')}</span></div>
      <p>${esc(item.body)}</p>
      ${meta ? `<div class="action-meta">${meta}</div>` : ''}
    </div>
    <div class="actions">${(item.actions || []).map(action => `<button class="${esc(action.primary ? 'btn-primary' : 'btn-quiet')}" onclick="${esc(action.onclick)}">${esc(action.label)}</button>`).join('')}</div>
  </div>`;
}
function buildActionQueue(data) {
  const items = [];
  const bubble = data.handoff_bubble;
  if (bubble && bubble.session_id) {
    items.push({
      severity: bubble.severity === 'critical' ? 'critical' : 'high',
      title: bubble.title || 'Fresh Start recommended',
      body: bubble.body || bubble.reason || 'Context pressure is elevated in a local session.',
      evidence: 'observed local session',
      meta: [
        bubble.expected_saved_context_label ? `~${bubble.expected_saved_context_label} context at risk` : 'proof pending',
        bubble.tool || 'unknown tool',
        bubble.project || 'unknown project',
      ],
      actions: [
        { label: bubble.primary_label || 'Open Fresh Start', primary: true, onclick: `openHandoff(${jsArg(bubble.session_id)})` },
        { label: 'Inspect session', onclick: `selectSession(${jsArg(bubble.session_id)})` },
      ],
    });
  }
  (data.context_health || []).slice(0, bubble ? 2 : 3).forEach(row => {
    if (bubble && row.session_id === bubble.session_id) return;
    items.push({
      severity: row.severity === 'critical' ? 'critical' : 'high',
      title: row.action && row.action.label ? row.action.label : 'Review context health',
      body: row.recommendation || row.action && row.action.reason || 'AIWatcher found context or pace pressure.',
      evidence: row.bloat_measurable ? 'measured replay' : 'observed signal',
      meta: [row.project, row.tool, row.latest_turn_tokens ? `${row.latest_turn_tokens} latest turn` : '', row.bloat_label ? `${row.bloat_label} replay` : ''].filter(Boolean),
      actions: [
        { label: row.can_handoff ? 'Open Fresh Start' : 'Review session', primary: true, onclick: row.can_handoff ? `openHandoff(${jsArg(row.session_id)})` : `selectSession(${jsArg(row.session_id)})` },
        { label: 'Inspect evidence', onclick: `selectSession(${jsArg(row.session_id)})` },
      ],
    });
  });
  (data.handoff_decisions || []).filter(decision => !decision.next_session_id).slice(0, 2).forEach(decision => {
    const sessionId = decision.source_session_id || decision.session_id || '';
    items.push({
      severity: 'medium',
      title: 'Fresh Start proof pending',
      body: decision.proof_reason || 'A brief was copied, but AIWatcher has not linked a follow-up session yet.',
      evidence: 'receipt pending',
      meta: [decision.expected_saved_context_label ? `~${decision.expected_saved_context_label} context at risk` : 'no savings claim yet', sessionId ? `source ${shortSessionId(sessionId)}` : 'source unknown'].filter(Boolean),
      actions: [
        { label: sessionId ? 'Inspect source' : 'View Evidence', primary: false, onclick: sessionId ? `selectSession(${jsArg(sessionId)})` : "showView('receipts')" },
        { label: 'View receipts', primary: true, onclick: "showView('receipts')" },
      ],
    });
  });
  (data.recent_sessions || []).filter(s => !s.outcome && ['useful', 'needs_review', 'churned'].includes(s.inferred_outcome)).slice(0, 3).forEach(s => {
    items.push({
      severity: s.inferred_outcome === 'churned' ? 'high' : 'medium',
      title: s.inferred_outcome === 'useful' ? 'Confirm likely useful work' : s.inferred_outcome === 'churned' ? 'Review rewritten work' : 'Review unconfirmed outcome',
      body: 'AIWatcher found local code/test evidence, but your outcome confirmation is what makes value metrics trustworthy.',
      evidence: `inferred: ${s.inferred_outcome}`,
      meta: [s.project, s.tool, s.api_value || s.tokens],
      actions: [{ label: 'Mark outcome', primary: true, onclick: `selectSession(${jsArg(s.session_id)})` }],
    });
  });
  const coverageGap = (data.coverage || []).find(row => ['unverified', 'not_detected', 'unsupported'].includes(row.status));
  if (coverageGap) {
    items.push({
      severity: coverageGap.status === 'unsupported' ? 'medium' : 'high',
      title: `Verify ${coverageGap.label} coverage`,
      body: coverageGap.action || 'Coverage is not automatic yet. Verify whether this surface is protected, companion-only, or history-only.',
      evidence: 'coverage state',
      meta: [coverageGap.status_label, coverageGap.history, coverageGap.automatic_gate],
      actions: [{ label: 'Open Settings', primary: true, onclick: "showView('setup')" }],
    });
  }
  if (!items.length && data.insights && data.insights.length) {
    const insight = data.insights[0];
    items.push({
      severity: insight.severity || 'low',
      title: insight.title,
      body: insight.body,
      evidence: insight.impact_label ? 'cost signal' : 'local signal',
      meta: [insight.impact_label || 'no urgent intervention'],
      actions: insight.session_id ? [{ label: 'Inspect session', primary: true, onclick: `selectSession(${jsArg(insight.session_id)})` }] : [{ label: 'Open Spend', primary: true, onclick: "showView('insights')" }],
    });
  }
  if (!items.length) {
    return `<div class="empty">No action needed right now. AIWatcher will stay quiet until it has local evidence worth interrupting you for.</div>`;
  }
  const order = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };
  return items.sort((a, b) => (order[a.severity] ?? 4) - (order[b.severity] ?? 4)).slice(0, 5).map(actionRow).join('');
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
    new_chat: 'Prepared Fresh Start',
    continue_here: 'Continued in current session',
    copy_handoff: 'Copied Fresh Start brief',
    dismissed: 'Dismissed'
  };
  return labels[value] || value || 'unknown';
}
function renderLatestHandoffDecision(decisions) {
  const decision = (decisions || [])[0];
  if (!decision) return '<div class="empty">No Fresh Start decisions recorded yet.</div>';
  const saved = decision.expected_saved_context_label
    ? `<span class="pill">~${esc(decision.expected_saved_context_label)} expected context at risk</span>`
    : '';
  const usage = decision.next_usage
    ? `<span class="pill">${esc(decision.next_usage.tokens_label)} next-session tokens</span><span class="pill">${esc(decision.next_usage.api_value_label)} next-session value</span><span class="pill">${esc(decision.next_usage.model_calls)} model calls</span>`
    : '';
  const outcome = decision.outcome || decision.inferred_outcome
    ? `<span class="pill">${esc(decision.outcome ? `outcome: ${decision.outcome}` : `evidence: ${decision.inferred_outcome}`)}</span>`
    : '';
  const observed = decision.observed_followup
    ? `<span class="pill">${esc(decision.observed_followup.source_tokens_label)} → ${esc(decision.observed_followup.next_tokens_label)} tokens</span><span class="pill">${esc(decision.observed_followup.source_api_value_label)} → ${esc(decision.observed_followup.next_api_value_label)}</span><span class="pill">${esc(decision.observed_followup.label)}</span>`
    : '';
  const evidence = decision.proof_evidence
    ? `<span class="pill">${esc(decision.proof_evidence.label)} evidence</span><span class="pill">${esc(decision.proof_evidence.commits)} commits</span><span class="pill">${esc(decision.proof_evidence.tests)} tests</span>`
    : '';
  const observedNote = decision.observed_followup
    ? `<p class="receipt-note">${esc(decision.observed_followup.basis)}</p>`
    : `<p class="receipt-note">Proof pending means the brief was copied, but no follow-up session has been linked yet. AIWatcher records expected context at risk, not saved tokens.</p>`;
  const perCall = decision.observed_followup
    ? `<p class="receipt-note">Per model call: ${esc(decision.observed_followup.source_tokens_per_model_call_label)} tokens / ${esc(decision.observed_followup.source_cost_per_model_call_label)} source → ${esc(decision.observed_followup.next_tokens_per_model_call_label)} tokens / ${esc(decision.observed_followup.next_cost_per_model_call_label)} follow-up.</p>`
    : '';
  return `<div class="receipt-summary"><div>
    <div class="session-title">${esc(handoffDecisionLabel(decision.decision))}</div>
    <div class="session-meta">${esc(dateLabel(decision.created_at))} · source ${esc(decision.source_session_id || decision.session_id || 'unknown')}${decision.next_session_id ? ` → next ${esc(decision.next_session_id)}` : ''}</div>
    <p>${esc(decision.reason || 'AIWatcher recommended a Fresh Start because local context health crossed a threshold.')}</p>
    <p class="receipt-note"><strong>${esc(decision.proof_status || 'Proof pending')}</strong> · ${esc(decision.proof_reason || 'AIWatcher has not observed a follow-up session yet.')}</p>
    ${observedNote}
    ${perCall}
    <div class="pill-row"><span class="pill">Fresh Start receipt</span>${saved}${usage}${outcome}${observed}${evidence}</div>
  </div>${decision.next_session_id ? `<button class="btn-primary" onclick="selectSession('${esc(decision.next_session_id)}')">Inspect next session</button>` : decision.source_session_id ? `<button class="btn-quiet" onclick="selectSession('${esc(decision.source_session_id)}')">Inspect source</button>` : ''}</div>`;
}
function renderHandoffDecisionRows(decisions) {
  if (!decisions.length) return '<tr><td colspan="6"><div class="empty">No Fresh Start receipts recorded yet.</div></td></tr>';
  return decisions.map(decision => `<tr>
    <td>${esc(dateLabel(decision.created_at))}</td>
    <td>${esc(handoffDecisionLabel(decision.decision))}</td>
    <td>${esc(decision.expected_saved_context_label ? `~${decision.expected_saved_context_label}` : '—')}</td>
    <td><strong>${esc(decision.proof_status || 'Proof pending')}</strong><br><span class="sub">${esc(decision.observed_followup ? decision.observed_followup.label : decision.proof_reason || decision.outcome || decision.inferred_outcome || decision.proof_confidence || 'No saved-token claim yet')}</span>${decision.proof_evidence ? `<br><span class="sub">${esc(decision.proof_evidence.label)} · ${esc(decision.proof_evidence.commits)} commits · ${esc(decision.proof_evidence.tests)} tests</span>` : ''}</td>
    <td><span class="sub">source</span> ${esc(decision.source_session_id || decision.session_id || 'unknown')}<br><span class="sub">next</span> ${esc(decision.next_session_id || 'waiting')}</td>
    <td>${decision.next_session_id ? `<button class="row-action" onclick="selectSession('${esc(decision.next_session_id)}')">Inspect next</button>` : decision.source_session_id ? `<button class="row-action" onclick="selectSession('${esc(decision.source_session_id)}')">Inspect source</button>` : ''}</td>
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
async function requestRuntimeReturn(sessionId) {
  return fetch('/api/runtime-return', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: sessionId})
  });
}
async function returnToRuntime(sessionId) {
  try {
    const res = await requestRuntimeReturn(sessionId);
    const data = await res.json();
    if (!res.ok || data.error) {
      showToast(data.error || 'Could not find this local session.', 'error');
      return;
    }
    showToast(data.message || 'Return action requested.', data.ok ? 'success' : 'error');
  } catch (error) {
    showToast('Could not reach the local AIWatcher server.', 'error');
  }
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
  if (session.evidence_captured) return '<span class="pill outcome-pill evidence">Evidence captured</span>';
  return '';
}
function healthPill(health) {
  if (!health) return '<span class="health-pill limited">Limited data</span>';
  return `<span class="health-pill ${esc(health.tone || health.status || 'limited')}">${esc(health.label || 'Review')}</span>`;
}
function sessionStatePill(state) {
  if (!state) return '';
  return `<span class="session-state ${esc(state.status || 'unknown')}">${esc(state.label || state.status || 'unknown')}</span>`;
}
function shortSessionId(value) {
  const text = String(value || '');
  return text.length > 12 ? `${text.slice(0, 8)}...${text.slice(-4)}` : text || 'unknown';
}
function identityTone(runtime) {
  const level = (runtime || {}).identity_level || (runtime || {}).level || 'historical_log';
  if (level === 'exact_session' || level === 'active_process') return 'active';
  if (level === 'likely_workspace' || level === 'app' || level === 'workspace') return 'recent';
  return 'ended';
}
function renderIdentityStrip(item, runtime, sourcePath) {
  runtime = runtime || {};
  const sessionId = item.session_id || runtime.session_id || '';
  const project = item.project || item.project_short || runtime.project_path || 'unknown';
  const surface = runtime.surface || item.surface || 'unknown surface';
  const updated = item.updated_at || item.updated_at_label || '';
  const identityLabel = runtime.identity_label || runtime.label || 'Historical log only';
  const identityReason = runtime.identity_reason || runtime.reason || 'AIWatcher found local session evidence, but no verified exact chat attachment.';
  return `<div class="runtime-strip">
    <div class="runtime-copy">
      <strong>${esc(identityLabel)}</strong>
      <span>${esc(item.tool || runtime.tool || 'unknown tool')} · ${esc(surface)} · ${esc(project)}</span>
    </div>
    <span class="session-state ${identityTone(runtime)}">${esc(shortSessionId(sessionId))}</span>
  </div>
  <div class="runtime-source">
    <strong>Last activity</strong><span>${esc(updated ? dateLabel(updated) : 'unknown')}</span>
    <strong>Identity</strong><span>${esc(identityReason)}</span>
    ${sourcePath ? `<strong>Source log</strong><span>${esc(sourcePath)}</span>` : ''}
  </div>`;
}
function runtimeReturnPanel(runtime, sourcePath) {
  runtime = runtime || {};
  const available = !!runtime.available;
  const level = runtime.level || 'unavailable';
  const targetLabel = available
    ? (level === 'app' ? 'App return available' : level === 'active_process' ? 'Workspace return available' : 'Workspace available')
    : 'Log only';
  const reason = runtime.reason || 'AIWatcher found a local session log, but no live process, window handle, or platform deep link for this exact chat.';
  const exactLabel = runtime.exact_return_label || 'Exact chat unavailable';
  const exactReason = runtime.exact_return_reason || 'Exact chat return needs a verified app window, terminal pane, or host deep link.';
  const source = sourcePath || 'unknown';
  const action = available
    ? `<button class="btn-quiet" data-session="${esc(runtime.session_id || '')}" onclick="returnToRuntime(this.dataset.session)">${esc(runtime.action_label || 'Open workspace')}</button>`
    : `<button class="btn-quiet" disabled>No exact return</button>`;
  return `<section class="detail-section runtime-return">
    <div class="section-title">
      <div><h3>Return target</h3><p>${esc(reason)}</p></div>
      <span class="session-state ${available ? 'active' : 'ended'}">${esc(targetLabel)}</span>
    </div>
    <div class="copy-row">${action}<button class="btn-quiet" data-source="${esc(source)}" onclick="copyText(this.dataset.source, 'Session log path copied')">Copy log path</button></div>
    <div class="runtime-source"><strong>Session log</strong><span>${esc(source)}</span></div>
    <p class="tool-link-note"><strong>${esc(exactLabel)}:</strong> ${esc(exactReason)}</p>
  </section>`;
}
let watcherCommand = 'aiwatcher watch --notify --overlay --interval 60';
function renderWatcher(watcher) {
  const pill = document.getElementById('watcherPill');
  const button = document.getElementById('watcherCommandButton');
  watcherCommand = (watcher && watcher.command) || watcherCommand;
  if (watcher && watcher.running) {
    pill.className = 'cache-pill fresh';
    pill.textContent = 'Watcher running';
    button.hidden = true;
  } else {
    pill.className = 'cache-pill stale';
    pill.textContent = watcher && watcher.status === 'stale' ? 'Watcher stale' : 'Watcher stopped';
    button.hidden = false;
  }
}
function renderCacheStatus(cache) {
  const status = document.getElementById('cacheStatus');
  if (!cache) {
    status.textContent = 'Local data loaded';
    return;
  }
  const source = cache.source === 'disk' ? 'cached local index' : cache.source === 'memory' ? 'memory cache' : 'local scan';
  status.textContent = cache.refreshing
    ? `Showing ${source}; updating evidence in background...`
    : `Showing ${source}`;
}
function copyWatcherCommand() {
  copyText(watcherCommand, 'Watcher command copied — paste it in a terminal to enable ambient nudges');
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
  else if (churned) title = 'Looked useful, but its commit left the branch';
  else if (likelyUseful && highCost) title = 'Likely useful, but expensive';
  else if (likelyUseful) title = 'Likely useful, needs confirmation';
  else if (highCost) title = 'High-cost session, needs review';
  const bullets = [];
  if (churned) bullets.push('The commit this session produced is no longer reachable from HEAD -- rebased, reset or amended away. That is not the same as the work being undone: a revert would leave the commit in place and not show here.');
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
    ? 'Saved locally. Use the recommended action above if you are continuing from this session.'
    : 'Confirm the outcome, then use the expensive asks below to improve the next run.';
  return `<div class="verdict-card ${esc(verdict.tone)}"><h3>${esc(verdict.title)}</h3>
    <p>${esc(subtitle)}</p>
    <ul>${verdict.bullets.map(item => `<li>${esc(item)}</li>`).join('')}</ul>
  </div>`;
}
function splitLines(value) {
  return String(value || '').split(/\n+/).map(item => item.trim().replace(/\s+/g, ' ')).filter(Boolean).slice(0, 8);
}
function handoffOptionsFromForm(defaultType = 'coding') {
  const typeEl = document.getElementById('handoffType');
  const objectiveEl = document.getElementById('handoffObjective');
  const sourceEl = document.getElementById('handoffSources');
  const constraintEl = document.getElementById('handoffConstraints');
  const acceptanceEl = document.getElementById('handoffAcceptance');
  return {
    type: typeEl ? typeEl.value : defaultType,
    objective: objectiveEl ? objectiveEl.value.trim() : '',
    sources: splitLines(sourceEl ? sourceEl.value : ''),
    constraints: splitLines(constraintEl ? constraintEl.value : ''),
    acceptance: splitLines(acceptanceEl ? acceptanceEl.value : ''),
  };
}
function handoffPayload(sessionId, target, includePrompt, options) {
  const next = options || {};
  return {
    session_id: sessionId || '',
    target: target || 'generic',
    prompt: !!includePrompt,
    type: next.type || 'coding',
    objective: next.objective || '',
    source_refs: next.sources || [],
    constraints: next.constraints || [],
    acceptance_criteria: next.acceptance || [],
  };
}
async function postJson(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  return res.json();
}
function renderHandoffForm(capsule) {
  const selected = capsule.handoff_type || 'coding';
  const sourceRefs = (capsule.source_refs || []).join('\n');
  const constraints = (capsule.constraints || []).join('\n');
  const acceptance = (capsule.acceptance_criteria || []).join('\n');
  return `<div class="handoff-form">
    <div class="handoff-form-head">
      <div>
        <h3>Shape the next session</h3>
        <p>Keep this brief specific enough that the new chat can continue from evidence, not from hidden memory.</p>
      </div>
      <span class="pill">${esc(capsule.demo ? 'sample data' : 'local only')}</span>
    </div>
    <div class="handoff-form-grid">
      <label><span class="label">Work type</span><select id="handoffType">${HANDOFF_TYPES.map(item => `<option value="${esc(item.id)}" ${item.id === selected ? 'selected' : ''}>${esc(item.label)}</option>`).join('')}</select></label>
      <label><span class="label">Objective</span><input id="handoffObjective" value="${esc(capsule.objective || '')}" placeholder="What should the next chat accomplish?"></label>
      <label><span class="label">Source of truth</span><textarea id="handoffSources" placeholder="One file, PR, issue, or local state per line">${esc(sourceRefs)}</textarea></label>
      <label><span class="label">Constraints</span><textarea id="handoffConstraints" placeholder="Scope, privacy, files not to touch, decisions already made">${esc(constraints)}</textarea></label>
      <label><span class="label">Acceptance checks</span><textarea id="handoffAcceptance" placeholder="How should the next chat know it is done?">${esc(acceptance)}</textarea></label>
    </div>
    <div class="copy-row"><button class="btn-quiet" onclick="regenerateHandoff('${esc(capsule.session_id)}','${esc(capsule.target || 'generic')}', ${capsule.include_prompt_excerpt ? 'true' : 'false'}, ${capsule.demo ? 'true' : 'false'})">Regenerate brief</button></div>
  </div>`;
}
function renderHandoff(capsule) {
  const usage = capsule.usage || {};
  const evidence = capsule.evidence || {};
  const changedFiles = evidence.changed_files || [];
  const runtime = capsule.runtime_attachment || {};
  const target = capsule.target || 'generic';
  const includePrompt = !!capsule.include_prompt_excerpt;
  const isDemo = !!capsule.demo;
  const canOpenRuntime = !!runtime.available && runtime.level !== 'app';
  const enrichment = capsule.basic
    ? '<div class="loading">Basic brief is ready. Loading timeline, git evidence, and prompt enrichment...</div>'
    : '';
  const primaryLabel = canOpenRuntime ? 'Copy brief + open workspace' : 'Copy brief';
  const primaryHelp = canOpenRuntime
    ? 'AIWatcher will copy the brief, open the safest available workspace or app target, and save a local Fresh Start receipt.'
    : 'AIWatcher will copy the brief and save a local Fresh Start receipt. Open the correct AI chat or workspace yourself before pasting.';
  return `<section class="detail-section">
    <h2>Fresh Start</h2>
    <p>AIWatcher prepared a restart brief for the next ${esc(capsule.target_label || 'AI tool')} session. Copy it into a fresh chat only after the identity below matches the work you intend to continue.</p>
    ${renderIdentityStrip(capsule, runtime, capsule.source_path)}
    <div class="mini-grid">
      <div class="mini"><span class="label">Previous usage</span><strong>${esc(usage.tokens_label || '—')}</strong></div>
      <div class="mini"><span class="label">API value</span><strong>${esc(usage.api_value_label || '—')}</strong></div>
      <div class="mini"><span class="label">Model calls</span><strong>${esc(usage.model_calls ?? '—')}</strong></div>
      <div class="mini"><span class="label">Evidence</span><strong>${esc((evidence.commits || []).length)} commits</strong></div>
    </div>
    <div id="handoffStatus" class="verdict-card useful" style="margin-top:14px">
      <h3>Best next action: start fresh in the same workspace</h3>
      <p>${esc(primaryHelp)} AIWatcher will watch for a later same-project session as proof.</p>
      <div class="copy-row" style="margin-top:12px">
        <button class="btn-primary" data-runtime="${canOpenRuntime ? '1' : '0'}" onclick="copyFreshStartFromDrawer('${esc(capsule.session_id)}', this.dataset.runtime === '1', ${isDemo ? 'true' : 'false'})">${esc(isDemo ? 'Copy demo brief' : primaryLabel)}</button>
        ${isDemo ? `<button class="btn-quiet" onclick="showView('sessions'); closeDrawer()">Find real sessions</button>` : `<button class="btn-quiet" onclick="selectSession('${esc(capsule.session_id)}')">Inspect source session</button>`}
      </div>
    </div>
    ${renderHandoffForm(capsule)}
    <div class="copy-row">
      <span class="label" style="align-self:center">Format for</span>
      ${['generic','claude','codex','cursor','vscode'].map(item => `<button class="btn-quiet" aria-pressed="${item === target ? 'true' : 'false'}" onclick="regenerateHandoff('${esc(capsule.session_id)}','${item}', ${includePrompt}, ${isDemo ? 'true' : 'false'})">${esc(item === 'generic' ? 'Generic' : item)}</button>`).join('')}
    </div>
    <p class="tool-link-note">${esc(runtime.reason || 'Use the Fresh Start brief when the exact running chat cannot be reopened.')}</p>
    ${enrichment}
    <label class="prompt-opt-in">
      <input type="checkbox" ${includePrompt ? 'checked' : ''} onchange="regenerateHandoff('${esc(capsule.session_id)}','${target}', this.checked, ${isDemo ? 'true' : 'false'})">
      <span class="prompt-opt-in-label">Include prompt excerpt <span class="pill">Privacy opt-in</span></span>
      <span class="hint">Off by default: everything else in this brief is metadata (counts, hashes, file paths). This adds your actual prompt text from the costliest turn, so review it before pasting into another tool.</span>
    </label>
  </section>
  ${runtimeReturnPanel(runtime, capsule.source_path)}
  <section class="detail-section"><h3>Why start fresh now</h3>
    <ul class="insight-list">${(capsule.warnings || []).map(item => `<li>${esc(item)}</li>`).join('')}</ul>
  </section>
  <section class="detail-section"><h3>Brief that will be copied</h3>
    <textarea id="handoffBrief" class="brief-box">${esc(capsule.next_brief || '')}</textarea>
    <div class="copy-row"><button class="btn-quiet" onclick="copyText(document.getElementById('handoffBrief').value, 'Fresh Start brief copied')">Copy brief only</button></div>
    ${changedFiles.length ? `<details class="aiw-details"><summary>${esc(changedFiles.length)} changed file${changedFiles.length === 1 ? '' : 's'} to inspect</summary><div class="details-body"><div class="pill-row">${changedFiles.slice(0, 12).map(file => `<span class="pill">${esc(file)}</span>`).join('')}</div></div></details>` : ''}
  </section>`;
}
async function copyFreshStartFromDrawer(sessionId, openRuntime = false, isDemo = false) {
  const brief = document.getElementById('handoffBrief') ? document.getElementById('handoffBrief').value : '';
  const copied = await copyText(brief, 'Fresh Start brief copied');
  if (!copied) return;
  if (!isDemo) {
    await fetch('/api/handoff-decision', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sessionId,
        decision: 'copy_handoff',
        reason: 'Fresh Start brief copied from the session drawer.',
        action_channel: 'dashboard_session',
      })
    }).catch(() => {});
  }
  let message = isDemo
    ? 'Demo brief copied. In a live session AIWatcher would save a local Fresh Start receipt and watch for follow-up proof.'
    : 'Fresh Start receipt saved. Open a fresh chat in the same workspace and paste the brief.';
  if (openRuntime) {
    try {
      const returnRes = await requestRuntimeReturn(sessionId);
      const returned = await returnRes.json();
      if (returned && returned.ok) message = `${returned.message || 'Workspace opened.'} Paste the copied brief into a fresh chat.`;
    } catch (error) {}
  }
  const status = document.getElementById('handoffStatus');
  if (status) {
    status.innerHTML = `<h3>${esc(isDemo ? 'Demo Fresh Start copied' : 'Fresh Start ready')}</h3><p>${esc(message)}${isDemo ? '' : ' AIWatcher will link the next same-project session when it appears.'}</p>
      <div class="copy-row" style="margin-top:12px">${isDemo ? '<button class="btn-primary" onclick="showView(\'sessions\'); closeDrawer()">Find real sessions</button>' : '<button class="btn-primary" onclick="showView(\'receipts\'); closeDrawer()">View receipt</button>'}<button class="btn-quiet" onclick="closeDrawer()">Done</button></div>`;
  }
}
async function regenerateHandoff(sessionId, target = 'generic', includePrompt = false, isDemo = false) {
  const options = handoffOptionsFromForm(isDemo ? 'product' : 'coding');
  if (isDemo) {
    await openDemoHandoff(target, includePrompt, options);
  } else {
    await openHandoff(sessionId, target, includePrompt, options);
  }
}
async function openDemoHandoff(target = 'generic', includePrompt = false, options = null) {
  openDrawer('Fresh Start');
  const node = document.getElementById('detailContent');
  node.innerHTML = '<div class="loading">Building Fresh Start demo from sample context pressure...</div>';
  const demoOptions = options || {
    type: 'product',
    objective: 'Continue the work in a fresh session without losing decisions, constraints, or acceptance criteria.',
    sources: ['Current repo state', 'Strategy or spec document', 'Relevant PR or issue'],
    constraints: ['Do not assume access to the previous chat.', 'Do not broaden scope beyond the next checkpoint.', 'Preserve unrelated local changes and privacy boundaries.'],
    acceptance: ['First reply states what appears done, what remains uncertain, and the smallest next checkpoint.', 'The next session loads source-of-truth files before editing.', 'The result reports verification and remaining uncertainty.'],
  };
  const payload = handoffPayload('', target, includePrompt, demoOptions);
  const capsule = await postJson('/api/handoff-demo', payload);
  if (capsule.error) {
    node.innerHTML = `<div class="empty">${esc(capsule.error)}</div>`;
    return;
  }
  node.innerHTML = renderHandoff(capsule);
}
async function openHandoff(sessionId, target = 'generic', includePrompt = false, options = null) {
  openDrawer('Fresh Start');
  const node = document.getElementById('detailContent');
  node.innerHTML = '<div class="loading">Finding the source session before building the Fresh Start brief...</div>';
  const handoffOptions = options || handoffOptionsFromForm();
  const payload = handoffPayload(sessionId, target, includePrompt, handoffOptions);
  const summaryPromise = fetch(`/api/session-summary?id=${encodeURIComponent(sessionId)}`)
    .then(res => res.json())
    .catch(() => null);
  const basicPromise = postJson('/api/handoff-basic', payload)
    .catch(() => null);
  const handoffPromise = postJson('/api/handoff', payload);
  const fastSummary = await summaryPromise;
  if (fastSummary && !fastSummary.error) {
    node.innerHTML = renderSessionSummary(fastSummary, 'Building Fresh Start brief...');
  } else {
    node.innerHTML = '<div class="loading">Building local Fresh Start brief...</div>';
  }
  const basicCapsule = await basicPromise;
  if (basicCapsule && !basicCapsule.error && !includePrompt) {
    node.innerHTML = renderHandoff(basicCapsule);
  }
  const capsule = await handoffPromise;
  if (capsule.error) {
    node.innerHTML = `<div class="empty">${esc(capsule.error)}</div>`;
    return;
  }
  node.innerHTML = renderHandoff(capsule);
}
async function copyHandoffFromBubble(sessionId) {
  const res = await fetch(`/api/handoff-basic?id=${encodeURIComponent(sessionId)}&target=generic`);
  const capsule = await res.json();
  if (capsule.error) {
    showToast(capsule.error, 'error');
    return;
  }
  const copied = await copyText(capsule.next_brief || '', 'Fresh Start brief copied — paste it into a fresh AI chat');
  if (copied) {
    if (window.currentHandoffBubble) await recordHandoffDecision(window.currentHandoffBubble, 'copy_handoff');
    renderHandoffCopied(window.currentHandoffBubble, sessionId);
  }
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
        action_channel: 'dashboard',
      })
    });
  } catch (error) {
    // Decision receipts should never block the user's flow.
  }
}
async function startFreshFromBubble(sessionId) {
  const res = await fetch(`/api/handoff-basic?id=${encodeURIComponent(sessionId)}&target=generic`);
  const capsule = await res.json();
  if (capsule.error) {
    showToast(capsule.error, 'error');
    return;
  }
  const copied = await copyText(capsule.next_brief || '', 'Fresh Start brief copied');
  if (!copied) return;
  if (window.currentHandoffBubble) await recordHandoffDecision(window.currentHandoffBubble, 'copy_handoff');
  const runtime = (window.currentHandoffBubble || {}).runtime_attachment || {};
  if (runtime.available && runtime.level !== 'app') {
    try {
      const returnRes = await requestRuntimeReturn(sessionId);
      const returned = await returnRes.json();
      showToast(returned.message || 'Brief copied and return target opened.', returned.ok ? 'success' : 'error');
    } catch (error) {
      showToast('Brief copied. Open a fresh chat in the same workspace and paste it.', 'success');
    }
  } else {
    showToast('Brief copied. Open a fresh chat in the same workspace and paste it.', 'success');
  }
  renderHandoffCopied(window.currentHandoffBubble, sessionId);
}
async function continueFromBubble() {
  if (window.currentHandoffBubble) await recordHandoffDecision(window.currentHandoffBubble, 'continue_here');
  document.getElementById('handoffBubble').hidden = true;
  showToast('Fresh Start decision saved: continue here');
}
function renderHandoffCopied(bubble, sessionId) {
  const node = document.getElementById('handoffBubble');
  if (!node || !bubble) return;
  node.hidden = false;
  node.innerHTML = `<div class="section-title">
      <div>
        <h2>Fresh Start ready</h2>
        <p>Paste the copied brief into a fresh AI chat for the matching workspace. AIWatcher saved a local receipt and will watch for follow-up proof.</p>
      </div>
      <span class="pill">saved</span>
    </div>
    <div class="pill-row">
      <span class="pill">${esc(bubble.expected_saved_context_label ? '~' + bubble.expected_saved_context_label + ' expected context at risk' : 'proof pending')}</span>
      <span class="pill">privacy-safe metadata</span>
      <span class="pill">Fresh Start receipt saved</span>
    </div>
    <div class="actions" style="margin-top:14px">
      <button class="btn-primary" onclick="showView('receipts')">View receipt</button>
      <button class="btn-quiet" data-session="${esc(sessionId)}" onclick="openHandoff(this.dataset.session)">Review brief</button>
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
  const runtime = bubble.runtime_attachment || {};
  node.hidden = false;
  node.innerHTML = `<div class="section-title">
      <div>
        <h2>${esc(bubble.title)}</h2>
        <p>${esc(bubble.body)}</p>
      </div>
      <span class="pill">${esc(bubble.severity)}</span>
    </div>
    ${renderIdentityStrip(bubble, runtime, bubble.source_path)}
    <div class="pill-row">${(bubble.tags || []).map(tag => `<span class="pill">${esc(tag)}</span>`).join('')}</div>
    <div class="actions" style="margin-top:14px">
      <button class="btn-primary" data-session="${esc(bubble.session_id)}" onclick="startFreshFromBubble(this.dataset.session)">${esc(bubble.primary_label || 'Copy Fresh Start brief')}</button>
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
  const status = arguments.length > 1 ? arguments[1] : 'ready';
  if (status === 'pending') return '<div class="loading">Checking context health and handoff opportunities...</div>';
  if (!rows.length) return '<div class="empty">No active context-health warnings. AIWatcher will surface bloat, stale sessions, and handoff opportunities here.</div>';
  return `<div class="coverage-grid">${rows.map(row => `<div class="health-card">
    <div class="health-head">
      <div><h3>${esc(row.project)}</h3><p>${esc(row.tool)} · ${esc(row.age_label)}${row.session_count > 1 ? ` · ${esc(row.session_count)} sessions` : ''}</p></div>
      <span class="health-severity ${esc(row.severity)}">${esc(row.severity)}</span>
    </div>
    <div class="mini-grid">
      <div class="mini"><span class="label">Latest turn</span><strong>${esc(row.latest_turn_tokens)}</strong></div>
      <div class="mini"><span class="label">Peak turn</span><strong>${esc(row.peak_turn_tokens)}</strong></div>
      <div class="mini"><span class="label">Spend on replay</span><strong>${esc(row.bloat_label)}</strong></div>
      <div class="mini"><span class="label">Replay cost</span><strong>${esc(row.replayed_cost_label)}</strong></div>
    </div>
    ${row.session_count > 1 ? `<p class="receipt-note">${esc(row.group_note || `${row.session_count} related sessions need attention.`)} ${row.critical_sessions ? `${esc(row.critical_sessions)} critical.` : ''}</p>` : ''}
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
  const modeCopy = {
    automatic: 'Protected automatically when the tool invokes its hook.',
    companion: 'Companion/manual protection. AIWatcher can help, but it is not intercepting this surface directly.',
    limited: 'Partial coverage. Treat findings as local evidence, not full control.',
    unverified: 'Not verified on this machine yet. Run the suggested check before trusting protection claims.',
    not_detected: 'Tool not detected. Install or open the tool, then refresh coverage.',
    unsupported: 'No direct hook known. Use Prompt Companion or history-only review.',
  };
  return rows.map(row => `<div class="coverage-card">
    <div class="coverage-head">
      <h3>${esc(row.label)}</h3>
      <span class="coverage-status ${esc(row.status)}">${esc(row.status_label)}</span>
    </div>
    <div class="coverage-detail">
      <div><strong>Protection:</strong> ${esc(modeCopy[row.status] || 'Local evidence only until verified.')}</div>
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
      <div><strong>Verify:</strong> run this, then refresh Settings. If no recent hook event appears, that surface is companion/history-only until proven otherwise.</div>
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
      <div class="bar-label">${esc(row.short_name || row.name)}${kind === "project" && row.health ? ` ${healthPill(row.health)}` : ''}</div>
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
    <div class="verdict-card ${data.health && data.health.status === 'critical' ? 'high' : ''}">
      <h3>${healthPill(data.health)} ${esc(data.health ? data.health.reason : 'Review recent local sessions before optimizing.')}</h3>
      <p>The right next action is to review the sessions driving this project, then mark outcomes or create a handoff before continuing broad work.</p>
    </div>
    </section><section class="detail-section"><h3>Models used</h3>
    ${bars(data.models, "api_value_label", "model")}
    </section><section class="detail-section"><h3>Tools used</h3>
    ${bars(data.tools, "api_value_label", "tool")}
    </section><section class="detail-section"><h3>Recent sessions</h3>
    <div class="table-wrap"><table><thead><tr><th>Tool</th><th>Model</th><th>Status</th><th>Tokens</th><th></th></tr></thead>
      <tbody>${data.sessions.map(s => `<tr class="clickable" onclick="selectSession('${s.session_id}')">
        <td>${esc(s.tool)}</td><td>${esc(s.model)}</td><td>${sessionStatePill(s.state)} ${outcomeEvidencePill(s)}</td><td>${esc(s.tokens_label)}</td><td><button class="row-action">Review</button></td>
      </tr>`).join('')}</tbody></table></div></section>`;
}
function renderSessionSummary(s, label = 'Loading detailed evidence...') {
  const actions = s.actions || [];
  const action = actions.find(item => item.primary) || actions[0] || null;
  const actionButton = action
    ? action.id === 'handoff' && action.label !== 'Inspect evidence'
      ? `<button class="btn-primary" onclick="openHandoff('${esc(s.session_id)}')">${esc(action.label || 'Open Fresh Start')}</button>`
      : action.id === 'review_outcome'
        ? `<button class="btn-primary" disabled>${esc(action.label || 'Review outcome')}</button>`
        : `<button class="btn-primary" disabled>${esc(action.label || 'Review session')}</button>`
    : '';
  return `<section class="detail-section">
    <h2 class="session-title">${esc(s.project_short || s.project || 'Session')}</h2>
    <p class="session-meta">${esc(s.tool || 'unknown tool')} · ${esc(s.model || 'unknown model')}</p>
    ${renderIdentityStrip(s, s.runtime_attachment, s.source_path)}
    ${miniStats({ sessions: 1, api_value: s.api_value || '—', tokens: s.tokens_label || '—', tool_calls: s.tool_calls || 0 })}
    <div class="pill-row">${sessionStatePill(s.state)}${outcomePill(s.outcome)}</div>
  </section>
  <section class="detail-section recommended-action">
    <div class="section-title">
      <div><h3>${esc(action ? action.label : 'Review session')}</h3><p>${esc(action ? action.reason : 'AIWatcher is loading full local evidence for this session.')}</p></div>
      <span class="session-state recent">loading</span>
    </div>
    <div class="copy-row">${actionButton}<button class="btn-quiet" disabled>${esc(label)}</button></div>
  </section>`;
}
function renderSessionActions(s) {
  const actions = s.actions || [];
  const handoffAction = actions.find(action => action.id === 'handoff') || null;
  const hasHandoff = !!handoffAction;
  const needsOutcome = actions.some(action => action.id === 'review_outcome');
  const primaryId = (actions.find(action => action.primary) || {}).id || (needsOutcome ? 'review_outcome' : hasHandoff ? 'handoff' : 'optimize_next_prompt');
  const runtime = s.runtime_attachment || actions.find(action => action.id === 'open_tool') || {};
  const openAction = actions.find(action => action.id === 'open_tool') || {};
  const title = hasHandoff
    ? 'Recommended: continue in a fresh session'
    : needsOutcome
      ? 'Recommended: mark the outcome'
      : 'Recommended: tighten the next prompt';
  const body = hasHandoff
    ? 'This session has enough context, model calls, or tool calls that a Fresh Start brief is safer than replaying the whole history.'
    : needsOutcome
      ? 'Mark whether this worked so AIWatcher can measure value per useful change instead of only tokens.'
      : 'Use this session as evidence, then preflight the next prompt before sending it.';
  const openToolNote = runtime.reason || openAction.reason || 'No safe return target is available for this session yet.';
  const openButton = openAction.available
    ? `<button class="btn-quiet" onclick="returnToRuntime('${esc(s.session_id)}')" title="${esc(openToolNote)}">${esc(openAction.label || runtime.action_label || 'Open workspace')}</button>`
    : `<button class="btn-quiet" disabled title="${esc(openToolNote)}">${esc(openAction.label || runtime.action_label || 'No live return')}</button>`;
  return `<section class="detail-section recommended-action"><div class="action-kicker">${sessionStatePill(s.state)}${outcomePill(s.outcome)}</div>
    <h3>${esc(title)}</h3>
    <p>${esc(body)}</p>
    <div class="copy-row">
      ${hasHandoff ? `<button class="${primaryId === 'handoff' ? 'btn-primary' : 'btn-quiet'}" onclick="${handoffAction.label === 'Inspect evidence' ? "document.getElementById('evidencePanel').scrollIntoView({ behavior: 'smooth', block: 'center' })" : `openHandoff('${esc(s.session_id)}')`}">${esc(handoffAction.label || 'Open Fresh Start')}</button>` : ''}
      ${needsOutcome ? `<button class="${primaryId === 'review_outcome' ? 'btn-primary' : 'btn-quiet'}" onclick="document.getElementById('outcomePanel').scrollIntoView({ behavior: 'smooth', block: 'center' })">Mark outcome</button>` : ''}
      <button class="${primaryId === 'optimize_next_prompt' ? 'btn-primary' : 'btn-quiet'}" onclick="showView('prompt'); closeDrawer(); document.getElementById('promptInput').focus(); showToast('Paste the next prompt here to optimize it before sending')">Optimize next prompt</button>
      ${openButton}
    </div>
    <p class="tool-link-note">${esc(openToolNote)}</p>
  </section>`;
}
async function selectSession(sessionId, attempt = 0) {
  openDrawer('Session review');
  document.getElementById('drawerTitle').textContent = 'Session review';
  const node = document.getElementById('detailContent');
  node.innerHTML = `<div class="loading">Loading session identity for ${esc(sessionId)}...</div>`;
  const summaryPromise = fetch(`/api/session-summary?id=${encodeURIComponent(sessionId)}`)
    .then(res => res.json())
    .catch(() => null);
  const detailPromise = fetch(`/api/session?id=${encodeURIComponent(sessionId)}`);
  const fastSummary = await summaryPromise;
  if (fastSummary && !fastSummary.error) {
    node.innerHTML = renderSessionSummary(fastSummary);
  } else {
    node.innerHTML = `<div class="loading">Loading session details for ${esc(sessionId)}...</div>`;
  }
  const res = await detailPromise;
  const s = await res.json();
  if (s.error) {
    if (attempt < 5) {
      node.innerHTML = `<div class="loading">Still indexing this session. Retrying...</div>`;
      window.setTimeout(() => selectSession(sessionId, attempt + 1), 1400);
      return;
    }
    node.innerHTML = `<div class="empty">${esc(s.error)}</div>`;
    return;
  }
  if (s.detail_pending) {
    if (!fastSummary || fastSummary.error) node.innerHTML = renderSessionSummary(s);
    const pending = document.createElement('div');
    pending.className = 'loading';
    pending.textContent = s.detail_message || 'Timeline and evidence are still indexing in the background.';
    node.appendChild(pending);
    if (attempt < 8) window.setTimeout(() => selectSession(sessionId, attempt + 1), 1400);
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
  const outcomeButtons = `<div class="outcome-options">
      <button data-testid="outcome-useful" class="outcome-button useful ${s.outcome === 'useful' ? 'selected' : ''}" onclick="markOutcome('${esc(s.session_id)}','useful')">${s.outcome === 'useful' ? '✓ ' : ''}Useful</button>
      <button data-testid="outcome-rework" class="outcome-button rework ${s.outcome === 'rework' ? 'selected' : ''}" onclick="markOutcome('${esc(s.session_id)}','rework')">${s.outcome === 'rework' ? '✓ ' : ''}Needs rework</button>
      <button data-testid="outcome-abandoned" class="outcome-button abandoned ${s.outcome === 'abandoned' ? 'selected' : ''}" onclick="markOutcome('${esc(s.session_id)}','abandoned')">${s.outcome === 'abandoned' ? '✓ ' : ''}Abandoned</button>
    </div>`;
  const outcomeActions = s.outcome
    ? `<details id="outcomePanel" class="aiw-details outcome-control"><summary>Outcome marked: ${esc(s.outcome)} · change if needed</summary><div class="details-body">
        <p>Changing this updates local value metrics and future cost-per-useful-change calculations.</p>
        ${outcomeButtons}
      </div></details>`
    : `<div id="outcomePanel" class="outcome-control"><h3>Was this work useful?</h3>
        <p>Mark the result so AIWatcher can measure value instead of tokens alone.</p>
        ${outcomeButtons}
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
    ${renderIdentityStrip(s, s.runtime_attachment, s.source_path)}
    ${miniStats({ sessions: 1, api_value: s.api_value, tokens: s.tokens_label, tool_calls: s.tool_calls })}
    <div class="pill-row">${sessionStatePill(s.state)}${outcomePill(s.outcome)}</div>
    </section>
    ${runtimeReturnPanel(s.runtime_attachment, s.source_path)}
    ${renderSessionActions(s)}
    ${outcomeActions}
    ${renderVerdict(s)}
    ${promptReview}
    <div id="evidencePanel">${renderEvidence(s.outcome_evidence)}</div>
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
    showToast(`Outcome saved: ${labels[outcome]}. Continue only if there is a clear next checkpoint.`);
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
let sessionsLoadedForDays = null;
let reportLoadedForDays = null;
let reportLoading = false;
function showView(view) {
  document.querySelectorAll('.view').forEach(node => {
    node.hidden = node.id !== `view-${view}`;
  });
  const activeView = ({ projects: 'sessions', changes: 'sessions', coverage: 'setup' })[view] || view;
  document.querySelectorAll('.nav-tab').forEach(node => {
    node.classList.toggle('active', node.dataset.view === activeView);
  });
  const days = document.getElementById('days').value;
  if (view === 'sessions' && sessionsLoadedForDays !== days) loadSessions();
  if (view === 'insights' && reportLoadedForDays !== days) loadReport();
}
function changeWindow() {
  sessionsLoadedForDays = null;
  reportLoadedForDays = null;
  load();
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
        <td>${esc(s.project)}<br>${sessionStatePill(s.state)} ${s.outcome ? outcomePill(s.outcome) : outcomeEvidencePill(s)}</td>
        <td>${esc(s.model)}</td>
        <td class="mono">${esc(s.tokens)}</td>
        <td><button class="row-action">Review</button></td>
      </tr>`).join('')
    : `<tr><td colspan="5"><div class="empty">${filtered
        ? 'No sessions match those filters. Try clearing the search or a different outcome/evidence filter.'
        : 'No local sessions found for this window.'}</div></td></tr>`;
  sessionsLoadedForDays = days;
}
async function loadReport() {
  const days = document.getElementById('days').value;
  if (reportLoading || reportLoadedForDays === days) return;
  reportLoading = true;
  document.getElementById('report').innerHTML = '<div class="loading">Building local spend and outcome evidence...</div>';
  try {
    const reportRes = await fetch(`/api/report?days=${days}`);
    const report = await reportRes.json();
    if (document.getElementById('days').value !== days) return;
    document.getElementById('todayDigest').innerHTML = renderTodayDigest(report.digest);
    document.getElementById('report').innerHTML = renderReport(report);
    reportLoadedForDays = days;
  } catch (error) {
    document.getElementById('report').innerHTML = '<div class="empty">Spend evidence is still building. Try again in a moment.</div>';
  } finally {
    reportLoading = false;
  }
}
async function load(resetDetail = true, forceRefresh = false) {
  const days = document.getElementById('days').value;
  const refreshButton = document.getElementById('refreshButton');
  const previousRefreshText = refreshButton ? refreshButton.textContent : '';
  if (refreshButton) {
    refreshButton.disabled = true;
    refreshButton.textContent = forceRefresh ? 'Refreshing...' : 'Updating...';
  }
  let data;
  try {
    const summaryRes = await fetch(`/api/summary?days=${days}${forceRefresh ? '&refresh=1' : ''}`);
    data = await summaryRes.json();
  } catch (error) {
    showToast('Could not load local AIWatcher data.', 'error');
    if (refreshButton) {
      refreshButton.disabled = false;
      refreshButton.textContent = previousRefreshText || 'Refresh data';
    }
    return;
  }
  renderWatcher(data.watcher || null);
  renderCacheStatus(data.cache || null);
  renderHandoffBubble(data.handoff_bubble || null);
  document.getElementById('actionQueue').innerHTML = buildActionQueue(data);
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
  document.getElementById('contextHealth').innerHTML = renderContextHealth(data.context_health || [], data.context_health_status || 'ready');
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
       <div class="session-actions">${sessionStatePill(latest.state)}${outcomePill(latest.outcome)}${outcomeEvidencePill(latest)}
       <button data-testid="review-latest" class="btn-primary" onclick="selectSession('${esc(latest.session_id)}')">Review outcome</button></div></div>`
    : '<div class="empty">No local AI session detected yet.</div>';
  const recommendation = data.insights[0];
  document.getElementById('todayRecommendation').innerHTML = recommendation
    ? `<div class="insight"><strong>${esc(recommendation.title)}</strong><p>${esc(recommendation.body)}</p></div>`
    : '<div class="empty">Nothing unusual yet. Keep the next task scoped and define a stop condition.</div>';
  document.getElementById('projects').innerHTML = bars(data.projects, "api_value_label", "project");
  document.getElementById('models').innerHTML = bars(data.models, "api_value_label", "model");
  document.getElementById('tools').innerHTML = bars(data.tools, "api_value_label", "tool");
  document.getElementById('insights').innerHTML = data.insights.length
    ? data.insights.map(i => `<div class="insight"><strong>${esc(i.title)}</strong><p>${esc(i.body)}</p></div>`).join('')
    : '<div class="empty">No notable local signals yet.</div>';
  document.getElementById('privacy').innerHTML = data.privacy.map(p => `<div class="privacy-item"><span class="privacy-check">&#10003;</span><span>${esc(p)}</span></div>`).join('');
  document.getElementById('recent').innerHTML = data.recent_sessions.slice(0, 6).map(s => `<tr class="clickable" onclick="selectSession('${s.session_id}')">
    <td>${esc(s.tool)}</td><td>${esc(s.project)}<br>${sessionStatePill(s.state)} ${outcomeEvidencePill(s)}</td><td>${esc(s.tokens)}</td><td><button class="row-action">Review</button></td>
  </tr>`).join('');
  document.getElementById('projectWindow').textContent = totals.window_label;
  document.getElementById('projectRows').innerHTML = data.projects.length
    ? data.projects.map(p => `<tr class="clickable" onclick="selectProject(decodeURIComponent(this.dataset.id))" data-id="${encodeURIComponent(p.id)}">
        <td>${esc(p.short_name || p.name)}</td>
        <td>${healthPill(p.health)}</td>
        <td class="mono">${esc(p.sessions)}</td>
        <td class="mono">${esc(p.tokens_label)}</td>
        <td class="mono">${esc(p.calls)}</td>
        <td class="mono">${esc(p.api_value_label)}</td>
      </tr>`).join('')
    : '<tr><td colspan="6"><div class="empty">No local project usage found for this window.</div></td></tr>';
  document.getElementById('insightHeadline').innerHTML = renderInsightHeadline(data.totals);
  document.getElementById('insightFeed').innerHTML = renderInsightFeed(data.insights);
  if (reportLoadedForDays !== days) {
    document.getElementById('todayDigest').innerHTML = '<div class="empty">Open Spend for the evidence-backed weekly digest.</div>';
  }
  if (refreshButton) {
    refreshButton.disabled = false;
    refreshButton.textContent = previousRefreshText || 'Refresh data';
  }
  if (resetDetail && document.getElementById('detailDrawer').classList.contains('open')) closeDrawer();
  if (data.cache && data.cache.refreshing && !forceRefresh) {
    window.setTimeout(() => load(false, false), 1800);
  }
}
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeDrawer(); });
(async () => {
  await load();
  const requestedView = new URLSearchParams(location.search).get('view');
  if (requestedView && ['today','prompt','sessions','projects','changes','receipts','insights','setup','coverage'].includes(requestedView)) {
    showView(requestedView);
    if (requestedView === 'prompt') {
      document.getElementById('promptInput').focus();
    }
  }
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
  <title>AIWatcher Fresh Start</title>
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
    .identity {
      border: 1px solid rgba(126, 172, 255, 0.22);
      background: rgba(255, 255, 255, 0.04);
      border-radius: 12px;
      padding: 10px 12px;
      margin-bottom: 14px;
      color: #d9e5f7;
      font-size: 13px;
      line-height: 1.45;
    }
    .identity strong { color: #ffffff; }
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
    <div class="empty">Loading AIWatcher Fresh Start recommendation...</div>
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
    return true;
  } catch (error) {
    renderSaved('Copy failed. Open dashboard and copy from the Fresh Start drawer.');
    return false;
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
async function recordAmbientAction(action, snoozeMinutes = null) {
  const fingerprint = queryParam('intervention');
  if (!fingerprint) return;
  const payload = { fingerprint, action, channel: 'overlay' };
  if (snoozeMinutes) payload.snooze_minutes = snoozeMinutes;
  try {
    await fetch('/api/ambient-intervention-action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (error) {}
}
async function loadAmbientIntervention() {
  const fingerprint = queryParam('intervention');
  if (!fingerprint) return null;
  try {
    const res = await fetch(`/api/ambient-intervention?id=${encodeURIComponent(fingerprint)}`);
    if (!res.ok) return null;
    return await res.json();
  } catch (error) {
    return null;
  }
}
function renderSaved(message) {
  document.getElementById('bubble').innerHTML = `<div class="top"><div><h1>${esc(message)}</h1><p>You can close this AIWatcher companion and return to your AI tool.</p></div><span class="badge">saved</span></div>
    <div class="body"><div class="actions"><button class="primary" onclick="window.close()">Close</button><a href="/">Open dashboard</a></div></div>`;
}
async function copyHandoff(bubble, decision) {
  const res = await fetch(`/api/handoff-basic?id=${encodeURIComponent(bubble.session_id)}&target=generic`);
  const capsule = await res.json();
  if (capsule.error) {
    renderSaved(capsule.error);
    return;
  }
  const copied = await copyText(capsule.next_brief || '', 'Fresh Start brief copied');
  if (!copied) return;
  await recordDecision(decision, bubble);
  await recordAmbientAction('acted');
}
async function copyFocusedBrief(bubble, intervention) {
  const brief = `AIWatcher focused continuation

Workspace: ${bubble.project || 'current workspace'}
Observed signal: ${intervention.reason || bubble.reason || 'Execution pressure is elevated.'}

Before using more tools:
- Restate the exact outcome for this checkpoint in one sentence.
- Summarize what is already known and avoid repeating broad discovery.
- Inspect only the smallest relevant files or commands.
- Stop and ask before destructive changes or unrelated cleanup.

Continue only the smallest checkpoint, run the narrowest useful verification, and report what remains.`;
  await recordAmbientAction('acted');
  await copyText(brief, 'Focused next step copied');
}
async function inspectIntervention(bubble) {
  await recordAmbientAction('acted');
  window.location.href = `/?session=${encodeURIComponent(bubble.session_id || '')}`;
}
async function snoozeIntervention() {
  await recordAmbientAction('snooze', 15);
  renderSaved('Snoozed for 15 minutes');
}
async function dismissIntervention() {
  await recordAmbientAction('dismiss');
  renderSaved('Dismissed for this session state');
}
function interventionPresentation(bubble, intervention) {
  if (!intervention) {
    return {
      title: bubble.title,
      body: bubble.body,
      severity: bubble.severity,
      primaryLabel: 'Copy Fresh Start brief',
      primaryMode: 'fresh_chat',
    };
  }
  const action = intervention.action || 'fresh_chat';
  const options = {
    fresh_chat: ['Context is getting expensive', 'Copy Fresh Start brief', 'fresh_chat'],
    recover_loop: ['Possible loop detected', 'Inspect and stop', 'inspect'],
    continue_focused: ['Focus the next checkpoint', 'Copy focused next step', 'focused'],
    switch_tool: ['Usage runway is getting low', 'Copy Fresh Start brief', 'fresh_chat'],
  };
  const selected = options[action] || ['AIWatcher found something to review', 'Inspect', 'inspect'];
  return {
    title: selected[0],
    body: intervention.reason || bubble.body,
    severity: intervention.severity || bubble.severity,
    primaryLabel: selected[1],
    primaryMode: selected[2],
  };
}
function shortSessionId(value) {
  const text = String(value || '');
  return text.length > 12 ? `${text.slice(0, 8)}...${text.slice(-4)}` : text || 'unknown';
}
function renderBubble(bubble, intervention) {
  const presentation = interventionPresentation(bubble, intervention);
  const runtime = bubble.runtime_attachment || {};
  const identityLabel = runtime.identity_label || runtime.label || 'Local session';
  const surface = runtime.surface || 'unknown surface';
  const last = bubble.updated_at ? new Date(bubble.updated_at).toLocaleString() : 'unknown';
  const tags = (bubble.tags || []).map(tag => `<span class="tag">${esc(tag)}</span>`).join('');
  document.getElementById('bubble').innerHTML = `<div class="top">
    <div><h1>${esc(presentation.title || 'AIWatcher found something to review')}</h1><p>${esc(presentation.body || 'Review the local evidence before continuing.')}</p></div>
    <span class="badge">${esc(presentation.severity || 'warning')}</span>
  </div>
  <div class="body">
    <div class="identity"><strong>${esc(identityLabel)}</strong><br>${esc(bubble.tool || runtime.tool || 'unknown tool')} · ${esc(surface)} · ${esc(bubble.project || runtime.project_path || 'unknown workspace')} · ${esc(shortSessionId(bubble.session_id || runtime.session_id))}<br>Last activity: ${esc(last)}</div>
    <div class="tags">${tags}</div>
    <p>${esc(bubble.reason || 'Use a Fresh Start brief to preserve the outcome without carrying the full chat history.')}</p>
    <div class="actions">
      <button class="primary" id="primaryAction">${esc(presentation.primaryLabel)}</button>
      ${presentation.primaryMode === 'inspect' ? '' : '<button id="inspect">Inspect</button>'}
      <button id="snooze">Snooze 15 min</button>
      <button id="dismiss">Dismiss</button>
    </div>
  </div>
  <div class="foot">Local-only. Prompt/source content is not stored in this decision.</div>`;
  document.getElementById('primaryAction').onclick = () => {
    if (presentation.primaryMode === 'inspect') return inspectIntervention(bubble);
    if (presentation.primaryMode === 'focused') return copyFocusedBrief(bubble, intervention || {});
    return copyHandoff(bubble, 'copy_handoff');
  };
  const inspect = document.getElementById('inspect');
  if (inspect) inspect.onclick = () => inspectIntervention(bubble);
  document.getElementById('snooze').onclick = snoozeIntervention;
  document.getElementById('dismiss').onclick = dismissIntervention;
}
async function load() {
  const wanted = queryParam('session');
  const intervention = await loadAmbientIntervention();
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
        title: `Fresh Start recommended before ~${saved} tokens of replayed context compounds`,
        body: health.recommendation || 'This session is getting heavy. Use a Fresh Start brief before continuing.',
        reason: health.recommendation || 'Context pressure is elevated.',
        expected_saved_context_tokens: health.estimated_replayed_context_tokens || null,
        tags: [`${health.latest_turn_tokens} tokens/turn`, `${saved} replayed`].concat(
          health.bloat_measurable ? [`${health.bloat_label} of spend replayed`] : []),
      };
    }
  }
  if (wanted && (!bubble || bubble.session_id !== wanted) && data.context_health_status === 'pending') {
    document.getElementById('bubble').innerHTML = `<div class="top"><div><h1>Loading this session evidence</h1><p>AIWatcher is still building the local context-health index for this deep link.</p></div><span class="badge">checking</span></div>
      <div class="body"><div class="actions"><a class="primary" href="/?session=${encodeURIComponent(wanted)}">Open dashboard</a><button onclick="window.close()">Close</button></div></div>`;
    window.setTimeout(load, 1600);
    return;
  }
  if (!bubble) {
    document.getElementById('bubble').innerHTML = `<div class="top"><div><h1>No Fresh Start needed right now</h1><p>AIWatcher did not find warning or critical context pressure in the current local window.</p></div><span class="badge">healthy</span></div>
      <div class="body"><div class="actions"><a class="primary" href="/">Open dashboard</a><button onclick="window.close()">Close</button></div></div>`;
    return;
  }
  await recordAmbientAction('displayed');
  renderBubble(bubble, intervention);
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
        if parsed.path == "/api/ambient-intervention":
            params = parse_qs(parsed.query)
            fingerprint = params.get("id", [""])[0].strip()
            record = get_ambient_intervention(fingerprint) if fingerprint else None
            if record is None:
                self._send(404, json.dumps({"error": "intervention not found"}), "application/json; charset=utf-8")
                return
            payload = {
                key: record.get(key)
                for key in (
                    "fingerprint",
                    "session_id",
                    "signal_kind",
                    "action",
                    "severity",
                    "reason",
                    "state",
                    "expected_savings",
                )
            }
            self._send(200, json.dumps(payload), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/companion-state":
            self._send(200, json.dumps(build_companion_state()), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/summary":
            params = parse_qs(parsed.query)
            try:
                days = max(1, min(90, int(params.get("days", ["7"])[0])))
            except ValueError:
                days = 7
            force = params.get("refresh", ["0"])[0] == "1"
            self._send(200, json.dumps(build_summary_cached(days, force=force)), "application/json; charset=utf-8")
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
            self._send(200, json.dumps(build_session_detail(session_id, allow_pending=True)), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/session-summary":
            params = parse_qs(parsed.query)
            session_id = params.get("id", [""])[0]
            self._send(200, json.dumps(build_session_summary(session_id)), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/handoff-basic":
            params = parse_qs(parsed.query)
            session_id = params.get("id", [""])[0]
            target = params.get("target", ["generic"])[0]
            handoff_options = _handoff_options_from_query(params)
            try:
                days = max(1, min(90, int(params.get("days", ["30"])[0])))
            except ValueError:
                days = 30
            self._send(
                200,
                json.dumps(build_basic_handoff_detail(session_id, days, target, **handoff_options)),
                "application/json; charset=utf-8",
            )
            return
        if parsed.path == "/api/handoff":
            params = parse_qs(parsed.query)
            session_id = params.get("id", [""])[0]
            target = params.get("target", ["generic"])[0]
            include_prompt_excerpt = params.get("prompt", ["0"])[0] == "1"
            handoff_options = _handoff_options_from_query(params)
            try:
                days = max(1, min(90, int(params.get("days", ["30"])[0])))
            except ValueError:
                days = 30
            self._send(
                200,
                json.dumps(build_handoff_detail(session_id, days, target, include_prompt_excerpt, **handoff_options)),
                "application/json; charset=utf-8",
            )
            return
        if parsed.path == "/api/handoff-demo":
            params = parse_qs(parsed.query)
            target = params.get("target", ["generic"])[0]
            handoff_options = _handoff_options_from_query(params, default_type="product")
            self._send(
                200,
                json.dumps(build_demo_handoff_detail(target=target, **handoff_options)),
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
        if parsed.path not in {
            "/api/outcome",
            "/api/preflight",
            "/api/handoff-basic",
            "/api/handoff",
            "/api/handoff-demo",
            "/api/handoff-decision",
            "/api/ambient-intervention-action",
            "/api/runtime-return",
        }:
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
        if parsed.path in {"/api/handoff-basic", "/api/handoff", "/api/handoff-demo"}:
            target = str(payload.get("target", "generic")).strip() or "generic"
            if parsed.path == "/api/handoff-demo":
                handoff_options = _handoff_options_from_payload(payload, default_type="product")
                self._send(
                    200,
                    json.dumps(build_demo_handoff_detail(target=target, **handoff_options)),
                    "application/json; charset=utf-8",
                )
                return
            session_id = str(payload.get("session_id", payload.get("id", ""))).strip()
            if not session_id:
                self._send(400, json.dumps({"error": "session_id is required"}), "application/json; charset=utf-8")
                return
            raw_days = payload.get("days", 30)
            try:
                days = max(1, min(90, int(raw_days)))
            except (TypeError, ValueError):
                days = 30
            handoff_options = _handoff_options_from_payload(payload)
            if parsed.path == "/api/handoff-basic":
                response = build_basic_handoff_detail(session_id, days, target, **handoff_options)
            else:
                include_prompt_excerpt = bool(payload.get("prompt", False))
                response = build_handoff_detail(
                    session_id,
                    days,
                    target,
                    include_prompt_excerpt,
                    **handoff_options,
                )
            status = 404 if response.get("error") == "session not found" else 200
            self._send(status, json.dumps(response), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/ambient-intervention-action":
            fingerprint = str(payload.get("fingerprint", "")).strip()
            action = str(payload.get("action", "")).strip().lower()
            channel = str(payload.get("channel", "overlay")).strip().lower() or "overlay"
            if not fingerprint:
                self._send(400, json.dumps({"error": "fingerprint is required"}), "application/json; charset=utf-8")
                return
            if action not in {"acted", "snooze", "dismiss", "displayed", "failed"}:
                self._send(400, json.dumps({"error": "unsupported intervention action"}), "application/json; charset=utf-8")
                return
            state = {"snooze": "snoozed", "dismiss": "dismissed"}.get(action, action)
            snoozed_until = None
            if state == "snoozed":
                raw_minutes = payload.get("snooze_minutes", 15)
                if not isinstance(raw_minutes, (int, float)) or isinstance(raw_minutes, bool):
                    self._send(400, json.dumps({"error": "snooze_minutes must be a number"}), "application/json; charset=utf-8")
                    return
                minutes = max(1, min(1440, int(raw_minutes)))
                snoozed_until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
            try:
                record = record_ambient_intervention_action(
                    fingerprint,
                    state=state,
                    channel=channel,
                    snoozed_until=snoozed_until,
                    detail=str(payload.get("detail", "")).strip() or None,
                )
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}), "application/json; charset=utf-8")
                return
            if record is None:
                self._send(404, json.dumps({"error": "intervention not found"}), "application/json; charset=utf-8")
                return
            self._send(200, json.dumps(record), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/runtime-return":
            session_id = str(payload.get("session_id", payload.get("id", ""))).strip()
            if not session_id:
                self._send(400, json.dumps({"error": "session_id is required"}), "application/json; charset=utf-8")
                return
            raw_days = payload.get("days", 30)
            try:
                days = max(1, min(90, int(raw_days)))
            except (TypeError, ValueError):
                days = 30
            result = build_runtime_return(session_id, days)
            self._send(
                200 if result.get("ok") or result.get("error") != "session not found" else 404,
                json.dumps(result),
                "application/json; charset=utf-8",
            )
            return
        if parsed.path == "/api/handoff-decision":
            session_id = str(payload.get("session_id", "")).strip()
            decision = str(payload.get("decision", "")).strip()
            reason = str(payload.get("reason", "")).strip()
            action_channel = str(payload.get("action_channel", "dashboard")).strip() or "dashboard"
            expected = payload.get("expected_saved_context_tokens")
            if not session_id:
                self._send(400, json.dumps({"error": "session_id is required"}), "application/json; charset=utf-8")
                return
            try:
                source_row = _find_session_row(session_id)
                record = record_handoff_decision(
                    session_id=session_id,
                    decision=decision,
                    reason=reason,
                    expected_saved_context_tokens=expected if isinstance(expected, int) else None,
                    action_channel=action_channel,
                    source_project_path=source_row.project_path if source_row else None,
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
    on_started: Callable[[str, int], Any] | None = None,
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
    started_resource = None
    if on_started:
        started_resource = on_started(host, selected_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped AIWatcher Local UI.")
    finally:
        if started_resource is not None and hasattr(started_resource, "terminate"):
            try:
                started_resource.terminate()
            except OSError:
                pass
        server.server_close()
