"""Correlate local interventions with local AI sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .local_state import link_intervention_session, recent_interventions
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
    stamp = session.started_at or session.updated_at
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
    except OSError:
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
