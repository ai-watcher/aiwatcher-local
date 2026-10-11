"""Bounded, metadata-only evidence for a separate cleanup review.

Completion events describe turns, never completion of the user's whole goal.
No prompt, response text, command arguments, or source is returned or persisted.
"""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path

REVIEW_BATCH_SIZE = 8


@lru_cache(maxsize=512)
def _rollout_metadata(path: str, session_id: str, size: int, mtime: int) -> dict:
    result = {"session_role": "unknown", "parent_session_id": None,
              "last_turn_status": "unknown", "final_response_observed": False}
    p = Path(path)
    # Only inspect this exact session's observed Codex log, never an arbitrary file.
    if p.is_symlink() or p.suffix != ".jsonl" or session_id not in p.name:
        return result
    try:
        with p.open("rb") as stream:
            head = stream.readline(65536)
            start = max(0, size - 65536)
            stream.seek(start)
            tail = stream.read(65536)
        if start:
            tail = tail.partition(b"\n")[2]
        records = []
        for line in head.splitlines()[:1] + tail.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(row, dict) and isinstance(row.get("payload"), dict):
                records.append(row)
        identity = next((r["payload"] for r in records if r.get("type") == "session_meta"), {})
        if identity.get("id") != session_id:
            return result
        result["identity_verified"] = True
        result["source_modified_at"] = datetime.fromtimestamp(mtime / 1_000_000_000, tz=timezone.utc).isoformat()
        result["owning_cwd"] = identity.get("cwd")
        result["surface"] = "desktop" if "desktop" in str(identity.get("originator", "")).lower() else "unknown"
        source = identity.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        if isinstance(subagent, dict):
            spawn = subagent.get("thread_spawn")
            if isinstance(spawn, dict):
                result["session_role"] = "subagent"
                result["parent_session_id"] = spawn.get("parent_thread_id")
            elif subagent.get("other") == "guardian":
                result["session_role"] = "approval_session"
            else:
                result["session_role"] = "internal_session"
        elif isinstance(source, str):
            result["session_role"] = "user_session"
        for row in records[1:]:
            timestamp = row.get("timestamp")
            if isinstance(timestamp, str):
                try:
                    at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if at.tzinfo is not None:
                        prior = result.get("latest_observed_event_at")
                        if not prior or at > datetime.fromisoformat(prior):
                            result["latest_observed_event_at"] = at.isoformat()
                except ValueError:
                    pass
            payload = row["payload"]
            typ = row.get("type")
            if typ == "turn_context" and isinstance(payload.get("cwd"), str):
                result["last_recorded_cwd"] = payload["cwd"]
            if typ == "event_msg":
                event = payload.get("type")
                if event in {"task_started", "task_complete", "turn_aborted"}:
                    result["last_turn_status"] = {"task_started": "started", "task_complete": "completed", "turn_aborted": "aborted"}[event]
                    result["last_turn_at"] = row.get("timestamp")
                    if event == "task_started":
                        result["final_response_observed"] = False
            if typ == "response_item" and payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
                result["final_response_observed"] = True
        result["read_boundary"] = "First metadata record (up to 64 KiB) and last 64 KiB; missing events remain unknown. Final response text was not read into this packet."
    except OSError:
        pass
    return result


def session_review_identity(row, by_id: dict) -> dict:
    meta = {}
    if row.tool == "codex-cli" and row.source_path:
        try:
            stat = Path(row.source_path).stat()
            meta = dict(_rollout_metadata(row.source_path, row.session_id, stat.st_size, stat.st_mtime_ns))
        except OSError:
            pass
    parent = by_id.get(meta.get("parent_session_id"))
    parent_meta = {}
    if parent and parent.tool == "codex-cli" and parent.source_path:
        try:
            stat = Path(parent.source_path).stat()
            parent_meta = _rollout_metadata(parent.source_path, parent.session_id, stat.st_size, stat.st_mtime_ns)
        except OSError:
            pass
    stamp = _latest_stamp(row, meta)
    return {
        "session_id": row.session_id, "title": row.title,
        "tool": row.tool, "surface": row.surface or meta.get("surface") or "unknown",
        "project_path": row.project_path, "recorded_cwd": row.raw_cwd,
        "last_activity": stamp.isoformat() if stamp else None,
        "session_role": "unknown", "last_turn_status": "unknown",
        "final_response_observed": False, **meta,
        "parent_last_activity": _latest_stamp(parent, parent_meta).isoformat() if parent and _latest_stamp(parent, parent_meta) else None,
        "parent_title": parent.title if parent else None,
        "parent_last_turn_status": parent_meta.get("last_turn_status", "unknown"),
        "parent_source_modified_at": parent_meta.get("source_modified_at"),
        "parent_status": "Verify in owning app" if meta.get("parent_session_id") else "Unknown or not applicable",
        "runtime_status": "Not verified; log age does not prove runtime inactivity",
        "final_response_status": "Text not included; inspect latest final response and unresolved asks in owning app",
    }


def _latest_stamp(row, meta):
    stamp = row.updated_at or row.started_at
    observed = meta.get("latest_observed_event_at")
    if observed:
        try:
            at = datetime.fromisoformat(observed)
            if at.tzinfo is not None and (not stamp or at > stamp):
                stamp = at
        except (TypeError, ValueError):
            pass
    return stamp


def project_activity(items, latest_evidence=None) -> dict:
    dated = [r for r in items if r.updated_at or r.started_at]
    latest = next((row for row in items if row.session_id == (latest_evidence or {}).get("session_id")), None)
    latest = latest or (max(dated, key=lambda r: r.updated_at or r.started_at) if dated else None)
    stamp = _latest_stamp(latest, latest_evidence or {}) if latest else None
    return {"session_id": latest.session_id if latest else None,
            "title": latest.title if latest else None,
            "last_activity": stamp.isoformat() if stamp else None,
            "status": "Recent activity observed; preserve current work" if stamp and datetime.now(timezone.utc) - stamp.astimezone(timezone.utc) < timedelta(hours=4) else "No recent log in scan window; verify live state in owning app",
            "scope": "Latest session in scanned project rows; not a complete owning-app inventory"}
