"""Bounded, evidence-first summaries of one developer's AI-assisted work."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Iterable


MAX_INTENT_CHARS = 700
MAX_COMMITS = 5
MAX_AREAS = 6


def _clean_text(value: object, limit: int = MAX_INTENT_CHARS) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _intent_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean_text(value).lower()).strip()


def _duration_label(value: object) -> str | None:
    if not isinstance(value, (int, float)) or value < 0:
        return None
    seconds = int(round(value))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{round(seconds / 60)} min"
    return f"{seconds // 3600}h {round((seconds % 3600) / 60)}m"


def _changed_areas(files: Iterable[object]) -> list[str]:
    areas: list[str] = []
    for raw in files:
        value = str(raw or "").replace("\\", "/").strip("/")
        if not value:
            continue
        path = PurePosixPath(value)
        area = path.parts[0] if len(path.parts) > 1 else path.name
        if area and area not in areas:
            areas.append(area)
        if len(areas) >= MAX_AREAS:
            break
    return areas


def _turn_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def build_work_understanding(
    segments: Iterable[dict[str, object]],
    *,
    commits: Iterable[dict[str, object]] = (),
    files: Iterable[object] = (),
    checks: Iterable[dict[str, object]] = (),
    exact_session_link: bool = False,
    linked_commit_shas: Iterable[object] = (),
    excluded_delegated_sessions: int = 0,
    observed_through: object = None,
) -> dict[str, Any] | None:
    """Connect transient intent to observed work without claiming completion."""
    candidate_turns = [
        {**row, "_input_order": index} for index, row in enumerate(segments)
        if isinstance(row, dict)
        and not row.get("compact_summary")
        and _clean_text(row.get("prompt"))
    ]
    boundary_requested = bool(_clean_text(observed_through, 100))
    boundary = _turn_timestamp(observed_through)
    excluded_out_of_window_turns = 0
    turns = []
    for row in candidate_turns:
        timestamp = _turn_timestamp(row.get("at"))
        if boundary_requested and (boundary is None or timestamp is None or timestamp > boundary):
            excluded_out_of_window_turns += 1
            continue
        turns.append(row)
    turns.sort(key=lambda row: (
        _turn_timestamp(row.get("at")) is None,
        _turn_timestamp(row.get("at")) or 0,
        int(row.get("_input_order") or 0),
    ))
    linked_shas = {str(value or "").strip().lower() for value in linked_commit_shas if str(value or "").strip()}
    commit_rows = []
    for row in commits:
        if not isinstance(row, dict):
            continue
        subject = _clean_text(row.get("subject"), 240)
        sha = _clean_text(row.get("sha"), 64)
        if subject or sha:
            normalized_sha = sha.lower()
            session_linked = bool(
                exact_session_link
                or (
                    normalized_sha
                    and any(value.startswith(normalized_sha) or normalized_sha.startswith(value) for value in linked_shas)
                )
            )
            commit_rows.append({"sha": sha, "subject": subject, "session_linked": session_linked})
        if len(commit_rows) >= MAX_COMMITS:
            break
    file_rows = [str(value) for value in files if str(value or "").strip()]
    check_rows = [dict(row) for row in checks if isinstance(row, dict)]
    if not turns and not commit_rows and not file_rows and not check_rows:
        return None

    opening = _clean_text(turns[0].get("prompt")) if turns else ""
    latest = _clean_text(turns[-1].get("prompt")) if turns else ""
    latest_turn = turns[-1] if turns else {}
    differs = bool(opening and latest and _intent_key(opening) != _intent_key(latest))
    passed_checks = [
        row for row in check_rows
        if str(row.get("status") or "").lower() == "passed"
        and (row.get("exact_state") is True or row.get("authoritative") is True)
    ]
    linked_commit_count = sum(1 for row in commit_rows if row["session_linked"])
    exact_commits = linked_commit_count > 0
    if turns and exact_commits and passed_checks:
        confidence = "medium"
        alignment_label = "Intent and exact-state evidence are connected; confirm requirement coverage."
    elif commit_rows or file_rows:
        confidence = "low"
        alignment_label = "Code evidence is available; confirm that it matches the final request."
    else:
        confidence = "low"
        alignment_label = "No code delivery is linked yet; review the turn before treating it as complete."

    intent_seed = json.dumps([
        {
            "session_id": str(row.get("session_id") or ""),
            "at": str(row.get("at") or ""),
            "prompt": _clean_text(row.get("prompt"), 2_000),
        }
        for row in turns
    ], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    limitation = (
        "AIWatcher connects observed intent, Git, and verification evidence. "
        "It does not prove that the implementation satisfies every prose requirement."
    )
    if boundary_requested and boundary is None:
        limitation += " Delivery-event time was unavailable, so no transcript intent was included."
    elif boundary_requested:
        limitation += (
            " Intent includes only timestamped developer turns observed no later than the delivery event; "
            "earlier unrelated turns in a linked session may still be present."
        )
    return {
        "opening_objective": opening or None,
        "latest_request": latest or None,
        "latest_request_differs": differs,
        "turn_count": len(turns),
        "latest_turn": {
            "turn": latest_turn.get("turn"),
            "model_calls": max(0, int(latest_turn.get("requests") or 0)),
            "tool_calls": max(0, int(latest_turn.get("tool_calls") or 0)),
            "tokens": max(0, int(latest_turn.get("tokens") or 0)),
            "duration_label": _duration_label(latest_turn.get("took_seconds")),
        },
        "commits": commit_rows,
        "commit_count": len(commit_rows),
        "linked_commit_count": linked_commit_count,
        "changed_file_count": len(file_rows),
        "changed_areas": _changed_areas(file_rows),
        "passed_check_count": len(passed_checks),
        "exact_session_link": exact_commits,
        "excluded_delegated_sessions": max(0, int(excluded_delegated_sessions)),
        "excluded_out_of_window_turns": excluded_out_of_window_turns,
        "intent_window_ends_at": _clean_text(observed_through, 100) if boundary is not None else None,
        "confidence": confidence,
        "alignment_status": "confirmation_required",
        "alignment_label": alignment_label,
        "limitation": limitation,
        "intent_source_hash": (
            hashlib.sha256(intent_seed.encode("utf-8")).hexdigest()
            if opening or latest else None
        ),
    }
