"""Private local state for AIWatcher interventions and outcomes."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl


STATE_VERSION = 1
VALID_OUTCOMES = {"useful", "rework", "abandoned"}

# Private: guards this process's own threads only. Every hook invocation is
# a separate OS process though, so this alone does not prevent two
# concurrent processes from racing a read-modify-write on local-state.json
# and silently dropping one side's update. Deliberately not exported/reused
# directly by any record_*/recent_*/get_* function -- _locked_state() below
# is the only supported way to guard a read-modify-write of local state.
# Leading underscore is load-bearing: nothing outside this module (and
# nothing else in this module) should reach for this lock on its own.
_STATE_LOCK = threading.RLock()
LOCK_TIMEOUT_SECONDS = 10
LOCK_POLL_SECONDS = 0.05


class StateLockTimeout(RuntimeError):
    """Another AIWatcher process held the local-state lock too long."""


def _lock_path() -> Path:
    return state_path().parent / ".local-state.lock"


def _acquire_file_lock(handle) -> None:
    if os.name == "nt":
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise StateLockTimeout(
                        "Timed out waiting for another AIWatcher process to release local-state.json."
                    )
                time.sleep(LOCK_POLL_SECONDS)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_file_lock(handle) -> None:
    if os.name == "nt":
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _cross_process_lock():
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        _acquire_file_lock(handle)
        try:
            yield
        finally:
            _release_file_lock(handle)


@contextlib.contextmanager
def _locked_state():
    """Hold both the in-process thread lock and a cross-process file lock.

    Every AIWatcher command (hook invocations especially) runs as its own
    fresh OS process, so guarding local-state.json with only a
    threading.Lock lets two concurrent processes interleave a
    read-modify-write and silently clobber each other's append. This
    combined lock makes read-modify-write of local-state.json atomic across
    processes, not just threads.
    """
    with _STATE_LOCK, _cross_process_lock():
        yield


def state_path() -> Path:
    override = os.environ.get("AIWATCHER_STATE_FILE")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("AIWATCHER_HOME", Path.home() / ".aiwatcher")).expanduser()
    return home / "local-state.json"


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "interventions": [], "outcomes": [], "hook_events": []}


def _load() -> dict[str, Any]:
    path = state_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    data.setdefault("version", STATE_VERSION)
    data.setdefault("interventions", [])
    data.setdefault("outcomes", [])
    data.setdefault("hook_events", [])
    return data


def _save(data: dict[str, Any]) -> None:
    path = state_path()
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".local-state-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            temp_path = handle.name
        os.replace(temp_path, path)
        if os.name != "nt":
            if not parent_existed:
                os.chmod(path.parent, 0o700)
            os.chmod(path, 0o600)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _safe_impact(impact: dict[str, Any] | None) -> dict[str, Any] | None:
    if not impact:
        return None
    safe: dict[str, Any] = {}
    for key in ("available", "confidence", "basis", "sample_count", "history_span_days"):
        value = impact.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
    for key in ("original", "safer", "savings"):
        value = impact.get(key)
        if isinstance(value, dict):
            safe[key] = {
                metric: numbers
                for metric, numbers in value.items()
                if metric in {"tokens", "model_calls", "tool_calls", "api_value_usd"}
                and isinstance(numbers, list)
                and all(isinstance(number, (int, float)) for number in numbers)
            }
    return safe


def record_intervention(
    *,
    tool: str,
    cwd: str,
    risk: str,
    score: int,
    findings: list[str],
    original_prompt: str,
    suggested_prompt: str,
    decision: str,
    selected_prompt: str | None,
    estimated_impact: dict[str, Any] | None = None,
) -> str:
    with _locked_state():
        data = _load()
        intervention_id = str(uuid.uuid4())
        data["interventions"].append({
            "id": intervention_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "phase": "plan",
            "intervention_type": "prompt_preflight",
            "tool": tool,
            "cwd": cwd,
            "risk": risk,
            "score": score,
            "findings": list(findings),
            "original_prompt_hash": hash_prompt(original_prompt),
            "suggested_prompt_hash": hash_prompt(suggested_prompt),
            "selected_prompt_hash": hash_prompt(selected_prompt) if selected_prompt else None,
            "decision": decision,
            "predicted_impact": _safe_impact(estimated_impact),
            "session_id": None,
        })
        _save(data)
    return intervention_id


def record_hook_event(
    *,
    tool: str,
    cwd: str,
    event: str,
    prompt_found: bool,
    risk: str | None = None,
    score: int | None = None,
    error: str | None = None,
) -> None:
    with _locked_state():
        data = _load()
        data["hook_events"].append({
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "cwd": cwd,
            "event": event,
            "prompt_found": prompt_found,
            "risk": risk,
            "score": score,
            "error": error,
        })
        data["hook_events"] = data["hook_events"][-50:]
        _save(data)


def recent_hook_events(limit: int = 10) -> list[dict[str, Any]]:
    with _locked_state():
        data = _load()
    rows = [row for row in data["hook_events"] if isinstance(row, dict)]
    return list(reversed(rows[-max(1, limit):]))


def link_intervention_session(intervention_id: str, session_id: str) -> bool:
    with _locked_state():
        data = _load()
        for row in reversed(data["interventions"]):
            if row.get("id") == intervention_id:
                row["session_id"] = session_id
                _save(data)
                return True
    return False


def record_outcome(session_id: str, outcome: str, note: str | None = None) -> dict[str, Any]:
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"Outcome must be one of: {', '.join(sorted(VALID_OUTCOMES))}")
    with _locked_state():
        data = _load()
        record = {
            "session_id": session_id,
            "outcome": outcome,
            "note": note.strip() if note else None,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        data["outcomes"] = [row for row in data["outcomes"] if row.get("session_id") != session_id]
        data["outcomes"].append(record)
        _save(data)
    return record


def get_outcome(session_id: str) -> dict[str, Any] | None:
    with _locked_state():
        data = _load()
    return next(
        (row for row in reversed(data["outcomes"]) if row.get("session_id") == session_id),
        None,
    )


def outcomes_for_sessions(session_ids: set[str] | None = None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with _locked_state():
        rows = list(_load()["outcomes"])
    for row in rows:
        session_id = row.get("session_id")
        if not isinstance(session_id, str):
            continue
        if session_ids is not None and session_id not in session_ids:
            continue
        result[session_id] = row
    return result


def recent_interventions(limit: int = 20, days: int | None = None) -> list[dict[str, Any]]:
    with _locked_state():
        data = _load()
    rows = [row for row in data["interventions"] if isinstance(row, dict)]
    if days is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - max(1, days) * 86400
        filtered = []
        for row in rows:
            try:
                created_at = datetime.fromisoformat(str(row.get("created_at", ""))).timestamp()
            except ValueError:
                continue
            if created_at >= cutoff:
                filtered.append(row)
        rows = filtered
    return list(reversed(rows[-max(1, limit):]))


def outcome_counts(session_ids: set[str] | None = None) -> dict[str, int]:
    counts = {key: 0 for key in sorted(VALID_OUTCOMES)}
    for row in outcomes_for_sessions(session_ids).values():
        outcome = row.get("outcome")
        if outcome in counts:
            counts[outcome] += 1
    return counts
