"""Correlate local interventions with local AI sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .local_state import (
    link_handoff_decision_next_session,
    link_intervention_session,
    recent_handoff_decisions,
    recent_interventions,
)
from .scanner import LocalSession


TOOL_ALIASES = {
    "claude": {"claude", "claude-code"},
    "codex": {"codex", "codex-cli"},
}


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _session_stamp(session: LocalSession) -> datetime | None:
    stamp = session.updated_at or session.started_at
    if not stamp:
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp


def _same_tool(intervention_tool: object, session_tool: str) -> bool:
    tool = str(intervention_tool or "").strip().lower()
    observed = session_tool.strip().lower()
    return observed in TOOL_ALIASES.get(tool, {tool})


def _same_project(intervention_cwd: object, session_project: str | None) -> bool:
    cwd = str(intervention_cwd or "").strip()
    project = str(session_project or "").strip()
    if not cwd or not project:
        return True
    try:
        cwd_path = Path(cwd).expanduser().resolve(strict=False)
        project_path = Path(project).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return cwd == project
    return cwd_path == project_path or cwd_path in project_path.parents or project_path in cwd_path.parents


def link_recent_interventions_to_sessions(
    sessions: Iterable[LocalSession],
    *,
    days: int = 7,
    max_delay_hours: int = 6,
) -> int:
    """Link unassigned preflight records to the first likely session that followed."""
    rows = list(sessions)
    interventions = recent_interventions(limit=500, days=days)
    linked = 0
    used_sessions: set[str] = set()
    for intervention in sorted(interventions, key=lambda row: str(row.get("created_at") or "")):
        if intervention.get("session_id"):
            continue
        if intervention.get("decision") in {"blocked", "cancelled"}:
            continue
        created_at = _parse_datetime(intervention.get("created_at"))
        if not created_at:
            continue
        upper = created_at + timedelta(hours=max_delay_hours)
        lower = created_at - timedelta(minutes=2)
        candidates: list[tuple[datetime, LocalSession]] = []
        for session in rows:
            if session.session_id in used_sessions:
                continue
            stamp = _session_stamp(session)
            if not stamp or stamp < lower or stamp > upper:
                continue
            if not _same_tool(intervention.get("tool"), session.tool):
                continue
            if not _same_project(intervention.get("cwd"), session.project_path):
                continue
            candidates.append((stamp, session))
        if not candidates:
            continue
        _, match = min(candidates, key=lambda item: item[0])
        if link_intervention_session(str(intervention.get("id")), match.session_id):
            used_sessions.add(match.session_id)
            linked += 1
    return linked


def link_recent_fresh_start_receipts_to_sessions(
    sessions: Iterable[LocalSession],
    *,
    days: int = 7,
    max_delay_hours: int = 24,
) -> int:
    """Link Fresh Start receipts to the first later same-project local session.

    This is deliberately narrower than preflight correlation: it should prove a
    fresh follow-up session happened, not mistake continued activity in the
    original chat for a successful restart.
    """
    rows = list(sessions)
    decisions = recent_handoff_decisions(limit=500)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    linked = 0
    used_sessions: set[str] = set()
    for decision in sorted(decisions, key=lambda row: str(row.get("created_at") or "")):
        if decision.get("decision") not in {"new_chat", "copy_handoff"}:
            continue
        decision_id = str(decision.get("id") or "")
        if not decision_id or decision.get("next_session_id"):
            continue
        created_at = _parse_datetime(decision.get("created_at"))
        if not created_at or created_at < cutoff:
            continue
        source_session_id = str(decision.get("source_session_id") or decision.get("session_id") or "")
        source_session = next((session for session in rows if session.session_id == source_session_id), None)
        source_project = (
            source_session.project_path
            if source_session
            else decision.get("source_project_path") or decision.get("project_path")
        )
        if not str(source_project or "").strip() or str(source_project).strip().lower() == "unknown":
            link_handoff_decision_next_session(
                decision_id,
                correlation={
                    "status": "waiting",
                    "method": "first_following_local_session",
                    "window_hours": max_delay_hours,
                    "confidence": None,
                    "reason": "Source project is unavailable, so AIWatcher cannot safely identify a follow-up session.",
                },
            )
            continue
        upper = created_at + timedelta(hours=max_delay_hours)
        candidates: list[tuple[datetime, str, LocalSession]] = []
        for session in rows:
            if session.session_id == source_session_id or session.session_id in used_sessions:
                continue
            started = session.started_at
            if started and started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            updated = _session_stamp(session)
            stamp = started or updated
            if not stamp or stamp <= created_at or stamp > upper:
                continue
            if not _same_project(source_project, session.project_path):
                continue
            confidence = "high" if started and started > created_at else "medium"
            candidates.append((stamp, confidence, session))
        if not candidates:
            link_handoff_decision_next_session(
                decision_id,
                correlation={
                    "status": "waiting",
                    "method": "first_following_local_session",
                    "window_hours": max_delay_hours,
                    "confidence": None,
                    "reason": "No later same-project local session has been observed yet.",
                },
            )
            continue
        candidates.sort(key=lambda item: item[0])
        first_stamp = candidates[0][0]
        nearest = [item for item in candidates if item[0] == first_stamp]
        if len(nearest) > 1:
            link_handoff_decision_next_session(
                decision_id,
                correlation={
                    "status": "ambiguous",
                    "method": "first_following_local_session",
                    "window_hours": max_delay_hours,
                    "confidence": "low",
                    "reason": "Multiple same-project sessions started at the same time after the action.",
                },
            )
            continue
        _, confidence, match = candidates[0]
        if link_handoff_decision_next_session(
            decision_id,
            next_session_id=match.session_id,
            correlation={
                "status": "linked",
                "method": "first_following_local_session",
                "window_hours": max_delay_hours,
                "confidence": confidence,
                "reason": "Observed a later local session in the same project after the Fresh Start action.",
            },
        ):
            used_sessions.add(match.session_id)
            linked += 1
    return linked
