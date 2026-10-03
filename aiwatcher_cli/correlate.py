"""Correlate local interventions with local AI sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .git_identity import identity_for_session
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


def _same_project(
    intervention_cwd: object,
    session_project: str | None,
    *,
    intervention_raw_cwd: str | None = None,
    session_raw_cwd: str | None = None,
    intervention_repository_id: str | None = None,
    session_repository_id: str | None = None,
    intervention_identity_source: str | None = None,
    session_identity_source: str | None = None,
) -> bool:
    cwd = str(intervention_cwd or "").strip()
    project = str(session_project or "").strip()
    if not cwd or not project:
        return False
    if "identity_conflict" in {intervention_identity_source, session_identity_source}:
        return False
    left = identity_for_session(cwd, intervention_raw_cwd or cwd)
    right = identity_for_session(project, session_raw_cwd or project)
    if any(identity is not None and identity.identity_source == "identity_conflict" for identity in (left, right)):
        return False
    left_id = intervention_repository_id or (left.repository_id if left else None)
    right_id = session_repository_id or (right.repository_id if right else None)
    if left_id or right_id:
        return bool(left_id and right_id and left_id == right_id)
    try:
        cwd_path = Path(cwd).expanduser().resolve(strict=False)
        project_path = Path(project).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if not cwd_path.exists() or not project_path.exists():
        return False
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
            if not _same_project(
                intervention.get("cwd"),
                session.project_path,
                intervention_repository_id=intervention.get("repository_id"),
                session_repository_id=session.repository_id,
                intervention_identity_source=intervention.get("identity_source"),
                session_identity_source=session.identity_source,
                session_raw_cwd=session.raw_cwd,
            ):
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
    """Record the first later same-project session as a possible follow-up.

    Project and timing are discovery signals, not proof that the copied handoff
    was used. Explicit receipt linkage may set ``next_session_id`` elsewhere;
    this correlator only records a candidate.
    """
    rows = list(sessions)
    decisions = recent_handoff_decisions(limit=500)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    correlated = 0
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
            if not _same_project(
                source_project,
                session.project_path,
                intervention_raw_cwd=source_session.raw_cwd if source_session else None,
                session_raw_cwd=session.raw_cwd,
                intervention_repository_id=(
                    source_session.repository_id
                    if source_session
                    else decision.get("source_repository_id")
                ),
                session_repository_id=session.repository_id,
                intervention_identity_source=(
                    source_session.identity_source
                    if source_session
                    else decision.get("source_identity_source")
                ),
                session_identity_source=session.identity_source,
            ):
                continue
            confidence = "medium" if started and started > created_at else "low"
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
            correlation={
                "status": "candidate",
                "method": "first_following_local_session",
                "window_hours": max_delay_hours,
                "confidence": confidence,
                "candidate_session_id": match.session_id,
                "reason": "A later same-project session may be the follow-up, but no explicit handoff linkage was observed.",
            },
        ):
            used_sessions.add(match.session_id)
            correlated += 1
    return correlated
