#!/usr/bin/env python3
"""AIWatcher Local CLI.

Local-first, read-only, no network calls. This module is intentionally
standalone so it can later become the public `aiwatcher` package entrypoint.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
import subprocess
import sys
import tempfile
import threading
import time as time_module
import webbrowser
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Iterable

from .correlate import link_recent_interventions_to_sessions
from .local_state import (
    VALID_OUTCOMES,
    get_baselines,
    get_outcome,
    link_intervention_session,
    recent_hook_events,
    recent_interventions,
    record_decision,
    record_evidence_snapshot,
    record_intervention,
    record_hook_event,
    record_outcome,
    save_baselines,
    state_path,
)
from .handoff import TARGET_LABELS, build_handoff_capsule, render_handoff_capsule
from .outcome_evidence import build_outcome_evidence
from .pricing import is_subscription_model
from .scanner import LocalEvent, LocalSession, discover_tools, scan_all, scan_all_events


CLOUD_URL = "https://www.getaiwatcher.com"
MIN_DT = datetime.min.replace(tzinfo=timezone.utc)
DEFAULT_DAILY_BUDGET_USD = 10.0
DEFAULT_MONTHLY_BUDGET_USD = 100.0
MIN_SAVINGS_SESSIONS = 10
MIN_SAVINGS_HISTORY_DAYS = 14
PROMPT_GATE_TIMEOUT_SECONDS = 180
# Claude/Codex kill a hook command after their own default timeout (30s),
# independent of how long AIWatcher itself is willing to wait for a gate
# decision. Without raising it, the host kills the hook process mid-wait,
# discarding its output -- so a decision made on the gate page never reaches
# the agent, even though the page looks alive. Installed hook entries set
# their "timeout" field to this value when --gate is used.
PROMPT_GATE_HOST_TIMEOUT_SECONDS = PROMPT_GATE_TIMEOUT_SECONDS + 30
CODEX_WRAPPER_MARKER_START = "# >>> aiwatcher codex wrapper >>>"
CODEX_WRAPPER_MARKER_END = "# <<< aiwatcher codex wrapper <<<"


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    """Loopback server that avoids HTTPServer's unnecessary FQDN lookup."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


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


def compact_duration(seconds: int) -> str:
    if seconds <= 0:
        return "unknown"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, remaining = divmod(minutes, 60)
    return f"{hours}h {remaining}m" if remaining else f"{hours}h"


def short_path(path: str | None, max_len: int = 46) -> str:
    if not path:
        return "unknown"
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


def local_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min).astimezone()


def format_full_date(value: datetime) -> str:
    return f"{value.strftime('%A, %B')} {value.day}, {value.year}"


def format_short_datetime(value: datetime) -> str:
    return f"{value.strftime('%b')} {value.day} {value.strftime('%H:%M')}"


def in_window(session: LocalSession, since: datetime) -> bool:
    stamp = session.updated_at or session.started_at
    return bool(stamp and stamp.astimezone() >= since)


def sessions_since(days: int) -> list[LocalSession]:
    since = datetime.now().astimezone() - timedelta(days=days)
    return [session for session in scan_all() if in_window(session, since)]


def summarize(sessions: Iterable[LocalSession]) -> dict[str, float | int]:
    rows = list(sessions)
    return {
        "sessions": len(rows),
        "tokens_in": sum(row.tokens_in for row in rows),
        "tokens_out": sum(row.tokens_out for row in rows),
        "cost_usd": sum(row.cost_usd for row in rows),
        "agent_calls": sum(row.agent_calls for row in rows),
        "tool_calls": sum(row.tool_calls for row in rows),
    }


def token_summary_label(sessions: Iterable[LocalSession]) -> str:
    rows = list(sessions)
    priced_tokens = sum(
        row.tokens_in + row.tokens_out
        for row in rows
        if row.cost_usd > 0 and not is_subscription_model(row.model)
    )
    total_tokens = sum(row.tokens_in + row.tokens_out for row in rows)
    unpriced_tokens = max(0, total_tokens - priced_tokens)
    if priced_tokens and unpriced_tokens:
        return f"{compact_int(priced_tokens)} API-priced tokens | {compact_int(unpriced_tokens)} plan/limited tokens observed"
    if priced_tokens:
        return f"{compact_int(priced_tokens)} API-priced tokens"
    return f"{compact_int(total_tokens)} tokens observed"


def top_project(sessions: Iterable[LocalSession]) -> tuple[str, float, int] | None:
    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for session in sessions:
        label = session.project_path or "unknown"
        totals[label] += session.cost_usd
        counts[label] += 1
    if not totals:
        return None
    best = max(totals, key=lambda key: (totals[key], counts[key]))
    return best, totals[best], counts[best]


def reliable_session_seconds(session: LocalSession, since: datetime | None = None) -> int:
    """Return a conservative session span for display only.

    Some local tools store long-lived thread windows rather than active work
    time. Avoid turning those into fake "hours worked" claims.
    """
    if not session.started_at or not session.updated_at:
        return 0
    if since and session.started_at.astimezone() < since:
        return 0
    seconds = session.duration_seconds
    if seconds <= 0 or seconds > 8 * 60 * 60:
        return 0
    return seconds


def longest_session(sessions: Iterable[LocalSession], since: datetime | None = None) -> LocalSession | None:
    rows = [row for row in sessions if reliable_session_seconds(row, since) > 0]
    if not rows:
        return None
    return max(rows, key=lambda row: reliable_session_seconds(row, since))


def print_cloud_hint(message: str) -> None:
    print(f"\nCloud: {message}")
    print(f"       {CLOUD_URL}")


def session_sort_key(session: LocalSession) -> datetime:
    return session.updated_at or session.started_at or MIN_DT


def latest_session(sessions: Iterable[LocalSession]) -> LocalSession | None:
    rows = list(sessions)
    if not rows:
        return None
    return max(rows, key=session_sort_key)


def has_cumulative_totals(session: LocalSession) -> bool:
    return any("cumulative" in note.lower() for note in session.notes)


def session_insights(
    session: LocalSession,
    *,
    cost_threshold: float = 5.0,
    calls_threshold: int = 250,
    tokens_threshold: int = 500_000,
) -> list[str]:
    insights: list[str] = []
    tokens = session.tokens_in + session.tokens_out
    if session.cost_usd >= cost_threshold:
        insights.append(
            f"High API-equivalent value: {money(session.cost_usd)}. Review whether the task needed this model or this many steps."
        )
    reliable_pressure = not has_cumulative_totals(session)
    if reliable_pressure and session.agent_calls >= calls_threshold:
        insights.append(
            f"Many model calls: {session.agent_calls}. This can indicate iterative prompting, a loop, or an agent that needed tighter instructions."
        )
    if reliable_pressure and session.tool_calls >= 80:
        insights.append(
            f"Heavy tool use: {session.tool_calls} tool calls. Check whether the agent searched broadly before narrowing the task."
        )
    if reliable_pressure and tokens >= tokens_threshold:
        insights.append(
            f"Large context: {compact_int(tokens)} tokens observed. Consider smaller file scopes, checkpoints, or a cheaper model for exploration."
        )
    reliable_seconds = reliable_session_seconds(session)
    if reliable_seconds >= 45 * 60:
        insights.append(
            f"Long session: {compact_duration(reliable_seconds)}. Split future work into smaller prompts with explicit stop conditions."
        )
    if any("limited" in note or "subscription" in note for note in session.notes):
        insights.append("Plan/subscription usage detected. Treat this as usage pressure, not necessarily incremental invoice spend.")
    if not session.project_path:
        insights.append("Project attribution is missing. Run from the repo directory when possible so AIWatcher can group work correctly.")
    return insights


def print_session_detail(session: LocalSession, *, heading: str = "Latest local AI session") -> None:
    stamp = session_sort_key(session)
    when = format_short_datetime(stamp.astimezone()) if stamp != MIN_DT else "unknown"
    reliable_seconds = reliable_session_seconds(session)
    print(f"{heading}\n")
    print(f"Session: {session.session_id}")
    print(f"When: {when}")
    print(f"Tool: {session.tool}")
    print(f"Project: {session.project_path or 'unknown'}")
    print(f"Model: {session.model or 'unknown'}")
    print(f"API-equivalent value: {money(session.cost_usd)}")
    print(f"Tokens: {compact_int(session.tokens_in + session.tokens_out)} ({compact_int(session.tokens_in)} in / {compact_int(session.tokens_out)} out)")
    print(f"Calls: {session.agent_calls} model | {session.tool_calls} tool")
    print(f"Measured duration: {compact_duration(reliable_seconds)}")
    outcome = get_outcome(session.session_id)
    print(f"Outcome: {outcome['outcome'] if outcome else 'not marked'}")
    evidence = build_outcome_evidence(session)
    try:
        record_evidence_snapshot(session.session_id, evidence.to_json())
    except OSError:
        pass
    if evidence.inferred_outcome:
        print(f"Inferred outcome: {evidence.inferred_outcome} ({evidence.confidence})")
    if evidence.commits:
        print(f"Nearby commits: {len(evidence.commits)}")
    if evidence.changed_files:
        print(f"Uncommitted files: {len(evidence.changed_files)}")
    if evidence.tests:
        print(f"Recent test artifacts: {len(evidence.tests)}")
    if session.source_path:
        print(f"Source: {short_path(session.source_path, 80)}")
    if session.notes:
        print("\nNotes")
        for note in session.notes[:4]:
            print(f"- {note}")
    insights = session_insights(session)
    if evidence.reasons:
        insights = [*evidence.reasons[:2], *insights]
    if insights:
        print("\nWhat to check next")
        for insight in insights[:5]:
            print(f"- {insight}")
    else:
        print("\nWhat to check next")
        print("- Nothing unusual in this session summary. Use `aiwatcher ui` for project and session drill-down.")
    if not outcome:
        print(f"\nMark the result: aiwatcher outcome useful --session-id {session.session_id}")


def render_session_detail(session: LocalSession, *, heading: str = "Latest local AI session") -> str:
    stamp = session_sort_key(session)
    when = format_short_datetime(stamp.astimezone()) if stamp != MIN_DT else "unknown"
    reliable_seconds = reliable_session_seconds(session)
    outcome = get_outcome(session.session_id)
    evidence = build_outcome_evidence(session)
    try:
        record_evidence_snapshot(session.session_id, evidence.to_json())
    except OSError:
        pass
    lines = [
        heading,
        "",
        f"Session: {session.session_id}",
        f"When: {when}",
        f"Tool: {session.tool}",
        f"Project: {session.project_path or 'unknown'}",
        f"Model: {session.model or 'unknown'}",
        f"API-equivalent value: {money(session.cost_usd)}",
        f"Tokens: {compact_int(session.tokens_in + session.tokens_out)} ({compact_int(session.tokens_in)} in / {compact_int(session.tokens_out)} out)",
        f"Calls: {session.agent_calls} model | {session.tool_calls} tool",
        f"Measured duration: {compact_duration(reliable_seconds)}",
        f"Outcome: {outcome['outcome'] if outcome else 'not marked'}",
    ]
    if evidence.inferred_outcome:
        lines.append(f"Inferred outcome: {evidence.inferred_outcome} ({evidence.confidence})")
    if evidence.commits:
        lines.append(f"Nearby commits: {len(evidence.commits)}")
    if evidence.changed_files:
        lines.append(f"Uncommitted files: {len(evidence.changed_files)}")
    if evidence.tests:
        lines.append(f"Recent test artifacts: {len(evidence.tests)}")
    if session.source_path:
        lines.append(f"Source: {short_path(session.source_path, 80)}")
    if session.notes:
        lines.extend(["", "Notes"])
        lines.extend(f"- {note}" for note in session.notes[:4])
    insights = [*evidence.reasons[:2], *session_insights(session)]
    lines.extend(["", "What to check next"])
    if insights:
        lines.extend(f"- {insight}" for insight in insights[:5])
    else:
        lines.append("- Nothing unusual in this session summary. Use `aiwatcher ui` for project and session drill-down.")
    if not outcome:
        lines.extend(["", f"Mark the result: aiwatcher outcome useful --session-id {session.session_id}"])
    return "\n".join(lines)


def render_today(days: int = 1) -> str:
    rows = sessions_since(days)
    stats = summarize(rows)
    by_tool: dict[str, list[LocalSession]] = defaultdict(list)
    by_project: dict[str, list[LocalSession]] = defaultdict(list)
    by_model: dict[str, list[LocalSession]] = defaultdict(list)
    for row in rows:
        by_tool[row.tool].append(row)
        by_project[row.project_path or "unknown"].append(row)
        by_model[row.model or "unknown"].append(row)

    lines = [
        f"AIWatcher Local summary - last {days} day{'s' if days != 1 else ''}",
        f"Sessions: {stats['sessions']}",
        f"API-equivalent value: {money(float(stats['cost_usd']))}",
        f"Tokens: {token_summary_label(rows)}",
        f"Model calls: {stats['agent_calls']}",
        f"Tool calls: {stats['tool_calls']}",
    ]
    if by_project:
        project, project_rows = max(by_project.items(), key=lambda item: summarize(item[1])["cost_usd"])
        project_stats = summarize(project_rows)
        lines.append(f"Top project: {short_path(project)} ({money(float(project_stats['cost_usd']))})")
    if by_tool:
        tool, tool_rows = max(by_tool.items(), key=lambda item: summarize(item[1])["cost_usd"])
        lines.append(f"Top tool: {tool} ({summarize(tool_rows)['sessions']} sessions)")
    if by_model:
        model, model_rows = max(by_model.items(), key=lambda item: summarize(item[1])["cost_usd"])
        model_stats = summarize(model_rows)
        lines.append(f"Top model: {model} ({compact_int(int(model_stats['tokens_in']) + int(model_stats['tokens_out']))} tokens)")
    return "\n".join(lines)


def project_summary_text(days: int = 7, project: str | None = None) -> str:
    rows = sessions_since(days)
    if project:
        rows = [row for row in rows if (row.project_path or "unknown") == project]
    by_project: dict[str, list[LocalSession]] = defaultdict(list)
    for row in rows:
        by_project[row.project_path or "unknown"].append(row)
    if not by_project:
        return f"No local AI project activity found in the last {days} days."
    lines = [f"AIWatcher project summary - last {days} days"]
    ranked = sorted(by_project.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True)
    for label, project_rows in ranked[:8]:
        stats = summarize(project_rows)
        lines.append(
            f"- {short_path(label, 72)}: {stats['sessions']} sessions, "
            f"{compact_int(int(stats['tokens_in']) + int(stats['tokens_out']))} tokens, "
            f"{money(float(stats['cost_usd']))} API-equivalent value"
        )
    return "\n".join(lines)


def budget_check_text(
    *,
    daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD,
    monthly_budget_usd: float = DEFAULT_MONTHLY_BUDGET_USD,
) -> str:
    now = datetime.now().astimezone()
    today_start = local_midnight(now.date())
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = scan_all()
    today_rows = [row for row in rows if in_window(row, today_start)]
    month_rows = [row for row in rows if in_window(row, month_start)]
    today_cost = float(summarize(today_rows)["cost_usd"])
    month_cost = float(summarize(month_rows)["cost_usd"])
    today_pct = (today_cost / daily_budget_usd * 100) if daily_budget_usd > 0 else 0
    month_pct = (month_cost / monthly_budget_usd * 100) if monthly_budget_usd > 0 else 0
    status = "ok"
    if today_pct >= 100 or month_pct >= 100:
        status = "over budget"
    elif today_pct >= 75 or month_pct >= 75:
        status = "watch"
    return "\n".join([
        "AIWatcher local budget check",
        f"Status: {status}",
        f"Today: {money(today_cost)} of {money(daily_budget_usd)} ({today_pct:.0f}%)",
        f"This month: {money(month_cost)} of {money(monthly_budget_usd)} ({month_pct:.0f}%)",
        "Note: API-equivalent value is not necessarily subscription invoice spend.",
    ])


def _range_label(low: float, high: float, formatter) -> str:
    if abs(high - low) < 0.000001:
        return formatter(low)
    return f"{formatter(low)}-{formatter(high)}"


def _number_range_label(low: int, high: int) -> str:
    if high == low:
        return compact_int(high)
    return f"{compact_int(low)}-{compact_int(high)}"


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return ordered[index]


BASELINE_HISTORY_DAYS = 30
BASELINE_TOOLS = ("claude-code", "codex-cli")


def _normalize_tool_for_baseline(tool: str) -> str | None:
    if tool in {"claude", "claude-code"}:
        return "claude-code"
    if tool in {"codex", "codex-cli"}:
        return "codex-cli"
    return None


def _compute_baselines() -> dict[str, object]:
    """Scan local session history and aggregate per-tool p75 stats.

    This is the expensive full-history scan estimate_prompt_savings() used
    to run on every hook invocation. It must only ever be called off the
    hot path (see get_or_refresh_baselines()) -- never from a hook.
    """
    sessions = sessions_since(BASELINE_HISTORY_DAYS)
    per_tool: dict[str, object] = {}
    for tool_name in BASELINE_TOOLS:
        relevant = [row for row in sessions if row.tool == tool_name]
        excluded_cumulative = 0
        if tool_name == "codex-cli":
            excluded_cumulative = sum(1 for row in relevant if has_cumulative_totals(row))
            relevant = [row for row in relevant if not has_cumulative_totals(row)]

        dated_rows = [row for row in relevant if session_sort_key(row) != MIN_DT]
        history_span_days = 0
        if dated_rows:
            oldest = min(session_sort_key(row) for row in dated_rows)
            newest = max(session_sort_key(row) for row in dated_rows)
            history_span_days = max(1, (newest - oldest).days + 1)

        token_values = [float(row.tokens_in + row.tokens_out) for row in relevant if row.tokens_in + row.tokens_out > 0]
        call_values = [float(row.agent_calls) for row in relevant if row.agent_calls > 0]
        tool_values = [float(row.tool_calls) for row in relevant if row.tool_calls > 0]
        cost_values = [float(row.cost_usd) for row in relevant if row.cost_usd > 0]
        per_tool[tool_name] = {
            "p75_tokens": _quantile(token_values, 0.75),
            "p75_calls": _quantile(call_values, 0.75),
            "p75_tool_calls": _quantile(tool_values, 0.75),
            "p75_api_value": _quantile(cost_values, 0.75),
            "session_count": len(relevant),
            "history_span_days": history_span_days,
            "excluded_cumulative": excluded_cumulative,
        }
    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "history_days": BASELINE_HISTORY_DAYS,
        "per_tool": per_tool,
    }


def get_or_refresh_baselines(max_age_hours: int = 24) -> dict[str, object]:
    """Return cached prompt-savings baselines, recomputing if missing/stale.

    Callers on the hook hot path must use local_state.get_baselines()
    directly instead -- that never scans, it only reads whatever is
    already cached. This refreshing version belongs only in places that
    aren't latency-sensitive: `today`, `report`, and `ui` startup.
    """
    cached = get_baselines()
    computed_at = cached.get("computed_at") if isinstance(cached, dict) else None
    stale = True
    if computed_at:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(str(computed_at))
            stale = age > timedelta(hours=max_age_hours)
        except ValueError:
            stale = True
    if cached and not stale:
        return cached
    fresh = _compute_baselines()
    try:
        save_baselines(fresh)
    except OSError:
        pass
    return fresh


def estimate_prompt_savings(prompt: str, *, risk_score: int, tool: str, cwd: str | None = None) -> dict[str, object]:
    # cwd is accepted for call-site compatibility but intentionally unused:
    # baselines are cached per-tool only (see get_or_refresh_baselines), not
    # per-project, so a hook invocation can read the cache without ever
    # touching disk. A project-scoped estimate would need a per-project
    # cache dimension, which is more staleness/complexity than this
    # hot-path fix is worth -- see the P0-3 tradeoff discussion.
    del cwd
    tool_key = _normalize_tool_for_baseline(tool)
    baselines = get_baselines()
    per_tool = baselines.get("per_tool") if isinstance(baselines, dict) else None
    stats = per_tool.get(tool_key) if isinstance(per_tool, dict) and tool_key else None

    sample_count = int(stats["session_count"]) if stats else 0
    history_span_days = int(stats["history_span_days"]) if stats else 0
    excluded_cumulative = int(stats.get("excluded_cumulative", 0)) if stats else 0

    history_sufficient = (
        stats is not None
        and sample_count >= MIN_SAVINGS_SESSIONS
        and history_span_days >= MIN_SAVINGS_HISTORY_DAYS
    )
    basis = (
        f"{sample_count} local {'AI' if tool == 'agent' else tool} session{'s' if sample_count != 1 else ''} "
        f"spanning {history_span_days} day{'s' if history_span_days != 1 else ''}"
        if sample_count
        else "no comparable local history"
    )
    if tool_key == "codex-cli" and excluded_cumulative:
        basis += f"; excluded {excluded_cumulative} cumulative Codex thread total{'s' if excluded_cumulative != 1 else ''}"
    if tool_key == "codex-cli" and not sample_count:
        basis += "; no per-session Codex rollout token events are available"

    if not history_sufficient:
        return {
            "available": False,
            "confidence": "insufficient",
            "basis": basis,
            "sample_count": sample_count,
            "history_span_days": history_span_days,
            "required_sessions": MIN_SAVINGS_SESSIONS,
            "required_history_days": MIN_SAVINGS_HISTORY_DAYS,
            "direction": "A narrower prompt with checkpoints should reduce context and tool-call pressure, but AIWatcher cannot quantify savings yet.",
        }

    base_tokens = stats["p75_tokens"] or 120_000
    base_calls = stats["p75_calls"] or 80
    base_tool_calls = stats["p75_tool_calls"] or 40
    base_cost = stats["p75_api_value"] or 1.5
    confidence = "medium"

    risk_multiplier = 1.0 + min(8, max(0, risk_score)) * 0.28
    broad_bonus = 1.4 if any(term in prompt.lower() for term in ["entire codebase", "whole codebase", "all files", "everything"]) else 1.0
    original_low_tokens = int(base_tokens * risk_multiplier * broad_bonus * 0.65)
    original_high_tokens = int(base_tokens * risk_multiplier * broad_bonus * 1.35)
    original_low_calls = int(base_calls * risk_multiplier * broad_bonus * 0.65)
    original_high_calls = int(base_calls * risk_multiplier * broad_bonus * 1.35)
    original_low_tools = int(base_tool_calls * risk_multiplier * broad_bonus * 0.65)
    original_high_tools = int(base_tool_calls * risk_multiplier * broad_bonus * 1.35)
    original_low_cost = base_cost * risk_multiplier * broad_bonus * 0.65
    original_high_cost = base_cost * risk_multiplier * broad_bonus * 1.35

    safer_factor = 0.32 if risk_score >= 6 else 0.45 if risk_score >= 3 else 0.75
    safer_low_tokens = int(original_low_tokens * safer_factor)
    safer_high_tokens = int(original_high_tokens * min(0.65, safer_factor + 0.18))
    safer_low_calls = int(original_low_calls * safer_factor)
    safer_high_calls = int(original_high_calls * min(0.65, safer_factor + 0.18))
    safer_low_tools = int(original_low_tools * safer_factor)
    safer_high_tools = int(original_high_tools * min(0.65, safer_factor + 0.18))
    safer_low_cost = original_low_cost * safer_factor
    safer_high_cost = original_high_cost * min(0.65, safer_factor + 0.18)

    return {
        "available": True,
        "confidence": confidence,
        "basis": basis,
        "sample_count": sample_count,
        "history_span_days": history_span_days,
        "original": {
            "tokens": [original_low_tokens, original_high_tokens],
            "model_calls": [original_low_calls, original_high_calls],
            "tool_calls": [original_low_tools, original_high_tools],
            "api_value_usd": [round(original_low_cost, 4), round(original_high_cost, 4)],
        },
        "safer": {
            "tokens": [safer_low_tokens, safer_high_tokens],
            "model_calls": [safer_low_calls, safer_high_calls],
            "tool_calls": [safer_low_tools, safer_high_tools],
            "api_value_usd": [round(safer_low_cost, 4), round(safer_high_cost, 4)],
        },
        "savings": {
            "tokens": [max(0, original_low_tokens - safer_low_tokens), max(0, original_high_tokens - safer_high_tokens)],
            "model_calls": [max(0, original_low_calls - safer_low_calls), max(0, original_high_calls - safer_high_calls)],
            "tool_calls": [max(0, original_low_tools - safer_low_tools), max(0, original_high_tools - safer_high_tools)],
            "api_value_usd": [round(max(0, original_low_cost - safer_low_cost), 4), round(max(0, original_high_cost - safer_high_cost), 4)],
        },
    }


def build_execution_brief(
    prompt: str,
    *,
    cwd: str | None,
    broad_scope: bool,
    needs_checkpoint: bool,
    sensitive_or_destructive: bool,
    vague_scope: bool,
    multiple_tasks: bool,
) -> str:
    """Preserve the requested outcome while adding only relevant controls."""
    lines = [
        "Task",
        prompt.strip(),
        "",
        "Execution approach",
    ]
    if broad_scope:
        lines.append(
            "- Inspect the repository structure, identify the smallest relevant subsystem, and propose a phased plan before editing."
        )
        lines.append("- Complete one coherent phase at a time instead of changing the entire repository at once.")
    elif needs_checkpoint:
        lines.append("- Inspect the files directly relevant to this task and name the intended edits before changing them.")
    else:
        lines.append("- Work only in the files directly relevant to the requested outcome.")

    if vague_scope:
        lines.append("- Infer concrete acceptance criteria from current behavior and state them before implementation.")
    if multiple_tasks:
        lines.append("- Separate the request into discrete tasks and checkpoint after the first complete task.")
    if sensitive_or_destructive:
        lines.append(
            "- Do not reveal secret values or customer data. Ask for confirmation before deleting data, credentials, or production resources."
        )

    lines.extend([
        "- Preserve unrelated behavior and existing user changes.",
        "- Run the narrowest relevant verification after implementation.",
        "- Stop when the requested outcome is verified; do not expand into unrelated cleanup.",
    ])
    if cwd:
        lines.extend(["", "Working directory", cwd])
    lines.extend([
        "",
        "Completion report",
        "Summarize what changed, verification performed, remaining uncertainty, and any cost or security tradeoff.",
    ])
    return "\n".join(lines)


def _is_generated_brief(text: str) -> bool:
    """Detect text that is already an AIWatcher execution brief.

    Prevents re-scoring and re-wrapping a brief the user pasted back in — without
    this, `build_execution_brief` nests a second Task/Execution approach/Completion
    report shell around the first one every time a brief is resubmitted.
    """
    return (
        text.startswith("Task\n")
        and "\nExecution approach\n" in text
        and "\nCompletion report\n" in text
    )


def analyze_prompt(
    prompt: str,
    *,
    tool: str = "agent",
    cwd: str | None = None,
    include_estimate: bool = True,
) -> dict[str, object]:
    text = prompt.strip()
    if _is_generated_brief(text):
        return {
            "risk": "low",
            "score": 0,
            "tool": tool,
            "findings": ["This is already a scoped AIWatcher execution brief — not re-analyzing."],
            "suggestions": [],
            "suggested_prompt": "",
            "estimated_impact": {},
        }
    lower = text.lower()
    findings: list[str] = []
    suggestions: list[str] = []
    guardrails: list[dict[str, str]] = []
    score = 0

    broad_terms = [
        "entire codebase", "whole codebase", "all files", "everything", "full rewrite",
        "rewrite the app", "refactor everything", "fix all", "scan all",
    ]
    broad_scope = any(term in lower for term in broad_terms)
    scope_guardrails = any(term in lower for term in [
        "smallest relevant subsystem", "one coherent phase", "phased plan",
        "do not expand into unrelated", "smallest relevant files",
    ])
    if broad_scope:
        if scope_guardrails:
            score += 1
            findings.append("Scope is broad, but explicit phasing and stop conditions reduce execution pressure.")
        else:
            score += 3
            findings.append("Scope looks broad and likely to create large context or many tool calls.")
            suggestions.append("Start with a plan-only pass over the smallest relevant files before editing.")
        # build_execution_brief() still adds the scope-narrowing bullet
        # unconditionally on broad_scope (regardless of scope_guardrails), so
        # the chip must match what's actually added to the brief.
        guardrails.append({"icon": "\U0001F50E", "label": "Scope narrowed"})
    else:
        # Multi-file/product-UI breadth doesn't always use the fixed
        # broad_terms phrases above (e.g. "every page", "all screens") — this
        # catches quantifier + surface-area-noun patterns the keyword list
        # misses, so scope pressure is flagged even without a security word.
        breadth_nouns = (
            "page|pages|screen|screens|view|views|component|components|module|modules|"
            "file|files|endpoint|endpoints|route|routes|service|services|api|apis"
        )
        breadth_match = re.search(
            rf"\b(every|all|each)\b.{{0,20}}?\b(?:{breadth_nouns})\b", lower
        ) or re.search(r"\bthroughout the (app|codebase|repo|project|site)\b", lower)
        if breadth_match:
            broad_scope = True
            if scope_guardrails:
                score += 1
                findings.append("Scope is broad, but explicit phasing and stop conditions reduce execution pressure.")
            else:
                score += 3
                findings.append("Request touches many files or pages across the app, which risks a large, hard-to-review change.")
                suggestions.append(
                    "Identify the shared pattern or component first and propose a phased rollout instead of touching every page at once."
                )
            guardrails.append({"icon": "\U0001F50E", "label": "Scope narrowed"})

    edit_terms = ["change", "modify", "edit", "write", "implement", "refactor", "delete", "migrate", "rename", "add", "update"]
    plan_terms = ["plan first", "do not edit", "inspect first", "propose", "before editing", "ask before"]
    needs_checkpoint = any(term in lower for term in edit_terms) and not any(term in lower for term in plan_terms)
    if needs_checkpoint:
        score += 2
        findings.append("Prompt asks for changes without an explicit plan/checkpoint.")
        suggestions.append("Ask the agent to inspect, summarize the intended change, then proceed after the plan is clear.")
        guardrails.append({"icon": "\U0001F4CB", "label": "Plan-first checkpoint"})

    risky_terms = [
        "production", "prod database", "customer data", "pii", "secret", "api key",
        "access token", "auth token", "bearer token", "refresh token", "session token",
        ".env", "credential", "delete", "drop table", "rm -rf", "payment", "stripe",
    ]
    # Security-weakening prompts ("remove signature check", "make auth less
    # strict") rarely use any risky_terms keyword — they read as ordinary
    # feature work unless we also look for a security-control noun paired
    # with a verb that removes or loosens it.
    security_controls = (
        r"signature (?:check|verification|validation)|jwt signature|"
        r"token (?:check|validation|verification)|"
        r"auth(?:entication|orization)? (?:check|guard|validation|verification|middleware)|"
        r"permission(?:s)? (?:check|validation|verification)|access control|"
        r"input validation|request validation|schema validation|"
        r"csrf (?:check|validation|verification)|cors (?:check|validation|verification)|"
        r"(?:ssl|tls|certificate) verification|encryption|2fa|mfa"
    )
    weaken_verbs = (
        r"remove|disable|skip|bypass|turn off|weaken|ignore|less strict|"
        r"no longer (?:check|verify|validate)|stop (?:checking|verifying|validating)"
    )
    security_weakening = bool(
        re.search(rf"\b(?:{weaken_verbs})\b.{{0,35}}?\b(?:{security_controls})\b", lower)
        or re.search(rf"\b(?:{security_controls})\b.{{0,35}}?\b(?:{weaken_verbs})\b", lower)
        or re.search(
            r"\bmake\s+(?:auth|authentication|authorization|permissions?|access control)\s+less strict\b",
            lower,
        )
    )
    sensitive_or_destructive = any(term in lower for term in risky_terms) or security_weakening
    safety_guardrails = any(term in lower for term in [
        "ask for confirmation before", "require confirmation before",
        "do not reveal secret", "do not make destructive changes",
        "avoid exposing secrets",
    ])
    if sensitive_or_destructive:
        if safety_guardrails:
            score += 1
            findings.append("Sensitive or destructive work is present with an explicit confirmation boundary.")
        elif security_weakening:
            score += 3
            findings.append("Prompt weakens or removes a security control (auth/signature/validation) without a guardrail.")
            suggestions.append("Keep the existing check in place, or require explicit confirmation and a follow-up security review before removing it.")
        else:
            score += 3
            findings.append("Prompt mentions sensitive data, credentials, production systems, or destructive actions.")
            suggestions.append("Require confirmation before destructive changes and avoid exposing secrets or customer data.")
        # build_execution_brief() still adds the confirm-before-destructive
        # bullet unconditionally on sensitive_or_destructive (regardless of
        # safety_guardrails), so the chip must match what's actually added.
        guardrails.append({"icon": "\U0001F6D1", "label": "Confirm before destructive changes"})

    vague_terms = ["make it better", "improve everything", "clean this up", "fix it", "optimize it"]
    vague_scope = any(term in lower for term in vague_terms)
    if vague_scope:
        score += 1
        findings.append("Prompt is vague, which can cause exploratory loops.")
        suggestions.append("Name the target files, acceptance criteria, and what should stay unchanged.")
        guardrails.append({"icon": "\U0001F3AF", "label": "Vague ask clarified"})

    multiple_tasks = len(text) > 2500
    if multiple_tasks:
        score += 2
        findings.append("Prompt is long enough to hide multiple tasks in one request.")
        suggestions.append("Split this into one task per prompt and checkpoint between them.")
        guardrails.append({"icon": "✂️", "label": "Split into smaller tasks"})

    if not findings:
        findings.append("No obvious cost or safety risk found from prompt text alone.")
        suggestions.append("Keep the task scoped and ask for a brief plan before large edits.")

    risk = "low"
    if score >= 6:
        risk = "high"
    elif score >= 3:
        risk = "medium"

    safer_prompt = build_execution_brief(
        text,
        cwd=cwd,
        broad_scope=broad_scope,
        needs_checkpoint=needs_checkpoint,
        sensitive_or_destructive=sensitive_or_destructive,
        vague_scope=vague_scope,
        multiple_tasks=multiple_tasks,
    )
    return {
        "risk": risk,
        "score": score,
        "tool": tool,
        "findings": findings,
        "suggestions": suggestions,
        "guardrails": guardrails,
        "suggested_prompt": safer_prompt,
        "estimated_impact": (
            estimate_prompt_savings(text, risk_score=score, tool=tool, cwd=cwd)
            if score > 0 and include_estimate
            else {}
        ),
    }


def render_preflight(result: dict[str, object]) -> str:
    impact = result.get("estimated_impact") if isinstance(result.get("estimated_impact"), dict) else {}
    original = impact.get("original", {}) if isinstance(impact.get("original"), dict) else {}
    safer = impact.get("safer", {}) if isinstance(impact.get("safer"), dict) else {}
    savings = impact.get("savings", {}) if isinstance(impact.get("savings"), dict) else {}
    lines = [
        "AIWatcher prompt preflight",
        f"Risk: {result['risk']}",
        f"Score: {result['score']}",
        f"Tool: {result['tool']}",
        "",
        "Findings",
    ]
    lines.extend(f"- {item}" for item in result["findings"])
    if int(result.get("score", 0)) > 0 and impact and not impact.get("available", False):
        lines.extend([
            "",
            "Expected impact",
            str(impact.get("direction", "A narrower prompt should reduce execution pressure.")),
            (
                "Quantified savings unavailable: "
                f"AIWatcher needs at least {impact.get('required_sessions', MIN_SAVINGS_SESSIONS)} comparable sessions "
                f"spanning {impact.get('required_history_days', MIN_SAVINGS_HISTORY_DAYS)} days. "
                f"Current basis: {impact.get('basis', 'local history unavailable')}."
            ),
        ])
    elif int(result.get("score", 0)) > 0 and original and safer and savings:
        lines.extend([
            "",
            "Estimated impact",
            f"Original prompt: {_number_range_label(*original['tokens'])} tokens | {_number_range_label(*original['model_calls'])} model calls | {_number_range_label(*original['tool_calls'])} tool calls | {_range_label(*original['api_value_usd'], money)} API-equivalent",
            f"Safer prompt: {_number_range_label(*safer['tokens'])} tokens | {_number_range_label(*safer['model_calls'])} model calls | {_number_range_label(*safer['tool_calls'])} tool calls | {_range_label(*safer['api_value_usd'], money)} API-equivalent",
            f"Estimated savings: {_number_range_label(*savings['tokens'])} tokens | {_number_range_label(*savings['model_calls'])} model calls | {_number_range_label(*savings['tool_calls'])} tool calls | {_range_label(*savings['api_value_usd'], money)} API-equivalent",
            f"Planning confidence: {impact.get('confidence', 'low')} ({impact.get('basis', 'local history unavailable')})",
            "These are planning ranges, not guaranteed billing savings.",
        ])
    lines.extend(["", "Suggestions"])
    lines.extend(f"- {item}" for item in result["suggestions"])
    lines.extend(["", "Suggested execution brief", str(result["suggested_prompt"])])
    return "\n".join(lines)


def _impact_summary(result: dict[str, object]) -> str:
    impact = result.get("estimated_impact") if isinstance(result.get("estimated_impact"), dict) else {}
    if not impact:
        return "AIWatcher can identify risk, but no local history exists yet for savings estimates."
    if not impact.get("available", False):
        return (
            f"{impact.get('direction', 'A narrower prompt should reduce execution pressure.')} "
            f"Measured savings need at least {impact.get('required_sessions', MIN_SAVINGS_SESSIONS)} comparable sessions "
            f"over {impact.get('required_history_days', MIN_SAVINGS_HISTORY_DAYS)} days."
        )
    savings = impact.get("savings", {}) if isinstance(impact.get("savings"), dict) else {}
    if not savings:
        return "AIWatcher found comparable sessions, but could not calculate a savings range."
    return (
        f"Estimated avoidable pressure: {_number_range_label(*savings['tokens'])} tokens, "
        f"{_number_range_label(*savings['tool_calls'])} tool calls, "
        f"{_range_label(*savings['api_value_usd'], money)} API-equivalent. "
        f"Confidence: {impact.get('confidence', 'low')}."
    )


def _selected_prompt_assessment(
    selected_prompt: str | None,
    *,
    original_prompt: str,
    original_result: dict[str, object],
    tool: str,
    cwd: str,
) -> tuple[str | None, int | None]:
    if not selected_prompt:
        return None, None
    if selected_prompt == original_prompt:
        return str(original_result["risk"]), int(original_result["score"])
    selected = analyze_prompt(selected_prompt, tool=tool, cwd=cwd, include_estimate=False)
    return str(selected["risk"]), int(selected["score"])


def _hero_savings_label(result: dict[str, object]) -> str | None:
    """A short, glanceable savings figure for the gate header -- no reading required.

    Returns None (rather than a placeholder string) when there isn't enough
    local history for a real number, so the caller can omit the badge
    entirely instead of showing a hedge like "no estimate yet".
    """
    impact = result.get("estimated_impact") if isinstance(result.get("estimated_impact"), dict) else {}
    if not impact or not impact.get("available", False):
        return None
    savings = impact.get("savings", {}) if isinstance(impact.get("savings"), dict) else {}
    api_value = savings.get("api_value_usd")
    if not isinstance(api_value, list) or len(api_value) != 2:
        return None
    return f"~{_range_label(*api_value, money)} avoidable"


def _hero_pressure_label(result: dict[str, object]) -> str | None:
    """A compact tokens/tool-calls figure to sit right next to the dollar
    savings badge -- the full sentence (with confidence wording and basis)
    still lives in the "What AIWatcher noticed" card via _impact_summary()
    for anyone who wants the detail behind the headline number.
    """
    impact = result.get("estimated_impact") if isinstance(result.get("estimated_impact"), dict) else {}
    if not impact or not impact.get("available", False):
        return None
    savings = impact.get("savings", {}) if isinstance(impact.get("savings"), dict) else {}
    tokens = savings.get("tokens")
    tool_calls = savings.get("tool_calls")
    if not isinstance(tokens, list) or len(tokens) != 2:
        return None
    if not isinstance(tool_calls, list) or len(tool_calls) != 2:
        return None
    return f"{_number_range_label(*tokens)} tokens · {_number_range_label(*tool_calls)} tool calls avoided"


_BRIEF_STATIC_SUFFIX_MARKERS = ("\n\nWorking directory\n", "\n\nCompletion report\n")


def _split_brief_for_display(brief: str) -> tuple[str, str]:
    """Split a full execution brief into the decision-relevant core (Task +
    execution-approach bullets) and the static suffix (working directory,
    completion-report instructions) that reads identically on every gate
    screen. The suffix still gets sent as part of the final prompt -- only
    its on-screen presentation is collapsed, since it carries no
    decision-relevant signal and was costing pure scroll distance.
    """
    indices = [i for i in (brief.find(marker) for marker in _BRIEF_STATIC_SUFFIX_MARKERS) if i != -1]
    if not indices:
        return brief, ""
    cut = min(indices)
    return brief[:cut], brief[cut:].lstrip("\n")


def _split_core_for_diff(core: str) -> tuple[str, list[str]]:
    """Split the brief core into the original task text and the added
    execution-approach bullets, so the caller can render them with distinct
    styling (unchanged vs. added) instead of undifferentiated prose --
    scannable the way a code diff is, not something read line by line to
    detect what changed.
    """
    marker = "\n\nExecution approach\n"
    idx = core.find(marker)
    if idx == -1:
        return core, []
    task_section = core[:idx]
    if task_section.startswith("Task\n"):
        task_section = task_section[len("Task\n"):]
    bullets_block = core[idx + len(marker):]
    bullets = [line[2:] for line in bullets_block.split("\n") if line.startswith("- ")]
    return task_section, bullets


def _prompt_gate_html(*, tool: str, cwd: str, prompt: str, result: dict[str, object]) -> str:
    findings = "".join(f"<li>{html.escape(str(item))}</li>" for item in result["findings"])
    suggestions = "".join(f"<li>{html.escape(str(item))}</li>" for item in result["suggestions"])
    brief_core, brief_suffix = _split_brief_for_display(str(result["suggested_prompt"]))
    brief = html.escape(brief_core)
    brief_footer = (
        f'<details class="brief-footer"><summary>Working directory &amp; completion report '
        f'(unchanged every time)</summary><pre id="brief-suffix">{html.escape(brief_suffix)}</pre></details>'
        if brief_suffix
        else ""
    )
    task_section, brief_bullets = _split_core_for_diff(brief_core)
    added_lines = "".join(
        f'<div class="added-line">{html.escape(bullet)}</div>' for bullet in brief_bullets
    )
    brief_editable = f'<textarea id="brief">{brief}</textarea>'
    brief_body = (
        f'<div class="brief-diff">'
        f'<div class="brief-task">{html.escape(task_section)}</div>'
        f'<div class="brief-added">{added_lines}</div>'
        f'</div>'
        f'<details class="brief-edit"><summary>Edit this brief before sending</summary>{brief_editable}</details>'
        if brief_bullets
        else brief_editable
    )
    original = html.escape(prompt)
    risk = html.escape(str(result["risk"]))
    score = html.escape(str(result["score"]))
    impact = html.escape(_impact_summary(result))
    tool_label = html.escape(tool)
    savings_label = _hero_savings_label(result)
    savings_pill = (
        f'<span class="pill savings">{html.escape(savings_label)}</span>' if savings_label else ""
    )
    pressure_label = _hero_pressure_label(result)
    pressure_caption = (
        f'<div class="pressure-caption">{html.escape(pressure_label)}</div>' if pressure_label else ""
    )
    guardrail_chips = "".join(
        f'<span class="chip"><span class="chip-icon">{html.escape(str(g["icon"]))}</span>{html.escape(str(g["label"]))}</span>'
        for g in result.get("guardrails", [])
    )
    guardrail_row = (
        f'<div class="guardrails">{guardrail_chips}</div>'
        if guardrail_chips
        else '<div class="guardrails"><span class="chip chip-clean">No guardrails needed — prompt looked scoped as written.</span></div>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIWatcher Prompt Gate</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #090d12;
  --panel: #111821;
  --panel-2: #172131;
  --line: #28364a;
  --text: #eef4fb;
  --muted: #9ba9bb;
  --accent: #54d7b7;
  --blue: #75a7ff;
  --amber: #f7c66b;
  --red: #ff7f93;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: radial-gradient(circle at 15% 0%, rgba(84,215,183,.12), transparent 32%), var(--bg);
  color: var(--text);
}}
main {{ width: min(1180px, calc(100vw - 40px)); margin: 36px auto; }}
.top {{ display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; margin-bottom: 24px; }}
h1 {{ margin: 0 0 8px; font-size: clamp(32px, 5vw, 58px); letter-spacing: 0; }}
p {{ margin: 0; color: var(--muted); line-height: 1.5; }}
.pill {{ display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--line); border-radius: 999px; padding: 8px 12px; color: var(--muted); background: #0c121a; }}
.risk {{ color: {'var(--red)' if risk == 'high' else 'var(--amber)'}; border-color: {'rgba(255,127,147,.42)' if risk == 'high' else 'rgba(247,198,107,.42)'}; }}
.savings {{ color: #061019; background: linear-gradient(135deg, #36d6a5, #6aa7ff); border: 0; font-weight: 800; }}
.savings-block {{ display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }}
.pressure-caption {{ font-size: 15px; color: var(--muted); text-align: left; }}
.guardrails {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 24px; }}
.chip {{ display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--line); border-radius: 999px; padding: 10px 16px; background: var(--panel-2); color: var(--text); font-weight: 600; font-size: 15px; }}
.chip-icon {{ font-size: 17px; line-height: 1; }}
.chip-clean {{ color: var(--muted); font-weight: 400; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
.card {{ background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.01)), var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 22px; box-shadow: 0 16px 48px rgba(0,0,0,.28); }}
h2 {{ margin: 0 0 14px; font-size: 21px; }}
h3 {{ margin: 18px 0 10px; font-size: 15px; color: #c8d4e4; text-transform: uppercase; letter-spacing: .08em; }}
ul {{ margin: 0; padding-left: 20px; color: #dbe5f1; line-height: 1.55; }}
pre, textarea {{ width: 100%; min-height: 220px; border: 1px solid var(--line); border-radius: 8px; background: #080d14; color: #e8f0fa; padding: 16px; white-space: pre-wrap; overflow: auto; font: 14px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
textarea {{ resize: vertical; min-height: 260px; }}
.impact {{ border-left: 4px solid var(--accent); padding: 12px 14px; background: rgba(84,215,183,.08); border-radius: 8px; color: #d9f8ee; margin: 14px 0 0; }}
.actions {{ position: sticky; bottom: 0; margin-top: 20px; display: grid; grid-template-columns: 1.2fr 1fr 1fr 1fr; gap: 10px; padding: 16px; background: rgba(9,13,18,.94); border: 1px solid var(--line); border-radius: 8px; backdrop-filter: blur(10px); }}
button {{ appearance: none; border: 1px solid var(--line); border-radius: 8px; min-height: 48px; padding: 0 16px; background: #0f1722; color: var(--text); font: inherit; font-weight: 700; cursor: pointer; }}
button:hover {{ border-color: var(--blue); transform: translateY(-1px); }}
button:disabled {{ cursor: wait; opacity: .62; transform: none; }}
.primary {{ background: linear-gradient(135deg, #36d6a5, #6aa7ff); color: #061019; border: 0; }}
.danger {{ color: #ffd4dc; border-color: rgba(255,127,147,.45); }}
.privacy {{ margin-top: 16px; font-size: 13px; color: var(--muted); }}
.brief-footer {{ margin-top: 10px; }}
.brief-footer summary {{ cursor: pointer; font-size: 13px; color: var(--muted); user-select: none; }}
.brief-footer summary:hover {{ color: var(--text); }}
.brief-footer pre {{ min-height: 0; margin-top: 8px; font-size: 13px; color: var(--muted); background: #0c121a; }}
.brief-diff {{ border: 1px solid var(--line); border-radius: 8px; background: #080d14; padding: 16px; }}
.brief-task {{ white-space: pre-wrap; color: #dbe5f1; font: 14px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
.brief-added {{ margin-top: 14px; display: flex; flex-direction: column; gap: 6px; }}
.added-line {{ border-left: 3px solid var(--accent); background: rgba(84,215,183,.08); padding: 6px 10px; color: #d9f8ee; font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; }}
.added-line::before {{ content: "+ "; color: var(--accent); font-weight: 700; }}
.brief-edit {{ margin-top: 12px; }}
.brief-edit summary {{ cursor: pointer; font-size: 13px; color: var(--muted); user-select: none; }}
.brief-edit summary:hover {{ color: var(--text); }}
.brief-edit textarea {{ margin-top: 8px; min-height: 220px; }}
@media (max-width: 880px) {{
  main {{ width: min(100vw - 24px, 720px); margin: 18px auto; }}
  .top, .grid, .actions {{ grid-template-columns: 1fr; display: grid; }}
}}
</style>
</head>
<body>
<main>
  <div class="top">
    <div>
      <h1>AIWatcher Prompt Gate</h1>
      <p>Review the work before {tool_label} starts. Use the brief to narrow scope, keep checkpoints, and reduce avoidable cost or safety risk.</p>
    </div>
    <div>
      <span class="pill risk">Risk: {risk} | score {score}</span>
      <span class="pill">{tool_label}</span>
      <div class="savings-block">
        {savings_pill}
        {pressure_caption}
      </div>
    </div>
  </div>
  {guardrail_row}
  <div class="grid">
    <section class="card">
      <h2>What AIWatcher noticed</h2>
      <h3>Findings</h3>
      <ul>{findings}</ul>
      <h3>Suggestions</h3>
      <ul>{suggestions}</ul>
      <div class="impact">{impact}</div>
      <p class="privacy">Prompt content is only held in this local browser page while you decide. AIWatcher stores hashes and decisions, not prompt text.</p>
      <h3>Original prompt</h3>
      <pre>{original}</pre>
    </section>
    <section class="card">
      <h2>Execution brief</h2>
      <p>Keep the requested outcome, but add guardrails before tools run.</p>
      {brief_body}
      {brief_footer}
    </section>
  </div>
  <div class="actions">
    <button class="primary" onclick="sendDecision('use_brief')">Add safer brief</button>
    <button onclick="sendDecision('edit')">Add edited brief</button>
    <button onclick="sendDecision('run_original')">Run original</button>
    <button class="danger" onclick="sendDecision('cancel')">Cancel run</button>
  </div>
  <p id="decision-status" class="privacy" role="status"></p>
</main>
<script>
async function sendDecision(decision) {{
  const buttons = Array.from(document.querySelectorAll('button'));
  const status = document.getElementById('decision-status');
  buttons.forEach(button => button.disabled = true);
  status.textContent = 'Applying your decision…';
  try {{
    const core = document.getElementById('brief').value;
    const suffixEl = document.getElementById('brief-suffix');
    // The static suffix (working directory, completion-report instructions) is
    // collapsed out of view because it never changes, but it still has to be
    // part of what's actually sent -- reattach it here rather than dropping it.
    const prompt = suffixEl ? core + '\\n\\n' + suffixEl.textContent : core;
    const body = {{ decision, prompt }};
    const response = await fetch('/decision', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(body) }});
    const saved = await response.json();
    if (!response.ok) throw new Error(saved.error || `Request failed (${{response.status}})`);
    const title = decision === 'cancel' ? 'Run cancelled' : decision === 'run_original' ? 'Original approved' : 'Execution brief added';
    const riskChange = saved.selected_score === null
      ? 'No prompt will run.'
      : `Risk ${{saved.original_risk}} (${{saved.original_score}}) → ${{saved.selected_risk}} (${{saved.selected_score}})`;
    document.body.innerHTML = `<main>
      <div class="top"><div><h1>${{title}}</h1><p>Your choice has been returned to ${{saved.tool}}.</p></div><span class="pill">${{saved.decision_label}}</span></div>
      <section class="card" style="max-width:760px">
        <h2>${{riskChange}}</h2>
        <div class="impact">${{saved.impact}}</div>
        <p style="margin-top:16px">Return to your AI tool. On Claude hooks, an accepted brief is added beside the original request; cancelling blocks the original request entirely.</p>
      </section>
    </main>`;
  }} catch (error) {{
    buttons.forEach(button => button.disabled = false);
    status.textContent = `AIWatcher could not apply this decision: ${{error.message}}. The host may have timed out; return to the AI tool and confirm before continuing.`;
  }}
}}
</script>
</body>
</html>"""


def _display_available() -> bool:
    """Best-effort check for whether a GUI display exists to open a browser.

    Deliberately conservative (only returns False when we're fairly sure
    there's no display) -- a false positive here just means we still try
    webbrowser.open(), whose own failure is caught separately in
    run_prompt_gate(). A false negative would wrongly deny a working
    display, so this never denies Windows or a non-SSH, non-Linux POSIX
    session (e.g. a normal macOS desktop, which doesn't use DISPLAY at all).
    """
    if os.name == "nt":
        return True
    is_ssh = bool(
        os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT")
    )
    display = os.environ.get("DISPLAY", "").strip()
    if is_ssh and not display:
        return False
    if sys.platform.startswith("linux") and not display:
        return False
    return True


def _terminal_prompt_gate(*, tool: str, prompt: str, result: dict[str, object]) -> dict[str, str]:
    """Render the gate as plain text and prompt for a decision at the TTY.

    Returns the same {"decision", "prompt"} shape as the browser gate, using
    the same decision vocabulary (use_brief/edit/run_original/cancel), so
    callers need no special-casing for this path.
    """
    print(f"\nNo display available for the {tool} prompt gate -- using the terminal instead.", file=sys.stderr)
    print(render_preflight(result), file=sys.stderr)
    print(file=sys.stderr)
    while True:
        sys.stderr.write("[b]rief / [e]dit / [o]riginal / [c]ancel: ")
        sys.stderr.flush()
        try:
            choice = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return {"decision": "cancel", "prompt": ""}
        if choice in {"b", "brief"}:
            return {"decision": "use_brief", "prompt": str(result["suggested_prompt"])}
        if choice in {"e", "edit"}:
            print("Enter the edited prompt, then press Enter:", file=sys.stderr)
            edited = input()
            return {"decision": "edit", "prompt": edited.strip() or str(result["suggested_prompt"])}
        if choice in {"o", "original"}:
            return {"decision": "run_original", "prompt": prompt}
        if choice in {"c", "cancel"}:
            return {"decision": "cancel", "prompt": ""}
        print("Please enter b, e, o, or c.", file=sys.stderr)


def _auto_decide_headless_gate(*, result: dict[str, object]) -> dict[str, str]:
    """No display and no TTY: decide instantly instead of waiting to time out.

    Returns a distinct auto_brief_headless/auto_block_headless decision
    (rather than reusing use_brief/cancel) so a fully unattended default is
    never confused with a real human's use_brief/cancel choice -- callers
    must record these as their own decision, not translate them.
    """
    default = os.environ.get("AIWATCHER_GATE_DEFAULT", "block").strip().lower()
    if default == "brief":
        return {"decision": "auto_brief_headless", "prompt": str(result["suggested_prompt"])}
    return {"decision": "auto_block_headless", "prompt": ""}


def _fallback_prompt_gate(*, tool: str, prompt: str, result: dict[str, object]) -> dict[str, str]:
    if sys.stdin.isatty():
        return _terminal_prompt_gate(tool=tool, prompt=prompt, result=result)
    return _auto_decide_headless_gate(result=result)


def run_prompt_gate(
    *,
    tool: str,
    cwd: str,
    prompt: str,
    result: dict[str, object],
    timeout_seconds: int = PROMPT_GATE_TIMEOUT_SECONDS,
    open_browser: bool = True,
    ready_callback: Callable[[str], None] | None = None,
) -> dict[str, str] | None:
    if open_browser and not _display_available():
        return _fallback_prompt_gate(tool=tool, prompt=prompt, result=result)

    decision_event = threading.Event()
    state: dict[str, str] = {}

    class GateHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)
            self.wfile.flush()

        def do_GET(self) -> None:
            if self.path != "/":
                self._send(404, "Not found", "text/plain; charset=utf-8")
                return
            self._send(200, _prompt_gate_html(tool=tool, cwd=cwd, prompt=prompt, result=result))

        def do_POST(self) -> None:
            if self.path != "/decision":
                self._send(404, json.dumps({"error": "not found"}), "application/json; charset=utf-8")
                return
            length = int(self.headers.get("Content-Length") or "0")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except json.JSONDecodeError:
                payload = {}
            decision = str(payload.get("decision") or "").strip()
            if decision not in {"use_brief", "edit", "run_original", "cancel"}:
                self._send(400, json.dumps({"error": "unknown decision"}), "application/json; charset=utf-8")
                return
            state["decision"] = decision
            selected_prompt = str(payload.get("prompt") or result.get("suggested_prompt") or "")
            if decision == "run_original":
                selected_prompt = prompt
            elif decision == "cancel":
                selected_prompt = ""
            selected_risk, selected_score = _selected_prompt_assessment(
                selected_prompt or None,
                original_prompt=prompt,
                original_result=result,
                tool=tool,
                cwd=cwd,
            )
            state["prompt"] = selected_prompt
            state["selected_risk"] = selected_risk or ""
            state["selected_score"] = str(selected_score) if selected_score is not None else ""
            labels = {
                "use_brief": "Add safer brief",
                "edit": "Add edited brief",
                "run_original": "Run original",
                "cancel": "Cancel run",
            }
            self._send(200, json.dumps({
                "ok": True,
                "tool": tool,
                "decision_label": labels[decision],
                "original_risk": result["risk"],
                "original_score": result["score"],
                "selected_risk": selected_risk,
                "selected_score": selected_score,
                "impact": _impact_summary(result),
            }), "application/json; charset=utf-8")
            decision_event.set()

    server = LocalThreadingHTTPServer(("127.0.0.1", 0), GateHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        if ready_callback:
            ready_callback(url)
        if open_browser:
            try:
                opened = webbrowser.open(url)
            except Exception:
                opened = False
            if not opened:
                # A display looked available, but webbrowser.open() itself
                # couldn't launch anything (no known browser controller) --
                # equivalent to no display: fall back rather than wait out
                # the full timeout for a decision that will never come.
                return _fallback_prompt_gate(tool=tool, prompt=prompt, result=result)
        if not decision_event.wait(max(1, timeout_seconds)):
            return None
        return dict(state)
    finally:
        server.shutdown()
        server.server_close()


def event_sort_key(event: LocalEvent) -> datetime:
    return event.timestamp or MIN_DT


def events_for_session(session_id: str, *, days: int = 30) -> list[LocalEvent]:
    since = datetime.now().astimezone() - timedelta(days=days)
    return sorted(
        [
            event for event in scan_all_events()
            if event.session_id == session_id and event.timestamp and event.timestamp.astimezone() >= since
        ],
        key=event_sort_key,
    )


def render_session_timeline(session_id: str, *, days: int = 30, limit: int = 30) -> str:
    events = events_for_session(session_id, days=days)
    session = next((row for row in scan_all() if row.session_id == session_id), None)
    if not events and not session:
        return f"No local timeline found for session {session_id!r} in the last {days} days."
    if not events:
        return (
            f"No event-level timeline is available for session {session_id!r}.\n"
            "This usually means the tool exposes only session totals locally. Claude Code currently has the richest event timeline."
        )

    total_cost = sum(event.cost_usd for event in events)
    total_tokens = sum(event.tokens_in + event.tokens_out for event in events)
    by_type: dict[str, int] = defaultdict(int)
    for event in events:
        by_type[event.event_type] += 1
    costliest = max(events, key=lambda event: (event.cost_usd, event.tokens_in + event.tokens_out))
    repeated = sorted(by_type.items(), key=lambda item: item[1], reverse=True)

    lines = [
        f"AIWatcher session timeline - {session_id}",
        f"Events: {len(events)}",
        f"Timeline API-equivalent value: {money(total_cost)}",
        f"Timeline tokens: {compact_int(total_tokens)}",
    ]
    if session:
        lines.append(f"Project: {session.project_path or 'unknown'}")
        lines.append(f"Tool/model: {session.tool} / {session.model or 'unknown'}")
    lines.extend([
        "",
        "Why this may have become expensive",
        f"- Costliest event: {costliest.event_type} | {money(costliest.cost_usd)} | {compact_int(costliest.tokens_in + costliest.tokens_out)} tokens",
    ])
    if repeated:
        label, count = repeated[0]
        lines.append(f"- Most repeated event type: {label} ({count} events)")
    if len(events) >= 80:
        lines.append("- High event count. Consider splitting similar future tasks into smaller checkpoints.")
    if total_tokens >= 500_000:
        lines.append("- Large token footprint. Consider narrowing files/context before asking for implementation.")

    lines.extend(["", "Timeline"])
    for idx, event in enumerate(events[:limit], 1):
        stamp = format_short_datetime(event.timestamp.astimezone()) if event.timestamp else "unknown"
        token_label = compact_int(event.tokens_in + event.tokens_out)
        cost_label = money(event.cost_usd)
        hash_label = f" | hash {event.content_hash[:10]}" if event.content_hash else ""
        lines.append(
            f"{idx:>2}. {stamp} | {event.event_type} | {event.model or 'unknown'} | {token_label} tokens | {cost_label}{hash_label}"
        )
    if len(events) > limit:
        lines.append(f"... {len(events) - limit} more events omitted. Increase --limit to inspect more.")
    return "\n".join(lines)


def render_journal(days: int = 1) -> str:
    rows = sessions_since(days)
    if not rows:
        return f"No local AI work detected in the last {days} day{'s' if days != 1 else ''}."
    stats = summarize(rows)
    by_project: dict[str, list[LocalSession]] = defaultdict(list)
    for row in rows:
        by_project[row.project_path or "unknown"].append(row)
    top = max(by_project.items(), key=lambda item: summarize(item[1])["cost_usd"])
    top_project_stats = summarize(top[1])
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

    return "\n".join([
        f"AIWatcher daily journal - last {days} day{'s' if days != 1 else ''}",
        f"Sessions: {stats['sessions']} | {money(float(stats['cost_usd']))} API-equivalent | {token_summary_label(rows)}",
        f"Top project: {short_path(top[0], 72)} ({money(float(top_project_stats['cost_usd']))})",
        f"Most expensive session: {short_path(costliest.project_path)} | {costliest.tool} | {money(costliest.cost_usd)} | {compact_int(costliest.tokens_in + costliest.tokens_out)} tokens",
        (
            f"Largest reliable context session: {short_path(largest_context.project_path)} | "
            f"{compact_int(largest_context.tokens_in + largest_context.tokens_out)} tokens"
            if largest_context else "Largest reliable context session: unavailable from local logs"
        ),
        (
            f"Loop signal: {loop_candidate.agent_calls} model calls in {short_path(loop_candidate.project_path)}"
            if loop_candidate else "Loop signal: unavailable from local logs"
        ),
        "",
        "One thing to change next time",
        f"- {improvement}",
    ])


def command_start(_args: argparse.Namespace) -> int:
    detected = discover_tools()
    sessions = sessions_since(1)
    print("AIWatcher v0.1.0 - local mode")
    print("Read-only scan. No data leaves this machine.\n")
    print("Watching:")
    labels = {
        "claude-code": "Claude Code",
        "cursor": "Cursor",
        "codex-cli": "Codex CLI",
        "cline": "Cline",
        "windsurf": "Windsurf",
    }
    for key, label in labels.items():
        print(f"  {'[OK]' if detected.get(key) else '[--]'} {label}")
    print(f"\nCollected {len(sessions)} sessions from the last 24 hours.")
    print("Run `aiwatcher today` or `python -m aiwatcher_cli today` to see your usage.")
    print("Connect Cloud later for team spend, budget guardrails, and audit evidence.")
    return 0


def command_status(_args: argparse.Namespace) -> int:
    detected = discover_tools()
    sessions = scan_all()
    print("AIWatcher Local status\n")
    for tool, installed in detected.items():
        tool_sessions = [row for row in sessions if row.tool == tool]
        print(f"{'[OK]' if installed else '[--]'} {tool:12} {len(tool_sessions):>5} sessions")
    print("\nMode: local-only")
    print("Network: disabled unless hosted sync is configured separately")
    return 0


def command_today(_args: argparse.Namespace) -> int:
    now = datetime.now().astimezone()
    today_start = local_midnight(now.date())
    week_start = now - timedelta(days=7)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    all_sessions = scan_all()
    try:
        link_recent_interventions_to_sessions(all_sessions)
    except OSError:
        pass
    try:
        get_or_refresh_baselines()
    except OSError:
        pass
    today = [row for row in all_sessions if in_window(row, today_start)]
    week = [row for row in all_sessions if in_window(row, week_start)]
    month = [row for row in all_sessions if in_window(row, month_start)]

    print(f"Today - {format_full_date(now)}")
    by_tool: dict[str, list[LocalSession]] = defaultdict(list)
    for session in today:
        by_tool[session.tool].append(session)

    if not by_tool:
        print("No local AI coding sessions detected today.")

    today_stats = summarize(today)
    week_stats = summarize(week)
    month_stats = summarize(month)
    day_of_month = max(1, now.day)
    projected_month = float(month_stats["cost_usd"]) / day_of_month * 30
    reliable_today_seconds = sum(reliable_session_seconds(row, today_start) for row in today)
    if reliable_today_seconds:
        print(f"{compact_duration(reliable_today_seconds)} of measured AI work")
    else:
        print("Active work time unavailable from local logs")
    print(f"{int(today_stats['sessions'])} sessions | {token_summary_label(today)} | {money(float(today_stats['cost_usd']))} API-equivalent value")
    print(f"Projected month: ~{money(projected_month)} API-equivalent at current pace")
    print("Note: subscription plans may not bill this as incremental spend.\n")

    if by_tool:
        print("By tool")
        print(f"{'Tool':16} {'API value':>10} {'Calls':>7} {'Tokens':>9} {'Sessions':>8}")
        print("-" * 56)
        for tool, rows in sorted(by_tool.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True):
            stats = summarize(rows)
            print(
                f"{tool:16} "
                f"{money(float(stats['cost_usd'])):>10} "
                f"{int(stats['agent_calls']):>7} "
                f"{compact_int(int(stats['tokens_in']) + int(stats['tokens_out'])):>9} "
                f"{int(stats['sessions']):>8}"
            )

        by_model: dict[str, list[LocalSession]] = defaultdict(list)
        for session in today:
            by_model[session.model or "unknown"].append(session)
        print("\nBy model")
        print(f"{'Model':28} {'API value':>10} {'Tokens':>9} {'Calls':>7}")
        print("-" * 58)
        for model, rows in sorted(by_model.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True)[:8]:
            stats = summarize(rows)
            print(
                f"{model[:28]:28} "
                f"{money(float(stats['cost_usd'])):>10} "
                f"{compact_int(int(stats['tokens_in']) + int(stats['tokens_out'])):>9} "
                f"{int(stats['agent_calls']):>7}"
            )

        project = top_project(today)
        if project:
            label, spend, session_count = project
            share_base = float(today_stats["cost_usd"]) or float(today_stats["sessions"]) or 1
            share_value = spend if float(today_stats["cost_usd"]) else session_count
            share = round(share_value / share_base * 100)
            print(f"\nTop project: {short_path(label)} ({share}% of today's {'API-equivalent value' if float(today_stats['cost_usd']) else 'sessions'})")

        longest = longest_session(today, today_start)
        if longest:
            print(
                f"Longest session: {compact_duration(reliable_session_seconds(longest, today_start))} "
                f"in {short_path(longest.project_path)} "
                f"({longest.tool}, {money(longest.cost_usd)})"
            )
        else:
            print("Longest session: unavailable from reliable local timestamps")

        notes = sorted({note for row in today for note in row.notes if "limited" in note or "subscription" in note})
        for note in notes[:2]:
            print(f"Note: {note}")

    print(f"\nThis week: {money(float(week_stats['cost_usd']))}")
    print(f"This month: {money(float(month_stats['cost_usd']))}")
    if float(today_stats["cost_usd"]) > 0 or int(today_stats["sessions"]) >= 3:
        print_cloud_hint("See the same cost view for your whole team, with budget caps and anomaly alerts.")
    return 0


def command_tools(args: argparse.Namespace) -> int:
    sessions = sessions_since(args.days)
    by_tool: dict[str, list[LocalSession]] = defaultdict(list)
    for session in sessions:
        by_tool[session.tool].append(session)
    print(f"AI usage by tool - last {args.days} days")
    print("Cost is shown as API-equivalent value; subscription plans may differ.\n")
    for tool, rows in sorted(by_tool.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True):
        stats = summarize(rows)
        print(f"{tool:14} {stats['sessions']:>5} sessions  {compact_int(int(stats['tokens_in'])):>8} in  {compact_int(int(stats['tokens_out'])):>8} out  {money(float(stats['cost_usd'])):>10}")
    if args.days > 30:
        print_cloud_hint("Need retention beyond local history? Cloud keeps team history searchable.")
    return 0


def command_projects(args: argparse.Namespace) -> int:
    sessions = sessions_since(args.days)
    by_project: dict[str, list[LocalSession]] = defaultdict(list)
    for session in sessions:
        by_project[session.project_path or "unknown"].append(session)
    print(f"AI usage by project - last {args.days} days")
    print("Cost is shown as API-equivalent value; subscription plans may differ.\n")
    ranked = sorted(by_project.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True)
    for project, rows in ranked[: args.limit]:
        stats = summarize(rows)
        print(f"{money(float(stats['cost_usd'])):>10}  {stats['sessions']:>4} sessions  {compact_int(int(stats['tokens_in']) + int(stats['tokens_out'])):>8} tokens  {project}")
    if args.days > 30:
        print_cloud_hint("Need org-wide project attribution? Cloud maps spend by user, team, and repo.")
    return 0


def command_report(args: argparse.Namespace) -> int:
    days = args.days
    rows = sessions_since(days)
    try:
        get_or_refresh_baselines()
    except OSError:
        pass
    stats = summarize(rows)
    projects: dict[str, list[LocalSession]] = defaultdict(list)
    tools: dict[str, list[LocalSession]] = defaultdict(list)
    models: dict[str, list[LocalSession]] = defaultdict(list)
    for row in rows:
        projects[row.project_path or "unknown"].append(row)
        tools[row.tool].append(row)
        models[row.model or "unknown"].append(row)

    ranked_projects = sorted(projects.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True)
    ranked_tools = sorted(tools.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True)
    ranked_models = sorted(models.items(), key=lambda item: summarize(item[1])["cost_usd"], reverse=True)

    print(f"AIWatcher Local report - last {days} days\n")
    print(f"Sessions: {stats['sessions']}")
    print(f"API-equivalent value: {money(float(stats['cost_usd']))}")
    print(f"Tokens: {compact_int(int(stats['tokens_in']) + int(stats['tokens_out']))}")
    print(f"Model calls: {stats['agent_calls']}")
    print(f"Tool calls: {stats['tool_calls']}\n")

    if ranked_projects:
        project, project_rows = ranked_projects[0]
        project_stats = summarize(project_rows)
        print(f"Top project: {short_path(project)} ({money(float(project_stats['cost_usd']))})")
    if ranked_tools:
        tool, tool_rows = ranked_tools[0]
        tool_stats = summarize(tool_rows)
        print(f"Top tool: {tool} ({tool_stats['sessions']} sessions)")
    if ranked_models:
        model, model_rows = ranked_models[0]
        model_stats = summarize(model_rows)
        print(f"Top model: {model} ({compact_int(int(model_stats['tokens_in']) + int(model_stats['tokens_out']))} tokens)")

    print("\nSuggested next checks:")
    print("- Review top project sessions for runaway or abandoned work.")
    print("- Compare API-priced tokens with plan/limited tokens before interpreting invoice impact.")
    print("- Run `aiwatcher ui` for clickable local drill-down.")
    return 0


def command_sessions(args: argparse.Namespace) -> int:
    if args.team:
        print("Team session history is a Cloud feature.")
        print("Local OSS shows your machine only; Cloud adds shared visibility, retention, and policy controls.")
        print(CLOUD_URL)
        return 0

    sessions = sorted(sessions_since(args.days), key=session_sort_key, reverse=True)
    if args.search:
        needle = args.search.strip().lower()
        sessions = [
            row for row in sessions
            if needle in " ".join([
                row.session_id,
                row.tool,
                row.model or "",
                row.project_path or "",
            ]).lower()
        ]
    print(f"Recent AI sessions - last {args.days} days\n")
    for row in sessions[: args.limit]:
        stamp = row.updated_at or row.started_at
        when = format_short_datetime(stamp.astimezone()) if stamp else "unknown"
        print(f"{when:12} {row.tool:12} {money(row.cost_usd):>10} {compact_int(row.tokens_in + row.tokens_out):>8} tokens  {row.project_path or 'unknown'}")
    if args.days > 30:
        print_cloud_hint("Cloud adds retention, team filters, and scheduled exports for session history.")
    return 0


def _copy_to_clipboard(text: str) -> tuple[bool, str]:
    if sys.platform == "darwin":
        commands = [["pbcopy"]]
    elif os.name == "nt":
        commands = [["clip"]]
    else:
        commands = [["wl-copy"], ["xclip", "-selection", "clipboard"]]
    for command in commands:
        if not shutil.which(command[0]):
            continue
        try:
            subprocess.run(command, input=text, text=True, check=True, timeout=3)
            return True, command[0]
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return False, "no clipboard command found"


def command_last(args: argparse.Namespace) -> int:
    rows = sessions_since(args.days)
    if args.session_id:
        session = next((row for row in rows if row.session_id == args.session_id), None)
        if not session:
            print(f"No local session found for {args.session_id!r} in the last {args.days} days.", file=sys.stderr)
            return 2
    else:
        session = latest_session(rows)
        if not session:
            print(f"No local AI sessions detected in the last {args.days} days.")
            print("Run `aiwatcher start` to check which tools AIWatcher can read on this machine.")
            return 0

    print_session_detail(session)
    return 0


def command_timeline(args: argparse.Namespace) -> int:
    session_id = args.session_id
    if not session_id:
        session = latest_session(sessions_since(args.days))
        if not session:
            print(f"No local AI sessions detected in the last {args.days} days.")
            return 0
        session_id = session.session_id
    print(render_session_timeline(session_id, days=args.days, limit=args.limit))
    return 0


def command_handoff(args: argparse.Namespace) -> int:
    rows = sessions_since(args.days)
    if args.session_id:
        session = next((row for row in rows if row.session_id == args.session_id), None)
        if not session:
            print(f"No local session found for {args.session_id!r} in the last {args.days} days.", file=sys.stderr)
            return 2
    else:
        session = latest_session(rows)
        if not session:
            print(f"No local AI sessions detected in the last {args.days} days.")
            return 0

    outcome = get_outcome(session.session_id)
    capsule = build_handoff_capsule(
        session,
        events_for_session(session.session_id, days=args.days),
        outcome=outcome.get("outcome") if outcome else None,
        include_prompt_excerpt=args.include_prompt_excerpt,
        target=args.target,
    )
    if args.format == "json":
        rendered = json.dumps(capsule, indent=2)
    else:
        rendered = render_handoff_capsule(capsule)
    if args.copy:
        ok, detail = _copy_to_clipboard(str(capsule.get("next_brief") or rendered))
        if ok:
            print(f"Copied {capsule.get('target_label') or 'handoff'} brief to clipboard.\n")
        else:
            print(f"Could not copy to clipboard ({detail}); printing instead.\n", file=sys.stderr)
    print(rendered)
    return 0


def command_resume(args: argparse.Namespace) -> int:
    if not args.session_id and args.search:
        rows = sorted(sessions_since(args.days), key=session_sort_key, reverse=True)
        needle = args.search.strip().lower()
        matches = [
            row for row in rows
            if needle in " ".join([
                row.session_id,
                row.tool,
                row.model or "",
                row.project_path or "",
            ]).lower()
        ]
        if not matches:
            print(f"No local session matched {args.search!r} in the last {args.days} days.", file=sys.stderr)
            return 2
        args.session_id = matches[0].session_id
    return command_handoff(args)


def command_journal(args: argparse.Namespace) -> int:
    print(render_journal(days=args.days))
    return 0


def command_watch(args: argparse.Namespace) -> int:
    print("AIWatcher Local watch")
    print("Read-only local scan. No data leaves this machine. Press Ctrl+C to stop.\n")

    seen: dict[str, datetime] = {}
    try:
        while True:
            rows = sorted(sessions_since(args.days), key=session_sort_key, reverse=True)
            interesting: list[LocalSession] = []
            for row in rows:
                stamp = session_sort_key(row)
                if seen.get(row.session_id) == stamp:
                    continue
                seen[row.session_id] = stamp
                if session_insights(
                    row,
                    cost_threshold=args.cost_threshold,
                    calls_threshold=args.calls_threshold,
                    tokens_threshold=args.tokens_threshold,
                ):
                    interesting.append(row)

            if args.once:
                if not rows:
                    print(f"No local AI sessions detected in the last {args.days} days.")
                    return 0
                if not interesting:
                    latest = rows[0]
                    print_session_detail(latest, heading="Latest session, no urgent local signals")
                    return 0

            for row in interesting[:5]:
                stamp = session_sort_key(row)
                when = format_short_datetime(stamp.astimezone()) if stamp != MIN_DT else "unknown"
                print(f"[{when}] {row.tool} | {short_path(row.project_path)} | {money(row.cost_usd)} | {compact_int(row.tokens_in + row.tokens_out)} tokens")
                for insight in session_insights(
                    row,
                    cost_threshold=args.cost_threshold,
                    calls_threshold=args.calls_threshold,
                    tokens_threshold=args.tokens_threshold,
                )[:3]:
                    print(f"  - {insight}")
                print(f"  Next: aiwatcher resume --session-id {row.session_id} --target codex --copy")

            if args.once:
                return 0
            time_module.sleep(max(2, args.interval))
    except KeyboardInterrupt:
        print("\nStopped AIWatcher Local watch.")
        return 0


def command_run(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("Usage: aiwatcher run -- <command>", file=sys.stderr)
        return 2

    run_started = datetime.now().astimezone()
    print("AIWatcher Local run")
    print("Watching local AI logs while your command runs. No prompt or source content is uploaded.\n")
    print(f"$ {' '.join(command)}\n")
    sys.stdout.flush()

    try:
        completed = subprocess.run(command, check=False)
    except OSError as exc:
        print(f"Could not run command: {exc}", file=sys.stderr)
        return 2

    after_rows = scan_all()
    candidates = [
        row
        for row in after_rows
        if session_sort_key(row) >= run_started
    ]
    session = latest_session(candidates) or latest_session(after_rows)
    if session:
        if candidates:
            print()
            print_session_detail(session, heading="AIWatcher summary after run")
        else:
            print("\nNo new local AI session was detected after this command.")
    else:
        print("\nNo local AI session was detected after this command.")
    return int(completed.returncode)


def command_preflight(args: argparse.Namespace) -> int:
    prompt = args.text or " ".join(args.prompt).strip()
    if not prompt:
        print("Usage: aiwatcher preflight \"prompt text\"", file=sys.stderr)
        return 2
    result = analyze_prompt(prompt, tool=args.tool, cwd=args.cwd or os.getcwd())
    print(render_preflight(result))
    if result["risk"] == "high" and args.fail_on_high:
        return 3
    return 0


def _edit_prompt(prompt: str) -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        edited = input("Edited prompt: ").strip()
        return edited or prompt
    path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write(prompt)
            handle.write("\n")
            path = handle.name
        completed = subprocess.run([*shlex.split(editor), path], check=False)
        if completed.returncode != 0:
            return prompt
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip() or prompt
    except OSError:
        return prompt
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _choose_preflight_prompt(
    prompt: str,
    result: dict[str, object],
    *,
    apply_suggestion: bool = False,
    run_original: bool = False,
    interactive: bool | None = None,
) -> tuple[str | None, str]:
    if apply_suggestion:
        return str(result["suggested_prompt"]), "suggested"
    if run_original or result["risk"] == "low":
        return prompt, "original"

    can_prompt = sys.stdin.isatty() if interactive is None else interactive
    if not can_prompt:
        if result["risk"] == "high":
            return None, "blocked"
        return prompt, "original"

    print()
    print("[U] Use safer prompt  [R] Run original  [E] Edit safer prompt  [C] Cancel")
    while True:
        choice = input("Choose: ").strip().lower()
        if choice in {"u", "use", "suggested"}:
            return str(result["suggested_prompt"]), "suggested"
        if choice in {"r", "run", "original"}:
            return prompt, "original"
        if choice in {"e", "edit"}:
            return _edit_prompt(str(result["suggested_prompt"])), "edited"
        if choice in {"c", "cancel", "q", "quit"}:
            return None, "cancelled"
        print("Choose U, R, E, or C.")


def command_agent_prompt(args: argparse.Namespace) -> int:
    prompt = args.text or " ".join(args.prompt).strip()
    if not prompt:
        print(f"Usage: aiwatcher {args.agent} \"prompt text\"", file=sys.stderr)
        return 2

    cwd = args.cwd or os.getcwd()
    result = analyze_prompt(prompt, tool=args.agent, cwd=cwd)
    if result["risk"] != "low" or args.dry_run:
        print(render_preflight(result))
    risk = str(result["risk"])
    selected_prompt, decision = _choose_preflight_prompt(
        prompt,
        result,
        apply_suggestion=args.apply_suggestion,
        run_original=args.yes,
        interactive=False if args.dry_run else None,
    )

    if args.dry_run:
        print()
        if selected_prompt is None:
            print(f"Dry run: would block `{args.agent}` because prompt risk is high.")
        else:
            print(f"Dry run: would launch `{args.agent}` with {decision} prompt.")
        return 0
    intervention_id = None
    try:
        selected_risk, selected_score = _selected_prompt_assessment(
            selected_prompt,
            original_prompt=prompt,
            original_result=result,
            tool=args.agent,
            cwd=cwd,
        )
        intervention_id = record_intervention(
            tool=args.agent,
            cwd=cwd,
            risk=risk,
            score=int(result["score"]),
            findings=[str(item) for item in result["findings"]],
            original_prompt=prompt,
            suggested_prompt=str(result["suggested_prompt"]),
            decision=decision,
            selected_prompt=selected_prompt,
            estimated_impact=(
                result["estimated_impact"]
                if isinstance(result.get("estimated_impact"), dict)
                else None
            ),
            selected_risk=selected_risk,
            selected_score=selected_score,
        )
    except OSError as exc:
        print(f"Warning: could not record AIWatcher preflight decision: {exc}", file=sys.stderr)
    if selected_prompt is None:
        print()
        if decision == "cancelled":
            print("Cancelled. No prompt was sent.")
            return 0
        print(f"Blocked local {args.agent} launch because prompt risk is high and no interactive confirmation was available.")
        print(f"Use `--yes` to run the original prompt or `--apply-suggestion` to run the safer prompt.")
        return 3

    run_started = datetime.now().astimezone()
    binary = args.binary or args.agent
    command = [binary, selected_prompt]
    print()
    print(f"Launching {args.agent} with {decision} prompt.")
    sys.stdout.flush()
    try:
        completed = subprocess.run(command, check=False)
    except OSError as exc:
        print(f"Could not run {args.agent}: {exc}", file=sys.stderr)
        return 2

    candidates = [row for row in scan_all() if session_sort_key(row) >= run_started]
    session = latest_session(candidates)
    if session:
        if intervention_id:
            try:
                link_intervention_session(intervention_id, session.session_id)
            except OSError as exc:
                print(f"Warning: could not link AIWatcher decision to the session: {exc}", file=sys.stderr)
        print()
        print_session_detail(session, heading=f"AIWatcher summary after {args.agent}")
    else:
        print(f"\nNo new local {args.agent} session was detected after this command.")
    return int(completed.returncode)


def command_outcome(args: argparse.Namespace) -> int:
    session_id = args.session_id
    session = None
    if not session_id:
        session = latest_session(sessions_since(args.days))
        if not session:
            print(f"No local AI sessions detected in the last {args.days} days.", file=sys.stderr)
            return 2
        session_id = session.session_id
    else:
        session = next((row for row in sessions_since(args.days) if row.session_id == session_id), None)
    try:
        record = record_outcome(session_id, args.outcome, args.note)
        if session:
            record_evidence_snapshot(session_id, build_outcome_evidence(session).to_json())
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Marked session {session_id} as {record['outcome']}.")
    print("Stored locally; no prompt or source content was recorded.")
    return 0


def command_log_decision(args: argparse.Namespace) -> int:
    session_id = args.session_id
    if not session_id:
        session = latest_session(sessions_since(args.days))
        if not session:
            print(f"No local AI sessions detected in the last {args.days} days.", file=sys.stderr)
            return 2
        session_id = session.session_id
    try:
        record = record_decision(
            session_id,
            args.summary,
            reasoning=args.reasoning,
            alternatives_rejected=args.alternatives_rejected,
        )
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Logged decision for session {session_id}: {record['summary']}")
    print("Stored locally; self-reported, not verified against what actually happened.")
    return 0


def _read_stdin_text() -> str:
    # Hook payloads are always written as UTF-8. Text-mode sys.stdin decodes
    # using the platform's default encoding (the Windows console codepage,
    # e.g. cp1252), which mangles em dashes, smart quotes, and other
    # multi-byte characters into mojibake. Decode the raw bytes explicitly.
    try:
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _extract_prompt_from_hook(payload: dict[str, object]) -> str:
    for key in ("prompt", "user_prompt", "message", "input"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    nested = payload.get("hook_event")
    if isinstance(nested, dict):
        for key in ("prompt", "user_prompt", "message"):
            value = nested.get(key)
            if isinstance(value, str):
                return value
    return ""


def _extract_session_meta(payload: dict[str, object]) -> dict[str, str | None]:
    """Pull only session_id/transcript_path from a hook payload.

    Deliberately narrow: reads exactly two keys, only accepts them as plain
    strings, and returns nothing else from the payload. This is the one
    place allowed to look at hook payload shape beyond the prompt text
    itself -- it must never grow into a general payload passthrough.
    """
    def _string(source: dict[str, object], key: str) -> str | None:
        value = source.get(key)
        return value if isinstance(value, str) and value else None

    session_id = _string(payload, "session_id")
    transcript_path = _string(payload, "transcript_path")
    nested = payload.get("hook_event")
    if isinstance(nested, dict):
        session_id = session_id or _string(nested, "session_id")
        transcript_path = transcript_path or _string(nested, "transcript_path")
    return {"session_id": session_id, "transcript_path": transcript_path}


def _log_hook_payload_keys(payload: dict[str, object]) -> None:
    # Keys only, never values -- values may contain prompt/session content.
    if os.environ.get("AIWATCHER_DEBUG_HOOK_KEYS", "").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"AIWatcher debug: hook payload keys = {sorted(payload.keys())}", file=sys.stderr)


def _record_hook_intervention(
    *,
    tool: str,
    cwd: str,
    prompt: str,
    result: dict[str, object],
    decision: str,
    selected_prompt: str | None = None,
    session_id: str | None = None,
) -> None:
    try:
        effective_prompt = (
            selected_prompt or str(result["suggested_prompt"])
            if decision in {"context_added", "brief_accepted", "brief_edited", "auto_brief_headless"}
            else prompt if decision == "allowed_original" else None
        )
        selected_risk, selected_score = _selected_prompt_assessment(
            effective_prompt,
            original_prompt=prompt,
            original_result=result,
            tool=tool,
            cwd=cwd,
        )
        record_intervention(
            tool=tool,
            cwd=cwd,
            risk=str(result["risk"]),
            score=int(result["score"]),
            findings=[str(item) for item in result["findings"]],
            original_prompt=prompt,
            suggested_prompt=str(result["suggested_prompt"]),
            decision=decision,
            selected_prompt=effective_prompt,
            estimated_impact=(
                result["estimated_impact"]
                if isinstance(result.get("estimated_impact"), dict)
                else None
            ),
            selected_risk=selected_risk,
            selected_score=selected_score,
            session_id=session_id,
        )
    except OSError:
        pass


def _record_hook_event(
    *,
    tool: str,
    cwd: str,
    event: str,
    prompt_found: bool,
    result: dict[str, object] | None = None,
    error: str | None = None,
    session_id: str | None = None,
) -> None:
    try:
        record_hook_event(
            tool=tool,
            cwd=cwd,
            event=event,
            prompt_found=prompt_found,
            risk=str(result.get("risk")) if result else None,
            score=int(result.get("score", 0)) if result else None,
            error=error,
            session_id=session_id,
        )
    except OSError:
        pass


def _hook_output_with_brief(tool: str, selected_prompt: str) -> dict[str, object]:
    return {
        "systemMessage": "AIWatcher added a scoped execution brief alongside the submitted request.",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "AIWatcher identified avoidable cost or safety pressure. "
                "Treat the following execution brief as controlling guidance for how to execute "
                "the user's submitted request while preserving its intended outcome:\n\n"
                + selected_prompt
            ),
        },
    }


def _hook_block_output(reason: str, *, tool: str) -> dict[str, object]:
    return {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": f"AIWatcher paused this high-risk {tool} prompt before execution.",
        },
    }


def _prompt_gate_requested(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "gate", False)
        or os.environ.get("AIWATCHER_PROMPT_GATE", "").strip().lower() in {"1", "true", "yes", "on"}
    )


def _command_prompt_hook(args: argparse.Namespace, *, tool: str) -> int:
    raw = _read_stdin_text()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    _log_hook_payload_keys(payload)
    prompt = args.text or _extract_prompt_from_hook(payload)
    cwd = str(payload.get("cwd") or payload.get("workspace") or os.getcwd())
    session_id = _extract_session_meta(payload)["session_id"]
    result = analyze_prompt(prompt, tool=tool, cwd=cwd) if prompt else {
        "risk": "low",
        "score": 0,
        "tool": tool,
        "findings": ["No prompt text found in hook payload."],
        "suggestions": ["Allowing prompt because AIWatcher could not inspect it."],
        "suggested_prompt": "",
        "estimated_impact": {},
    }
    _record_hook_event(
        tool=tool,
        cwd=cwd,
        event="received",
        prompt_found=bool(prompt),
        result=result,
        session_id=session_id,
    )
    if result["risk"] == "low":
        print("{}")
        return 0

    rendered = render_preflight(result)
    # Prompt Gate is intentionally interactive for both medium and high risk
    # prompts when the hook was installed with --gate. Low risk still passes
    # unchanged above.
    if _prompt_gate_requested(args) and result["risk"] in {"medium", "high"}:
        gate = None
        try:
            gate = run_prompt_gate(tool=tool, cwd=cwd, prompt=prompt, result=result)
        except OSError as exc:
            _record_hook_event(
                tool=tool,
                cwd=cwd,
                event="gate_failed",
                prompt_found=bool(prompt),
                result=result,
                error=str(exc),
                session_id=session_id,
            )
        if gate:
            decision = gate.get("decision")
            selected_prompt = gate.get("prompt") or str(result["suggested_prompt"])
            if decision in {"use_brief", "edit"}:
                _record_hook_intervention(
                    tool=tool,
                    cwd=cwd,
                    prompt=prompt,
                    result=result,
                    decision="brief_edited" if decision == "edit" else "brief_accepted",
                    selected_prompt=selected_prompt,
                    session_id=session_id,
                )
                print(json.dumps(_hook_output_with_brief(tool, selected_prompt)))
                return 0
            if decision == "run_original":
                _record_hook_intervention(
                    tool=tool,
                    cwd=cwd,
                    prompt=prompt,
                    result=result,
                    decision="allowed_original",
                    session_id=session_id,
                )
                print("{}")
                return 0
            if decision == "cancel":
                _record_hook_intervention(
                    tool=tool,
                    cwd=cwd,
                    prompt=prompt,
                    result=result,
                    decision="cancelled",
                    session_id=session_id,
                )
                print(json.dumps(_hook_block_output("AIWatcher Prompt Gate cancelled this run.", tool=tool)))
                return 0
            if decision == "auto_brief_headless":
                _record_hook_intervention(
                    tool=tool,
                    cwd=cwd,
                    prompt=prompt,
                    result=result,
                    decision="auto_brief_headless",
                    selected_prompt=selected_prompt,
                    session_id=session_id,
                )
                print(json.dumps(_hook_output_with_brief(tool, selected_prompt)))
                return 0
            if decision == "auto_block_headless":
                _record_hook_intervention(
                    tool=tool,
                    cwd=cwd,
                    prompt=prompt,
                    result=result,
                    decision="auto_block_headless",
                    session_id=session_id,
                )
                print(json.dumps(_hook_block_output(
                    f"AIWatcher blocked this {result['risk']}-risk prompt automatically: no interactive display or "
                    "terminal was available to review it. Set AIWATCHER_GATE_DEFAULT=brief to add a safer "
                    "brief automatically instead of blocking.",
                    tool=tool,
                )))
                return 0
        # If the browser gate times out, fall back to the deterministic hook policy below.

    if result["risk"] == "high":
        _record_hook_intervention(
            tool=tool,
            cwd=cwd,
            prompt=prompt,
            result=result,
            decision="blocked",
            session_id=session_id,
        )
        print(json.dumps(_hook_block_output(rendered, tool=tool)))
        return 0


    _record_hook_intervention(
        tool=tool,
        cwd=cwd,
        prompt=prompt,
        result=result,
        decision="context_added",
        session_id=session_id,
    )
    print(json.dumps(_hook_output_with_brief(tool, str(result["suggested_prompt"]))))
    return 0


def command_claude_hook(args: argparse.Namespace) -> int:
    return _command_prompt_hook(args, tool="claude")


def command_codex_hook(args: argparse.Namespace) -> int:
    return _command_prompt_hook(args, tool="codex")


def _cursor_hook_response(*, allow: bool, message: str | None = None) -> dict[str, object]:
    response: dict[str, object] = {"continue": allow}
    if message:
        response["user_message"] = message
        response["agent_message"] = message
    return response


def command_cursor_hook(args: argparse.Namespace) -> int:
    """Handle Cursor's beforeSubmitPrompt event.

    Cursor can allow or block a submitted prompt, but cannot replace its text
    or inject additional context. Risky prompts are paused with a scoped brief
    that the developer can review and resubmit.
    """
    raw = _read_stdin_text()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    _log_hook_payload_keys(payload)
    prompt = args.text or _extract_prompt_from_hook(payload)
    workspace_roots = payload.get("workspace_roots")
    workspace = workspace_roots[0] if isinstance(workspace_roots, list) and workspace_roots else None
    cwd = str(payload.get("cwd") or payload.get("workspace") or workspace or os.getcwd())
    session_id = _extract_session_meta(payload)["session_id"]
    result = analyze_prompt(prompt, tool="cursor", cwd=cwd) if prompt else {
        "risk": "low",
        "score": 0,
        "tool": "cursor",
        "findings": ["No prompt text found in hook payload."],
        "suggestions": ["Allowing prompt because AIWatcher could not inspect it."],
        "suggested_prompt": "",
        "estimated_impact": {},
    }
    _record_hook_event(
        tool="cursor", cwd=cwd, event="received", prompt_found=bool(prompt), result=result, session_id=session_id
    )
    if result["risk"] == "low":
        print(json.dumps(_cursor_hook_response(allow=True)))
        return 0

    # Prompt Gate is intentionally interactive for both medium and high risk
    # prompts when the Cursor hook was installed with --gate. Cursor still cannot
    # rewrite prompt text in place, so brief decisions return a resubmission note.
    if _prompt_gate_requested(args) and result["risk"] in {"medium", "high"}:
        try:
            gate = run_prompt_gate(tool="cursor", cwd=cwd, prompt=prompt, result=result)
        except OSError as exc:
            gate = None
            _record_hook_event(
                tool="cursor", cwd=cwd, event="gate_failed", prompt_found=bool(prompt), result=result,
                error=str(exc), session_id=session_id,
            )
        if gate:
            decision = gate.get("decision")
            selected_prompt = str(gate.get("prompt") or result["suggested_prompt"])
            if decision == "run_original":
                _record_hook_intervention(
                    tool="cursor", cwd=cwd, prompt=prompt, result=result, decision="allowed_original",
                    session_id=session_id,
                )
                print(json.dumps(_cursor_hook_response(allow=True)))
                return 0
            if decision in {"use_brief", "edit"}:
                _record_hook_intervention(
                    tool="cursor",
                    cwd=cwd,
                    prompt=prompt,
                    result=result,
                    decision="brief_edited" if decision == "edit" else "brief_accepted",
                    selected_prompt=selected_prompt,
                    session_id=session_id,
                )
                message = (
                    "AIWatcher paused the original prompt. Cursor hooks cannot replace prompt text. "
                    "Resubmit this scoped execution brief:\n\n" + selected_prompt
                )
                print(json.dumps(_cursor_hook_response(allow=False, message=message)))
                return 0
            if decision == "cancel":
                _record_hook_intervention(
                    tool="cursor", cwd=cwd, prompt=prompt, result=result, decision="cancelled",
                    session_id=session_id,
                )
                print(json.dumps(_cursor_hook_response(
                    allow=False, message="AIWatcher cancelled this prompt before execution."
                )))
                return 0
            if decision == "auto_brief_headless":
                _record_hook_intervention(
                    tool="cursor", cwd=cwd, prompt=prompt, result=result, decision="auto_brief_headless",
                    selected_prompt=selected_prompt, session_id=session_id,
                )
                message = (
                    "AIWatcher paused the original prompt automatically (no interactive display or terminal "
                    "was available to review it). Resubmit this scoped execution brief:\n\n" + selected_prompt
                )
                print(json.dumps(_cursor_hook_response(allow=False, message=message)))
                return 0
            if decision == "auto_block_headless":
                _record_hook_intervention(
                    tool="cursor", cwd=cwd, prompt=prompt, result=result, decision="auto_block_headless",
                    session_id=session_id,
                )
                print(json.dumps(_cursor_hook_response(
                    allow=False,
                    message=(
                        f"AIWatcher blocked this {result['risk']}-risk prompt automatically: no interactive display or "
                        "terminal was available to review it."
                    ),
                )))
                return 0

    _record_hook_intervention(
        tool="cursor", cwd=cwd, prompt=prompt, result=result, decision="blocked", session_id=session_id
    )
    message = (
        f"AIWatcher paused this {result['risk']}-risk prompt (score {result['score']}). "
        "Cursor hooks cannot rewrite submitted text. Review and resubmit this scoped execution brief:\n\n"
        + str(result["suggested_prompt"])
    )
    print(json.dumps(_cursor_hook_response(allow=False, message=message)))
    return 0


def _cli_command_for_current_file() -> str:
    executable = sys.executable
    if os.name == "nt":
        # Claude/Codex/Cursor invoke hook commands through a POSIX shell (Git
        # Bash) even on Windows, where backslash is an escape character. An
        # unquoted Windows path like C:\Users\... gets mangled to C:Users...,
        # so normalize to forward slashes, which Windows accepts too.
        executable = executable.replace("\\", "/")
    parts = [executable, "-m", "aiwatcher_cli"]
    return " ".join(shlex.quote(part) for part in parts)


def _hook_command(command: str, transport: str, *, gate: bool = False) -> str:
    suffix = " --gate" if gate else ""
    return f"{command} {transport}{suffix}"


def _merge_claude_hook(settings: dict[str, object], command: str, *, gate: bool = False) -> dict[str, object]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    event_hooks = hooks.get("UserPromptSubmit")
    if not isinstance(event_hooks, list):
        event_hooks = []
    command_hook: dict[str, object] = {
        "type": "command",
        "command": _hook_command(command, "claude-hook", gate=gate),
        "statusMessage": "AIWatcher is checking execution pressure",
    }
    if gate:
        command_hook["timeout"] = PROMPT_GATE_HOST_TIMEOUT_SECONDS
    handler = {"hooks": [command_hook]}
    event_hooks = [
        item for item in event_hooks
        if not (
            isinstance(item, dict)
            and any(
                isinstance(hook, dict) and "claude-hook" in str(hook.get("command", ""))
                for hook in item.get("hooks", [])
            )
        )
    ]
    event_hooks.append(handler)
    hooks["UserPromptSubmit"] = event_hooks
    settings["hooks"] = hooks
    return settings


def _remove_claude_hook(settings: dict[str, object]) -> tuple[dict[str, object], bool]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings, False
    event_hooks = hooks.get("UserPromptSubmit")
    if not isinstance(event_hooks, list):
        return settings, False
    filtered = [
        item for item in event_hooks
        if not (
            isinstance(item, dict)
            and any(
                isinstance(hook, dict) and "claude-hook" in str(hook.get("command", ""))
                for hook in item.get("hooks", [])
            )
        )
    ]
    if len(filtered) == len(event_hooks):
        return settings, False
    if filtered:
        hooks["UserPromptSubmit"] = filtered
    else:
        hooks.pop("UserPromptSubmit", None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings, True


def _merge_codex_hook(settings: dict[str, object], command: str, *, gate: bool = False) -> dict[str, object]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    event_hooks = hooks.get("UserPromptSubmit")
    if not isinstance(event_hooks, list):
        event_hooks = []
    event_hooks = [
        item for item in event_hooks
        if not (
            isinstance(item, dict)
            and any(
                isinstance(hook, dict) and "codex-hook" in str(hook.get("command", ""))
                for hook in item.get("hooks", [])
            )
        )
    ]
    codex_command_hook: dict[str, object] = {
        "type": "command",
        "command": _hook_command(command, "codex-hook", gate=gate),
        "statusMessage": "AIWatcher is checking execution pressure",
    }
    if gate:
        codex_command_hook["timeout"] = PROMPT_GATE_HOST_TIMEOUT_SECONDS
    event_hooks.append({"hooks": [codex_command_hook]})
    hooks["UserPromptSubmit"] = event_hooks
    settings["hooks"] = hooks
    return settings


def _remove_codex_hook(settings: dict[str, object]) -> tuple[dict[str, object], bool]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings, False
    event_hooks = hooks.get("UserPromptSubmit")
    if not isinstance(event_hooks, list):
        return settings, False
    filtered = [
        item for item in event_hooks
        if not (
            isinstance(item, dict)
            and any(
                isinstance(hook, dict) and "codex-hook" in str(hook.get("command", ""))
                for hook in item.get("hooks", [])
            )
        )
    ]
    if len(filtered) == len(event_hooks):
        return settings, False
    if filtered:
        hooks["UserPromptSubmit"] = filtered
    else:
        hooks.pop("UserPromptSubmit", None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings, True


def _claude_settings_path(scope: str, project_dir: str | None = None) -> str:
    if scope == "project":
        return os.path.abspath(os.path.join(project_dir or os.getcwd(), ".claude", "settings.local.json"))
    return os.path.expanduser("~/.claude/settings.json")


def _codex_hooks_path(scope: str, project_dir: str | None = None) -> str:
    if scope == "project":
        return os.path.abspath(os.path.join(project_dir or os.getcwd(), ".codex", "hooks.json"))
    return os.path.expanduser("~/.codex/hooks.json")


def _cursor_hooks_path(scope: str, project_dir: str | None = None) -> str:
    if scope == "project":
        return os.path.abspath(os.path.join(project_dir or os.getcwd(), ".cursor", "hooks.json"))
    return os.path.expanduser("~/.cursor/hooks.json")


def _merge_cursor_hook(settings: dict[str, object], command: str, *, gate: bool = False) -> dict[str, object]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    event_hooks = hooks.get("beforeSubmitPrompt")
    if not isinstance(event_hooks, list):
        event_hooks = []
    event_hooks = [
        item for item in event_hooks
        if not (isinstance(item, dict) and "cursor-hook" in str(item.get("command", "")))
    ]
    event_hooks.append({
        "command": _hook_command(command, "cursor-hook", gate=gate),
        "failClosed": False,
    })
    hooks["beforeSubmitPrompt"] = event_hooks
    settings["version"] = 1
    settings["hooks"] = hooks
    return settings


def _remove_cursor_hook(settings: dict[str, object]) -> tuple[dict[str, object], bool]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings, False
    event_hooks = hooks.get("beforeSubmitPrompt")
    if not isinstance(event_hooks, list):
        return settings, False
    filtered = [
        item for item in event_hooks
        if not (isinstance(item, dict) and "cursor-hook" in str(item.get("command", "")))
    ]
    if len(filtered) == len(event_hooks):
        return settings, False
    if filtered:
        hooks["beforeSubmitPrompt"] = filtered
    else:
        hooks.pop("beforeSubmitPrompt", None)
    settings["hooks"] = hooks
    return settings, True


def command_install_claude_hook(args: argparse.Namespace) -> int:
    command = args.command or _cli_command_for_current_file()
    command_hook: dict[str, object] = {
        "type": "command",
        "command": _hook_command(command, "claude-hook", gate=args.gate),
        "statusMessage": "AIWatcher is checking execution pressure",
    }
    if args.gate:
        command_hook["timeout"] = PROMPT_GATE_HOST_TIMEOUT_SECONDS
    snippet = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [command_hook]
                }
            ]
        }
    }
    if not args.write:
        print("Add this to your Claude Code settings JSON:")
        print(json.dumps(snippet, indent=2))
        print("\nProject-local path: .claude/settings.local.json")
        print("User-global path: ~/.claude/settings.json")
        return 0

    settings_path = _claude_settings_path(args.scope, args.project_dir)
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    existing: dict[str, object] = {}
    if os.path.exists(settings_path):
        with open(settings_path, "r", encoding="utf-8") as handle:
            try:
                existing = json.load(handle)
            except json.JSONDecodeError:
                backup = settings_path + ".aiwatcher.bak"
                shutil.copyfile(settings_path, backup)
                print(f"Existing settings were not valid JSON. Backed up to {backup}.")
    merged = _merge_claude_hook(existing, command, gate=args.gate)
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2)
        handle.write("\n")
    print(f"Installed AIWatcher Claude Code UserPromptSubmit hook at {settings_path}")
    return 0


def command_uninstall_claude_hook(args: argparse.Namespace) -> int:
    settings_path = _claude_settings_path(args.scope, args.project_dir)
    if not os.path.exists(settings_path):
        print(f"No Claude settings file found at {settings_path}.")
        return 0
    try:
        with open(settings_path, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read Claude settings at {settings_path}: {exc}", file=sys.stderr)
        return 2
    updated, removed = _remove_claude_hook(settings)
    if not removed:
        print(f"No AIWatcher Claude hook found in {settings_path}.")
        return 0
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump(updated, handle, indent=2)
        handle.write("\n")
    print(f"Removed AIWatcher Claude hook from {settings_path}")
    return 0


DECISION_LOG_MARKER_START = "<!-- aiwatcher:decision-log:start -->"
DECISION_LOG_MARKER_END = "<!-- aiwatcher:decision-log:end -->"


def _claude_user_memory_path() -> str:
    # Deliberately global/personal only (~/.claude/CLAUDE.md), never a
    # project-local CLAUDE.md -- that file is typically committed and shared
    # with every collaborator on the repo, so writing to it would change how
    # the whole team's AI behaves, not just the person running this command.
    return os.path.expanduser("~/.claude/CLAUDE.md")


def _decision_log_convention_block() -> str:
    return "\n".join([
        DECISION_LOG_MARKER_START,
        "## AIWatcher decision log",
        "",
        "When you seriously consider and reject a real alternative during a coding",
        "session -- an approach, a library, a design -- and that rejection doesn't",
        "end up reflected in a commit message, log it:",
        "",
        "```",
        'aiwatcher log-decision "<one-line summary>" --reasoning "<why>" --rejected "<alternative>"',
        "```",
        "",
        "Use `--rejected` more than once if there were multiple alternatives. Only",
        "log real decision points, not routine implementation choices -- if it",
        "wouldn't be worth explaining to a teammate picking up the work fresh, skip",
        "it.",
        DECISION_LOG_MARKER_END,
    ])


def _merge_decision_log_convention(existing: str) -> tuple[str, bool]:
    if DECISION_LOG_MARKER_START in existing:
        return existing, False
    block = _decision_log_convention_block()
    if not existing.strip():
        return block + "\n", True
    return existing.rstrip("\n") + "\n\n" + block + "\n", True


def _remove_decision_log_convention(existing: str) -> tuple[str, bool]:
    start = existing.find(DECISION_LOG_MARKER_START)
    if start == -1:
        return existing, False
    end = existing.find(DECISION_LOG_MARKER_END)
    if end == -1:
        return existing, False
    end += len(DECISION_LOG_MARKER_END)
    before = existing[:start].rstrip("\n")
    after = existing[end:].lstrip("\n")
    if before and after:
        updated = before + "\n\n" + after
    else:
        updated = (before + after).strip("\n")
    if updated:
        updated += "\n"
    return updated, True


def command_install_claude_decision_log(args: argparse.Namespace) -> int:
    path = _claude_user_memory_path()
    if not args.write:
        print("Add this to your personal Claude Code memory (never a project-shared file):")
        print(_decision_log_convention_block())
        print(f"\nUser-global path: {path}")
        print("Re-run with --write to install it there directly.")
        return 0

    existing = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            existing = handle.read()
    updated, changed = _merge_decision_log_convention(existing)
    if not changed:
        print(f"AIWatcher decision-log convention is already installed at {path}.")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(updated)
    print(f"Installed AIWatcher decision-log convention at {path}")
    print("This is personal to this machine -- it is not committed, and does not affect other collaborators.")
    return 0


def command_uninstall_claude_decision_log(args: argparse.Namespace) -> int:
    path = _claude_user_memory_path()
    if not os.path.exists(path):
        print(f"No personal Claude memory file found at {path}.")
        return 0
    with open(path, "r", encoding="utf-8") as handle:
        existing = handle.read()
    updated, removed = _remove_decision_log_convention(existing)
    if not removed:
        print(f"No AIWatcher decision-log convention found in {path}.")
        return 0
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(updated)
    print(f"Removed AIWatcher decision-log convention from {path}")
    return 0


def command_install_codex_hook(args: argparse.Namespace) -> int:
    command = args.command or _cli_command_for_current_file()
    snippet = _merge_codex_hook({}, command, gate=args.gate)
    if not args.write:
        print("Add this to your Codex hooks JSON:")
        print(json.dumps(snippet, indent=2))
        print("\nUser-global path: ~/.codex/hooks.json")
        print("Project-local path: .codex/hooks.json")
        print("After installation, review and trust the hook with `/hooks` in Codex.")
        return 0

    hooks_path = _codex_hooks_path(args.scope, args.project_dir)
    os.makedirs(os.path.dirname(hooks_path), exist_ok=True)
    existing: dict[str, object] = {}
    if os.path.exists(hooks_path):
        try:
            with open(hooks_path, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except json.JSONDecodeError:
            backup = hooks_path + ".aiwatcher.bak"
            shutil.copyfile(hooks_path, backup)
            print(f"Existing hooks were not valid JSON. Backed up to {backup}.")
    merged = _merge_codex_hook(existing, command, gate=args.gate)
    with open(hooks_path, "w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2)
        handle.write("\n")
    print(f"Installed AIWatcher Codex UserPromptSubmit hook at {hooks_path}")
    print("Open Codex and run `/hooks` to review and trust it.")
    return 0


def command_uninstall_codex_hook(args: argparse.Namespace) -> int:
    hooks_path = _codex_hooks_path(args.scope, args.project_dir)
    if not os.path.exists(hooks_path):
        print(f"No Codex hooks file found at {hooks_path}.")
        return 0
    try:
        with open(hooks_path, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read Codex hooks at {hooks_path}: {exc}", file=sys.stderr)
        return 2
    updated, removed = _remove_codex_hook(settings)
    if not removed:
        print(f"No AIWatcher Codex hook found in {hooks_path}.")
        return 0
    with open(hooks_path, "w", encoding="utf-8") as handle:
        json.dump(updated, handle, indent=2)
        handle.write("\n")
    print(f"Removed AIWatcher Codex hook from {hooks_path}")
    return 0


def command_install_cursor_hook(args: argparse.Namespace) -> int:
    command = args.command or _cli_command_for_current_file()
    snippet = _merge_cursor_hook({}, command, gate=args.gate)
    if not args.write:
        print("Add this to your Cursor hooks JSON:")
        print(json.dumps(snippet, indent=2))
        print("\nUser-global path: ~/.cursor/hooks.json")
        print("Project-local path: .cursor/hooks.json")
        print("Reload the Cursor window, then inspect Output > Hooks after submitting a prompt.")
        return 0

    hooks_path = _cursor_hooks_path(args.scope, args.project_dir)
    os.makedirs(os.path.dirname(hooks_path), exist_ok=True)
    existing: dict[str, object] = {}
    if os.path.exists(hooks_path):
        try:
            with open(hooks_path, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except json.JSONDecodeError:
            backup = hooks_path + ".aiwatcher.bak"
            shutil.copyfile(hooks_path, backup)
            print(f"Existing hooks were not valid JSON. Backed up to {backup}.")
    merged = _merge_cursor_hook(existing, command, gate=args.gate)
    with open(hooks_path, "w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2)
        handle.write("\n")
    print(f"Installed AIWatcher Cursor beforeSubmitPrompt hook at {hooks_path}")
    print("Reload the Cursor window, submit a prompt, then run `aiwatcher hook-status`.")
    return 0


def command_uninstall_cursor_hook(args: argparse.Namespace) -> int:
    hooks_path = _cursor_hooks_path(args.scope, args.project_dir)
    if not os.path.exists(hooks_path):
        print(f"No Cursor hooks file found at {hooks_path}.")
        return 0
    try:
        with open(hooks_path, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read Cursor hooks at {hooks_path}: {exc}", file=sys.stderr)
        return 2
    updated, removed = _remove_cursor_hook(settings)
    if not removed:
        print(f"No AIWatcher Cursor hook found in {hooks_path}.")
        return 0
    with open(hooks_path, "w", encoding="utf-8") as handle:
        json.dump(updated, handle, indent=2)
        handle.write("\n")
    print(f"Removed AIWatcher Cursor hook from {hooks_path}")
    return 0


def _detect_binary(name: str, fallback: str | None = None) -> str:
    found = shutil.which(name)
    if found:
        return found
    if fallback and os.path.exists(fallback):
        return fallback
    return name


def command_install_codex_wrapper(args: argparse.Namespace) -> int:
    codex_binary = args.codex_binary or _detect_binary("codex", "/Applications/Codex.app/Contents/Resources/codex")
    aiwatcher_command = args.command or _cli_command_for_current_file()
    function_name = args.function_name
    snippet = f'''# AIWatcher Codex preflight wrapper
{function_name}() {{
  local real_codex="{codex_binary}"
  if [ "$#" -eq 0 ]; then
    "$real_codex"
    return $?
  fi
  case "$1" in
    mcp|login|logout|doctor|features|plugin|app|completion|update|help|--help|-h|--version|-V)
      "$real_codex" "$@"
      ;;
    *)
      {aiwatcher_command} codex --binary "$real_codex" "$@"
      ;;
  esac
}}
'''
    if not args.write:
        print("Add this shell function to ~/.zshrc:")
        print(snippet)
        return 0
    shell_rc = os.path.expanduser(args.shell_rc or "~/.zshrc")
    block = f"{CODEX_WRAPPER_MARKER_START}\n{snippet}{CODEX_WRAPPER_MARKER_END}\n"
    existing = ""
    if os.path.exists(shell_rc):
        with open(shell_rc, "r", encoding="utf-8") as handle:
            existing = handle.read()
    if CODEX_WRAPPER_MARKER_START in existing and CODEX_WRAPPER_MARKER_END in existing:
        before, rest = existing.split(CODEX_WRAPPER_MARKER_START, 1)
        _, after = rest.split(CODEX_WRAPPER_MARKER_END, 1)
        updated = before + block + after.lstrip("\n")
    else:
        updated = existing.rstrip() + "\n\n" + block
    with open(shell_rc, "w", encoding="utf-8") as handle:
        handle.write(updated)
    print(f"Installed AIWatcher Codex wrapper in {shell_rc}")
    print("Run `source ~/.zshrc` or open a new terminal.")
    return 0


def _remove_marked_block(content: str, start: str, end: str) -> tuple[str, bool]:
    if start not in content or end not in content:
        return content, False
    before, rest = content.split(start, 1)
    _, after = rest.split(end, 1)
    return (before.rstrip() + "\n" + after.lstrip("\n")).lstrip("\n"), True


def command_uninstall_codex_wrapper(args: argparse.Namespace) -> int:
    shell_rc = os.path.expanduser(args.shell_rc or "~/.zshrc")
    if not os.path.exists(shell_rc):
        print(f"No shell configuration found at {shell_rc}.")
        return 0
    try:
        with open(shell_rc, "r", encoding="utf-8") as handle:
            existing = handle.read()
    except OSError as exc:
        print(f"Could not read {shell_rc}: {exc}", file=sys.stderr)
        return 2
    updated, removed = _remove_marked_block(existing, CODEX_WRAPPER_MARKER_START, CODEX_WRAPPER_MARKER_END)
    if not removed:
        print(f"No AIWatcher Codex wrapper found in {shell_rc}.")
        return 0
    with open(shell_rc, "w", encoding="utf-8") as handle:
        handle.write(updated)
    print(f"Removed AIWatcher Codex wrapper from {shell_rc}")
    print("Open a new terminal for the change to take effect.")
    return 0


def command_doctor(_args: argparse.Namespace) -> int:
    detected = discover_tools()
    claude_project = _claude_settings_path("project")
    claude_user = _claude_settings_path("user")
    codex_project = _codex_hooks_path("project")
    codex_user = _codex_hooks_path("user")
    cursor_project = _cursor_hooks_path("project")
    cursor_user = _cursor_hooks_path("user")
    shell_rc = os.path.expanduser("~/.zshrc")
    codex_config = os.path.expanduser("~/.codex/config.toml")

    def file_contains(path: str, needle: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return needle in handle.read()
        except OSError:
            return False

    print("AIWatcher Local doctor\n")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    print(f"Platform: {sys.platform}")
    print(f"Claude history: {'detected' if detected.get('claude-code') else 'not detected'}")
    print(f"Codex history: {'detected' if detected.get('codex-cli') else 'not detected'}")
    print(f"Claude project hook: {'installed' if file_contains(claude_project, 'claude-hook') else 'not installed'}")
    print(f"Claude user hook: {'installed' if file_contains(claude_user, 'claude-hook') else 'not installed'}")
    print(f"Codex project hook: {'installed' if file_contains(codex_project, 'codex-hook') else 'not installed'}")
    print(f"Codex user hook: {'installed' if file_contains(codex_user, 'codex-hook') else 'not installed'}")
    print(f"Cursor project hook: {'installed' if file_contains(cursor_project, 'cursor-hook') else 'not installed'}")
    print(f"Cursor user hook: {'installed' if file_contains(cursor_user, 'cursor-hook') else 'not installed'}")
    print(f"Codex shell wrapper: {'installed' if file_contains(shell_rc, CODEX_WRAPPER_MARKER_START) else 'not installed'}")
    print(f"Codex MCP config: {'referenced' if file_contains(codex_config, 'aiwatcher') else 'not detected'}")
    print(f"Local state: {state_path()}")
    print("\nSurface coverage")
    print("- Claude Code CLI / Claude Desktop Code tab: hook-capable; verify with `aiwatcher hook-status`.")
    print("- Claude Desktop general chat, browser chat, and editor sidebars: use Prompt Companion or an extension.")
    print("- Codex CLI/TUI: hook-capable only when the host invokes UserPromptSubmit and the hook is trusted with `/hooks`.")
    print("- Codex Desktop conversation surface: do not assume hook interception; verify with `aiwatcher hook-status`.")
    print("- Cursor: hook can block and return a scoped brief for resubmission, but cannot replace prompt text in place.")
    print("\nPrivacy: local-only; AIWatcher Local does not upload prompts, source, or telemetry.")
    if os.name == "nt":
        print("Note: core scanning works on Windows; the Codex zsh wrapper is not available in PowerShell yet.")
    return 0


def _hook_decision_action(decision: object) -> str:
    labels = {
        "context_added": "added brief context (no popup)",
        "brief_accepted": "added safer brief",
        "brief_edited": "added edited brief",
        "allowed_original": "allowed original",
        "blocked": "blocked by policy",
        "cancelled": "cancelled in Prompt Gate",
        "auto_block_headless": "blocked headless",
        "auto_brief_headless": "added safer brief headless",
    }
    return labels.get(str(decision), str(decision or "recorded"))


def _matching_hook_intervention(
    event: dict[str, object],
    interventions: list[dict[str, object]],
) -> dict[str, object] | None:
    event_tool = event.get("tool")
    event_session = event.get("session_id")
    event_cwd = event.get("cwd")
    for row in interventions:
        if row.get("tool") != event_tool:
            continue
        if event_session and row.get("session_id") != event_session:
            continue
        if event_cwd and row.get("cwd") and row.get("cwd") != event_cwd:
            continue
        return row
    return None


def _hook_event_action(event: dict[str, object], interventions: list[dict[str, object]]) -> str:
    if not event.get("prompt_found"):
        return "allowed uninspected"
    if event.get("event") == "gate_failed":
        return "gate failed; used fallback policy"
    match = _matching_hook_intervention(event, interventions)
    if match:
        return _hook_decision_action(match.get("decision"))
    if event.get("risk") == "low":
        return "allowed unchanged"
    return "received; check recent decisions"


def command_hook_status(_args: argparse.Namespace) -> int:
    events = recent_hook_events(limit=8)
    interventions = recent_interventions(limit=5, days=7)
    print("AIWatcher hook status\n")
    if not events:
        print("No recent hook events recorded.")
        print("Submit a test prompt after installing a Claude, Codex, or Cursor hook, then check again.")
    else:
        for event in events:
            stamp = str(event.get("created_at", "unknown"))
            tool = str(event.get("tool", "unknown"))
            name = str(event.get("event", "unknown"))
            prompt_label = "prompt found" if event.get("prompt_found") else "prompt missing"
            risk = event.get("risk") or "unknown"
            score = event.get("score")
            line = f"- {stamp} | {tool} | {name} | {prompt_label} | risk {risk}"
            if score is not None:
                line += f" | score {score}"
            line += f" | action {_hook_event_action(event, interventions)}"
            if event.get("session_id"):
                line += f" | session {event['session_id']}"
            print(line)
            if event.get("error"):
                print(f"  error: {event['error']}")
    if interventions:
        print("\nRecent preflight decisions")
        for row in interventions:
            selected_score = row.get("selected_score")
            change = f"{row.get('score', 'unknown')} -> {selected_score}" if selected_score is not None else str(row.get("score", "unknown"))
            line = (
                f"- {row.get('created_at', 'unknown')} | {row.get('tool', 'unknown')} | "
                f"{row.get('decision', 'recorded')} ({_hook_decision_action(row.get('decision'))}) | risk score {change}"
            )
            if row.get("session_id"):
                line += f" | session {row['session_id']}"
            print(line)
    print("\nIf an event appears but the tool did not pause, inspect its risk and decision. If no event appears, reload the tool and verify its hook configuration.")
    return 0


def _mcp_tool_specs() -> list[dict[str, object]]:
    return [
        {
            "name": "aiwatcher_today",
            "description": "Summarize local AI coding usage for this machine.",
            "inputSchema": {
                "type": "object",
                "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 1}},
            },
        },
        {
            "name": "aiwatcher_last_session",
            "description": "Inspect the latest local Claude/Codex/Cursor session without exposing prompt or source content.",
            "inputSchema": {
                "type": "object",
                "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 30}},
            },
        },
        {
            "name": "aiwatcher_project_summary",
            "description": "Show which local projects are driving AI usage.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7},
                    "project": {"type": "string"},
                },
            },
        },
        {
            "name": "aiwatcher_budget_check",
            "description": "Check local API-equivalent usage against personal daily/monthly budgets.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "daily_budget_usd": {"type": "number", "default": DEFAULT_DAILY_BUDGET_USD},
                    "monthly_budget_usd": {"type": "number", "default": DEFAULT_MONTHLY_BUDGET_USD},
                },
            },
        },
        {
            "name": "aiwatcher_preflight_prompt",
            "description": "Review a planned prompt for cost, scope, loop, and safety risk before running an AI coding agent.",
            "inputSchema": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "tool": {"type": "string", "default": "agent"},
                    "cwd": {"type": "string"},
                },
            },
        },
        {
            "name": "aiwatcher_session_timeline",
            "description": "Inspect a privacy-safe local event timeline for a session when the tool exposes event history.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 30},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
                },
            },
        },
        {
            "name": "aiwatcher_daily_journal",
            "description": "Create a personal local AI work journal with top project, costly sessions, loop signals, and one improvement.",
            "inputSchema": {
                "type": "object",
                "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 30, "default": 1}},
            },
        },
    ]


def _mcp_tool_call(name: str, arguments: dict[str, object] | None) -> str:
    args = arguments or {}
    if name == "aiwatcher_today":
        return render_today(days=max(1, min(90, int(args.get("days", 1)))))
    if name == "aiwatcher_last_session":
        days = max(1, min(90, int(args.get("days", 30))))
        session = latest_session(sessions_since(days))
        return render_session_detail(session) if session else f"No local AI sessions detected in the last {days} days."
    if name == "aiwatcher_project_summary":
        days = max(1, min(90, int(args.get("days", 7))))
        project = str(args["project"]) if args.get("project") else None
        return project_summary_text(days=days, project=project)
    if name == "aiwatcher_budget_check":
        return budget_check_text(
            daily_budget_usd=float(args.get("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)),
            monthly_budget_usd=float(args.get("monthly_budget_usd", DEFAULT_MONTHLY_BUDGET_USD)),
        )
    if name == "aiwatcher_preflight_prompt":
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return "Missing required prompt."
        result = analyze_prompt(
            prompt,
            tool=str(args.get("tool", "agent")),
            cwd=str(args["cwd"]) if args.get("cwd") else None,
        )
        return render_preflight(result)
    if name == "aiwatcher_session_timeline":
        session_id = str(args.get("session_id", "")).strip()
        days = max(1, min(90, int(args.get("days", 30))))
        limit = max(1, min(100, int(args.get("limit", 30))))
        if not session_id:
            session = latest_session(sessions_since(days))
            if not session:
                return f"No local AI sessions detected in the last {days} days."
            session_id = session.session_id
        return render_session_timeline(session_id, days=days, limit=limit)
    if name == "aiwatcher_daily_journal":
        return render_journal(days=max(1, min(30, int(args.get("days", 1)))))
    return f"Unknown AIWatcher tool: {name}"


# The MCP stdio transport delimits JSON-RPC messages by newline. Some clients
# (and older AIWatcher builds) instead use LSP-style Content-Length framing.
# Track whichever framing the connected client uses so responses match it.
_MCP_FRAMING = "line"


def _read_mcp_message() -> dict[str, object] | None:
    global _MCP_FRAMING
    line = sys.stdin.buffer.readline()
    while line in (b"\r\n", b"\n"):  # skip blank separators between messages
        line = sys.stdin.buffer.readline()
    if line == b"":
        return None
    if line.strip().lower().startswith(b"content-length:"):
        _MCP_FRAMING = "lsp"
        headers: dict[str, str] = {}
        while line not in (b"\r\n", b"\n"):
            if line == b"":
                return None
            key, _, value = line.decode("utf-8").partition(":")
            headers[key.strip().lower()] = value.strip()
            line = sys.stdin.buffer.readline()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            return None
        return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))
    _MCP_FRAMING = "line"
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def _write_mcp_message(payload: dict[str, object]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if _MCP_FRAMING == "lsp":
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(body)
    else:
        sys.stdout.buffer.write(body + b"\n")
    sys.stdout.buffer.flush()


def _mcp_response(message_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _mcp_error(message_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def command_mcp(_args: argparse.Namespace) -> int:
    while True:
        message = _read_mcp_message()
        if message is None:
            return 0
        method = str(message.get("method", ""))
        message_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}

        if method.startswith("notifications/"):
            continue
        if method == "initialize":
            _write_mcp_message(_mcp_response(message_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "aiwatcher-local", "version": "0.1.0"},
            }))
        elif method == "ping":
            _write_mcp_message(_mcp_response(message_id, {}))
        elif method == "tools/list":
            _write_mcp_message(_mcp_response(message_id, {"tools": _mcp_tool_specs()}))
        elif method == "tools/call":
            name = str(params.get("name", ""))
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            try:
                text = _mcp_tool_call(name, arguments)
                _write_mcp_message(_mcp_response(message_id, {"content": [{"type": "text", "text": text}]}))
            except Exception as exc:
                _write_mcp_message(_mcp_error(message_id, -32000, f"AIWatcher tool failed: {exc}"))
        else:
            _write_mcp_message(_mcp_error(message_id, -32601, f"Unknown method: {method}"))


def command_export(args: argparse.Namespace) -> int:
    if args.since:
        try:
            since = datetime.fromisoformat(args.since).astimezone()
        except ValueError:
            print(f"Invalid --since value: {args.since}. Use an ISO date or datetime, for example 2026-06-01.", file=sys.stderr)
            return 2
    else:
        since = datetime.now().astimezone() - timedelta(days=args.days)
    if args.format != "json":
        print("Only --format json is supported in the local MVP.", file=sys.stderr)
        return 2
    if args.level == "events":
        rows = [
            row.to_json()
            for row in scan_all_events()
            if row.timestamp and row.timestamp.astimezone() >= since
        ]
        print(json.dumps({"schema": "aiwatcher.local_events.v0", "events": rows}, indent=2))
    else:
        rows = [row.to_json() for row in scan_all() if in_window(row, since)]
        print(json.dumps({"schema": "aiwatcher.local_sessions.v0", "sessions": rows}, indent=2))
    print("Tip: Cloud can schedule exports and evidence packs for teams.", file=sys.stderr)
    return 0


def command_ui(args: argparse.Namespace) -> int:
    from .ui import serve

    try:
        get_or_refresh_baselines()
    except OSError:
        pass

    try:
        serve(
            host=args.host,
            port=args.port,
            auto_port=not args.no_port_fallback,
            port_attempts=args.port_attempts,
            restart=args.restart,
        )
    except OSError as exc:
        print(f"Could not start AIWatcher Local UI: {exc}", file=sys.stderr)
        print("Try `aiwatcher ui --restart` or omit `--no-port-fallback` to use the next available port.", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiwatcher", description="AIWatcher Local: private AI coding usage visibility")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start", help="Detect local AI coding tools and run a one-time local scan").set_defaults(func=command_start)
    sub.add_parser("status", help="Show detected tools and local AIWatcher status").set_defaults(func=command_status)
    sub.add_parser("today", help="Show today's local AI usage").set_defaults(func=command_today)

    tools = sub.add_parser("tools", help="Rank AI usage by tool")
    tools.add_argument("--days", type=int, default=7)
    tools.set_defaults(func=command_tools)

    projects = sub.add_parser("projects", help="Rank AI usage by project")
    projects.add_argument("--days", type=int, default=7)
    projects.add_argument("--limit", type=int, default=10)
    projects.set_defaults(func=command_projects)

    report = sub.add_parser("report", help="Show a local weekly AI usage report")
    report.add_argument("--days", type=int, default=7)
    report.set_defaults(func=command_report)

    sessions = sub.add_parser("sessions", help="Show recent local AI sessions")
    sessions.add_argument("--days", type=int, default=1)
    sessions.add_argument("--limit", type=int, default=20)
    sessions.add_argument("--search", help="Filter by project path, tool, model, or session id")
    sessions.add_argument("--team", action="store_true", help="Explain team session visibility in AIWatcher Cloud")
    sessions.set_defaults(func=command_sessions)

    last = sub.add_parser("last", help="Inspect the latest local AI session")
    last.add_argument("--days", type=int, default=30)
    last.add_argument("--session-id")
    last.set_defaults(func=command_last)

    timeline = sub.add_parser("timeline", help="Show a privacy-safe event timeline for a local AI session")
    timeline.add_argument("--session-id")
    timeline.add_argument("--days", type=int, default=30)
    timeline.add_argument("--limit", type=int, default=30)
    timeline.set_defaults(func=command_timeline)

    handoff = sub.add_parser("handoff", help="Create a local handoff capsule for continuing work in a fresh AI session")
    handoff.add_argument("--session-id")
    handoff.add_argument("--days", type=int, default=30)
    handoff.add_argument("--target", choices=sorted(TARGET_LABELS), default="generic", help="Format the brief for a target AI tool")
    handoff.add_argument("--copy", action="store_true", help="Copy the next-session brief to the clipboard when supported")
    handoff.add_argument("--format", choices=["text", "json"], default="text")
    handoff.add_argument("--include-prompt-excerpt", action="store_true", help="Include a local prompt excerpt in the capsule output")
    handoff.set_defaults(func=command_handoff)

    resume = sub.add_parser("resume", help="Find a local session and create a target-ready continuation brief")
    resume.add_argument("--session-id")
    resume.add_argument("--search", help="Find the most recent matching project, tool, model, or session id")
    resume.add_argument("--days", type=int, default=30)
    resume.add_argument("--target", choices=sorted(TARGET_LABELS), default="generic")
    resume.add_argument("--copy", action="store_true")
    resume.add_argument("--format", choices=["text", "json"], default="text")
    resume.add_argument("--include-prompt-excerpt", action="store_true")
    resume.set_defaults(func=command_resume)

    journal = sub.add_parser("journal", help="Show a personal local AI work journal")
    journal.add_argument("--days", type=int, default=1)
    journal.set_defaults(func=command_journal)

    outcome = sub.add_parser("outcome", help="Mark a local AI session as useful, rework, or abandoned")
    outcome.add_argument("outcome", choices=sorted(VALID_OUTCOMES))
    outcome.add_argument("--session-id")
    outcome.add_argument("--note")
    outcome.add_argument("--days", type=int, default=30)
    outcome.set_defaults(func=command_outcome)

    log_decision = sub.add_parser(
        "log-decision",
        help="Record a local note for a design decision made or rejected this session",
    )
    log_decision.add_argument("summary", help="One-line summary of the decision")
    log_decision.add_argument("--reasoning", help="Why this decision was made")
    log_decision.add_argument(
        "--rejected",
        action="append",
        default=[],
        dest="alternatives_rejected",
        help="An alternative that was considered and rejected; repeat for multiple",
    )
    log_decision.add_argument("--session-id")
    log_decision.add_argument("--days", type=int, default=30)
    log_decision.set_defaults(func=command_log_decision)

    watch = sub.add_parser("watch", help="Watch local AI sessions for high-cost or looping behavior")
    watch.add_argument("--days", type=int, default=1)
    watch.add_argument("--interval", type=int, default=15)
    watch.add_argument("--once", action="store_true", help="Run one scan and exit")
    watch.add_argument("--cost-threshold", type=float, default=5.0)
    watch.add_argument("--calls-threshold", type=int, default=250)
    watch.add_argument("--tokens-threshold", type=int, default=500_000)
    watch.set_defaults(func=command_watch)

    run = sub.add_parser("run", help="Run a command and summarize the latest local AI session afterwards")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=command_run)

    preflight = sub.add_parser("preflight", help="Review a prompt for local cost, scope, and safety risk")
    preflight.add_argument("prompt", nargs="*")
    preflight.add_argument("--text")
    preflight.add_argument("--tool", default="agent")
    preflight.add_argument("--cwd")
    preflight.add_argument("--fail-on-high", action="store_true")
    preflight.set_defaults(func=command_preflight)

    for agent in ("codex", "claude"):
        agent_parser = sub.add_parser(agent, help=f"Preflight a prompt, then launch {agent}")
        agent_parser.add_argument("prompt", nargs="*")
        agent_parser.add_argument("--text")
        agent_parser.add_argument("--cwd")
        agent_parser.add_argument("--binary", help=f"Path to the real {agent} binary")
        agent_parser.add_argument("--yes", action="store_true", help="Launch even when AIWatcher marks the prompt high risk")
        agent_parser.add_argument("--apply-suggestion", action="store_true", help="Launch the safer prompt suggested by AIWatcher")
        agent_parser.add_argument("--dry-run", action="store_true", help="Run preflight without launching the agent")
        agent_parser.set_defaults(func=command_agent_prompt, agent=agent)

    install_claude_hook = sub.add_parser("install-claude-hook", help="Print or install a Claude Code prompt preflight hook")
    install_claude_hook.add_argument("--write", action="store_true", help="Write the hook into Claude settings")
    install_claude_hook.add_argument("--scope", choices=["project", "user"], default="project")
    install_claude_hook.add_argument("--project-dir")
    install_claude_hook.add_argument("--command", help="AIWatcher command to put in Claude settings")
    install_claude_hook.add_argument("--gate", action="store_true", help="Open the local Prompt Gate decision screen for risky prompts")
    install_claude_hook.set_defaults(func=command_install_claude_hook)

    uninstall_claude_hook = sub.add_parser("uninstall-claude-hook", help="Remove the AIWatcher Claude Code preflight hook")
    uninstall_claude_hook.add_argument("--scope", choices=["project", "user"], default="project")
    uninstall_claude_hook.add_argument("--project-dir")
    uninstall_claude_hook.set_defaults(func=command_uninstall_claude_hook)

    install_decision_log = sub.add_parser(
        "install-claude-decision-log",
        help="Print or install a personal Claude Code convention for logging rejected decisions",
    )
    install_decision_log.add_argument("--write", action="store_true", help="Write the convention into your personal CLAUDE.md")
    install_decision_log.set_defaults(func=command_install_claude_decision_log)

    uninstall_decision_log = sub.add_parser(
        "uninstall-claude-decision-log",
        help="Remove the AIWatcher decision-log convention from your personal CLAUDE.md",
    )
    uninstall_decision_log.set_defaults(func=command_uninstall_claude_decision_log)

    install_codex_hook = sub.add_parser("install-codex-hook", help="Print or install a native Codex prompt preflight hook")
    install_codex_hook.add_argument("--write", action="store_true", help="Write the hook into Codex hooks.json")
    install_codex_hook.add_argument("--scope", choices=["project", "user"], default="user")
    install_codex_hook.add_argument("--project-dir")
    install_codex_hook.add_argument("--command", help="AIWatcher command to put in Codex hooks")
    install_codex_hook.add_argument("--gate", action="store_true", help="Open the local Prompt Gate decision screen for risky prompts")
    install_codex_hook.set_defaults(func=command_install_codex_hook)

    uninstall_codex_hook = sub.add_parser("uninstall-codex-hook", help="Remove the native AIWatcher Codex prompt hook")
    uninstall_codex_hook.add_argument("--scope", choices=["project", "user"], default="user")
    uninstall_codex_hook.add_argument("--project-dir")
    uninstall_codex_hook.set_defaults(func=command_uninstall_codex_hook)

    install_cursor_hook = sub.add_parser("install-cursor-hook", help="Print or install a native Cursor prompt preflight hook")
    install_cursor_hook.add_argument("--write", action="store_true", help="Write the hook into Cursor hooks.json")
    install_cursor_hook.add_argument("--scope", choices=["project", "user"], default="user")
    install_cursor_hook.add_argument("--project-dir")
    install_cursor_hook.add_argument("--command", help="AIWatcher command to put in Cursor hooks")
    install_cursor_hook.add_argument("--gate", action="store_true", help="Open the local Prompt Gate decision screen for risky prompts")
    install_cursor_hook.set_defaults(func=command_install_cursor_hook)

    uninstall_cursor_hook = sub.add_parser("uninstall-cursor-hook", help="Remove the native AIWatcher Cursor prompt hook")
    uninstall_cursor_hook.add_argument("--scope", choices=["project", "user"], default="user")
    uninstall_cursor_hook.add_argument("--project-dir")
    uninstall_cursor_hook.set_defaults(func=command_uninstall_cursor_hook)

    install_codex_wrapper = sub.add_parser("install-codex-wrapper", help="Print or install a shell wrapper that preflights Codex prompts")
    install_codex_wrapper.add_argument("--write", action="store_true", help="Write the wrapper into your shell rc file")
    install_codex_wrapper.add_argument("--shell-rc", default="~/.zshrc")
    install_codex_wrapper.add_argument("--codex-binary", help="Path to the real Codex binary")
    install_codex_wrapper.add_argument("--command", help="AIWatcher command to call from the shell function")
    install_codex_wrapper.add_argument("--function-name", default="codex")
    install_codex_wrapper.set_defaults(func=command_install_codex_wrapper)

    uninstall_codex_wrapper = sub.add_parser("uninstall-codex-wrapper", help="Remove the AIWatcher Codex shell wrapper")
    uninstall_codex_wrapper.add_argument("--shell-rc", default="~/.zshrc")
    uninstall_codex_wrapper.set_defaults(func=command_uninstall_codex_wrapper)

    sub.add_parser("doctor", help="Check local tool detection and AIWatcher integrations").set_defaults(func=command_doctor)
    sub.add_parser("hook-status", help="Show recent local Claude, Codex, and Cursor hook invocations").set_defaults(func=command_hook_status)

    sub.add_parser("mcp", help="Run AIWatcher Local as a stdio MCP server").set_defaults(func=command_mcp)

    export = sub.add_parser("export", help="Export local session summaries")
    export.add_argument("--format", default="json", choices=["json"])
    export.add_argument("--level", default="sessions", choices=["sessions", "events"], help="Export session summaries or privacy-safe event hashes")
    export.add_argument("--since", help="ISO date/datetime, for example 2026-06-01")
    export.add_argument("--days", type=int, default=30)
    export.set_defaults(func=command_export)

    ui = sub.add_parser("ui", help="Run the local-only AIWatcher dashboard")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--port-attempts", type=int, default=20, help="How many sequential ports to try when the requested port is busy")
    ui.add_argument("--no-port-fallback", action="store_true", help="Fail instead of trying the next available port")
    ui.add_argument("--restart", action="store_true", help="Stop an existing local process on the requested port before starting")
    ui.set_defaults(func=command_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"claude-hook", "codex-hook", "cursor-hook"}:
        hook_parser = argparse.ArgumentParser(add_help=False)
        hook_parser.add_argument("--text")
        hook_parser.add_argument("--gate", action="store_true")
        hook_args = hook_parser.parse_args(arguments[1:])
        handlers = {
            "claude-hook": command_claude_hook,
            "codex-hook": command_codex_hook,
            "cursor-hook": command_cursor_hook,
        }
        handler = handlers[arguments[0]]
        return int(handler(hook_args))
    args = build_parser().parse_args(arguments)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
