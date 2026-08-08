"""Passive privacy-safe evidence capture for local sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping

from .local_state import evidence_snapshots_for_sessions, record_evidence_snapshot
from .outcome_evidence import OutcomeEvidence, build_outcome_evidence
from .scanner import LocalSession


DEFAULT_PASSIVE_EVIDENCE_LIMIT = 5
DEFAULT_PASSIVE_EVIDENCE_MIN_AGE_HOURS = 2


def _session_stamp(session: LocalSession) -> datetime | None:
    stamp = session.updated_at or session.started_at
    if not stamp:
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp


def record_missing_evidence_snapshots(
    sessions: Iterable[LocalSession],
    *,
    limit: int = DEFAULT_PASSIVE_EVIDENCE_LIMIT,
    min_age_hours: int = DEFAULT_PASSIVE_EVIDENCE_MIN_AGE_HOURS,
    now: datetime | None = None,
) -> int:
    """Persist a capped batch of missing privacy-safe evidence snapshots."""
    if limit <= 0:
        return 0
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=min_age_hours)

    candidates = list(sessions)
    try:
        existing = evidence_snapshots_for_sessions({row.session_id for row in candidates})
    except OSError:
        return 0

    captured = 0
    for session in candidates:
        if captured >= limit:
            break
        if session.session_id in existing:
            continue
        stamp = _session_stamp(session)
        if not stamp or stamp > cutoff:
            continue
        try:
            record_evidence_snapshot(session.session_id, build_outcome_evidence(session).to_json())
        except OSError:
            continue
        captured += 1
    return captured


def record_missing_evidence_snapshots_from_evidence(
    sessions: Iterable[LocalSession],
    evidence_by_session: Mapping[str, OutcomeEvidence],
    *,
    limit: int = DEFAULT_PASSIVE_EVIDENCE_LIMIT,
    min_age_hours: int = DEFAULT_PASSIVE_EVIDENCE_MIN_AGE_HOURS,
    now: datetime | None = None,
) -> int:
    """Persist missing snapshots from evidence already computed by callers."""
    if limit <= 0:
        return 0
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=min_age_hours)

    candidates = list(sessions)
    try:
        existing = evidence_snapshots_for_sessions({row.session_id for row in candidates})
    except OSError:
        return 0

    captured = 0
    for session in candidates:
        if captured >= limit:
            break
        if session.session_id in existing:
            continue
        stamp = _session_stamp(session)
        if not stamp or stamp > cutoff:
            continue
        evidence = evidence_by_session.get(session.session_id)
        if not evidence:
            continue
        try:
            record_evidence_snapshot(session.session_id, evidence.to_json())
        except OSError:
            continue
        captured += 1
    return captured
