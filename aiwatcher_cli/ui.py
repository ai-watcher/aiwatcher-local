"""Private-by-default dashboard for AIWatcher Local."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePath
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

from . import __version__
from . import analyst, prompt_signals, statusline
from .cli import (
    SEARCH_RANK_FIELDS,
    SEARCH_RANK_TOPIC,
    search_field_rank,
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
from .metrics import (
    model_cost_comparison,
    pace_vs_baseline,
    replay_share_vs_baseline,
    replayed_context_cost,
)
from .local_state import (
    session_waiting_signals,
    COMMAND_GATE_BLOCKED_DECISIONS,
    MAX_COMMAND_DECISIONS_STORED,
    PROMPT_MODIFIED_DECISIONS,
    VALID_OUTCOMES,
    active_prompt_gate,
    analyst_consent,
    analyst_contents_allowed,
    analyst_month_spend,
    companion_skip_active,
    evidence_snapshots_for_sessions,
    get_outcome,
    get_watcher_status,
    outcome_counts,
    outcomes_for_sessions,
    recent_ambient_interventions,
    recent_command_decisions,
    recent_handoff_decisions,
    recent_interventions,
    recent_optimize_decisions,
    get_ambient_intervention,
    mark_active_prompt_gate_seen,
    mark_recent_handoff_receipts_viewed,
    record_companion_skip,
    record_ambient_intervention_action,
    record_analyst_consent,
    record_analyst_contents,
    record_analyst_run,
    record_handoff_decision,
    record_optimize_decision,
    record_evidence_snapshot,
    record_outcome,
    record_ui_server,
    state_path,
)
from .outcome_evidence import VALID_EVIDENCE_OUTCOMES, build_outcome_evidence, evidence_for_sessions
from .local_state import dismiss_first_run, first_run_dismissed_at
from .ledger import (
    UNBANKED_OUTSIDE_REPO,
    Ledger,
    build_ledger,
    checkpoint_distance,
    unbanked_summary,
)
from .pricing import cache_read_cost, estimate_cost, is_subscription_model
from .runtime_attachment import (
    RuntimeAttachment,
    format_resume_command,
    launch_resume_command,
    perform_runtime_return,
    resolve_resume_cwd,
    resume_command_for_session,
    resume_unavailable_reason,
    runtime_attachment_for_session,
    safe_runtime_processes,
)
from .runtime_nudge import foreground_tool, presentation_for_signal
from .session_presence import (
    LIVE_WINDOW_MINUTES,
    SessionPresence,
    live_presence,
    presence_for_sessions,
    tool_label,
)
from .session_health import (
    CRITICAL_TOKENS_PER_TURN,
    PRESSURE_TOKENS_PER_TURN,
    ContextHealth,
    analyze_all_sessions,
    analyze_session_health,
    gate_health_warning,
)
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
from .updater import apply_updates, check_for_updates


MAX_REQUEST_BYTES = 64 * 1024

MIN_DT = datetime.min.replace(tzinfo=timezone.utc)
# Per-turn history shipped per health card. The summary is cached to disk and read
# on every paint, so this is capped rather than unbounded; 60 turns is well past
# where a session has already crossed the action threshold.
CONTEXT_CHART_MAX_TURNS = 60
# The replay chart keeps far more than the runway chart does, and they are capped
# apart for a reason. The runway is one line per project and up to five of them
# ride in a single summary, where the recent shape is the whole question. The
# replay chart is one session, and its claim is that replay compounds over a
# session -- which the last sixty turns of a nine-hundred-turn session cannot
# show, because by then the curve has long since flattened at the top. It stays
# bounded rather than unbounded: the payload is cached to disk and read on every
# paint, and no chart needs to be the reason that grows without limit.
REPLAY_CHART_MAX_TURNS = 1_200
# A turn writes the conversation to cache, rather than just topping it up, at
# roughly this size. Below it every ordinary turn would read as a cache write.
CACHE_WRITE_TURN_TOKENS = 10_000
SUMMARY_MEMORY_TTL_SECONDS = 45
SUMMARY_DISK_TTL_SECONDS = 6 * 60 * 60
# Bump whenever build_summary's payload shape changes, so a cache written by an
# older build is discarded instead of rendering blank sections in a newer UI.
SUMMARY_CACHE_SCHEMA_VERSION = 8


def _restart_current_process() -> None:
    os.execv(sys.executable, [sys.executable, *sys.argv])


def schedule_dashboard_restart(delay_seconds: float = 0.8) -> None:
    timer = threading.Timer(delay_seconds, _restart_current_process)
    timer.daemon = True
    timer.start()

# POST endpoints whose only fact is that they happened, so they carry no JSON
# body and are exempt from the content-type check. Named rather than written
# inline: a second `not in {...}` inside do_POST is also what the route parser
# in test_cli reads to find the supported endpoints, and an inline set here
# shadowed the real list.
_POST_WITHOUT_BODY = frozenset({
    "/api/handoff-receipts-viewed",
    "/api/first-run-dismissed",
})
SESSION_SNAPSHOT_SCHEMA_VERSION = 1
SUMMARY_BACKGROUND_COOLDOWN_SECONDS = 8
SUMMARY_WINDOWS = (1, 7, 30)
# One definition of "live", shared with session_presence, which subdivides
# this window into working/quiet. Aliased rather than duplicated so the two
# surfaces cannot drift into disagreeing about when a session stops counting.
ACTIVE_SESSION_MINUTES = LIVE_WINDOW_MINUTES
RECENT_SESSION_HOURS = 4
FRESH_START_PROJECT_COOLDOWN_MINUTES = 2 * 24 * 60
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


def short_session_id(session_id: str | None) -> str:
    value = str(session_id or "")
    if len(value) <= 16:
        return value or "unknown"
    return f"{value[:8]}...{value[-4:]}"


def bytes_label(value: int) -> str:
    if value >= 1024 ** 3:
        return f"{value / (1024 ** 3):.1f} GB"
    if value >= 1024 ** 2:
        return f"{value / (1024 ** 2):.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value} B"


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


def project_name(path: str | None, segments: int = 2) -> str:
    """The last *segments* path components, for display in a column.

    Left-truncating a path mid-word ("...s/tadan/Downloads/...") makes a column
    unscannable, and the part that identifies a project is at the end. Two
    components rather than one because the leaf alone does not separate
    aiwatcher-local-public from aiwatcher-local-pr46 at a glance. Kept beside
    short_path rather than replacing it: that is also used for CLI output.
    """
    if not path:
        return "unknown"
    parts = [part for part in re.split(r"[\\/]+", str(path)) if part]
    if not parts:
        return "unknown"
    return "/".join(parts[-segments:])


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


def fresh_start_project_skip_key(project_path: str | None) -> str | None:
    """Stable project-level quiet key for Fresh Start nudges."""
    if not is_reliable_project_path(project_path):
        return None
    return f"control_recommended_project:{project_key(project_path)}"


def _fresh_start_project_quiet(project_path: str | None) -> bool:
    key = fresh_start_project_skip_key(project_path)
    return bool(key and companion_skip_active(key))


def _tool_family_label(tool: object) -> str:
    lower = str(tool or "").lower()
    if "claude" in lower:
        return "claude"
    if "codex" in lower:
        return "codex"
    if "cursor" in lower:
        return "cursor"
    if "vscode" in lower or "visual studio code" in lower:
        return "vscode"
    if "terminal" in lower or "iterm" in lower:
        return "terminal"
    return lower.strip()


def _foreground_matches_fresh_start_bubble(bubble: dict[str, object]) -> bool:
    """Only let Companion blink for Fresh Start when the related AI surface is foreground."""
    active = foreground_tool()
    if active is None:
        return False
    session_tool = _tool_family_label(bubble.get("tool"))
    runtime = bubble.get("runtime_attachment") if isinstance(bubble.get("runtime_attachment"), dict) else {}
    surface = str(runtime.get("surface") or "").lower()
    allowed = {session_tool}
    if session_tool == "cursor":
        allowed.add("vscode")
    if surface == "cli" or str(bubble.get("tool") or "").lower().endswith("-cli"):
        allowed.add("terminal")
    return active in {item for item in allowed if item}


def _fresh_start_context_candidates(summary: dict[str, object]) -> list[dict[str, object]]:
    rows = summary.get("context_health") if isinstance(summary.get("context_health"), list) else []
    candidates: list[dict[str, object]] = []
    seen_projects: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("severity") not in {"critical", "warning"} or not row.get("can_handoff"):
            continue
        project = str(row.get("project_full") or "")
        if _fresh_start_project_quiet(project):
            continue
        project_key_value = project_key(project)
        if project_key_value in seen_projects:
            continue
        seen_projects.add(project_key_value)
        candidates.append(row)
    return candidates


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


DETECTED_TOOL_LABELS = {
    "cursor": "Cursor",
    "ollama": "Ollama",
}


def _append_detected_tool_rows(
    tools: list[dict[str, object]],
    detected: dict[str, bool],
) -> list[dict[str, object]]:
    """Show installed/running AI tools even when AIWatcher has no spend rows yet."""
    result = list(tools)
    existing = {str(row.get("name") or "").lower() for row in result}
    for tool_id, label in DETECTED_TOOL_LABELS.items():
        if not detected.get(tool_id):
            continue
        if any(tool_id in name for name in existing):
            continue
        result.append({
            "name": label,
            "id": tool_id,
            "short_name": label,
            "sessions": 0,
            "tokens": 0,
            "tokens_label": "0",
            "api_value_usd": 0.0,
            "api_value_label": "$0.00",
            "calls": 0,
            "tool_calls": 0,
            "detected_only": True,
            "status_label": "Detected, not measured",
        })
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
            # The column holds states: Critical, Healthy, Limited data. "Review"
            # was an instruction sitting among them.
            "label": "Needs review",
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


def _elapsed_label(stamp: datetime | None, *, now: datetime | None = None) -> str:
    if stamp is None:
        return "unknown age"
    now = now or datetime.now(timezone.utc)
    delta = now - stamp.astimezone(timezone.utc)
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _worktree_rows(projects: set[str]) -> list[dict[str, object]]:
    """Read-only git worktree inventory for known project roots."""
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for project in sorted(projects):
        try:
            root = Path(project).expanduser()
        except (TypeError, ValueError):
            continue
        if not root.exists():
            continue
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), "worktree", "list", "--porcelain"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode != 0:
            continue
        current: dict[str, object] = {}
        for raw_line in [*completed.stdout.splitlines(), ""]:
            line = raw_line.strip()
            if not line:
                path = current.get("path")
                if isinstance(path, str) and path not in seen:
                    seen.add(path)
                    rows.append(current)
                current = {}
                continue
            if line.startswith("worktree "):
                current["path"] = line.split(" ", 1)[1]
            elif line.startswith("branch "):
                current["branch"] = line.split(" ", 1)[1].replace("refs/heads/", "")
            elif line == "bare":
                current["bare"] = True
            elif line == "detached":
                current["detached"] = True
    return rows


def _optimize_candidate_checklist(item: dict[str, object]) -> str:
    project = str(item.get("project") or "Local machine")
    title = str(item.get("title") or "Review workspace")
    reason = str(item.get("why_inactive") or item.get("summary") or "AIWatcher found local evidence worth reviewing.")
    impact = str(item.get("impact_label") or "No savings claim")
    evidence = str(item.get("evidence") or "Local metadata only.")
    project_full = str(item.get("project_full") or "")
    kind = str(item.get("kind") or "")
    lines = [
        f"AIWatcher Optimize review: {project}",
        "",
        f"Goal: {title}.",
        f"Why AIWatcher surfaced this: {reason}",
        f"Evidence: {evidence}",
        f"Impact signal: {impact}",
        "",
        "Safe review steps:",
    ]
    if kind == "session_cluster":
        lines.extend([
            "1. Open the matching AI app and find this project/workspace.",
            "2. Confirm the work is finished, handed off, or no longer needed.",
            "3. Archive or mark only those chats done inside the AI app.",
            "4. Keep final source-of-truth files, commits, receipts, and notes.",
            "5. Do not delete code, worktrees, chats, or processes from this checklist.",
        ])
    elif kind == "fresh_start_pending":
        lines.extend([
            "1. If you already started the fresh session, paste or keep the Fresh Start brief there.",
            "2. If you stayed in the old session, mark the receipt as skipped/continue so AIWatcher stops nudging.",
            "3. After the new session produces useful work, refresh AIWatcher so it can link proof.",
            "4. Do not claim saved tokens until AIWatcher observes the follow-up.",
        ])
    elif kind == "worktree":
        lines.extend([
            f"1. Run: git -C {project_full or '<worktree>'} status --short",
            "2. Confirm the branch is merged, abandoned, or intentionally disposable.",
            "3. Remove the worktree only through git/worktree-safe commands after confirmation.",
            "4. Do not delete the folder directly from this checklist.",
        ])
    elif kind == "stale_processes":
        lines.extend([
            "1. Run: aiwatcher processes --stale-only",
            "2. Confirm each process is not attached to live AI work.",
            "3. Stop only stale/orphaned runtimes you recognize.",
            "4. Leave unknown processes alone.",
        ])
    else:
        lines.extend([
            "1. Review the local evidence in AIWatcher.",
            "2. Confirm the work is finished before taking action.",
            "3. Prefer archive/mark-done actions over deletion.",
        ])
    lines.extend([
        "",
        "Privacy: this checklist uses local metadata only. It does not include prompt/source content.",
    ])
    return "\n".join(lines)


def _optimize_checklist(candidates: list[dict[str, object]]) -> str:
    lines = [
        "AIWatcher Optimize Workspace review",
        "",
        "Use this as a review queue. Take action one project at a time, and only inside the owning AI app or git tool.",
        "",
    ]
    if not candidates:
        lines.append("- No optimize candidates stood out in the current local window.")
        return "\n".join(lines)
    for index, item in enumerate(candidates, start=1):
        lines.extend([
            f"{index}. {item.get('title')}: {item.get('project')}",
            f"   Why: {item.get('why_inactive') or item.get('summary')}",
            f"   Evidence: {item.get('evidence_label')} - {item.get('evidence')}",
            f"   Impact: {item.get('impact_label')}",
        ])
        lines.append("   Action: copy the project-specific checklist in AIWatcher before doing anything.")
    lines.extend([
        "",
        "Do not delete worktrees, chats, or processes from this global list. Review each candidate independently.",
    ])
    return "\n".join(lines)


def _group_pending_fresh_starts(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    """Collapse pending Fresh Start rows that nothing on screen distinguishes.

    One row per project, carrying the count. They differ only by the decision id
    and, sometimes, by how long ago it was -- neither of which is rendered, so
    three of them read as the same item repeated.
    """
    grouped: dict[str, dict[str, object]] = {}
    out: list[dict[str, object]] = []
    for candidate in candidates:
        if candidate.get("kind") != "fresh_start_pending":
            out.append(candidate)
            continue
        key = str(candidate.get("project_full") or candidate.get("project") or "")
        first = grouped.get(key)
        if first is None:
            grouped[key] = candidate
            out.append(candidate)
            continue
        first["session_count"] = int(first.get("session_count") or 1) + 1
        tokens = int(first.get("tokens_at_risk") or 0) + int(candidate.get("tokens_at_risk") or 0)
        first["tokens_at_risk"] = tokens
        first["impact_label"] = f"~{compact_int(tokens)} context at risk" if tokens else None
        count = first["session_count"]
        first["title"] = f"Finish Fresh Start cleanup ({count} decisions)"
        first["summary"] = (
            f"{count} Fresh Start briefs were copied without a linked follow-up session. "
            "Mark the old chats done, or paste each brief into its new chat."
        )
    return out


def build_optimize_inventory(
    rows: list[LocalSession],
    *,
    outcomes: dict[str, dict[str, object]] | None = None,
    handoff_decisions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Find safe, local cleanup opportunities without mutating any AI app."""
    now = datetime.now(timezone.utc)
    outcomes = outcomes or {}
    handoff_decisions = handoff_decisions or []
    receipts = recent_optimize_decisions(limit=20)
    suppressed_projects: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get("decision") not in {"marked_done", "skipped"}:
            continue
        created = _parse_iso_datetime(receipt.get("created_at"))
        if created is not None and now - created <= timedelta(days=3):
            project_path = receipt.get("project_path")
            if isinstance(project_path, str) and project_path:
                suppressed_projects.add(project_path)

    by_project: dict[str, list[LocalSession]] = defaultdict(list)
    for row in rows:
        if is_reliable_project_path(row.project_path):
            by_project[str(row.project_path)].append(row)

    candidates: list[dict[str, object]] = []
    for project, items in by_project.items():
        if project in suppressed_projects:
            continue
        inactive = [
            row for row in items
            if (row.updated_at or row.started_at)
            and (now - (row.updated_at or row.started_at).astimezone(timezone.utc)) >= timedelta(hours=4)
        ]
        if len(inactive) < 2:
            continue
        tokens = sum(row.tokens_in + row.tokens_out for row in inactive)
        calls = sum(row.agent_calls for row in inactive)
        if tokens < 300_000 and calls < 120 and len(inactive) < 3:
            continue
        latest = max((row.updated_at or row.started_at for row in inactive if (row.updated_at or row.started_at)), default=None)
        completed = sum(1 for row in inactive if outcomes.get(row.session_id, {}).get("outcome") == "useful")
        tools = sorted({row.tool for row in inactive if row.tool})
        latest_label = _elapsed_label(latest, now=now)
        why_inactive = (
            f"Last local activity was {latest_label}; {len(inactive)} same-project sessions "
            f"from {', '.join(tools[:3]) if tools else 'local AI tools'} are still carrying context."
        )
        if completed:
            why_inactive += f" {completed} already have useful outcomes, so archive review is lower risk."
        candidates.append({
            "id": f"sessions:{project}",
            "kind": "session_cluster",
            "title": "Archive completed or stale chats",
            "project": project_label(project),
            "project_full": project,
            "summary": f"{len(inactive)} inactive same-project sessions are carrying ~{compact_int(tokens)} context. Archive or mark done once the work is no longer active.",
            "why_inactive": why_inactive,
            "evidence_label": "Observed",
            "evidence": "Observed from local session timestamps, project path, token pressure, and outcome metadata. Archive action must happen in the AI app.",
            "impact_label": f"~{compact_int(tokens)} context at risk",
            "tokens_at_risk": tokens,
            "session_count": len(inactive),
            "completed_count": completed,
            "updated_label": latest_label,
            "action_label": "Copy project steps",
        })

    for decision in handoff_decisions:
        if not isinstance(decision, dict):
            continue
        if str(decision.get("decision") or "") not in {"new_chat", "copy_handoff"}:
            continue
        if decision.get("next_session_id"):
            continue
        created_at = _parse_iso_datetime(decision.get("created_at"))
        if created_at is not None and now - created_at < timedelta(hours=2):
            continue
        expected = decision.get("expected_saved_context_tokens")
        tokens = int(expected) if isinstance(expected, int) and expected > 0 else 0
        project = str(decision.get("source_project_path") or "")
        if project and project in suppressed_projects:
            continue
        candidates.append({
            "id": f"fresh-start:{decision.get('id') or decision.get('session_id')}",
            "kind": "fresh_start_pending",
            "title": "Finish Fresh Start cleanup",
            "project": project_label(project),
            "project_full": project if is_reliable_project_path(project) else "",
            "summary": "A Fresh Start brief was copied, but no follow-up proof is linked yet. Mark the old chat done or paste the brief into the new chat.",
            "why_inactive": "AIWatcher saw a Fresh Start decision but has not linked a later same-project session yet.",
            "evidence_label": "Observed",
            "evidence": "Observed from AIWatcher Fresh Start receipt metadata.",
            "impact_label": f"~{compact_int(tokens)} context at risk" if tokens else None,
            "tokens_at_risk": tokens,
            "session_count": 1,
            "updated_label": _elapsed_label(created_at, now=now),
            "action_label": "Open Fresh Start receipts",
            "view": "receipts",
        })

    projects = {project for project in by_project if is_reliable_project_path(project)}
    for worktree in _worktree_rows(projects):
        path = str(worktree.get("path") or "")
        if not path or path in projects or path in suppressed_projects:
            continue
        path_obj = Path(path)
        looks_agent_owned = ".claude/worktrees" in path or "/.worktrees/" in path or path_obj.name.startswith(("agent-", "codex-"))
        if not looks_agent_owned:
            continue
        related_sessions = [row for row in rows if row.project_path and _is_inside_path(Path(row.project_path), path_obj)]
        latest = max((row.updated_at or row.started_at for row in related_sessions if (row.updated_at or row.started_at)), default=None)
        if latest is not None and now - latest.astimezone(timezone.utc) < timedelta(hours=12):
            continue
        candidates.append({
            "id": f"worktree:{path}",
            "kind": "worktree",
            "title": "Review stale AI worktree",
            "project": short_path(path),
            "project_full": path,
            "summary": "This looks like an AI-created worktree. AIWatcher will not delete it automatically; check git status first.",
            "why_inactive": "The worktree path looks agent-created and no recent same-path local session activity was observed.",
            "evidence_label": "Inferred",
            "evidence": "Inferred from git worktree path shape and local session age.",
            "impact_label": "disk cleanup possible",
            "tokens_at_risk": 0,
            "session_count": len(related_sessions),
            "updated_label": _elapsed_label(latest, now=now) if latest else "no recent session",
            "action_label": "Copy cleanup checklist",
        })

    try:
        stale_processes = [process for process in safe_runtime_processes() if process.stale]
    except OSError:
        stale_processes = []
    if stale_processes:
        rss_kb = sum(process.rss_kb or 0 for process in stale_processes)
        review_command = "aiwatcher processes --stale-only"
        candidates.append({
            "id": "stale-processes",
            "kind": "stale_processes",
            "title": "Review stale AI runtimes",
            "project": "Local machine",
            "project_full": "",
            "summary": f"{len(stale_processes)} AI-related runtime process(es) look stale or orphaned. Review before killing anything.",
            "why_inactive": "Local process metadata shows AI-related runtimes with stale/orphan signals.",
            "evidence_label": "Observed",
            "evidence": "Observed from local process metadata, not provider billing.",
            "impact_label": f"{bytes_label(int(rss_kb * 1024))} RSS observed" if rss_kb else "runtime clutter",
            "tokens_at_risk": 0,
            "session_count": len(stale_processes),
            "updated_label": "now",
            "action_label": "Copy safe review steps",
            "review_command": review_command,
            "resource_note": "RSS/CPU are local machine resources, not model/API spend.",
            "privacy_note": "This checklist uses local metadata only. It does not include prompt/source content.",
            "safe_review_steps": [
                f"Run: {review_command}",
                "Confirm each process is not attached to live AI work.",
                "Stop only stale/orphaned runtimes you recognize.",
                "Leave unknown processes alone.",
            ],
        })

    candidates = _group_pending_fresh_starts(candidates)
    candidates.sort(key=lambda item: (int(item.get("tokens_at_risk") or 0), int(item.get("session_count") or 0)), reverse=True)
    for item in candidates:
        item["checklist"] = _optimize_candidate_checklist(item)
    total_tokens = sum(int(item.get("tokens_at_risk") or 0) for item in candidates)
    return {
        "status": "needs_action" if candidates else "quiet",
        "title": "Optimize workspace" if candidates else "Workspace clean",
        "summary": f"{len(candidates)} cleanup opportunity{'ies' if len(candidates) != 1 else 'y'} found." if candidates else "No stale forks, worktrees, or runtime cleanup opportunities stood out.",
        "impact_label": f"~{compact_int(total_tokens)} context at risk" if total_tokens else "no context savings claim",
        "evidence_label": "Observed/inferred" if candidates else "Observed",
        "candidates": candidates[:8],
        "top": candidates[0] if candidates else None,
        "recent_receipts": receipts[:5],
        "checklist": _optimize_checklist(candidates[:8]),
    }


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
        handoff_label = "Build Fresh Start brief"
        handoff_reason = "This active AI work is heavy enough that a Fresh Start brief is safer than replaying the full history."
    elif identity_level == "likely_workspace":
        handoff_label = "Build Fresh Start brief"
        handoff_reason = "AIWatcher can identify the workspace, but not the exact running chat. Copy a Fresh Start brief before continuing."
    else:
        handoff_label = "Build Fresh Start brief"
        handoff_reason = "This is historical local evidence, not a live chat. Copy a Fresh Start brief from the evidence instead of trying to return to the old session."
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
        "tokens_value": row.tokens_in + row.tokens_out,
        "api_value": money(row.cost_usd),
        "api_value_usd": round(row.cost_usd, 6),
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
    state_filter: str | None = None,
) -> dict[str, object]:
    """S-27: UI-facing search/filter over local sessions, reusing filter_sessions()
    (cli.py) rather than re-implementing matching here."""
    rows = rows_for_window(days, prefer_cache=True)
    matched = filter_sessions(rows, search=search, outcome=outcome, evidence=evidence)
    if state_filter:
        def state_matches(row: LocalSession) -> bool:
            status = str(session_state(row).get("status") or "")
            if state_filter == "active_recent":
                return status in {"active", "recent"}
            if state_filter == "active":
                return status == "active"
            if state_filter == "history":
                return status in {"ended", "stale", "unknown"}
            return True
        matched = [row for row in matched if state_matches(row)]
    # Recency alone would undo filter_sessions' relevance order, so a search
    # sorts by where the term matched first and recency second. Without a search
    # there is nothing to rank on and it stays purely recent-first.
    needle = (search or "").strip().lower()
    if needle:
        matched = sorted(
            matched,
            key=lambda row: (
                search_field_rank(row, needle)
                if search_field_rank(row, needle) is not None else SEARCH_RANK_TOPIC,
                -( (row.updated_at or row.started_at or MIN_DT).timestamp() ),
            ),
        )
    else:
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
        "query": {"search": search or "", "outcome": outcome or "", "evidence": evidence or "", "state": state_filter or ""},
        "total_scanned": len(rows),
        "total_matched": total_matched,
        "sessions": [
            {
                **_session_row_json(row, window_outcomes, evidence_by_session),
                # Named so a reader can see why a row is in the results at all --
                # "parent folder" is what made a search for one project return
                # every sibling under the same directory.
                "match_field": (
                    SEARCH_RANK_FIELDS.get(
                        search_field_rank(row, needle)
                        if search_field_rank(row, needle) is not None else SEARCH_RANK_TOPIC
                    ) if needle else None
                ),
            }
            for row in matched
        ],
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
        "tokens_label": compact_int(row.tokens_in + row.tokens_out),
        "tokens_value": row.tokens_in + row.tokens_out,
        "api_value": money(row.cost_usd),
        "api_value_usd": round(row.cost_usd, 6),
        "calls": row.agent_calls,
        "tool_calls": row.tool_calls,
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


def _replay_share_history(exclude_session_id: str) -> list[float] | None:
    """Replay share for each of the owner's *other* sessions, for the baseline.

    Returns None -- not an empty list -- while the shared event index is still
    building, so the caller can say "not yet" instead of "no history", which
    would read as a verdict.

    Cheap enough to do per request: ~12ms over the whole local corpus (52
    sessions, 38k events), because the events are already indexed in memory and
    analyze_session_health is a single pass over each session's list.
    """
    with _SUMMARY_CACHE_LOCK:
        if not _EVENT_INDEX_READY:
            return None
        rows = list(_SESSION_INDEX.values())
        # Shallow: the per-session lists are replaced wholesale on reindex, never
        # mutated in place, so holding a reference outside the lock is safe.
        by_session = dict(_EVENT_INDEX)

    shares: list[float] = []
    for row in rows:
        if row.session_id == exclude_session_id:
            continue
        health = analyze_session_health(row, by_session.get(row.session_id, []))
        if health and health.bloat_measurable:
            shares.append(health.bloat_ratio * 100)
    return shares


def _session_verdict_inputs(row: LocalSession, events: list[LocalEvent]) -> dict[str, object]:
    """The three things a session can be judged on, and whether each is knowable yet.

    They are deliberately separate. "How much room is left" is answerable now and
    is the only urgent one; "did it cost more than it needed to" is answerable
    once the session stops; "was it worth it" needs commits to age past
    survival.MIN_AGE_DAYS before it means anything. Collapsing them into a single
    verdict is what made the old one unable to say anything -- an absolute token
    threshold stood in for all three and fired for two sessions in three.
    """
    health = analyze_session_health(row, events)
    pressure: dict[str, object] = {"measurable": False}
    if health:
        pressure = {
            "measurable": True,
            "latest_turn_tokens": health.latest_turn_tokens,
            "latest_turn_label": compact_int(health.latest_turn_tokens),
            "peak_turn_tokens": health.peak_turn_tokens,
            "peak_turn_label": compact_int(health.peak_turn_tokens),
            "pressure_tokens": PRESSURE_TOKENS_PER_TURN,
            "critical_tokens": CRITICAL_TOKENS_PER_TURN,
            "turns_to_critical": health.turns_to_critical,
            "turns_since_reset": health.turns_since_reset,
            "severity": health.severity,
        }

    # Share of *spend*, not of tokens. The token-share reading is ~98% for every
    # session because cache reads dominate the count, so it separates nothing;
    # weighted by what was actually billed it runs 40-70% and discriminates.
    replay: dict[str, object] = {
        "measurable": bool(health and health.bloat_measurable),
        "reason": None if (health and health.bloat_measurable)
        else "This tool is plan-based, so there is no per-session bill to apportion.",
    }
    if health and health.bloat_measurable:
        share_pct = health.bloat_ratio * 100
        history = _replay_share_history(row.session_id)
        if history is None:
            comparison: dict[str, object] = {
                "available": False,
                "reason": "Your other sessions are still indexing, so there is nothing "
                          "to compare this against yet.",
            }
        else:
            comparison = replay_share_vs_baseline(share_pct, history)
        replay.update({
            "share_pct": round(share_pct, 1),
            "share_label": f"{share_pct:.0f}%",
            "replayed_cost_label": money(health.replayed_cost_usd),
            # Whether this is high is now a statement about the owner's own
            # history, so it is only answerable when that history exists. No
            # baseline means unknown, and unknown is not the same as fine.
            "comparison": comparison,
            "high": bool(comparison.get("high")),
        })
    return {"pressure": pressure, "replay": replay}


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
        "verdict": _session_verdict_inputs(row, events),
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


def build_session_resume(session_id: str, days: int = 30, *, launch: bool = False) -> dict[str, object]:
    """Resolve the tool's own resume command for one session.

    Deliberately does not consult session_state or runtime processes: resume
    works on a session that ended days ago in a terminal since closed, which
    is the case the live-attachment tiers cannot serve at all.

    `command` is always returned when one exists, whether or not `launch`
    succeeded, so the front end can fall back to copying it.
    """
    row = _find_session_row(session_id, days=days)
    if not row:
        return {"ok": False, "available": False, "error": "session not found"}
    command = resume_command_for_session(row.tool, row.session_id)
    if not command:
        return {"ok": False, "available": False, "message": resume_unavailable_reason(row.tool, row.session_id)}
    cwd = resolve_resume_cwd(row.project_path)
    display = format_resume_command(command, cwd=cwd) or ""
    result: dict[str, object] = {
        "ok": True,
        "available": True,
        "tool": row.tool,
        "cwd": cwd,
        "command": display,
        "message": f"Copy this into a terminal: `{display}`",
    }
    if launch:
        # launch_resume_command owns ok/message from here: a failed spawn must
        # report itself so the reader copies instead of assuming a window opened.
        result.update(launch_resume_command(command, cwd=row.project_path))
        result["available"] = True
        result["command"] = display
    return result


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


def _same_project_session_count(row: LocalSession) -> int:
    current = project_key(row.project_path)
    if current == UNATTRIBUTED_PROJECT:
        return 1
    with _SUMMARY_CACHE_LOCK:
        candidates = list(_SESSION_INDEX.values())
    session_ids = {
        candidate.session_id
        for candidate in candidates
        if candidate.session_id and project_key(candidate.project_path) == current
    }
    session_ids.add(row.session_id)
    return max(1, len(session_ids))


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
    same_project_count = _same_project_session_count(row)
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
    objective_text = objective.strip() if objective and objective.strip() else (
        "Continue the same user goal from the source workspace, but verify the source session identity before editing."
    )
    next_brief = "\n".join([
        "AIWatcher Fresh Start brief",
        "",
        "You are starting a fresh AI work session from an AIWatcher handoff.",
        "Do not assume access to the previous chat, hidden memory, or unstated decisions.",
        "Continue from source-session metadata and workspace state, not from hidden conversation history.",
        f"Target tool: {TARGET_LABELS[target]}.",
        f"Continuation type: {HANDOFF_TYPE_LABELS[handoff_type]}.",
        "",
        "Source session identity",
        f"- Identity confidence: {attachment.identity_label} ({attachment.confidence})",
        f"- Source session id: {session_id}",
        f"- Source tool/surface: {row.tool} / {row.surface or 'unknown'}",
        f"- Source model: {row.model or 'unknown'}",
        f"- Last observed activity: {row.updated_at.isoformat() if row.updated_at else 'unknown'}",
        f"- Identity note: {attachment.identity_reason}",
        f"- Return capability: {attachment.exact_return_label}",
        f"- Return note: {attachment.exact_return_reason}",
        *(
            [
                f"- Same-project sessions observed: {same_project_count}",
                "- If this is not the intended source chat, stop and ask the user which session to continue.",
            ]
            if same_project_count > 1
            else []
        ),
        "",
        "Goal",
        f"- User objective: {objective_text}",
        "- Preserve momentum without replaying the bloated prior conversation.",
        "- Choose the smallest useful next checkpoint before editing.",
        "",
        "How to continue",
        "- If this is a fresh chat: first reconstruct the task from the workspace and evidence below.",
        "- If this is a forked chat: keep the parent chat as source of truth and return only the final summary, files touched, verification, and unresolved questions.",
        "- If this is a subagent task: inspect only the assigned lane, then report evidence and recommendations back to the orchestrator.",
        "- If evidence is insufficient, ask one focused clarification instead of guessing.",
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
        f"- Source tool/model: {row.tool} / {row.model or 'unknown'}",
        "",
        "What remains uncertain",
        "- Detailed git, timeline, and prompt evidence is still loading.",
        "- Working-tree files may come from another AI chat or manual edits in the same repository.",
        *(
            ["- AIWatcher has not verified the exact active chat. Confirm this handoff matches the intended work before editing."]
            if attachment.identity_label != "Exact active session"
            else []
        ),
        "",
        "Why start fresh",
        *[f"- {item}" for item in warnings],
        "",
        "First response required",
        "- Say what appears done.",
        "- Say what remains uncertain.",
        "- Name the exact files, docs, commands, or screens you will inspect first.",
        "- Propose one smallest next checkpoint and wait if the scope is ambiguous or risky.",
        "",
        "Immediate next checkpoint",
        "- First verify that the source session identity above matches the work the user meant to continue.",
        "- Run `git status --short`.",
        "- Treat changed files as workspace evidence, not guaranteed proof from this source session.",
        "- Inspect changed files and any source-of-truth files listed above before editing.",
        "- Continue only after that checkpoint is clear; do not replay broad exploration from the old session.",
        "",
        "Guardrails",
        "- Preserve unrelated changes.",
        "- Do not expose secrets.",
        "- Stop before destructive changes, force pushes, broad refactors, production writes, or unrelated cleanup.",
        "",
        "Done report",
        "- Summarize what changed, what was verified, what remains uncertain, and whether the result looks useful.",
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
        "same_project_session_count": same_project_count,
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
    attachment = runtime_attachment_for_session(row, state=session_state(row), processes=safe_runtime_processes())
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
        runtime_attachment=attachment.to_json(),
        same_project_session_count=_same_project_session_count(row),
    )
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

    # DIGEST_CANDIDATE_LIMIT is a fixed cap, so the list reads identically whether
    # these five sessions are most of the window or a rounding error on it -- and the
    # right next move is opposite in those two cases. Each row carries its share of
    # window spend, and the panel reports what the five together cover, so the reader
    # gets the denominator the ranking cannot supply.
    window_cost = sum(row.cost_usd for row in rows)
    top_sessions = sorted(rows, key=lambda row: row.cost_usd, reverse=True)[:DIGEST_CANDIDATE_LIMIT]
    top_sessions_share_pct = (
        round(100.0 * sum(row.cost_usd for row in top_sessions) / window_cost, 1)
        if window_cost > 0
        else None
    )

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
                # None rather than 0 when the window has no priced spend: a plan-only
                # window is "not measurable here", which is not the same claim as 0%.
                "share_pct": (
                    round(100.0 * row.cost_usd / window_cost, 1) if window_cost > 0 else None
                ),
                "outcome": (window_outcomes.get(row.session_id) or {}).get("outcome"),
            }
            for row in top_sessions
        ],
        "top_sessions_share_pct": top_sessions_share_pct,
        "top_sessions_window_total_label": money(window_cost),
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


# The two destinations a health card can send you to. A button names one of
# these and nothing else, so its label cannot drift from what it does.
_ACTION_REVIEW = ("Review session", "review")
_ACTION_FRESH = ("Start fresh", "handoff")


def _context_action(health: ContextHealth) -> dict[str, str]:
    """What the two buttons on a health card say, and where each one goes.

    Label and behaviour used to be decided in different files. This function
    returned advice -- "Compact", "Keep going" -- as the primary label, while
    the primary button's handler was hardcoded to open the session review. So
    three of these four states shipped a button that promised something it did
    not do. "Compact" was the worst of them: it reads as an instruction the
    button will carry out, and the control that actually compacts sits directly
    beside it. In the healthy state the two labels were outright swapped.

    The advice still exists -- it is `reason`, rendered under the buttons. What
    changed is that it is no longer wearing a button.
    """
    if health.severity == "critical":
        primary, secondary = _ACTION_FRESH, _ACTION_REVIEW
        reason = "Critical context pressure is likely to waste turns or miss details."
    elif health.is_context_pressure or health.is_high_bloat:
        primary, secondary = _ACTION_FRESH, _ACTION_REVIEW
        reason = "Context is growing; compact before it compounds further."
    elif health.is_stale:
        primary, secondary = _ACTION_REVIEW, _ACTION_FRESH
        reason = "The session is old enough that a focused restart may be cleaner."
    else:
        primary, secondary = _ACTION_REVIEW, _ACTION_FRESH
        reason = "Context looks healthy."
    return {
        "label": primary[0],
        "kind": primary[1],
        "secondary_label": secondary[0],
        "secondary_kind": secondary[1],
        "reason": reason,
    }


def _context_health_card(
    health: ContextHealth,
    session: LocalSession | None,
    *,
    group: list[ContextHealth],
    turn_series: list[int] | None = None,
    charted_because_live: bool = False,
) -> dict[str, object]:
    action = _context_action(health)
    critical_count = sum(1 for item in group if item.severity == "critical")
    warning_count = sum(1 for item in group if item.severity == "warning")
    replayed_tokens = sum(item.latest_turn_replayed_tokens for item in group)
    replayed_cost = sum(item.replayed_cost_usd for item in group if item.bloat_measurable)
    analyzed_cost = sum(item.analyzed_cost_usd for item in group if item.bloat_measurable)
    bloat_measurable = any(item.bloat_measurable for item in group)
    # When a session is charted for being reachable rather than for being the
    # worst, the bigger one still exists and the card would otherwise be the only
    # place it could have been mentioned. Naming it keeps the swap honest.
    #
    # Reported as silence, not as an ending. Nothing local can tell a finished
    # session from one sitting in a tab the user will return to after lunch --
    # all that was observed is a log that stopped changing, and a session can be
    # picked up again at any time. So the card says how long it has been quiet
    # and lets the reader decide what that means.
    heaviest_item = max(group, key=lambda item: item.latest_turn_tokens, default=None)
    bigger = (
        heaviest_item
        if heaviest_item is not None and heaviest_item.latest_turn_tokens > health.latest_turn_tokens
        else None
    )
    runtime_attachment = (
        runtime_attachment_for_session(session, state=session_state(session), processes=[]).to_json()
        if session
        else None
    )
    identity_label = str((runtime_attachment or {}).get("identity_label") or "Historical log only")
    return_label = str((runtime_attachment or {}).get("exact_return_label") or "Exact chat unavailable")
    intent_summary = (
        "Start a fresh session from this source before continuing broad work."
        if health.severity == "critical"
        else "Compact or prepare a Fresh Start before the next broad task."
    )
    context_summary = (
        f"{action['label']} because {health.tool} is replaying about "
        f"{compact_int(health.latest_turn_tokens)} tokens in the latest turn."
    )
    if len(group) > 1:
        context_summary += f" This is the highest-pressure source among {len(group)} same-project sessions."
    return {
        "charted_because_live": charted_because_live,
        "bigger_idle_label": compact_int(bigger.latest_turn_tokens) if bigger else None,
        "bigger_idle_age_label": (
            (f"{bigger.age_days:.1f}d" if bigger.age_days >= 1 else f"{bigger.age_hours:.0f}h")
            if bigger else None
        ),
        "session_id": health.session_id,
        "session_short": short_session_id(health.session_id),
        "tool": health.tool,
        "project": project_label(health.project_path),
        "project_full": health.project_path,
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
        # The charted session's own peak, not the project's. These two sit side
        # by side and the chart draws a "peak N -- already crossed once" line
        # from it, so a group maximum here claims this session reached a number
        # another one did. Harmless while the representative was always the
        # largest session; wrong the moment it is chosen for being reachable
        # instead. The project-wide view is the session count and the group note.
        "peak_turn_tokens": compact_int(health.peak_turn_tokens),
        # Chart inputs. Raw numbers, deliberately suffixed so nothing confuses them
        # with the formatted strings above. The series is capped because the whole
        # summary is cached to disk and read on every dashboard paint -- an
        # uncapped per-turn history would grow without bound on long sessions.
        "chart": None if turn_series is None else {
            "turn_series": turn_series[-CONTEXT_CHART_MAX_TURNS:],
            "latest_turn_tokens_n": health.latest_turn_tokens,
            "peak_turn_tokens_n": health.peak_turn_tokens,
            "growth_per_turn_n": round(health.segment_growth_rate),
            "turns_to_critical": health.turns_to_critical,
            "turns_since_reset": health.turns_since_reset,
            "context_resets": health.context_resets,
            "pressure_tokens_n": PRESSURE_TOKENS_PER_TURN,
            "critical_tokens_n": CRITICAL_TOKENS_PER_TURN,
        },
        "estimated_replayed_context_tokens": replayed_tokens,
        "estimated_replayed_context_label": compact_int(replayed_tokens),
        "bloat_measurable": bloat_measurable,
        "efficiency_label": f"{health.efficiency_pct:.0f}%" if health.bloat_measurable else "n/a",
        "bloat_label": f"{health.bloat_ratio * 100:.0f}%" if health.bloat_measurable else "n/a",
        "replayed_cost_label": f"${replayed_cost:.2f}" if bloat_measurable else "n/a",
        "analyzed_cost_label": f"${analyzed_cost:.2f}" if bloat_measurable else "n/a",
        "age_label": f"{health.age_days:.1f}d" if health.age_days >= 1 else f"{health.age_hours:.0f}h",
        "intent_summary": intent_summary,
        "context_summary": context_summary,
        "identity_label": identity_label,
        "return_label": return_label,
        "recommendation": health.recommendations[0] if health.recommendations else "Context is healthy.",
        "action": action,
        "runtime_attachment": runtime_attachment,
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
    # Per-turn input, kept raw. Everything else on this card is display-formatted
    # (compact_int turns 158000 into "158K"), which a chart cannot plot -- so the
    # series and the projection fields below travel as numbers alongside the
    # strings the existing card already renders, rather than replacing them.
    turns_by_session: dict[str, list[int]] = defaultdict(list)
    for event in sorted(events, key=lambda e: (e.timestamp or MIN_DT)):
        if event.tokens_in > 0:
            turns_by_session[event.session_id].append(event.tokens_in)
    grouped: dict[str, list[ContextHealth]] = defaultdict(list)
    for health in analyze_all_sessions(rows, events):
        grouped[project_key(health.project_path)].append(health)
    severity_order = {"critical": 0, "warning": 1, "healthy": 2}
    cards: list[dict[str, object]] = []
    def _still_reachable(item: ContextHealth) -> bool:
        """Is this a session you could still act on, or one you have left?"""
        session = sessions_by_id.get(item.session_id)
        if session is None:
            return False
        return str(session_state(session).get("status")) in {"active", "recent"}

    for group in grouped.values():
        # Severity first, then whether the session is still live, and only then
        # size. Ranking on size alone charted the biggest number in the project
        # regardless of whether anyone was still in it: a session left six hours
        # earlier at 824K outranked the one running right now at 343K, which was
        # also critical and never appeared. Every button on this card -- start
        # fresh, hand off, copy a compact prompt -- is an instruction to do
        # something in that session, and none of them can be carried out in one
        # that has ended, so the worst *reachable* session is the useful pick.
        # With nothing live the order is unchanged and the biggest still wins.
        group.sort(key=lambda item: (
            severity_order.get(item.severity, 9),
            0 if _still_reachable(item) else 1,
            -int(item.latest_turn_tokens * item.bloat_ratio),
            -item.total_input_tokens,
        ))
        representative = group[0]
        session = sessions_by_id.get(representative.session_id)
        # Only sources with real per-turn numbers can be plotted against a per-turn
        # threshold. The Codex DB path exposes a running thread total and nothing
        # per turn, so its "turns" would be one growing number; the Codex rollout
        # path reads last_token_usage and does have genuine per-turn prompt sizes,
        # which is why it deliberately carries no cumulative note and is charted.
        # Same exclusion _insight_feed already applies via pressure_rows.
        plottable = session is not None and not has_cumulative_totals(session)
        cards.append(_context_health_card(
            representative,
            session,
            group=group,
            turn_series=turns_by_session.get(representative.session_id, []) if plottable else None,
            charted_because_live=_still_reachable(representative),
        ))
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
        (
            row for row in context_health
            if row.get("severity") in {"critical", "warning"}
            and row.get("can_handoff")
            and not _fresh_start_project_quiet(str(row.get("project_full") or ""))
        ),
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
        "project_full": candidate.get("project_full"),
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
    try:
        summary = build_summary_cached(7)
    except OSError:
        summary = {}
    handoff_bubble = summary.get("handoff_bubble") if isinstance(summary, dict) else None
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
        # Stage 1 output, so the Plan result can separate what was read out of
        # this prompt from the advice every prompt of this shape receives.
        "signals": result.get("signals") if isinstance(result.get("signals"), dict) else {},
        "removals": result.get("removals") if isinstance(result.get("removals"), list) else [],
        # Itemised so the score can be accounted for. A number a reader cannot
        # take apart is a number they stop believing, and this one decides
        # whether the product spends money on a second opinion.
        "blast": result.get("blast") if isinstance(result.get("blast"), dict) else {},
        "workflow": result.get("workflow") if isinstance(result.get("workflow"), dict) else {},
        "plan_action": _prompt_plan_action(text, result, handoff_bubble),
        # Whether Stage 2 is worth paying for, decided by the free local
        # score alone. The verdict travels with Stage 1 so the front end
        # knows whether to ask for a second opinion without guessing at the
        # threshold itself -- and so a gated-out prompt can say so rather
        # than leaving the derived zone silent.
        "second_opinion": _second_opinion_gate(result),
        "impact_label": impact_label,
        "privacy": "Prompt text is analyzed locally for this response and is not persisted by the Prompt Companion.",
    }


# Measured, not guessed: real runs on the small tier came in at $0.037 and
# $0.028. Stated as a range so it does not read as a quote.
PRIVACY_CLAIMS = [
    "Read-only local scan",
    "No AIWatcher cloud call unless you connect or configure one.",
    "Second opinion and AI Assist use your configured tools and keys.",
    "Prompt and file-path access is workflow-scoped; file contents require opt-in.",
    "Source stays local unless a connected workflow is explicitly enabled.",
]

# Measured across both hosts, and the spread is real: the same prompt has
# returned in 17s and in 206s. Stated as a range so it does not read as a
# quote, and the duration is given as "usually" for the same reason.
ANALYST_RUN_ESTIMATE_LABEL = "about $0.03-0.04, usually under a minute"


def _second_opinion_gate(result: dict[str, object]) -> dict[str, object]:
    """Stage 1's verdict on whether Stage 2 runs. No spawn happens here.

    Three distinct answers, and none of them is silence: the gate was not
    reached, there is no CLI to ask, or it is worth asking and the front end
    should now request it. Spec 8 requires every one of them to leave zones B
    and C complete, which they do -- this only decides whether zone A is worth
    filling.
    """
    blast = result.get("blast") if isinstance(result.get("blast"), dict) else {}
    if not blast.get("gate"):
        return {
            "gated": False,
            "available": False,
            "reason": "Nothing in this prompt matched a signal worth a second opinion.",
        }
    detection = analyst.detect(tool=str(result.get("tool") or ""))
    if not detection.get("available"):
        return {
            "gated": True,
            "available": False,
            "reason": str(detection.get("reason")
                          or "Second opinion unavailable. No agent CLI found."),
        }
    return {
        "gated": True,
        "available": True,
        "pending": True,
        "cli": detection.get("cli"),
        "cli_label": detection.get("label"),
        # Whether this is the vendor the user is about to prompt, or a stand-in
        # because theirs has no analyst. Worth saying: it is their bill.
        "preferred": detection.get("preferred", False),
        "reason": "",
    }


def build_second_opinion(prompt: str, *, tool: str = "agent",
                         cwd: str | None = None) -> dict[str, object]:
    """Stage 2. Only ever reached once Stage 1 has already said yes.

    The gate is re-checked here rather than trusted from the request: the
    endpoint is reachable directly, and "the cheap analysis decides whether the
    expensive one runs" is not a rule the client gets to waive.
    """
    text = (prompt or "").strip()
    if not text:
        return {"available": False, "reason": "prompt is required"}
    blast = prompt_signals.score_blast_radius(text, cwd=cwd)
    if not blast.get("gate"):
        return {
            "gated": False,
            "available": False,
            "reason": "Nothing in this prompt matched a signal worth a second opinion.",
        }
    if not cwd:
        return {
            "gated": True,
            "available": False,
            "reason": "Second opinion needs a workspace path to read the file tree from.",
        }
    # Spec 5: a hard stop before the spawn, not a warning after it. A product
    # whose anchor story is a runaway agent bill does not get to ship a budget
    # that is only checked on the way out.
    budget = analyst_month_spend()
    if budget["capped"]:
        # Quoting dollars at somebody whose CLI reports none would be the same
        # defect this codebase keeps finding: a true number answering a question
        # it was not asked.
        detail = (f"{money(budget['spent_usd'])} of {money(budget['cap_usd'])}"
                  if budget.get("capped_by") == "cost"
                  else f"{budget['runs']} of {budget['run_cap']} runs")
        return {
            "gated": True, "available": False, "capped": True, "budget": budget,
            "reason": f"Second opinion paused. Monthly cap reached ({detail}).",
        }
    # Spec 7: asked once per project, with the cost in the question, rather than
    # a modal on every prompt. Spending someone's money is not something to
    # infer from them having clicked Plan.
    consent = analyst_consent(cwd)
    if consent is None:
        # Name the agent that would actually run. It is the user's key being
        # spent, and when their own vendor has no analyst the stand-in is not
        # something to discover afterwards from a cost chip.
        found = analyst.detect(tool=tool)
        host_label = found.get("label") or "your own agent"
        instead = ("" if found.get("preferred")
                   else f" {host_label} is standing in, because the tool you picked has no analyst yet.")
        return {
            "gated": True, "available": False, "needs_consent": True,
            "project_path": cwd, "budget": budget,
            "cli": found.get("cli"), "cli_label": found.get("label"),
            "preferred": found.get("preferred", False),
            "estimate_label": ANALYST_RUN_ESTIMATE_LABEL,
            "reason": (f"A second opinion runs {host_label}, on your machine, with your "
                       "key. It sees this prompt and your file paths, never file contents. "
                       f"Typical run: {ANALYST_RUN_ESTIMATE_LABEL}.{instead}"),
        }
    if not consent.get("allowed"):
        return {
            "gated": True, "available": False, "declined": True,
            "reason": "Second opinion is turned off for this project.",
        }
    paths = analyst.ranked_paths(cwd, prompt_signals.repo_paths(cwd))
    result = analyst.run(text, project_root=cwd, paths=paths, tool=tool,
                         read_contents=analyst_contents_allowed(cwd))
    result["gated"] = True
    result["tool"] = tool
    if result.get("available"):
        # Recorded from what the CLI reported it cost, not from an estimate, so
        # the counter the cap is enforced against is money that actually moved.
        try:
            # 0.0 when the host reports nothing, which is honest for the dollar
            # total and is exactly why the run counter exists beside it.
            record_analyst_run(project_path=cwd,
                               cost_usd=float(result.get("cost_usd") or 0.0),
                               session_id=result.get("session_id"))
        except (OSError, ValueError):
            pass
    result["budget"] = analyst_month_spend()
    return result


def _prompt_plan_action(
    prompt: str,
    result: dict[str, object],
    handoff_bubble: object,
) -> dict[str, object]:
    workflow = result.get("workflow") if isinstance(result.get("workflow"), dict) else {}
    mode = str(workflow.get("mode") or "")
    lower = prompt.lower()
    close_terms = [
        "archive this",
        "archive the chat",
        "close this out",
        "wrap up",
        "mark outcome",
        "we are done",
        "this is done",
        "summarize final",
    ]
    if any(term in lower for term in close_terms):
        return {
            "kind": "archive",
            "label": "Archive / outcome",
            "title": "Close the loop instead of continuing",
            "why": "This prompt sounds like the work may be complete or ready to summarize.",
            "next_step": "Mark the outcome in AIWatcher, capture the final summary, then start a new prompt only if there is a new objective.",
            "primary_label": "Open Evidence",
            "primary_url": "/?view=receipts",
            "confidence": "inferred",
        }
    if mode == "fork_task":
        return {
            "kind": "fork",
            "label": "Fork",
            "title": str(workflow.get("title") or "Fork recommended"),
            "why": str(workflow.get("why") or "This task is broad enough to isolate in a separate chat."),
            "next_step": str(workflow.get("instruction") or "Fork the current chat or start a separate task, then paste the execution brief."),
            "primary_label": "Copy fork brief",
            "primary_url": "",
            "confidence": "inferred",
        }
    if mode == "use_subagents":
        return {
            "kind": "fork",
            "label": "Split work",
            "title": str(workflow.get("title") or "Use subagents"),
            "why": str(workflow.get("why") or "This request spans independent review lanes."),
            "next_step": str(workflow.get("instruction") or "Split the work into independent lanes, then consolidate."),
            "primary_label": "Copy split brief",
            "primary_url": "",
            "confidence": "inferred",
        }
    if mode in {"continue_with_confirmation", "checkpoint_current"} or str(result.get("risk")) in {"medium", "high"}:
        return {
            "kind": "prompt_change",
            "label": "Rewrite prompt",
            "title": str(workflow.get("title") or "Change the prompt first"),
            "why": str(workflow.get("why") or "AIWatcher found scope, safety, or cost pressure before execution."),
            "next_step": "Copy the execution brief instead of the original prompt.",
            "primary_label": "Copy safer brief",
            "primary_url": "",
            "confidence": "observed",
        }
    already_fresh_start_prompt = "AIWatcher Fresh Start brief" in prompt or "AIWatcher fresh-session handoff" in prompt
    if (
        isinstance(handoff_bubble, dict)
        and handoff_bubble.get("session_id")
        and not already_fresh_start_prompt
    ):
        session_id = str(handoff_bubble.get("session_id"))
        return {
            "kind": "fresh_start",
            "label": "Fresh Start",
            "title": "Start fresh before sending this",
            "why": str(
                handoff_bubble.get("reason")
                or handoff_bubble.get("body")
                or "Current local context has enough pressure that a Fresh Start brief is safer than replaying the chat."
            ),
            "next_step": "Open the session, copy the Fresh Start brief, then paste this planned task into the new chat.",
            # One verb opens the Fresh Start drawer, everywhere. "Open Fresh
            # Start", "Start fresh" and "Try Fresh Start demo" were three names
            # for one action. (Home's button keeps its own name because it
            # copies rather than opens.)
            "primary_label": "Start fresh",
            "primary_url": f"/?session={session_id}",
            "confidence": "observed",
        }
    return {
        "kind": "continue",
        "label": "Continue",
        "title": "Continue in this chat",
        "why": str(workflow.get("why") or "The prompt looks narrow enough to run in the current chat."),
        "next_step": "Copy the brief if you want the checkpoint wrapper, or paste the original prompt unchanged.",
        "primary_label": "Copy brief",
        "primary_url": "",
        "confidence": "observed",
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


# A short session, in model calls. Set at the top of the shortest length bucket
# measured locally, which is where the share of sessions producing nothing was
# highest -- longer sessions land work more often, not less.
FALSE_START_MAX_CALLS = 15
# Enough of them to be a habit rather than a bad week. Below this the card stays
# silent: "3 of 4 were false starts" is a coin flip dressed as a pattern.
FALSE_START_MIN_SESSIONS = 5

# Capped at the number of non-status hues this palette has. Past that the tail
# folds into one neutral segment rather than reusing amber or red, which mean
# something else on every other surface here.
UNBANKED_CHART_REPOS = 3

# Slices before the tail folds into "Other". A hard limit, not taste: this
# palette has exactly three hues that do not already mean something -- amber and
# red are warning and error everywhere else here -- and past about five slices a
# pie stops being readable regardless of colour.
COMPOSITION_SLICES = 3
# One slice this size makes the chart a number in disguise.
COMPOSITION_DOMINANT_PCT = 95.0
# Off for now: the chart renders with a caveat instead of withholding itself, so
# the degenerate case can be reviewed rather than guessed at. Flip to True to
# have it hide, which is the behaviour the rest of this dashboard prefers.
COMPOSITION_HIDE_WHEN_DOMINANT = False
# Below this a scatter is a handful of dots and any pattern in it is imagined.
MODEL_SCATTER_MIN_POINTS = 8


def _composition_chart(rows: list[dict[str, object]]) -> dict[str, object] | None:
    """Share of total tokens, for a pie beside the ranked bars.

    The bars answer "which is biggest" -- each is sized against the largest, not
    against the total -- so they cannot say "this one is 79% of everything".
    That is the only question this adds.

    Measured in tokens for the same reason the bars are: a plan-based tool is
    priced at zero on purpose, and a share-of-spend view would draw it as absent.
    """
    weighted = [row for row in rows if int(row.get("tokens") or 0) > 0]
    total = sum(int(row.get("tokens") or 0) for row in weighted)
    if len(weighted) < 2 or total <= 0:
        return None

    ranked = sorted(weighted, key=lambda row: int(row.get("tokens") or 0), reverse=True)
    def _legend_label(row: dict[str, object]) -> str:
        # A legend is read sideways at a glance. Project rows are paths that
        # share a parent directory, so they differ only in the last few
        # characters -- exactly what truncation eats. Tool rows are already
        # names like "claude-code (desktop)" and are left alone.
        name = str(row.get("name") or row.get("short_name") or "unknown")
        if "/" in name or "\\" in name:
            return PurePath(name.replace("\\", "/")).name or name
        return str(row.get("short_name") or name)

    segments: list[dict[str, object]] = [
        {
            "label": _legend_label(row),
            "title": str(row.get("name") or ""),
            "tokens": int(row.get("tokens") or 0),
            "kind": "item",
        }
        for row in ranked[:COMPOSITION_SLICES]
    ]
    tail = ranked[COMPOSITION_SLICES:]
    if tail:
        segments.append({
            "label": f"{len(tail)} more",
            "title": ", ".join(str(row.get("short_name") or "") for row in tail[:6]),
            "tokens": sum(int(row.get("tokens") or 0) for row in tail),
            "kind": "other",
        })
    segments = [segment for segment in segments if int(segment["tokens"]) > 0]
    if len(segments) < 2:
        return None

    for segment in segments:
        segment["pct"] = round(100.0 * int(segment["tokens"]) / total, 1)
        segment["tokens_label"] = compact_int(int(segment["tokens"]))
    top = max(float(segment["pct"]) for segment in segments)
    return {
        "segments": segments,
        "total_tokens": total,
        "total_label": compact_int(total),
        # Reported rather than acted on, so the caller decides whether a
        # single-slice chart is withheld or shown with a caveat.
        "dominant": top >= COMPOSITION_DOMINANT_PCT,
        "dominant_pct": top,
        "hide_when_dominant": COMPOSITION_HIDE_WHEN_DOMINANT,
    }


def _model_scatter(rows: list[LocalSession]) -> dict[str, object] | None:
    """One dot per session: tokens against cost, coloured by model.

    The model-mix card spends three prose branches explaining whether a model
    costs more per token or is simply pointed at bigger jobs. Plotted, that
    distinction is geometric and needs no explaining.

    Axes are logarithmic because the data is: locally, sessions span 33K to 382M
    tokens and six cents to $258. On linear axes every session but the largest
    collapses into one corner. The trade is that price per token reads as
    vertical offset rather than slope -- same rate means the same diagonal, and
    a dearer model sits above a cheaper one rather than climbing more steeply.

    Whether the work landed rides a second channel -- filled or hollow -- rather
    than colour, which is already carrying model identity. The thing to look for
    is a hollow dot high up: an expensive session that produced nothing,
    whichever model ran it.

    Deliberately not a verdict on which model is better value. If the dear model
    gets the hard problems, it will land less often for reasons that have
    nothing to do with the model, and nothing local can separate those.
    """
    try:
        snapshots = evidence_snapshots_for_sessions({row.session_id for row in rows})
    except OSError:
        snapshots = {}

    priced = [
        row for row in rows
        if row.cost_usd > 0 and (row.tokens_in + row.tokens_out) > 0
    ]
    if len(priced) < MODEL_SCATTER_MIN_POINTS:
        return None

    # A plan-based session did real work at a cost local logs cannot know, and a
    # log axis has no room for zero. Both facts argue against plotting it: put it
    # on the floor and the chart says the work was nearly free, which is a
    # stronger claim than "unpriced" and the wrong one. So it is withheld -- and
    # counted, because a card headed "one dot per session" that quietly draws
    # fewer is the same silence the replay chart's clipped turns were.
    unpriced = [
        row for row in rows
        if row.cost_usd <= 0 and (row.tokens_in + row.tokens_out) > 0
    ]
    unpriced_tokens = sum(row.tokens_in + row.tokens_out for row in unpriced)
    unpriced_tools: dict[str, int] = defaultdict(int)
    for row in unpriced:
        unpriced_tools[_tool_surface_key(row)] += 1

    by_model: dict[str, int] = defaultdict(int)
    for row in priced:
        by_model[display_model_name(row.model or "unknown")] += 1
    if len(by_model) < 2:
        return None
    named = [
        model for model, _ in
        sorted(by_model.items(), key=lambda item: item[1], reverse=True)[:COMPOSITION_SLICES]
    ]

    points: list[dict[str, object]] = []
    for row in priced:
        model = display_model_name(row.model or "unknown")
        snapshot = snapshots.get(row.session_id)
        landed = (
            isinstance(snapshot, dict)
            and bool(snapshot.get("commit_shas"))
            and snapshot.get("inferred_outcome") != "churned"
        )
        points.append({
            "session_id": row.session_id,
            "model": model if model in named else "other",
            "model_label": model,
            "project": project_label(row.project_path),
            "tokens": row.tokens_in + row.tokens_out,
            "cost_usd": round(row.cost_usd, 6),
            "cost_label": money(row.cost_usd),
            "tokens_label": compact_int(row.tokens_in + row.tokens_out),
            # None rather than False where nothing was ever looked at, so
            # "unexamined" cannot be drawn as "produced nothing".
            "landed": landed if isinstance(snapshot, dict) else None,
        })

    legend = [{"label": model, "kind": "item"} for model in named]
    if any(point["model"] == "other" for point in points):
        legend.append({"label": "other models", "kind": "other"})
    return {
        "points": points,
        "legend": legend,
        "unexamined": sum(1 for point in points if point["landed"] is None),
        "unpriced": {
            "sessions": len(unpriced),
            "tokens": unpriced_tokens,
            "tokens_label": compact_int(unpriced_tokens),
            "tools": [
                tool for tool, _ in
                sorted(unpriced_tools.items(), key=lambda item: item[1], reverse=True)
            ],
        },
    }


def _tool_model_breakdown(rows: list[LocalSession]) -> dict[str, object] | None:
    """Which models each tool actually ran, as one stacked bar per tool.

    The two flat lists above it -- by model, by tool -- cannot be crossed by eye.
    Seeing that Opus is most of your spend and that Claude Code is most of your
    tokens does not tell you whether Codex is running an expensive model or a
    cheap one, and that is the question worth asking of a tool you did not pick
    the model for.

    Models are ranked globally and capped at the palette's three non-status
    hues, with the rest folded into one neutral bucket. Crucially the colour map
    is global: a model keeps the same colour in every tool's bar, so the eye can
    follow it across rows. Colouring per row would make the same model change
    colour between tools, which is the one thing a reader must never have to
    second-guess.

    Tokens, not dollars: a plan-based tool is priced at zero on purpose, and
    this chart exists partly to show what such a tool is doing.
    """
    per_tool: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_model: dict[str, int] = defaultdict(int)
    for row in rows:
        breakdown = row.model_breakdown or {
            (row.model or "unknown"): {"tokens_in": row.tokens_in, "tokens_out": row.tokens_out}
        }
        tool = _tool_surface_key(row)
        for model_name, stats in breakdown.items():
            tokens = int(stats.get("tokens_in", 0) or 0) + int(stats.get("tokens_out", 0) or 0)
            if tokens <= 0:
                continue
            key = display_model_name(model_name or "unknown")
            per_tool[tool][key] += tokens
            per_model[key] += tokens

    tools = {tool: models for tool, models in per_tool.items() if sum(models.values()) > 0}
    if len(tools) < 2 and len(per_model) < 2:
        return None

    # Every tool's own leading model gets named before any runner-up elsewhere.
    # Ranking globally instead put the three Claude models in the legend and
    # folded Codex's model into "Other" -- so the one row whose model you did not
    # choose yourself, which is the row worth looking at, lost its label.
    ranked_models = sorted(per_model.items(), key=lambda item: item[1], reverse=True)
    leaders = {max(models.items(), key=lambda item: item[1])[0] for models in tools.values()}
    named = [model for model, _ in ranked_models if model in leaders][:COMPOSITION_SLICES]
    for model, _ in ranked_models:
        if len(named) >= COMPOSITION_SLICES:
            break
        if model not in named:
            named.append(model)
    tail_count = len(ranked_models) - len(named)
    legend = [{"label": model, "kind": "item"} for model in named]
    if tail_count > 0:
        legend.append({"label": f"{tail_count} more", "kind": "other"})

    rows_out: list[dict[str, object]] = []
    for tool, models in sorted(tools.items(), key=lambda item: sum(item[1].values()), reverse=True):
        total = sum(models.values())
        segments = [
            {"label": model, "tokens": models.get(model, 0), "kind": "item"}
            for model in named
        ]
        if tail_count > 0:
            segments.append({
                "label": f"{tail_count} more",
                "tokens": sum(count for model, count in models.items() if model not in named),
                "kind": "other",
            })
        for segment in segments:
            segment["pct"] = round(100.0 * int(segment["tokens"]) / total, 1) if total else 0.0
            segment["tokens_label"] = compact_int(int(segment["tokens"]))
        rows_out.append({
            "tool": tool,
            "total_tokens": total,
            "total_label": compact_int(total),
            "segments": segments,
            # Named so a reader can see at a glance which tool is on which model
            # without reading the bar, which is the whole point of crossing them.
            "top_model": max(models.items(), key=lambda item: item[1])[0] if models else None,
        })
    return {"legend": legend, "tools": rows_out}


def _unbanked_chart(ledger: Ledger) -> dict[str, object] | None:
    """Unbanked spend split by *where* it happened, not by why.

    The by-reason split is only ever two buckets, and the card already states
    both as a headline percentage -- a two-piece bar tells a reader nothing the
    sentence did not. Splitting by repo grows a segment for every project worked
    in, and points at something actionable: which repo is accumulating work that
    never landed.

    Spend outside any repo is a segment rather than a footnote, because it is the
    one piece with a different fix -- a session started in the wrong directory,
    not exploration that went nowhere. Every dollar of unbanked_usd is placed, so
    the segments sum to the headline rather than to some subset of it.
    """
    outside = float(ledger.unbanked_by_reason.get(UNBANKED_OUTSIDE_REPO, 0.0) or 0.0)
    ranked = sorted(ledger.unbanked_by_repo.items(), key=lambda item: item[1], reverse=True)
    if not ranked and outside <= 0:
        return None

    # Legend labels are the repo's own name, not its path. A stacked bar's legend
    # is read sideways at a glance, and three full paths sharing a parent
    # directory differ only in their last few characters -- exactly the part that
    # gets truncated. The full path stays available as the row's title.
    segments: list[dict[str, object]] = [
        {
            "label": Path(str(repo)).name or short_path(str(repo)),
            "title": short_path(str(repo)),
            "usd": round(spend, 6),
            "kind": "repo",
        }
        for repo, spend in ranked[:UNBANKED_CHART_REPOS]
        if spend > 0
    ]
    tail = sum(spend for _, spend in ranked[UNBANKED_CHART_REPOS:])
    if tail > 0:
        segments.append({
            "label": f"{len(ranked) - UNBANKED_CHART_REPOS} more repos",
            "usd": round(tail, 6),
            "kind": "other",
        })
    if outside > 0:
        segments.append({"label": "Outside any repo", "usd": round(outside, 6), "kind": "outside"})
    if len(segments) < 2:
        # One segment is a stat, not a chart; the headline already carries it.
        return None
    total = sum(float(item["usd"]) for item in segments)
    for item in segments:
        item["pct"] = round(100.0 * float(item["usd"]) / total, 1) if total > 0 else 0.0
        item["label_usd"] = money(float(item["usd"]))
    return {"segments": segments, "total_usd": round(total, 6), "total_label": money(total)}


def _checkpoint_card(
    ledger: Ledger | None,
    events: list[LocalEvent],
    cards: list[dict[str, object]],
) -> dict[str, object]:
    """Checkpoint distance for the session Home is charting.

    Scoped to the charted session's repo rather than the whole machine: the
    figure sits under a hero that is one session, and a distance summed across
    every repo would be the scope defect this dashboard keeps producing.
    """
    if ledger is None:
        return {"available": False, "reason": "Could not read git history for the active repos."}
    live = next((card for card in cards if card.get("charted_because_live")), None)
    if live is None:
        return {"available": False, "reason": "No live session to measure from."}
    repo = str(live.get("project_full") or "")
    if not repo:
        return {"available": False, "reason": "The charted session has no resolved project path."}
    card = dict(checkpoint_distance(ledger, events, repo))
    if card.get("available"):
        hours = float(card.get("hours_since") or 0)
        card["elapsed_label"] = (
            f"{hours / 24:.1f}d" if hours >= 24
            else (f"{hours:.0f}h" if hours >= 1 else f"{hours * 60:.0f}m")
        )
        card["spend_label"] = money(float(card.get("spend_usd") or 0))
        baseline = card.get("baseline") or {}
        if isinstance(baseline, dict) and baseline.get("available"):
            baseline["median_label"] = money(float(baseline.get("median_usd") or 0))
    return card


def _first_run_card(
    *,
    sessions: int,
    spend_label: str,
    window_label: str,
    replayed_spend_share_pct: object,
    coverage: list[dict[str, object]],
    unbanked: dict[str, object],
    ledger: Ledger | None,
) -> dict[str, object]:
    """The screen a machine sees once, before anything is gated.

    Deliberately outside the nav: it is a moment in a journey, not a
    destination, and you should not be able to navigate back to your own
    onboarding a month later.

    Two cases, and they fail in opposite directions if you only design for one.
    AIWatcher reads history that already exists, so somebody who has been using
    Claude Code for months is met with everything at once at the moment they
    understand least. Somebody genuinely new is met with nine empty states, none
    of which say when anything will appear. The first gets a finding about
    themselves; the second gets a status report about their machine.

    Neither gets invented numbers. "Not measurable" is a first-class state in
    this product and there is a written rule against a figure that answers the
    wrong question -- breaking both on the very first screen anyone sees would
    cost more than the polish gains.
    """
    gated = [row for row in coverage if row.get("status") == "automatic"]
    dismissed = first_run_dismissed_at()
    if dismissed:
        return {"show": False, "reason": "Already dismissed.", "dismissed_at": dismissed}
    if gated:
        # The one action this screen exists to prompt is already done.
        return {"show": False, "reason": "A tool is already gated automatically."}

    card: dict[str, object] = {
        "show": True,
        "reason": None,
        "gate_installed": False,
        # Named so the copy can say what it can and cannot see, rather than
        # implying the listed tools are the covered ones.
        "readable": [str(row.get("tool")) for row in coverage
                     if row.get("status") in {"automatic", "limited"}],
        "unmeasured": [
            {"tool": str(row.get("tool")), "why": str(row.get("status_label") or "")}
            for row in coverage if row.get("status") not in {"automatic", "limited"}
        ],
        "repos": len(ledger.repos) if ledger else 0,
        "sessions": sessions,
    }
    if sessions <= 0:
        card["kind"] = "new"
        return card

    # History already on the machine, so the finding needs no configuration to
    # produce and is about them rather than about the product.
    card["kind"] = "has_history"
    card["spend_label"] = spend_label
    card["window_label"] = window_label
    card["replayed_spend_share_pct"] = replayed_spend_share_pct
    card["unbanked_label"] = unbanked.get("unbanked_label") if unbanked.get("available") else None
    return card


def _unbanked_card(ledger: Ledger | None) -> dict[str, object]:
    """Spend in this window with no commit behind it."""
    if ledger is None:
        return {"available": False, "reason": "Could not read git history for the active repos."}

    card = dict(unbanked_summary(ledger))
    card["chart"] = _unbanked_chart(ledger)
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
            # The numeric field is emitted alongside the label because the column
            # header offers sorting on it. Every other sortable column ships both;
            # this one shipped only the label, so the header advertised a sort
            # that had nothing to sort by and silently did nothing when clicked.
            "usd_per_surviving_line": (
                round(float(measured["usd_per_surviving_line"]), 6)
                if measured.get("usd_per_surviving_line") is not None else None
            ),
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
            "receipt_viewed_at": row.get("receipt_viewed_at"),
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


def _false_starts_card(all_rows: list[LocalSession]) -> dict[str, object] | None:
    """Short sessions that produced no commit at all.

    Surfaced because a length chart showed it and then failed to earn a place:
    across local history the share of sessions that land work does not fall with
    length, it rises, and the shortest bucket lands least often. The cliff was
    not the finding; this was.

    Read over all history rather than the selected window. A week holds a
    handful of sessions, and "3 of 4 were false starts" is a coin flip wearing
    the clothes of a pattern.

    Deliberately not called waste. A short session with no commit is often a
    question that was answered, and nothing local can tell that apart from a
    start that went nowhere -- the same limit the unbanked card states about
    uncommitted work. The card reports the count and the money and leaves the
    judgement where it belongs.
    """
    try:
        snapshots = evidence_snapshots_for_sessions({row.session_id for row in all_rows})
    except OSError:
        return None
    if not snapshots:
        return None

    short = [
        row for row in all_rows
        if 0 < row.agent_calls <= FALSE_START_MAX_CALLS
        and isinstance(snapshots.get(row.session_id), dict)
    ]
    if len(short) < FALSE_START_MIN_SESSIONS:
        return None
    empty = [row for row in short if not snapshots[row.session_id].get("commit_shas")]
    if len(empty) < FALSE_START_MIN_SESSIONS:
        return None

    share = round(100.0 * len(empty) / len(short))
    spent = sum(row.cost_usd for row in empty)
    costliest = max(empty, key=lambda row: row.cost_usd, default=None)
    return {
        "id": "false-starts",
        "title": f"{len(empty)} short sessions produced no commit at all",
        "body": (
            f"Of {len(short)} sessions running {FALSE_START_MAX_CALLS} model calls or fewer across "
            f"your recent history, {share}% left nothing behind — {money(spent)} of spend, which is "
            "small. The count is the point, not the money: some of these answered a question worth "
            "asking, and nothing local tells that apart from a start that went nowhere. A run of "
            "them usually means opening in the wrong repo, or asking before scoping."
        ),
        # No dollar figure, for the same reason model-mix carries none: the money
        # is not recoverable. Some of these sessions answered a question worth
        # asking, so ranking this among the dollar findings would promise a saving
        # that does not exist -- and short sessions are cheap enough that it would
        # rank last anyway, reading as trivial when the count is the finding.
        "impact_usd": None,
        "session_id": costliest.session_id if costliest else None,
        "severity": "info",
    }


def _replay_turn_chart(session_id: str, events: list[LocalEvent]) -> dict[str, object] | None:
    """Per-turn cost for one session, split into new context and replayed history.

    Priced the way replayed_context_cost prices it -- replay at the cache-read
    rate, not face value -- so the chart and the card it sits under can never
    quote different totals for the same session.

    Returns None rather than an empty chart when the source reports no cache
    buckets: a flat zero replay band would read as "this session replayed
    nothing", when the truth is that nothing was measured.
    """
    priced = sorted(
        (event for event in events if event.session_id == session_id and event.cost_usd > 0),
        key=lambda event: (event.timestamp or MIN_DT),
    )
    turns = priced[-REPLAY_CHART_MAX_TURNS:]
    # Where the drawn window sits in the session. The axis used to read "turn 1"
    # for whatever the clip happened to start at, which on this repo's worst
    # session meant labelling turn 852 as the first one. LocalEvent.turn is no
    # help -- it repeats across a session rather than counting up -- so position
    # in the priced sequence is what there is.
    first_turn_no = len(priced) - len(turns) + 1
    if len(turns) < 3 or not any(event.cache_read_tokens for event in turns):
        return None

    replayed: list[float] = []
    written: list[float] = []
    fresh: list[float] = []
    for event in turns:
        replay_usd = cache_read_cost(event.model, event.cache_read_tokens, event.timestamp)
        # Writing the conversation to cache is not new context, and folding it
        # into the fresh band said it was: the worst turn here showed $7.54 of
        # "new context" against $0.05 of actual new work, out by a factor of 140.
        #
        # Priced as a residual rather than from the token count. A write is
        # billed at 1.25x or 2x the input rate depending on its lifetime, and the
        # event only carries the two buckets added together, so repricing the
        # tokens would have to guess which. Everything else in the turn can be
        # priced exactly, and what is left over is the write -- which also keeps
        # the three bands summing to the turn's actual cost rather than to an
        # estimate of it.
        plain_input = max(0, event.tokens_in - event.cache_read_tokens - event.cache_write_tokens)
        fresh_usd = estimate_cost(event.model, plain_input, event.tokens_out, when=event.timestamp)
        write_usd = max(0.0, event.cost_usd - replay_usd - fresh_usd)
        replayed.append(round(replay_usd, 6))
        written.append(round(write_usd, 6))
        fresh.append(round(max(0.0, fresh_usd), 6))

    # Raw series only. The hover text used to arrive as five parallel arrays of
    # formatted strings, which was affordable at sixty turns and is not at nine
    # hundred -- the labels are all derivable, so the client formats them and the
    # payload carries numbers. Four decimal places: the axis tops out well under a
    # dollar, so this is finer than a pixel, and six was costing bytes per turn to
    # say nothing.
    def series(values):
        return [round(value, 4) for value in values]

    # Seconds from the first drawn turn, rather than a formatted timestamp each.
    # A session can span days and the axis needs real dates, but 900 date strings
    # cost more than one start time and an offset apiece.
    start_at = turns[0].timestamp if turns[0].timestamp else None
    offsets = [
        int((event.timestamp - start_at).total_seconds())
        if event.timestamp and start_at else 0
        for event in turns
    ]
    # Only the turns that actually wrote the conversation down, not a mostly-zero
    # column: they are about 1.5% of a long session, and they are the only turns
    # with anything to say that the trend does not already show.
    write_turns = [
        {"i": index, "tokens": event.cache_write_tokens}
        for index, event in enumerate(turns)
        if event.cache_write_tokens >= CACHE_WRITE_TURN_TOKENS
    ]

    return {
        "replayed_usd": series(replayed),
        "written_usd": series(written),
        "fresh_usd": series(fresh),
        "resent_tokens": [event.cache_read_tokens for event in turns],
        "second_offsets": offsets,
        "started_at": start_at.astimezone().isoformat() if start_at else None,
        "write_turns": write_turns,
        "turns": len(turns),
        "first_turn_no": first_turn_no,
        "session_turns": len(priced),
        "replayed_total_usd": round(sum(replayed), 6),
        "written_total_usd": round(sum(written), 6),
        "session_total_usd": round(sum(replayed) + sum(written) + sum(fresh), 6),
    }


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
        # The chart is one session's turns, and the card never said which. Naming
        # it also decides what the closing advice can honestly be: "compact this"
        # is an instruction, and an instruction aimed at a session someone left
        # hours ago cannot be carried out.
        #
        # The session itself is still the worst one, not the worst still-active
        # one. The money claim above rests on it being the worst, and swapping in
        # a smaller session to make the advice actionable would leave the
        # headline resting on a session the card no longer shows.
        top_session = next((row for row in rows if row.session_id == top["session_id"]), None)
        top_state = session_state(top_session) if top_session else {}
        top_live = str(top_state.get("status") or "") in {"active", "recent"}
        quiet_hours = float(top_state.get("age_seconds") or 0) / 3600
        quiet_label = f"{quiet_hours / 24:.1f}d" if quiet_hours >= 24 else f"{quiet_hours:.0f}h"
        closing = (
            "It is still going, so compacting now is what buys the rest back."
            if top_live
            else f"It has been quiet for {quiet_label}, so this is what compacting earlier would have saved."
        )
        cards.append({
            "id": "replayed-context",
            # The share is window-scoped (total replayed / total window cost over
            # every session). The body already says "this window"; the headline
            # did not, and it is the half that gets read.
            "title": f"{share:.0f}% of this window's spend went on re-sending conversation history",
            "body": (
                f"{money(replay['total_replayed_usd'])} of {money(window_cost)} this window. The worst session replayed "
                f"{top['replayed_pct']:.0f}% of its context, {money(top['replayed_usd'])} of its "
                f"{money(top['session_usd'])}. {closing}"
            ),
            "session_label": (
                f"{project_name(project_key(top.get('project_path')))} · {top.get('tool') or 'session'}"
                if top.get("project_path") else str(top.get("tool") or "session")
            ),
            "impact_usd": replay["total_replayed_usd"],
            "session_id": top["session_id"],
            "severity": "high" if share >= 40 else "medium",
            # Per-turn split for the worst session. The card's own numbers are
            # session totals, which cannot show the one thing that matters here:
            # replay is not a flat overhead, it compounds turn by turn.
            "chart": _replay_turn_chart(top["session_id"], all_events),
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

    feed_now = datetime.now().astimezone()
    daily_spend = _daily_spend_chart(all_events, feed_now - timedelta(days=days), feed_now)
    if daily_spend:
        spikes = daily_spend["spike_count"]
        # Deliberately unjudged in both title and severity. This dashboard
        # refuses to call raw spend a failure -- spending more is not a fault on
        # its own, which is why the API-value tile carries a neutral rail rather
        # than a red one. The card reports shape, and leaves the verdict on
        # whether that shape is a problem to the person who spent it.
        body = (
            f"Your typical day over the last {daily_spend['baseline_days']} active days ran "
            f"{daily_spend['band_label']}, around {daily_spend['median_label']}. "
        )
        if spikes:
            body += (
                f"{spikes} day{'' if spikes == 1 else 's'} in this window went at least "
                f"{daily_spend['spike_multiple']:g}x past the top of that range. "
            )
        else:
            body += "No day in this window went clearly past it. "
        body += (
            "The band is the middle half of your own days, not a budget -- half of any "
            "stretch falls outside it by definition."
        )
        cards.append({
            "id": "daily_spend",
            "title": "Where the spend actually landed",
            "body": body,
            # None, not 0.0: this card describes shape and claims no saving, and
            # a "$0.00" beside it reads as a savings estimate that came out empty.
            "impact_usd": None,
            "session_id": None,
            "severity": "info",
            "chart": daily_spend,
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

    false_starts = _false_starts_card(all_rows)
    if false_starts:
        cards.append(false_starts)

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


# A sparkline is only worth its ink once there are a few days to compare. Below
# this the "trend" is one spike and a flat line, which reads as a shape without
# being one.
TILE_SPARK_MIN_ACTIVE_DAYS = 3


def _tile_trend_days(since: datetime, now: datetime) -> list[date]:
    """Every day in the window, including the empty ones.

    Gaps have to be present as zeroes rather than absent: a quiet Sunday that is
    simply missing from the array pulls Monday left and draws the week shorter
    than it was.
    """
    start, end = since.date(), now.date()
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _tile_trends(
    rows: list[LocalSession],
    all_events: list[LocalEvent],
    window_outcomes: dict[str, dict[str, object]],
    interventions: list[dict[str, Any]],
    since: datetime,
    now: datetime,
) -> dict[str, object] | None:
    """Daily series behind the four Home metric tiles.

    Bucketed by event timestamp, not by session.started_at. Sessions are clipped
    to the window for their spend but keep their original start date, so a
    session opened last month and worked on today would post today's dollars to
    last month -- off the left edge of the chart entirely. Events carry their own
    timestamp and their costs sum to the session's, which is the same property
    clip_sessions_to_window relies on.

    Sessions and outcomes count on the day of their first in-window event, so
    each session lands on exactly one day and the bars sum to the tile above
    them. Counting a three-day session on all three days would make the
    sparkline total more than the number it sits under.

    Outcomes are NOT bucketed by recorded_at. An outcome is stamped when you
    marked it, so a weekend spent reviewing a fortnight of work would draw a
    spike on the weekend and empty days across the fortnight -- a chart of
    reviewing habits wearing the label of a chart of results.
    """
    days = _tile_trend_days(since, now)
    if len(days) < TILE_SPARK_MIN_ACTIVE_DAYS:
        return None
    index = {day: position for position, day in enumerate(days)}

    def blank() -> list[float]:
        return [0.0 for _ in days]

    def slot(moment: datetime | None) -> int | None:
        """Bucket a timestamp, or None if it falls outside the window.

        The lower bound is compared as a timestamp, not as a date. `since` is a
        moment mid-morning, so testing only that the date is in range lets in
        everything that happened earlier on that first day -- spend the tile
        above has already clipped away, which made the sparkline sum to more
        than the number it sits under. Day zero stays a part-day in both.
        """
        if moment is None:
            return None
        stamp = moment.astimezone()
        if stamp < since:
            return None
        return index.get(stamp.date())

    api_value = blank()
    first_day: dict[str, date] = {}
    for event in all_events:
        position = slot(event.timestamp)
        if position is None:
            continue
        api_value[position] += event.cost_usd
        day = days[position]
        if event.session_id not in first_day or day < first_day[event.session_id]:
            first_day[event.session_id] = day

    sessions, useful, judged = blank(), blank(), blank()
    for row in rows:
        day = first_day.get(row.session_id)
        if day is None:
            # Cursor and the Codex sqlite path emit no per-turn events, so these
            # rows have nothing to bucket by and would silently vanish from a
            # chart that sums to the tile. They already carry CLIP_FALLBACK_NOTE
            # for the same imprecision; fall back to the session's own clock.
            position = slot(row.started_at) if slot(row.started_at) is not None else slot(row.updated_at)
            if position is None:
                continue
            day = days[position]
        position = index[day]
        sessions[position] += 1
        outcome = (window_outcomes.get(row.session_id) or {}).get("outcome")
        if outcome:
            judged[position] += 1
        if outcome == "useful":
            useful[position] += 1

    preflight = blank()
    for row in interventions:
        try:
            created = datetime.fromisoformat(str(row.get("created_at", "")))
        except (TypeError, ValueError):
            continue
        position = slot(created)
        if position is not None:
            preflight[position] += 1

    # An unjudged session is not a session that produced nothing. Everything
    # after the last day with a verdict is withheld rather than drawn: a line
    # falling to zero across the recent tail reads as work that stopped landing,
    # when it is only work nobody has marked yet. Same call the cost-per-
    # surviving-line chart makes about its own too-recent tail.
    judged_through = max((position for position, count in enumerate(judged) if count), default=None)

    def active(values: list[float]) -> int:
        return sum(1 for value in values if value > 0)

    series: dict[str, object] = {}
    if active(api_value) >= TILE_SPARK_MIN_ACTIVE_DAYS:
        series["apiValue"] = {
            "values": [round(value, 6) for value in api_value],
            "labels": [money(value) for value in api_value],
        }
    if active(sessions) >= TILE_SPARK_MIN_ACTIVE_DAYS:
        series["sessions"] = {
            "values": [int(value) for value in sessions],
            "labels": [f"{int(value)} session{'' if value == 1 else 's'}" for value in sessions],
        }
    if active(preflight) >= TILE_SPARK_MIN_ACTIVE_DAYS:
        series["preflightDecisions"] = {
            "values": [int(value) for value in preflight],
            "labels": [f"{int(value)} decision{'' if value == 1 else 's'}" for value in preflight],
        }
    if judged_through is not None and active(useful[: judged_through + 1]) >= TILE_SPARK_MIN_ACTIVE_DAYS:
        entry: dict[str, object] = {
            "values": [int(value) for value in useful],
            "labels": [f"{int(value)} useful" for value in useful],
            "judged_through": judged_through,
        }
        if judged_through < len(days) - 1:
            unjudged = len(days) - 1 - judged_through
            entry["caveat"] = (
                f"{unjudged} more recent day{'' if unjudged == 1 else 's'} not judged yet, so not drawn."
            )
        series["usefulOutcomes"] = entry

    if not series:
        return None
    return {"days": [day.isoformat() for day in days], "series": series}


# How many trailing active days form the band, and the floor below which the
# whole chart is withheld.
#
# 12 is where the false-alarm rate falls under 3%. Bootstrapping a band from
# this repo's own daily spend, a genuinely big day (1.5x the upper edge) is
# caught 95% of the time at 12 days and 92% at 6 -- but at 6 days one ordinary
# day in eight is wrongly called a spike. That is the worst failure available
# here: a new user's first fortnight peppered with false alarms about a tool
# they are still deciding whether to trust. Past 12 the gain is two points of
# detection for weeks more waiting.
SPEND_BASELINE_MIN_ACTIVE_DAYS = 12
SPEND_BASELINE_TRAILING_DAYS = 14
# A returning user should not be measured against habits from a quarter ago.
SPEND_BASELINE_MAX_AGE_DAYS = 60
# Only days this far past the upper edge are marked. The edge itself is not
# precise enough to support a finer call: resampling moves it by about half the
# band's own width even with a month of history, so a day sitting just above it
# is inside the uncertainty of where "just above" is. At 1.5x the verdict holds
# 95%+ of the time, which is the whole reason this multiple exists.
SPEND_SPIKE_MULTIPLE = 1.5


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _daily_spend_chart(
    all_events: list[LocalEvent],
    since: datetime,
    now: datetime,
) -> dict[str, object] | None:
    """Daily spend against a trailing band of your own recent days.

    Answers the question the window total cannot: which days drove it. Spend is
    bucketed from events, so a session running across three days contributes to
    the day each turn actually happened on.

    The band is a percentile range, never mean +/- a standard deviation. Daily
    spend is heavily skewed -- on this repo's history the mean is $33.71 against
    a median of $18.20, with a standard deviation of $37.54, which puts the
    lower edge of such a band at *minus* $3.84. There is no drawing that.

    The band trails rather than being fixed, so it follows drift. Computed once
    over the whole history, the reference is whatever the user was doing when
    they started: on a 30-day view here that produced a band centred on $1.28
    and flagged 10 of 30 days, which is not anomaly detection but the discovery
    that they use the tool more now than in their first week. A trailing band
    climbs with them and flags 8, all genuinely large.

    Days with no activity are held apart from days with little. Drawn as zero
    they would sit below the band and read as restraint where there was only a
    weekend -- the same reason pace_vs_baseline drops inactive windows instead
    of averaging them in.
    """
    by_day: dict[date, float] = defaultdict(float)
    for event in all_events:
        if event.cost_usd <= 0 or not event.timestamp:
            continue
        by_day[event.timestamp.astimezone().date()] += event.cost_usd

    active_days = sorted(day for day, spend in by_day.items() if spend > 0)
    if not active_days:
        return None
    plotted = _tile_trend_days(since, now)
    if len(plotted) < 3:
        return None

    def band_for(day: date) -> tuple[float, float, float] | None:
        history = [
            by_day[past] for past in active_days
            if past < day and (day - past).days <= SPEND_BASELINE_MAX_AGE_DAYS
        ][-SPEND_BASELINE_TRAILING_DAYS:]
        if len(history) < SPEND_BASELINE_MIN_ACTIVE_DAYS:
            return None
        return (
            _percentile(history, 0.25),
            _percentile(history, 0.50),
            _percentile(history, 0.75),
        )

    # The most recent day is the test of whether there is enough history at all.
    # Without a band there the chart has nothing to say about now, which is what
    # anyone opening it is asking about.
    latest = band_for(plotted[-1])
    if latest is None:
        return None

    today = now.date()
    values, labels, lows, mids, highs, active, spikes = [], [], [], [], [], [], []
    # Formatted alongside the raw numbers rather than instead of them: the chart
    # plots the figures and the hover text reads the strings, and neither can be
    # derived from the other on the client without reimplementing money().
    day_labels, band_labels = [], []
    for day in plotted:
        spend = by_day.get(day, 0.0)
        band = band_for(day)
        values.append(round(spend, 6))
        labels.append(money(spend))
        day_labels.append(day.strftime("%a %d %b"))
        band_labels.append(f"{money(band[0])} to {money(band[2])}" if band else None)
        active.append(spend > 0)
        lows.append(round(band[0], 6) if band else None)
        mids.append(round(band[1], 6) if band else None)
        highs.append(round(band[2], 6) if band else None)
        # Today is still being spent -- by midday a median day has landed under
        # half its eventual total. That bias runs one way only: the total can
        # only climb, so a day already past the threshold is past it for good,
        # while a day that looks quiet may simply be early. Marking a spike on
        # the partial day is therefore safe; concluding anything from a low one
        # is not, which is why nothing is ever marked for being below the band.
        spikes.append(bool(
            band and spend > 0 and spend >= band[2] * SPEND_SPIKE_MULTIPLE
        ))

    history_for_latest = [
        past for past in active_days
        if past < plotted[-1] and (plotted[-1] - past).days <= SPEND_BASELINE_MAX_AGE_DAYS
    ][-SPEND_BASELINE_TRAILING_DAYS:]
    return {
        "kind": "daily_spend",
        "days": [day.isoformat() for day in plotted],
        "day_labels": day_labels,
        "values": values,
        "labels": labels,
        "band_labels": band_labels,
        "band_low": lows,
        "band_mid": mids,
        "band_high": highs,
        "active": active,
        "spikes": spikes,
        "spike_count": sum(spikes),
        "quiet_days": sum(1 for flag in active if not flag),
        "baseline_days": len(history_for_latest),
        "spike_multiple": SPEND_SPIKE_MULTIPLE,
        "partial_index": plotted.index(today) if today in plotted else None,
        "band_label": f"{money(latest[0])} to {money(latest[2])}",
        "median_label": money(latest[1]),
    }


def _split_analyst_overhead(
    rows: list[LocalSession],
) -> tuple[list[LocalSession], list[LocalSession]]:
    """The user's own work, and what AIWatcher spent looking over their shoulder."""
    user: list[LocalSession] = []
    overhead: list[LocalSession] = []
    for row in rows:
        (overhead if row.analyst_run else user).append(row)
    return user, overhead


def _analyst_overhead(rows: list[LocalSession], days: int) -> dict[str, object]:
    """The Second Opinion overhead line for this window.

    Always present, including at zero, and it says so in words. A line that
    appears only once it has something to confess is a line nobody trusts when
    it does appear -- and "no second opinions ran in this window" is a real
    answer to the question the line exists to answer.

    Dollars alone would be a true number answering the wrong question. Codex
    sessions are priced at $0 here by design, and a subscription user's really
    are free while an API-key user's are not -- the local logs cannot tell those
    two apart. Measured on this machine: eight analyst runs, four of them priced
    at nothing, so "$0.14" describes half of them. Tokens are the denominator
    that holds for every host, so the count of unpriced runs is stated rather
    than folded into a dollar figure that quietly omits them.
    """
    runs = len(rows)
    cost = sum(row.cost_usd for row in rows)
    tokens = sum(row.tokens_in + row.tokens_out for row in rows)
    unpriced = sum(1 for row in rows
                   if row.cost_usd <= 0 and (row.tokens_in + row.tokens_out) > 0)
    window = f"last {days} day{'s' if days != 1 else ''}"
    if not runs:
        label = "AIWatcher overhead: nothing this window"
    elif unpriced:
        label = (f"AIWatcher overhead: {money(cost)} this window, plus "
                 f"{unpriced} run{'' if unpriced == 1 else 's'} your CLI does not price")
    else:
        label = f"AIWatcher overhead: {money(cost)} this window"
    return {
        "runs": runs,
        "cost_usd": round(cost, 6),
        "cost_label": money(cost),
        "tokens": tokens,
        "tokens_label": compact_int(tokens),
        "unpriced_runs": unpriced,
        "window_label": window,
        "label": label,
        "detail": (
            f"{runs} second opinion{'' if runs == 1 else 's'} ran in the {window}, "
            f"on your own agent and your own key, costing {compact_int(tokens)} tokens. "
            f"Not counted in the totals above."
            if runs else
            f"No second opinions ran in the {window}."
        ),
    }


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
    # Spec 6. AIWatcher watches Claude Code sessions and Second Opinion
    # spawns them, so left alone it would report its own analyst runs as the
    # user's AI spend and inflate every number the product exists to give
    # them. They are split out here rather than dropped: excluded spend is a
    # number nobody can audit, and the first question a buyer asks is what
    # the feature costs. The split reads raw_cwd, because the project path
    # has already folded the sandbox back into the repository it sits in.
    rows, analyst_rows = _split_analyst_overhead(rows)
    month_rows, _ = _split_analyst_overhead(month_rows)

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
    tools = _append_detected_tool_rows(tools, detected)
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
    if detected.get("ollama") and not any(row.tool == "ollama" for row in rows):
        insights.append({
            "title": "Ollama detected, but usage is not measured",
            "body": "AIWatcher can see the local model runtime, but does not claim prompt, token, cost, or outcome coverage for Ollama yet.",
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
    if detected.get("cursor") and not any(row.tool == "cursor" for row in rows):
        insights.append({
            "title": "Cursor detected, but usage is limited",
            "body": "Cursor is installed or running, but local token/cost history is not reliably exposed yet. Use Prompt Companion for risky prompts and treat Cursor as coverage-limited.",
            "view": "setup",
            "cta": "Check coverage",
        })
    if detected.get("ollama") and not any(row.tool == "ollama" for row in rows):
        insights.append({
            "title": "Ollama detected, but usage is not measured",
            "body": "AIWatcher can see the local model runtime, but does not claim prompt, token, cost, or outcome coverage for Ollama yet.",
            "view": "setup",
            "cta": "Check coverage",
        })
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
    optimize = build_optimize_inventory(
        rows,
        outcomes=window_outcomes,
        handoff_decisions=handoff_decisions,
    )
    useful_rows = [
        row for row in rows
        if (window_outcomes.get(row.session_id) or {}).get("outcome") == "useful"
    ]
    useful_cost = sum(row.cost_usd for row in useful_rows)
    cost_per_useful = useful_cost / len(useful_rows) if useful_rows else None
    handoff_decisions = _handoff_decision_rows(limit=10, sessions=all_rows)
    # Replay share weighted by money rather than tokens. The two answers differ
    # enormously on the same window -- about 98% against 70% -- because replayed
    # context is billed at the cache-read rate, so counting tokens says nearly
    # everything was replay while counting spend says what it actually cost.
    _replay_cost = replayed_context_cost(rows)
    _window_cost = sum(row.cost_usd for row in rows)
    replayed_spend_share = (
        round(100.0 * float(_replay_cost["total_replayed_usd"]) / _window_cost, 1)
        if _replay_cost.get("available") and _window_cost > 0 else None
    )
    return {
        "generated_at": now.isoformat(),
        "cache_schema_version": SUMMARY_CACHE_SCHEMA_VERSION,
        "summary_complete": True,
        "_session_index": _session_index_payload(all_rows),
        # all_rows, not the window-clipped rows: whether something is
        # running right now does not change because you switched the
        # dropdown to 24 hours. Analyst spawns are still in here and stay
        # flagged rather than filtered, so AIWatcher's own live sessions
        # cannot pass as the user's.
        "presence": live_presence(all_rows, now=now),
        "days": days,
        # Spec 7. "No LLM calls" stopped being true the moment Second Opinion
        # could spawn one, and a privacy claim that is only true until a
        # feature ships is worse than no claim. Written honestly the
        # replacement is the stronger sentence anyway: the point was never
        # that no model runs, it was that nothing of yours leaves.
        "privacy": PRIVACY_CLAIMS,
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
            # Copy that says "of what this cost" must read this one, not the
            # token-weighted figure above it.
            "replayed_spend_share_pct": replayed_spend_share,
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
        # Daily shape behind the four tiles. Absent from the shell payload
        # below, which never scans events -- the tiles paint their numbers
        # immediately and grow sparklines when the full refresh lands, rather
        # than drawing an approximation from session start dates that would
        # disagree with the real one a second later.
        "tile_trends": _tile_trends(
            rows, all_events, window_outcomes, interventions, since, now,
        ),
        "survival": survival_summary,
        "analyst_overhead": _analyst_overhead(analyst_rows, days),
        "unbanked": unbanked,
        "changes": changes,
        "changes_meta": changes_meta,
        "projects": projects[:10],
        "projects_composition": _composition_chart(projects),
        "tools": tools,
        "tools_composition": _composition_chart(tools),
        "tool_models": _tool_model_breakdown(rows),
        # all_rows, not the clipped window: how a model behaves is a question
        # about your history, and a seven-day slice held too few priced sessions
        # to plot. Same reason the false-starts card reads all history.
        "model_scatter": _model_scatter(all_rows),
        "models": models[:10],
        "insights": insights,
        "notes": notes[:5],
        "coverage": [row.to_json() for row in surface_coverage(all_rows)],
        "setup": setup_checklist(),
        "watcher": get_watcher_status(),
        "context_health": context_health,
        "context_health_status": "ready",
        # Distance from the last checkpoint in the repo the charted session is
        # working in. Home's question is "is something happening right now that
        # I should deal with", and this is the only spend figure that can be
        # answered honestly in the present tense -- see checkpoint_distance for
        # why the obvious one, live unbanked spend, cannot be.
        "checkpoint": _checkpoint_card(window_ledger, all_events, context_health),
        "first_run": _first_run_card(
            sessions=int(stats["sessions"]),
            spend_label=money(float(stats["api_value_usd"])),
            window_label="Last 24 hours" if days == 1 else f"Last {days} days",
            replayed_spend_share_pct=replayed_spend_share,
            coverage=[row.to_json() for row in surface_coverage(all_rows)],
            unbanked=unbanked,
            ledger=window_ledger,
        ),
        "optimize": optimize,
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
    detected = discover_tools()
    tools = _append_detected_tool_rows(tools, detected)
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
    if detected.get("cursor") and not any(row.tool == "cursor" for row in rows):
        insights.append({
            "title": "Cursor detected, but usage is limited",
            "body": "Cursor is installed or running, but local token/cost history is not reliably exposed yet. Use Prompt Companion for risky prompts and treat Cursor as coverage-limited.",
            "view": "setup",
            "cta": "Check coverage",
        })
    if detected.get("ollama") and not any(row.tool == "ollama" for row in rows):
        insights.append({
            "title": "Ollama detected, but usage is not measured",
            "body": "AIWatcher can see the local model runtime, but does not claim prompt, token, cost, or outcome coverage for Ollama yet.",
            "view": "setup",
            "cta": "Check coverage",
        })
    useful_rows = [
        row for row in rows
        if (window_outcomes.get(row.session_id) or {}).get("outcome") == "useful"
    ]
    useful_cost = sum(row.cost_usd for row in useful_rows)
    cost_per_useful = useful_cost / len(useful_rows) if useful_rows else None
    handoff_decisions = _handoff_decision_rows(limit=10, sessions=all_rows)
    optimize = {
        "status": "quiet",
        "title": "Optimize workspace",
        "summary": "Background evidence refresh pending.",
        "impact_label": "no context savings claim",
        "evidence_label": "Observed",
        "candidates": [],
        "top": None,
        "recent_receipts": [],
        "checklist": _optimize_checklist([]),
    }
    return {
        "generated_at": now.isoformat(),
        "cache_schema_version": SUMMARY_CACHE_SCHEMA_VERSION,
        # First-paint shell: the event/evidence-backed sections below are still
        # placeholders, so this payload must never reach the disk cache.
        "summary_complete": False,
        "_session_index": _session_index_payload(all_rows),
        # Same figure, from the cached snapshot, so it can only be behind
        # and never ahead: a session started since the snapshot is missing
        # rather than invented, and one that ended reads quiet rather than
        # working. The background refresh corrects both within a tick.
        "presence": live_presence(all_rows, now=now),
        "days": days,
        # Spec 7. "No LLM calls" stopped being true the moment Second Opinion
        # could spawn one, and a privacy claim that is only true until a
        # feature ships is worse than no claim. Written honestly the
        # replacement is the stronger sentence anyway: the point was never
        # that no model runs, it was that nothing of yours leaves.
        "privacy": PRIVACY_CLAIMS,
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
        # Same shape as the real payload's, so the front end reads one contract
        # rather than distinguishing "not computed yet" from "not present".
        "checkpoint": {"available": False, "reason": "Background evidence refresh pending."},
        # Never on the first paint: deciding to show onboarding needs coverage
        # and history, and guessing wrong here means flashing an install screen
        # at someone who installed months ago.
        "first_run": {"show": False, "reason": "Background evidence refresh pending."},
        "projects": projects[:10],
        "projects_composition": _composition_chart(projects),
        "tools": tools,
        "tools_composition": _composition_chart(tools),
        "tool_models": _tool_model_breakdown(rows),
        # all_rows, not the clipped window: how a model behaves is a question
        # about your history, and a seven-day slice held too few priced sessions
        # to plot. Same reason the false-starts card reads all history.
        "model_scatter": _model_scatter(all_rows),
        "models": models[:10],
        "insights": insights,
        "notes": sorted({note for row in rows for note in row.notes})[:5],
        "coverage": [row.to_json() for row in surface_coverage(all_rows)],
        "setup": setup_checklist(),
        "watcher": get_watcher_status(),
        "context_health": [],
        "context_health_status": "pending",
        "optimize": optimize,
        "handoff_bubble": None,
        "handoff_decisions": handoff_decisions,
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


def _ask_action(label: str, url: str) -> dict[str, str]:
    return {"label": label, "url": url}


def _ask_answer(
    answer: str,
    *,
    confidence: str = "Observed local evidence",
    bullets: list[str] | None = None,
    actions: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "answer": answer,
        "confidence": confidence,
        "bullets": bullets or [],
        "actions": actions or [],
        "privacy": "Answered from AIWatcher local metadata on this machine. No prompt/source text is sent to a model.",
    }


def answer_local_question(question: str, days: int = 7) -> dict[str, object]:
    """Answer Companion Ask questions from local AIWatcher evidence only.

    This is deliberately deterministic. It should help a user navigate local
    evidence, not invent a chat-level diagnosis that AIWatcher cannot prove.
    """
    q = " ".join(str(question or "").strip().split())
    if not q:
        return _ask_answer(
            "Ask about a current AI session, context health, archive safety, prompt outcomes, spend, or coverage.",
            confidence="No question asked",
            bullets=[
                "Try: Can I archive this chat?",
                "Try: What is my context health?",
                "Try: Are my prompts driving outcomes?",
            ],
            actions=[_ask_action("Open Home", "/")],
        )
    lower = q.lower()
    try:
        summary = build_summary_cached(days)
    except Exception as exc:  # fail soft; the UI should stay usable during refresh
        return _ask_answer(
            f"I could not read the local AIWatcher summary yet: {type(exc).__name__}. Refresh the console or try again after the background index finishes.",
            confidence="Local index unavailable",
            actions=[_ask_action("Open Console", "/")],
        )

    if any(word in lower for word in ("archive", "cleanup", "clean up", "stale", "close chat", "delete chat")):
        optimize = summary.get("optimize") if isinstance(summary.get("optimize"), dict) else {}
        candidates = optimize.get("candidates") if isinstance(optimize.get("candidates"), list) else []
        top = optimize.get("top") if isinstance(optimize.get("top"), dict) else (candidates[0] if candidates and isinstance(candidates[0], dict) else {})
        if top:
            project = str(top.get("project_full") or top.get("project") or "this workspace")
            impact = str(top.get("impact_label") or top.get("context_at_risk_label") or "context at risk")
            evidence = str(top.get("evidence") or top.get("summary") or "local session history shows inactive same-project work")
            count = top.get("session_count") or top.get("items") or top.get("count")
            bullets = [
                f"Top candidate: {project}",
                f"Why it surfaced: {evidence}",
                f"Potential impact: {impact}",
                "AIWatcher cannot archive inside the AI app yet. Confirm the chat is finished before archiving it.",
            ]
            if count:
                bullets.insert(1, f"Related local sessions: {count}")
            return _ask_answer(
                "Review this as an archive candidate, not a delete command. Keep the final source-of-truth files and receipts, then archive the finished AI chat in the owning AI app if you agree it is done.",
                confidence="Observed/inferred from local session history",
                bullets=bullets,
                actions=[
                    _ask_action("Review Optimize", "/?view=control#optimizeWorkspace"),
                    _ask_action("Open Sessions", "/?view=sessions"),
                ],
            )
        return _ask_answer(
            "No strong archive candidate stands out in the current local window. I would not interrupt your work for cleanup right now.",
            actions=[_ask_action("Open Improve", "/?view=insights")],
        )

    if any(word in lower for word in ("context", "bloat", "health", "fresh", "handoff", "compact")):
        health_rows = summary.get("context_health") if isinstance(summary.get("context_health"), list) else []
        if health_rows and isinstance(health_rows[0], dict):
            row = health_rows[0]
            project = str(row.get("project") or row.get("project_full") or "the top local session")
            tool = str(row.get("tool") or "AI tool")
            severity = str(row.get("severity") or "review")
            latest = str(row.get("latest_turn_tokens") or row.get("latest_turn_tokens_label") or "unknown")
            recommendation = str(row.get("recommendation") or "Use a narrower prompt or Fresh Start before continuing.")
            session_id = str(row.get("session_id") or "")
            actions = [_ask_action("Open Watch", "/?view=watch")]
            if session_id:
                actions.insert(0, _ask_action("Inspect Session", f"/?session={session_id}"))
            if row.get("can_handoff") and session_id:
                actions.insert(0, _ask_action("Build Fresh Start", f"/?session={session_id}"))
            return _ask_answer(
                f"Context health needs attention for {project}. The strongest local signal is {severity} pressure in {tool}.",
                confidence=str(row.get("confidence_label") or row.get("evidence_label") or "Observed local context signal"),
                bullets=[
                    f"Latest turn: {latest} tokens",
                    f"Recommendation: {recommendation}",
                    "AIWatcher should not claim saved tokens until a follow-up session is observed.",
                ],
                actions=actions,
            )
        return _ask_answer(
            "No current context-health warning is visible in the local summary. Keep the next prompt scoped and let the Companion nudge only if pressure rises.",
            actions=[_ask_action("Plan Next Prompt", "/?view=prompt")],
        )

    if any(word in lower for word in ("prompt", "prompts", "outcome", "productive", "useful", "rework", "driving")):
        totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
        receipts = summary.get("intervention_receipts") if isinstance(summary.get("intervention_receipts"), list) else []
        handoffs = summary.get("handoff_decisions") if isinstance(summary.get("handoff_decisions"), list) else []
        useful = totals.get("useful_outcomes", 0)
        review = totals.get("needs_review_outcomes", 0)
        decisions = totals.get("preflight_decisions", 0)
        bullets = [
            f"Useful outcomes recorded: {useful}",
            f"Outcomes needing review: {review}",
            f"Prompt/control decisions recorded: {decisions}",
        ]
        if receipts and isinstance(receipts[0], dict):
            bullets.append(f"Latest Prompt Gate receipt: {receipts[0].get('decision_label') or 'recorded'}")
        if handoffs and isinstance(handoffs[0], dict):
            bullets.append(f"Latest Fresh Start proof: {handoffs[0].get('proof_status') or 'not linked yet'}")
        return _ask_answer(
            "AIWatcher can show whether prompts are leading to recorded outcomes only where outcome evidence exists. If many sessions are unmarked, the honest next step is to review outcomes before trusting productivity ratios.",
            confidence="Observed receipts plus user-marked/inferred outcomes",
            bullets=bullets,
            actions=[
                _ask_action("Open Prove", "/?view=receipts"),
                _ask_action("Review Sessions", "/?view=sessions"),
                _ask_action("Plan Next Prompt", "/?view=prompt"),
            ],
        )

    if any(word in lower for word in ("cost", "spend", "money", "model", "tool", "token", "tokens")):
        totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
        projects = summary.get("projects") if isinstance(summary.get("projects"), list) else []
        tools = summary.get("tools") if isinstance(summary.get("tools"), list) else []
        top_project = projects[0] if projects and isinstance(projects[0], dict) else {}
        top_tool = tools[0] if tools and isinstance(tools[0], dict) else {}
        bullets = [
            f"Window: {totals.get('window_label') or f'Last {days} days'}",
            f"API-equivalent value: {totals.get('api_value_label') or 'unknown'}",
            f"Tokens observed: {totals.get('tokens_label') or 'unknown'}",
        ]
        if top_project:
            bullets.append(f"Top project: {top_project.get('short_name') or top_project.get('name')} ({top_project.get('api_value_label')})")
        if top_tool:
            bullets.append(f"Top tool: {top_tool.get('tool') or top_tool.get('name')} ({top_tool.get('api_value_label')})")
        return _ask_answer(
            "Here is the local spend picture AIWatcher can prove from observed usage. Subscription-limited tools are usage signals, not invoice spend.",
            confidence="Observed local usage and pricing metadata",
            bullets=bullets,
            actions=[
                _ask_action("Open Home", "/"),
                _ask_action("Open Improve", "/?view=insights"),
            ],
        )

    if any(word in lower for word in ("coverage", "hook", "hooks", "cursor", "ollama", "claude", "codex", "desktop")):
        coverage = summary.get("coverage") if isinstance(summary.get("coverage"), list) else []
        bullets = []
        for row in coverage[:8]:
            if isinstance(row, dict):
                bullets.append(f"{row.get('label') or row.get('surface')}: {row.get('status_label') or row.get('status') or 'unknown'}")
        return _ask_answer(
            "Coverage tells you where AIWatcher can gate automatically, where it only has history, and where Companion/Plan is the manual bridge.",
            confidence="Observed local setup and runtime detection",
            bullets=bullets or ["No coverage rows are available yet. Open Settings after the local scan finishes."],
            actions=[_ask_action("Open Settings", "/?view=setup")],
        )

    return _ask_answer(
        "I can answer from local AIWatcher evidence. The strongest questions right now are context health, archive/cleanup safety, prompt outcomes, spend, and surface coverage.",
        confidence="Local summary",
        bullets=[
            "For action: ask what should I do next?",
            "For cleanup: ask can I archive this chat?",
            "For quality: ask are my prompts driving outcomes?",
        ],
        actions=[
            _ask_action("Open Home", "/"),
            _ask_action("Plan Next Prompt", "/?view=prompt"),
            _ask_action("Open Prove", "/?view=receipts"),
        ],
    )


def _project_basename(path: object) -> str:
    raw = str(path or "").rstrip("/")
    return raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _waited_fragment(label: object) -> str:
    # "waiting 12m" -> "12m". Under a minute the presence label is "waiting on
    # you", and that fragment pastes into nonsense ("Claude · repo · on you"),
    # so it becomes "" and callers drop the duration instead.
    frag = str(label or "").replace("waiting ", "", 1).strip()
    return "" if frag in {"", "on you"} else frag


def _presence_block(rows: list[SessionPresence]) -> dict[str, object]:
    """Live now-counts for the Companion's resting state.

    The label these counts answer is "what is happening right now": sessions on
    this machine classified this second, never the cached 7-day summary. Two
    honesty rules shape the block. An AIWatcher Second Opinion spawn is left out
    of the counts -- "1 working" must not be AIWatcher's own feature passing as
    the user's work. And zero is two different answers: no snapshot yet means
    "cannot see" (measurable false, with the reason), while a snapshot whose
    sessions have all ended is a true, measured "no live sessions".
    """
    own = [row for row in rows if not row.analyst_run]
    measured = [row for row in own if row.measurable]
    counts = {"working": 0, "waiting": 0, "quiet": 0}
    if not own:
        return {
            **counts,
            "measurable": False,
            "reason": "no local session snapshot yet",
            "line": "Waiting for the first session scan",
        }
    if not measured:
        reason = next((row.reason for row in own if row.reason), None)
        return {
            **counts,
            "measurable": False,
            "reason": reason or "sessions not measurable here",
            "line": "Live sessions not measurable here",
        }
    for row in measured:
        if row.state in counts:
            counts[row.state] += 1
    working, waiting, quiet = counts["working"], counts["waiting"], counts["quiet"]
    if working or waiting:
        line = f"{working} working · {waiting} waiting"
        if waiting:
            longest = max(
                (row for row in measured if row.state == "waiting"),
                key=lambda row: row.idle_seconds or 0.0,
            )
            frag = _waited_fragment(longest.label)
            if frag:
                line += f" {frag}"
    elif quiet:
        line = f"{quiet} quiet session{'s' if quiet != 1 else ''}"
    else:
        line = "No live sessions"
    return {**counts, "measurable": True, "reason": None, "line": line}


# Latest-turn transcript reads keyed by source_path; the value is the
# session's updated_at stamp plus the tokens read then (0 = read but not
# measurable). A session only changes its transcript when it writes, and
# writing moves updated_at, so a hit means the 13ms parse can be skipped.
_PRESSURE_TRANSCRIPT_CACHE: dict[str, tuple[str, int]] = {}

_TRANSIENT_SIGNAL_KINDS = {"loop", "velocity", "runway", "usage_pressure"}
_TRANSIENT_SIGNAL_LABELS = {
    "loop": "Possible loop",
    "velocity": "Velocity spike",
    "runway": "Runway low",
    "usage_pressure": "Usage pressure",
}
# A signal older than the live window belongs to a session presumed gone;
# a chip for it would be an alarm about nothing the user can still act on.
RECENT_SIGNAL_WINDOW_MINUTES = LIVE_WINDOW_MINUTES


def _pressure_block(rows: list[SessionPresence], sessions: list[LocalSession]) -> dict[str, object]:
    """Context pressure of the session being worked in, for the resting bar.

    The label asks: "how close is this session's latest turn to the per-turn
    limit where a Fresh Start gets recommended?" Unit and scope: billed input
    tokens on the latest single turn of the most recently active *working*
    session -- per-turn, never a cumulative session total under a per-turn
    label. Compared against CRITICAL_TOKENS_PER_TURN, the same constant the
    runway chart and statusline read, so the surfaces cannot disagree.
    Freshness comes from statusline.read_transcript (one file, ~13ms), cached
    on the session's updated_at rather than served from the 6-hour summary
    cache. When the input is missing -- no working session, a cumulative-total
    source, an unreadable transcript -- the block says so and the widgets draw
    no meter, never a zero.
    """
    working = [
        row for row in rows
        if row.state == "working" and row.measurable and not row.analyst_run
    ]
    if not working:
        return {"available": False, "reason": "no working session right now"}
    target = working[0]
    # Looked up in the rows this poll already holds, not via _find_session_row:
    # that helper falls back to a full rescan, which has no place on a 3-second
    # poll path.
    session = next((row for row in sessions if row.session_id == target.session_id), None)
    if session is None or not session.source_path:
        return {"available": False, "reason": "session transcript not tracked"}
    if has_cumulative_totals(session):
        return {
            "available": False,
            "reason": "this tool reports cumulative totals, not per-turn context",
        }
    stamp = session.updated_at.isoformat() if session.updated_at else ""
    cached = _PRESSURE_TRANSCRIPT_CACHE.get(session.source_path)
    if cached is not None and cached[0] == stamp:
        latest = cached[1]
    else:
        stats = statusline.read_transcript(session.source_path)
        latest = int(stats.get("latest_context") or 0) if stats.get("available") else 0
        if len(_PRESSURE_TRANSCRIPT_CACHE) > 32:
            _PRESSURE_TRANSCRIPT_CACHE.clear()
        _PRESSURE_TRANSCRIPT_CACHE[session.source_path] = (stamp, latest)
    if latest <= 0:
        return {"available": False, "reason": "no per-turn usage recorded in this session yet"}
    severity = (
        "critical" if latest >= CRITICAL_TOKENS_PER_TURN
        else "warning" if latest >= PRESSURE_TOKENS_PER_TURN
        else "ok"
    )
    pct = round(100 * latest / CRITICAL_TOKENS_PER_TURN)
    # Absolute anchors for the percent: what this session has spent so far,
    # straight off the session row -- no new reads. Raw totals, so the
    # widgets draw them as plain muted text with no status colour (a total is
    # not a verdict), and the tooltip names the dollar figure API-equivalent:
    # for a subscription user no money moved.
    cost = float(session.cost_usd or 0.0)
    tokens_total = int(session.tokens_in or 0) + int(session.tokens_out or 0)
    stats_parts = []
    if cost > 0:
        stats_parts.append(f"${cost:,.2f}" if cost < 100 else f"${cost:,.0f}")
    if tokens_total > 0:
        stats_parts.append(compact_int(tokens_total))
    return {
        "available": True,
        "reason": None,
        "session_id": target.session_id,
        "latest_turn_tokens": latest,
        "pct_of_turn_limit": pct,
        "severity": severity,
        "label": f"{compact_int(latest)} · {pct}% of turn limit",
        "stats_label": " · ".join(stats_parts),
        "stats_detail": "This session so far, API-equivalent cost and total tokens.",
    }


# Finished-notice tracking, in this process's memory. "Finished" is a
# transition -- working on the last poll, quiet on this one -- which cannot be
# read from a single snapshot: statelessly, a session that just completed a
# turn and one abandoned twenty minutes ago look identical until the idle
# clock separates them, which is exactly too late. The cost of memory is that
# a dashboard restart loses at most one pending notice; the away digest
# (which is stateless over the gap window) covers the larger version.
_PRESENCE_LAST_STATES: dict[str, str] = {}
_FINISHED_NOTICES: dict[str, float] = {}
# Stopgap constant, not a measured threshold: after this long the notice is
# stale news rather than a nudge, and the session is plain "quiet" again.
FINISHED_NOTICE_TTL_SECONDS = 15 * 60


def _update_finished_notices(rows: list[SessionPresence]) -> None:
    now = time.time()
    current: dict[str, str] = {}
    for row in rows:
        if row.analyst_run or not row.measurable:
            continue
        current[row.session_id] = row.state
        previous = _PRESENCE_LAST_STATES.get(row.session_id)
        if row.state == "quiet" and previous == "working":
            _FINISHED_NOTICES[row.session_id] = now
        elif row.state in {"working", "waiting"}:
            # The session is active again; a "finished" claim about it would
            # be false the moment it rendered.
            _FINISHED_NOTICES.pop(row.session_id, None)
    _PRESENCE_LAST_STATES.clear()
    _PRESENCE_LAST_STATES.update(current)
    for session_id, finished_at in list(_FINISHED_NOTICES.items()):
        if now - finished_at > FINISHED_NOTICE_TTL_SECONDS or session_id not in current:
            _FINISHED_NOTICES.pop(session_id, None)


def _finished_rows(rows: list[SessionPresence]) -> list[tuple[float, SessionPresence]]:
    """Active finished notices, newest first, skips honored."""
    notices = []
    for row in rows:
        finished_at = _FINISHED_NOTICES.get(row.session_id)
        if finished_at is None or row.state != "quiet":
            continue
        if companion_skip_active(f"session_finished:{row.session_id}"):
            continue
        notices.append((finished_at, row))
    notices.sort(key=lambda item: item[0], reverse=True)
    return notices


def _freshened_for_presence(rows: list[LocalSession]) -> list[LocalSession]:
    """Presence-grade write stamps for the Companion poll.

    The session index refreshes on scan cadence, which is right for totals
    and minutes late for "is it writing this second" -- the bar kept saying
    Finished after the session had visibly resumed, and kept saying Waiting
    after a prompt was answered. A single-file transcript's mtime is the same
    fact, one stat() away, so presence reads max(indexed stamp, mtime) for
    .jsonl sources. Cumulative DB sources are excluded on purpose: their file
    is shared across sessions, and any one session writing would mark all of
    them working.
    """
    fresh: list[LocalSession] = []
    for row in rows:
        path = row.source_path or ""
        if not path.endswith(".jsonl"):
            fresh.append(row)
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            fresh.append(row)
            continue
        stamp = datetime.fromtimestamp(mtime, tz=timezone.utc)
        current = row.updated_at
        if current is not None and current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if current is None or stamp > current:
            fresh.append(dataclasses.replace(row, updated_at=stamp))
        else:
            fresh.append(row)
    return fresh


# Away-digest tracking. The bar polls every 2-3 seconds all day, so a hole in
# the poll stream longer than AWAY_GAP_SECONDS means the machine slept, was
# locked, or the developer was genuinely gone -- not just reading. The digest
# itself is reconstructed after the fact from records that already exist
# (session write stamps and ambient-intervention records inside the gap), so
# nothing needed to be watching during a gap the server slept through too.
# Everything in it is phrased as history -- "finished 31m ago" -- because
# nobody has re-measured whether any condition still holds.
_LAST_COMPANION_POLL: float | None = None
_AWAY_DIGEST: dict[str, object] | None = None
AWAY_GAP_SECONDS = 20 * 60
# Stopgap constant: a digest nobody expanded within this window is stale news;
# the evidence it pointed at stays in the dashboard.
AWAY_DIGEST_TTL_SECONDS = 15 * 60


def _away_minutes_label(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    return f"{minutes}m" if minutes < 120 else f"{minutes // 60}h"


def _update_away_digest(
    session_rows: list[LocalSession],
    presence_rows: list[SessionPresence],
) -> None:
    global _LAST_COMPANION_POLL, _AWAY_DIGEST
    now = time.time()
    previous = _LAST_COMPANION_POLL
    _LAST_COMPANION_POLL = now
    if previous is None or now - previous < AWAY_GAP_SECONDS:
        return
    gap_start = datetime.fromtimestamp(previous, tz=timezone.utc)
    gap_end = datetime.fromtimestamp(now, tz=timezone.utc)
    states = {row.session_id: row.state for row in presence_rows}
    rows: list[dict[str, object]] = []
    finished_count = 0
    for session in session_rows:
        if session.analyst_run:
            continue
        stamp = session.updated_at
        if stamp is None:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        # Last write inside the gap, and silent now: it finished while the
        # developer was away. A session writing again already (working or
        # waiting) is live and needs no history entry.
        if not (gap_start < stamp <= gap_end):
            continue
        if states.get(session.session_id) not in {"quiet", "gone"}:
            continue
        finished_count += 1
        rows.append({
            "kind": "finished",
            "session_id": session.session_id,
            "tool": tool_label(session.tool),
            "project": _project_basename(session.project_path) or "this machine",
            "waited_label": _away_minutes_label(now - stamp.timestamp()),
            "url": f"/?session={quote(session.session_id, safe='')}",
        })
        # The finished-notice path would re-announce the same sessions the
        # moment the digest is dismissed; the digest is their announcement.
        _FINISHED_NOTICES.pop(session.session_id, None)
    signal_count = 0
    for record in recent_ambient_interventions(limit=20):
        kind = str(record.get("signal_kind") or "")
        if kind not in _TRANSIENT_SIGNAL_KINDS:
            continue
        stamp = _parse_iso_datetime(record.get("updated_at") or record.get("created_at"))
        if stamp is None or not (gap_start < stamp <= gap_end):
            continue
        signal_count += 1
        session_id = str(record.get("session_id") or "")
        urls = record.get("urls") if isinstance(record.get("urls"), dict) else {}
        rows.append({
            "kind": kind,
            "session_id": session_id,
            "tool": _TRANSIENT_SIGNAL_LABELS.get(kind, kind),
            "project": "",
            "waited_label": _away_minutes_label(now - stamp.timestamp()),
            "url": str(urls.get("dashboard") or "")
            or (f"/?session={quote(session_id, safe='')}" if session_id else "/"),
        })
    if not rows:
        return
    _AWAY_DIGEST = {
        "created": now,
        "gap_label": _away_minutes_label(now - previous),
        "finished_count": finished_count,
        "signal_count": signal_count,
        "rows": rows[:3],
    }


def _active_away_digest() -> dict[str, object] | None:
    global _AWAY_DIGEST
    if _AWAY_DIGEST is None:
        return None
    created = float(_AWAY_DIGEST.get("created") or 0.0)
    if time.time() - created > AWAY_DIGEST_TTL_SECONDS:
        _AWAY_DIGEST = None
        return None
    return _AWAY_DIGEST


def _dismiss_away_digest() -> None:
    global _AWAY_DIGEST
    _AWAY_DIGEST = None


# One `ps` sweep per ~10s, not per 2-second poll: runtime attachment for the
# queue rows needs the live process list, and which windows exist does not
# change faster than this. Races between poller threads just refresh twice.
_RUNTIME_PROCESS_CACHE: tuple[float, list[object]] | None = None
_RUNTIME_PROCESS_TTL_SECONDS = 10.0


def _cached_runtime_processes() -> list[object]:
    global _RUNTIME_PROCESS_CACHE
    now = time.monotonic()
    cached = _RUNTIME_PROCESS_CACHE
    if cached is not None and now - cached[0] < _RUNTIME_PROCESS_TTL_SECONDS:
        return cached[1]
    processes = list(safe_runtime_processes())
    _RUNTIME_PROCESS_CACHE = (now, processes)
    return processes


def _waiting_row_return_available(session_id: str, sessions: list[LocalSession]) -> bool:
    """Whether a queue row can offer a real Return instead of Open.

    True means /api/runtime-return has something to perform for this session
    -- the same attachment.available gate build_runtime_return applies -- so
    the button never promises a jump the endpoint would refuse. That includes
    the app tier: for a desktop-app session the owner's call was that Return
    should bring the app forward even when it is already frontmost, rather
    than detour through the dashboard -- the "Find your chat there" result
    message owns the not-the-exact-chat limitation.
    """
    session = next((row for row in sessions if row.session_id == session_id), None)
    if session is None:
        return False
    try:
        attachment = runtime_attachment_for_session(
            session,
            state=session_state(session),
            processes=_cached_runtime_processes(),
        )
    except OSError:
        return False
    return bool(attachment.available)


def _recent_signal_block() -> dict[str, object] | None:
    """The most recent overlay-only signal, so the bar can catch a missed one.

    Loop, velocity, runway and usage-pressure nudges live in a 20-second
    transient overlay; step away and the signal is gone. Every such nudge
    already persists an ambient-intervention record, so the bar carries the
    newest one inside the live window as a passive chip. Recency, not truth:
    the chip says "this fired Nm ago", which stays true after the fact, rather
    than re-asserting a condition nobody has re-measured.
    """
    now = datetime.now(timezone.utc)
    for record in recent_ambient_interventions(limit=20):
        kind = str(record.get("signal_kind") or "")
        if kind not in _TRANSIENT_SIGNAL_KINDS:
            continue
        stamp = _parse_iso_datetime(record.get("updated_at") or record.get("created_at"))
        if stamp is None:
            continue
        age = (now - stamp).total_seconds()
        if age < 0 or age > RECENT_SIGNAL_WINDOW_MINUTES * 60:
            continue
        session_id = str(record.get("session_id") or "")
        minutes = int(age // 60)
        urls = record.get("urls") if isinstance(record.get("urls"), dict) else {}
        return {
            "kind": kind,
            "label": _TRANSIENT_SIGNAL_LABELS.get(kind, kind),
            "chip": f"{kind} {minutes}m" if minutes else f"{kind} now",
            "minutes_ago": minutes,
            "severity": str(record.get("severity") or "warning"),
            "session_id": session_id,
            "url": str(urls.get("dashboard") or "")
            or (f"/?session={quote(session_id, safe='')}" if session_id else "/"),
        }
    return None


def build_companion_state() -> dict[str, object]:
    """Small, fast state contract for the always-available Companion surface."""
    summary = build_summary_cached(7)
    # Presence is computed here, not read from `summary`. The summary is an
    # aggregate about a seven-day window and is cached accordingly -- 45s in
    # memory, six hours on disk -- which is correct for spend totals and wrong
    # for a fact about this second. Served from that cache, "waiting on you"
    # could be hours old, and the Companion would sit quiet through the wait it
    # exists to report.
    #
    # presence_for_sessions rather than live_presence: this needs per-session
    # states only, and live_presence also resolves working trees for the
    # collision check, which shells out to git per directory.
    try:
        waiting_signals = session_waiting_signals()
    except OSError:
        waiting_signals = {}
    session_rows = _freshened_for_presence(_cached_session_rows())
    presence_rows = presence_for_sessions(session_rows, waiting=waiting_signals)
    _update_finished_notices(presence_rows)
    finished_notices = _finished_rows(presence_rows)
    finished_payload = [
        {
            "session_id": row.session_id,
            "tool": tool_label(row.tool),
            "project": _project_basename(row.project_path) or "this machine",
            "finished_label": (
                f"{int((time.time() - at) // 60)}m" if at <= time.time() - 60 else "now"
            ),
            "url": f"/?session={quote(row.session_id, safe='')}",
        }
        for at, row in finished_notices[:3]
    ]
    _update_away_digest(session_rows, presence_rows)
    base = {
        "state": "watching",
        "label": "Watching quietly",
        "title": "AIWatcher",
        "subtitle": "Plan, control, watch",
        "primary_label": "Watch",
        "primary_action": "open_url",
        "primary_session_id": "",
        "primary_runtime_available": False,
        "primary_url": "/",
        "plan_url": "/?view=prompt",
        "ask_url": "/?ask=1",
        "control_url": "/?view=prompt",
        "watch_url": "/",
        "console_url": "/",
        "detail": "Private by default. No prompt or source text is shown in the Companion.",
        "presence": _presence_block(presence_rows),
        # In the base payload, not only the finished state: the bubble's blue
        # badge must survive whatever state owns the bar, or a finished
        # session vanishes from the glanceable surface the moment anything
        # else has the headline.
        "finished_sessions": finished_payload,
        "pressure": _pressure_block(presence_rows, session_rows),
        "recent_signal": _recent_signal_block(),
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
        gate_id = gate.get("id")
        if isinstance(gate_id, str) and gate_id:
            try:
                mark_active_prompt_gate_seen(gate_id)
            except OSError:
                pass
        # Seconds until the gate auto-releases: unit is seconds-from-now for
        # this one gate, recomputed on each poll so the widgets never tick a
        # clock themselves. A missing or unparsable expires_at yields None, and
        # the widgets show no countdown rather than an invented number.
        gate_expires = _parse_iso_datetime(gate.get("expires_at"))
        return {
            **base,
            "state": "prompt_gate",
            "label": str(gate.get("workflow_label") or "Prompt Gate"),
            "title": "Prompt Gate",
            "subtitle": str(gate.get("workflow_reward") or f"{tool} {risk}-risk prompt waiting{score_label}."),
            "expires_in_seconds": (
                max(0, int((gate_expires - datetime.now(timezone.utc)).total_seconds()))
                if gate_expires is not None
                else None
            ),
            "primary_label": "Review Gate",
            "primary_action": "open_prompt_gate",
            "primary_url": str(gate.get("url") or "/?view=prompt"),
            "continue_label": "Continue",
            "continue_action": "run_original_prompt",
            "continue_url": str(gate.get("url") or ""),
            "control_url": str(gate.get("url") or "/?view=prompt"),
            "detail": "A hook paused this prompt locally. Review it before the AI tool continues.",
        }
    # Second only to the prompt gate, and ahead of every advisory state below.
    # The gate outranks it because there AIWatcher is itself holding a prompt
    # and nothing proceeds until the developer answers. Everything after this
    # -- fresh start, proof, optimize -- is advice about work that is still
    # moving. A session that has stopped and cannot continue without you is the
    # one thing on this surface you are actually blocking.
    #
    # This widget's own detail line promises it will "interrupt only when a
    # matching active session has a justified action". A blocked session is the
    # most justified action the product has, and until this branch existed it
    # was the one case the Companion sat quiet through.
    waiting_rows = [row.to_json() for row in presence_rows if row.state == "waiting"]
    if waiting_rows:
        # Longest wait first: how long you have been the bottleneck is the part
        # worth reading, and with several waiting the count goes in the sentence
        # rather than replacing the duration.
        waiting_rows.sort(key=lambda row: float(row.get("idle_seconds") or 0.0), reverse=True)
        first = waiting_rows[0]
        session_id = str(first.get("session_id") or "")
        waited = _waited_fragment(first.get("label"))
        # Basename, not the path. The widget truncates its subtitle at 46
        # characters, and "/Users/dannylo/aiwatcher-local" spends thirty of them
        # on a prefix that is the same for every project the developer has --
        # measured on the real surface, where the project name was the part
        # being cut. The label above already says what is happening; this line
        # only has to say which session and for how long.
        project = _project_basename(first.get("project_path")) or "this machine"
        tool = tool_label(str(first.get("tool") or ""))
        if len(waiting_rows) == 1:
            subtitle = f"{tool} · {project}" + (f" · {waited}" if waited else "")
        else:
            subtitle = f"{len(waiting_rows)} sessions · longest {waited or project}"
        # One pre-worded row per blocked session so the widgets can draw a
        # queue instead of a headline. Worded here, once, because three
        # clients (Swift, Tk, browser overlay) would otherwise each respell
        # the same duration and path. Capped at three: the bar caps its
        # rows there, and the count above already says the full total.
        queue = [
            {
                "session_id": str(row.get("session_id") or ""),
                "tool": tool_label(str(row.get("tool") or "")),
                "project": _project_basename(row.get("project_path")) or "this machine",
                "waited_label": _waited_fragment(row.get("label")),
                "idle_seconds": row.get("idle_seconds"),
                "url": f"/?session={quote(str(row.get('session_id') or ''), safe='')}",
                "return_available": _waiting_row_return_available(
                    str(row.get("session_id") or ""), session_rows,
                ),
                # From the hook's closed vocabulary ("run Bash", "edit
                # files", ...): what kind of interruption answering is,
                # never what the session actually said.
                "wants": str(
                    (waiting_signals.get(str(row.get("session_id") or "")) or {}).get("wants") or ""
                ),
            }
            for row in waiting_rows[:3]
        ]
        # A reachable single session gets Return as the primary: the whole
        # point of noticing a blocked session is answering it, and the tool
        # window is one jump away. The dashboard stays the fallback -- the
        # widgets open primary_url whenever the return reports failure, so
        # the button never claims a jump that did not happen.
        first_returnable = bool(queue and queue[0]["return_available"])
        return {
            **base,
            "state": "session_waiting",
            "label": "Waiting on you",
            "title": "Waiting on you",
            "subtitle": subtitle,
            "primary_label": "Return" if first_returnable else "Open session",
            "primary_action": "runtime_return" if first_returnable else "open_url",
            "primary_session_id": session_id,
            "primary_url": f"/?session={quote(session_id, safe='')}" if session_id else "/",
            "waiting_sessions": queue,
            "detail": "This session asked for permission and has done nothing since."
            if len(waiting_rows) == 1
            else "These sessions asked for permission and have done nothing since.",
        }

    # The away digest sits between a blocked session (which still outranks
    # everything but the gate) and the single-finish notice: it is the same
    # kind of news, in bulk, and its rows include what the finish notices
    # would have said.
    digest = _active_away_digest()
    if digest is not None:
        digest_rows = list(digest.get("rows") or [])
        finished_count = int(digest.get("finished_count") or 0)
        signal_count = int(digest.get("signal_count") or 0)
        parts = []
        if finished_count:
            parts.append(f"{finished_count} finished")
        if signal_count:
            parts.append(f"{signal_count} signal{'s' if signal_count != 1 else ''}")
        parts.append(f"gap {digest.get('gap_label')}")
        first_url = str(digest_rows[0].get("url") or "/") if digest_rows else "/"
        return {
            **base,
            "state": "away_digest",
            "label": "While you were away",
            "title": "While you were away",
            "subtitle": " · ".join(parts),
            "primary_label": "Review",
            "primary_action": "open_url",
            "primary_url": first_url,
            "skip_label": "Dismiss",
            "skip_state": "away_digest",
            "digest_rows": digest_rows,
            "detail": "Reconstructed from local records inside the gap. Dismiss clears this summary; the evidence stays in the dashboard.",
        }

    # Below a blocked session -- blocked outranks done -- and below live work:
    # the takeover only happens while nothing is working. Field report: with
    # one session running and another freshly finished, the finished headline
    # owned the bar for its whole 15 minutes and hid the running session's
    # meter and totals -- old news masking live information. While anything
    # works, the resting layout wins and the finished count rides its
    # subtitle and the bubble's blue badge instead. Still above every
    # fresh-start advisory, and calm on purpose: the widgets render this
    # without the orange treatment, because "review when ready" is a
    # different claim than "blocked on you".
    presence_block = base["presence"] if isinstance(base["presence"], dict) else {}
    if finished_notices and int(presence_block.get("working") or 0) == 0:
        finished_at, finished_row = finished_notices[0]
        minutes = int((time.time() - finished_at) // 60)
        ago = f"{minutes}m ago" if minutes else "just now"
        finished_project = _project_basename(finished_row.project_path) or "this machine"
        finished_tool = tool_label(finished_row.tool)
        finished_id = finished_row.session_id
        return {
            **base,
            "state": "session_finished",
            "label": "Finished working",
            "title": "Finished working",
            "subtitle": f"{finished_tool} · {finished_project} · {ago}",
            "primary_label": "Review",
            "primary_action": "open_url",
            "primary_session_id": finished_id,
            "primary_url": f"/?session={quote(finished_id, safe='')}",
            "skip_label": "Skip",
            "skip_state": "session_finished",
            "skip_session_id": finished_id,
            "detail": "This session was working a moment ago and has gone quiet -- likely a completed turn awaiting review.",
        }

    fresh_start_candidates = _fresh_start_context_candidates(summary)
    if len(fresh_start_candidates) > 1:
        foreground_candidate = next(
            (row for row in fresh_start_candidates if _foreground_matches_fresh_start_bubble(row)),
            None,
        )
        project_count = len(fresh_start_candidates)
        critical_count = sum(1 for row in fresh_start_candidates if row.get("severity") == "critical")
        total_context = sum(int(row.get("estimated_replayed_context_tokens") or 0) for row in fresh_start_candidates)
        context_label = compact_int(total_context) if total_context else "context"
        project_lines = [
            str(row.get("project_full") or "")
            for row in fresh_start_candidates
            if row.get("project_full")
        ]
        if foreground_candidate is None:
            return {
                **base,
                "state": "watching",
                "label": "Watching quietly",
                "subtitle": f"{project_count} projects ready for context review in Console",
                "primary_label": "Console",
                "primary_url": "/?view=watch#contextHealth",
                "detail": "Fresh Start review is batched in Watch and only blinks while an affected AI surface is foreground.",
            }
        return {
            **base,
            "state": "control_review",
            "label": "Review context",
            "subtitle": (
                f"{project_count} projects need Fresh Start review"
                + (f" · {critical_count} critical" if critical_count else "")
            ),
            "primary_label": "Review",
            "primary_action": "open_url",
            "primary_url": "/?view=watch#contextHealth",
            "skip_label": "Snooze",
            "skip_state": "control_recommended_group",
            "skip_project": "\n".join(project_lines),
            "fresh_start_project_count": project_count,
            "fresh_start_context_label": context_label,
            "control_url": "/?view=watch#contextHealth",
            "watch_url": "/?view=watch#contextHealth",
            "detail": "Choose which projects to Fresh Start, continue, or snooze in one batch.",
        }
    bubble = summary.get("handoff_bubble")
    if isinstance(bubble, dict) and bubble.get("session_id"):
        session_id = str(bubble.get("session_id"))
        bubble_project = str(bubble.get("project_full") or "")
        if _fresh_start_project_quiet(bubble_project):
            return {
                **base,
                "state": "watching",
                "label": "Watching quietly",
                "subtitle": "Fresh Start snoozed for this project",
                "primary_label": "Console",
                "primary_url": "/?view=watch#contextHealth",
                "detail": "The project still appears in Watch, but the Companion will not blink for it during the cooldown.",
            }
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
                    "subtitle": "Fresh Start snoozed for this project",
                    "primary_label": "Console",
                    "primary_url": "/?view=watch#contextHealth",
                    "detail": "AIWatcher will stay quiet for this project during the cooldown unless a stronger intervention is justified.",
                }
            if decision in {"new_chat", "copy_handoff"}:
                if recent_decision.get("receipt_viewed_at"):
                    return {
                        **base,
                        "state": "watching",
                        "label": "Watching quietly",
                        "subtitle": "Fresh Start receipt reviewed; proof still pending.",
                        "primary_label": "Console",
                        "primary_url": "/?view=receipts",
                        "detail": "Receipt remains available in Evidence while AIWatcher watches for follow-up proof.",
                    }
                return {
                    **base,
                    "state": "watching",
                    "label": "Watching proof",
                    "subtitle": str(recent_decision.get("proof_reason") or "Waiting to observe the follow-up session."),
                    "primary_label": "Console",
                    "primary_url": "/?view=receipts",
                    "detail": "AIWatcher will not claim saved tokens until a follow-up session is observed.",
                }
        severity = str(bubble.get("severity") or "warning")
        label = "Fresh Start" if severity == "critical" else "Review context"
        skip_key = f"control_recommended:{session_id}"
        if companion_skip_active(skip_key):
            return {
                **base,
                "state": "watching",
                "label": "Watching quietly",
                "subtitle": "Fresh Start nudge skipped",
                "primary_label": "Console",
                "detail": "The recommendation remains in the Console, but the Companion will stay quiet for now.",
            }
        if not _foreground_matches_fresh_start_bubble(bubble):
            health_rows = summary.get("context_health") if isinstance(summary.get("context_health"), list) else []
            project_count = len({
                str(row.get("project_full") or row.get("project") or "")
                for row in health_rows
                if isinstance(row, dict)
                and row.get("severity") in {"critical", "warning"}
                and row.get("can_handoff")
                and not _fresh_start_project_quiet(str(row.get("project_full") or ""))
            })
            subtitle = (
                f"{project_count} project{'s' if project_count != 1 else ''} ready for context review in Console"
                if project_count
                else "Context review waiting in Console"
            )
            return {
                **base,
                "state": "watching",
                "label": "Watching quietly",
                "subtitle": subtitle,
                "primary_label": "Console",
                "primary_url": "/?view=watch#contextHealth",
                "detail": "Fresh Start nudges only blink when the matching AI tool or terminal is foreground.",
            }
        return {
            **base,
            "state": "control_recommended",
            "label": label,
            "subtitle": str(bubble.get("body") or bubble.get("reason") or "Context pressure needs a decision."),
            "primary_label": "Fresh Start",
            "primary_action": "copy_fresh_start",
            "primary_session_id": session_id,
            "primary_runtime_available": bool(
                isinstance(bubble.get("runtime_attachment"), dict)
                and bubble.get("runtime_attachment", {}).get("available")
            ),
            "primary_url": f"/?session={session_id}",
            "continue_label": "Continue",
            "continue_session_id": session_id,
            "continue_reason": str(bubble.get("reason") or bubble.get("body") or "User chose to keep working in the current session."),
            "continue_expected_saved_context_tokens": bubble.get("expected_saved_context_tokens"),
            "skip_label": "Skip",
            "skip_state": "control_recommended",
            "skip_session_id": session_id,
            "skip_project": bubble_project,
            "control_url": f"/?session={session_id}",
            "watch_url": f"/?session={session_id}",
            "detail": "Control recommendation is based on local context-health evidence.",
        }
    fresh_start_receipts = summary.get("handoff_decisions")
    if isinstance(fresh_start_receipts, list):
        now = datetime.now(timezone.utc)
        if companion_skip_active("proof_pending"):
            return {
                **base,
                "state": "watching",
                "label": "Watching quietly",
                "subtitle": "Proof reminder skipped",
                "primary_label": "Console",
                "primary_url": "/?view=receipts",
                "detail": "Fresh Start receipts remain available in Evidence while AIWatcher watches for proof.",
            }
        for receipt in fresh_start_receipts:
            if not isinstance(receipt, dict):
                continue
            if str(receipt.get("proof_status") or "").lower() != "proof pending":
                continue
            if receipt.get("receipt_viewed_at"):
                continue
            created_at = _parse_iso_datetime(receipt.get("created_at"))
            if created_at is not None and now - created_at > timedelta(minutes=45):
                continue
            return {
                **base,
                "state": "watching",
                "label": "Watching proof",
                "subtitle": str(receipt.get("proof_reason") or "Waiting to observe the follow-up session."),
                "primary_label": "Console",
                "primary_url": "/?view=receipts",
                "detail": "AIWatcher copied or recorded an intervention and is waiting for observed outcome evidence.",
            }
    optimize = summary.get("optimize")
    if isinstance(optimize, dict) and optimize.get("status") == "needs_action":
        top = optimize.get("top") if isinstance(optimize.get("top"), dict) else {}
        project = str(top.get("project_full") or top.get("project") or "")
        project_quiet = companion_skip_active(f"optimize_workspace:{project}") if project else False
        global_quiet = companion_skip_active("optimize_workspace:global")
        if not (project_quiet or global_quiet):
            return {
                **base,
                "state": "optimize_available",
                "label": "Optimize",
                "title": "Optimize workspace",
                "subtitle": str(top.get("summary") or optimize.get("summary") or "Cleanup opportunity found."),
                "primary_label": "Review",
                "primary_url": "/?view=control#optimizeWorkspace",
                "skip_label": "Skip",
                "skip_state": "optimize_available",
                "skip_project": project,
                "detail": str(top.get("evidence") or "AIWatcher found local cleanup evidence."),
            }
    watcher = summary.get("watcher")
    running = isinstance(watcher, dict) and bool(watcher.get("running"))
    # The resting subtitle answers "what is happening now" (the presence line),
    # not "what happened this week". The 7-day rollup is retrospective -- it
    # does not change what the developer does in the next minute -- so it moves
    # to `detail`, which the widgets surface as a tooltip.
    rollup = None
    totals = summary.get("totals")
    if isinstance(totals, dict):
        window = str(totals.get("window_label") or "Last 7 days").replace("Last ", "", 1)
        sessions = totals.get("sessions")
        value = totals.get("api_value_label")
        tokens = totals.get("tokens_label")
        parts = [f"{sessions} session{'s' if sessions != 1 else ''}"] if sessions is not None else []
        if value:
            parts.append(str(value))
        if tokens:
            parts.append(f"{tokens} tokens")
        if parts:
            rollup = f"{window}: " + " · ".join(parts)
    quiet_detail = "AIWatcher will interrupt only when a matching active session has a justified action."
    if rollup:
        quiet_detail = f"{rollup}. {quiet_detail}"
    presence = base["presence"]
    quiet_subtitle = str(presence["line"]) if isinstance(presence, dict) else "Local Companion is running"
    # Finished work that live work outranked still gets its line fragment,
    # subject to the widgets' 46-character subtitle.
    if finished_notices:
        appended = f"{quiet_subtitle} · {len(finished_notices)} finished"
        if len(appended) <= 46:
            quiet_subtitle = appended
    return {
        **base,
        "state": "watching" if running else "offline",
        "label": "Watching quietly" if running else "Open AIWatcher",
        "subtitle": quiet_subtitle if running else "Companion state is available from the Dashboard",
        "primary_label": "Console" if running else "Open",
        "detail": quiet_detail if running else base["detail"],
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


# The dashboard ships as one self-contained document: no build step, no CDN, no
# second request, nothing to install. That constraint is worth keeping, but it
# used to mean 4,400 lines of markup, CSS and JavaScript lived inside a Python
# string, where no linter, formatter or editor could see them and every diff
# read as a change to one enormous literal. The front end now lives in
# aiwatcher_cli/web/ as real files and is spliced back together here, so the
# served bytes are unchanged and the source is reviewable.
_WEB_DIR = Path(__file__).resolve().parent / "web"
_ASSET_INCLUDE = re.compile(r"@@INCLUDE:([A-Za-z0-9_.-]+)@@")


def _load_asset(name: str) -> str:
    """Read a front-end file from web/, replacing @@INCLUDE:...@@ with its file."""
    text = (_WEB_DIR / name).read_text(encoding="utf-8")
    return _ASSET_INCLUDE.sub(lambda match: _load_asset(match.group(1)), text)


HTML = _load_asset("index.html")


OVERLAY_HTML = _load_asset("overlay.html")


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
                "capabilities": ["preflight", "source-update"],
            }), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/update-status":
            params = parse_qs(parsed.query)
            fetch = params.get("fetch", ["1"])[0] != "0"
            try:
                payload = check_for_updates(fetch=fetch)
            except (OSError, subprocess.SubprocessError) as exc:
                payload = {"ok": False, "message": f"Could not check for updates: {exc}"}
            self._send(200, json.dumps(payload), "application/json; charset=utf-8")
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
            # Serve the wording rather than letting the overlay keep its own
            # copy of it. overlay.js had a second table keyed on action with no
            # entry for `return_session`, so a blocked session -- the strongest
            # signal the product has -- rendered there as the generic fallback
            # "AIWatcher found something to review", and its runway label had
            # drifted to "Copy Fresh Start brief". One table cannot drift from
            # itself.
            presentation = presentation_for_signal(
                str(record.get("signal_kind") or "usage_pressure"),
                str(record.get("reason") or ""),
            )
            payload["title"] = presentation["title"]
            payload["primary_label"] = presentation["primary_label"]
            self._send(200, json.dumps(payload), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/companion-state":
            self._send(200, json.dumps(build_companion_state()), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/companion-scan":
            try:
                _refresh_summary_cache(7)
                state = build_companion_state()
            except OSError as exc:
                self._send(
                    500,
                    json.dumps({"error": f"Could not scan local AI sessions: {exc}"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send(200, json.dumps({"ok": True, "state": state}), "application/json; charset=utf-8")
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
            state_filter = params.get("state", [""])[0].strip() or None
            if state_filter not in {"active_recent", "active", "history"}:
                state_filter = None
            self._send(
                200,
                json.dumps(build_session_search(
                    days,
                    search=search,
                    outcome=outcome,
                    evidence=evidence,
                    state_filter=state_filter,
                )),
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
            "/api/second-opinion",
            "/api/second-opinion-consent",
            "/api/second-opinion-contents",
            "/api/ask-aiwatcher",
            "/api/handoff-basic",
            "/api/handoff",
            "/api/handoff-demo",
            "/api/handoff-decision",
            "/api/handoff-receipts-viewed",
            "/api/first-run-dismissed",
            "/api/optimize-decision",
            "/api/companion-skip",
            "/api/ambient-intervention-action",
            "/api/runtime-return",
            "/api/session-resume",
            "/api/update-apply",
        }:
            self._send(404, "Not found", "text/plain; charset=utf-8")
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json" and parsed.path not in _POST_WITHOUT_BODY:
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
        if parsed.path == "/api/update-apply":
            fetch = not bool(payload.get("no_fetch"))
            restart = bool(payload.get("restart"))
            try:
                result = apply_updates(fetch=fetch)
            except (OSError, subprocess.SubprocessError) as exc:
                result = {"ok": False, "message": f"Could not apply update: {exc}"}
            if result.get("ok") and result.get("applied") and restart:
                result["restart_requested"] = True
                result["message"] = "Updated. Restarting AIWatcher so the dashboard and Companion use the new code."
                schedule_dashboard_restart()
            status = 200 if result.get("ok") else 409
            self._send(status, json.dumps(result), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/second-opinion":
            # Stage 2, on its own request. Stage 1 has already rendered by
            # the time this is called: the analyst takes 30s on the small
            # tier, so putting it in /api/preflight would hold a complete
            # and useful answer hostage to an optional one.
            prompt = str(payload.get("prompt", ""))
            tool = str(payload.get("tool", "agent")).strip() or "agent"
            cwd = str(payload.get("cwd", "")).strip() or None
            response = build_second_opinion(prompt, tool=tool, cwd=cwd)
            self._send(200, json.dumps(response), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/second-opinion-consent":
            project = str(payload.get("project_path", "")).strip()
            allowed = bool(payload.get("allowed"))
            if not project:
                self._send(400, json.dumps({"error": "project_path is required"}),
                           "application/json; charset=utf-8")
                return
            record_analyst_consent(project, allowed=allowed)
            self._send(200, json.dumps({"allowed": allowed, "project_path": project}),
                       "application/json; charset=utf-8")
            return
        if parsed.path == "/api/second-opinion-contents":
            project = str(payload.get("project_path", "")).strip()
            allowed = bool(payload.get("allowed"))
            if not project:
                self._send(400, json.dumps({"error": "project_path is required"}),
                           "application/json; charset=utf-8")
                return
            record_analyst_contents(project, allowed=allowed)
            self._send(200, json.dumps({"allowed": allowed, "project_path": project}),
                       "application/json; charset=utf-8")
            return
        if parsed.path == "/api/ask-aiwatcher":
            question = str(payload.get("question", "")).strip()
            raw_days = payload.get("days", 7)
            try:
                days = max(1, min(90, int(raw_days)))
            except (TypeError, ValueError):
                days = 7
            response = answer_local_question(question, days=days)
            self._send(200, json.dumps(response), "application/json; charset=utf-8")
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
        if parsed.path == "/api/session-resume":
            session_id = str(payload.get("session_id", payload.get("id", ""))).strip()
            if not session_id:
                self._send(400, json.dumps({"error": "session_id is required"}), "application/json; charset=utf-8")
                return
            raw_days = payload.get("days", 30)
            try:
                days = max(1, min(90, int(raw_days)))
            except (TypeError, ValueError):
                days = 30
            result = build_session_resume(session_id, days, launch=bool(payload.get("launch")))
            self._send(
                404 if result.get("error") == "session not found" else 200,
                json.dumps(result),
                "application/json; charset=utf-8",
            )
            return
        if parsed.path == "/api/handoff-decision":
            session_id = str(payload.get("session_id", "")).strip()
            decision = str(payload.get("decision", "")).strip()
            reason = str(payload.get("reason", "")).strip()
            source_project_path = str(payload.get("source_project_path", "")).strip()
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
                    source_project_path=source_row.project_path if source_row else source_project_path or None,
                )
                if decision in {"continue_here", "dismissed"}:
                    record_companion_skip(
                        key=f"control_recommended:{session_id}",
                        reason=f"User chose {decision} for Fresh Start.",
                        minutes=FRESH_START_PROJECT_COOLDOWN_MINUTES,
                    )
                    project_key_for_skip = fresh_start_project_skip_key(source_row.project_path if source_row else source_project_path)
                    if project_key_for_skip:
                        record_companion_skip(
                            key=project_key_for_skip,
                            reason=f"User chose {decision} for Fresh Start in this project.",
                            minutes=FRESH_START_PROJECT_COOLDOWN_MINUTES,
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
        if parsed.path == "/api/first-run-dismissed":
            # No body: the only fact is that it happened, and the timestamp is
            # the server's rather than the page's.
            try:
                at = dismiss_first_run()
            except OSError as exc:
                self._send(
                    500,
                    json.dumps({"error": f"Could not record the first-run dismissal: {exc}"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send(200, json.dumps({"ok": True, "dismissed_at": at}), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/handoff-receipts-viewed":
            try:
                updated = mark_recent_handoff_receipts_viewed()
            except OSError as exc:
                self._send(
                    500,
                    json.dumps({"error": f"Could not mark Fresh Start receipts viewed: {exc}"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send(200, json.dumps({"ok": True, "updated": updated}), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/optimize-decision":
            decision = str(payload.get("decision", "")).strip()
            reason = str(payload.get("reason", "")).strip()
            project = str(payload.get("project", "")).strip() or None
            evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
            try:
                record = record_optimize_decision(
                    decision=decision,
                    reason=reason,
                    project_path=project,
                    evidence=evidence,
                    action_channel="dashboard",
                )
                record_companion_skip(
                    key=f"optimize_workspace:{project or 'global'}",
                    reason=f"User {decision.replace('_', ' ')} the Optimize Workspace nudge.",
                    minutes=3 * 24 * 60,
                )
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}), "application/json; charset=utf-8")
                return
            except OSError as exc:
                self._send(
                    500,
                    json.dumps({"error": f"Could not save Optimize receipt: {exc}"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send(200, json.dumps(record), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/companion-skip":
            state = str(payload.get("state", "")).strip()
            session_id = str(payload.get("session_id", "")).strip()
            project = str(payload.get("project", "")).strip()
            if state == "prompt_gate":
                self._send(
                    409,
                    json.dumps({
                        "error": "Prompt Gate is blocking an AI tool. Review the gate instead of skipping it.",
                    }),
                    "application/json; charset=utf-8",
                )
                return
            try:
                if state == "proof_pending":
                    updated = mark_recent_handoff_receipts_viewed()
                    record_companion_skip(key="proof_pending", reason="User skipped the proof-pending Companion reminder.")
                    self._send(200, json.dumps({"ok": True, "updated": updated}), "application/json; charset=utf-8")
                    return
                if state in {"control_recommended_group", "control_recommended_project"}:
                    raw_projects = payload.get("projects")
                    projects = [str(item).strip() for item in raw_projects] if isinstance(raw_projects, list) else []
                    if project:
                        projects.extend(part.strip() for part in project.splitlines())
                    saved = []
                    for project_path in projects:
                        project_key_for_skip = fresh_start_project_skip_key(project_path)
                        if not project_key_for_skip or project_key_for_skip in saved:
                            continue
                        record_companion_skip(
                            key=project_key_for_skip,
                            reason="User snoozed Fresh Start review for this project.",
                            minutes=FRESH_START_PROJECT_COOLDOWN_MINUTES,
                        )
                        saved.append(project_key_for_skip)
                    if not saved:
                        self._send(
                            400,
                            json.dumps({"error": "No reliable project path was supplied for Fresh Start snooze."}),
                            "application/json; charset=utf-8",
                        )
                        return
                    self._send(200, json.dumps({"ok": True, "projects": len(saved)}), "application/json; charset=utf-8")
                    return
                if state == "control_recommended" and session_id:
                    source_row = _find_session_row(session_id)
                    record = record_handoff_decision(
                        session_id=session_id,
                        decision="dismissed",
                        reason="User skipped the Fresh Start Companion nudge.",
                        action_channel="companion_skip",
                        source_project_path=source_row.project_path if source_row else None,
                    )
                    record_companion_skip(
                        key=f"control_recommended:{session_id}",
                        reason="User skipped the Fresh Start Companion nudge.",
                        minutes=FRESH_START_PROJECT_COOLDOWN_MINUTES,
                    )
                    project_key_for_skip = fresh_start_project_skip_key(project)
                    if project_key_for_skip:
                        record_companion_skip(
                            key=project_key_for_skip,
                            reason="User skipped Fresh Start nudges for this project.",
                            minutes=FRESH_START_PROJECT_COOLDOWN_MINUTES,
                        )
                    self._send(200, json.dumps({"ok": True, "record": record}), "application/json; charset=utf-8")
                    return
                if state == "needs_review":
                    record = record_companion_skip(
                        key="needs_review",
                        reason="User skipped the needs-review Companion nudge.",
                    )
                    self._send(200, json.dumps({"ok": True, "record": record}), "application/json; charset=utf-8")
                    return
                if state == "away_digest":
                    # Memory-only, like the digest itself: dismissing clears
                    # the pending summary; the evidence it pointed at stays.
                    _dismiss_away_digest()
                    self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")
                    return
                if state == "session_finished" and session_id:
                    # The default hour outlives the notice's own 15-minute TTL,
                    # so a skip is final for that finish rather than a snooze.
                    record = record_companion_skip(
                        key=f"session_finished:{session_id}",
                        reason="User skipped the finished-session Companion notice.",
                    )
                    self._send(200, json.dumps({"ok": True, "record": record}), "application/json; charset=utf-8")
                    return
                if state == "optimize_available":
                    record = record_companion_skip(
                        key=f"optimize_workspace:{project or 'global'}",
                        reason="User skipped the Optimize Workspace Companion nudge.",
                        minutes=3 * 24 * 60,
                    )
                    self._send(200, json.dumps({"ok": True, "record": record}), "application/json; charset=utf-8")
                    return
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}), "application/json; charset=utf-8")
                return
            except OSError as exc:
                self._send(
                    500,
                    json.dumps({"error": f"Could not skip Companion nudge: {exc}"}),
                    "application/json; charset=utf-8",
                )
                return
            self._send(400, json.dumps({"error": "No skippable Companion nudge is active."}), "application/json; charset=utf-8")
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
    print("Private by default. No data leaves this machine unless you configure it. Press Ctrl+C to stop.")
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
