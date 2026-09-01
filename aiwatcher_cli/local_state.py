"""Private local state for AIWatcher interventions and outcomes."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl


STATE_VERSION = 2
VALID_OUTCOMES = {"useful", "rework", "abandoned"}

# How long an issued brief/capsule token remains redeemable. Short enough to
# limit the window a leaked local-state.json could be replayed in, long
# enough to cover copy-paste into a fresh session.
BRIEF_TOKEN_TTL_SECONDS = 900

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


class StateLockTimeout(OSError):
    """Another AIWatcher process held the local-state lock too long.

    Deliberately a subclass of OSError, for the same reason as
    StateReadError above: hook write paths in cli.py already catch OSError
    around record_*() calls so a slow/stuck lock skips recording instead of
    blocking or crashing the user's prompt, with no cli.py changes needed.
    """


class StateReadError(OSError):
    """local-state.json exists but could not be read (not a corrupt-JSON case).

    Deliberately a subclass of OSError: callers up the stack (especially
    hook write paths in cli.py) already catch OSError around record_*()
    calls to avoid blocking the user's AI flow, so this propagates through
    those existing handlers without each one needing a new except clause.
    Distinct from corrupt/malformed JSON, which _load() recovers from by
    quarantining the bad file and starting fresh -- a real read failure
    (permissions, I/O error) must NOT be treated the same way, since doing
    so would let a subsequent _save() silently overwrite a ledger that was
    never actually read.
    """


def _lock_path() -> Path:
    return state_path().parent / ".local-state.lock"


def _acquire_file_lock(handle) -> None:
    # Both branches poll a non-blocking lock attempt against a deadline.
    # fcntl.flock's blocking mode (LOCK_EX with no LOCK_NB) has no timeout
    # at all, so a stuck holder would wedge every other AIWatcher process --
    # including a prompt-gate hook -- forever instead of failing safe.
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
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StateLockTimeout(
                        "Timed out waiting for another AIWatcher process to release local-state.json."
                    )
                time.sleep(LOCK_POLL_SECONDS)


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
    return {
        "version": STATE_VERSION,
        "interventions": [],
        "outcomes": [],
        "hook_events": [],
        "evidence_snapshots": [],
        "decisions": [],
        "baselines": {},
        "survival_summary": {},
        "command_decisions": [],
        "command_gate_allowlist": [],
        "brief_tokens": [],
        "watch_notifications": [],
        "handoff_decisions": [],
        "optimize_decisions": [],
        "companion_skips": [],
        "ambient_interventions": [],
        "sent_notification_keys": [],
        "active_prompt_gate": None,
        "active_command_gate": None,
        # Second Opinion spends the user's own money on their own key, so
        # consent is per project and the spend ledger is what the monthly cap
        # is enforced against.
        "analyst_consent": {},
        # Whether the analyst may open files in this project. Off unless asked
        # for, and separate from consent: agreeing to pay for a second opinion
        # is not agreeing to let it read your source.
        "analyst_contents": {},
        "analyst_runs": [],
        # When the first-run screen was dismissed. One timestamp, not a flag:
        # "never seen" and "seen on install day" are different facts, and a
        # bare boolean cannot tell a fresh machine from a long-dismissed one.
        "first_run_dismissed_at": None,
        "ui_server": None,
        "watcher_heartbeat": None,
        # One record per session, latest wins: {session_id: {at, tool, kind}}.
        # A dict rather than a log because only the most recent signal can
        # still be true, and a per-session log would grow without ever being
        # read past its head.
        "session_waiting": {},
    }


def _quarantine_corrupt_state(path: Path) -> None:
    """Move an unparseable state file aside instead of discarding it.

    Called only for malformed JSON / wrong top-level type, never for a
    real read failure -- see StateReadError.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = path.with_name(f"local-state.corrupt-{timestamp}.json")
    try:
        path.replace(backup_path)
    except OSError as exc:
        print(
            f"Warning: AIWatcher could not back up corrupt state file {path}: {exc}",
            file=sys.stderr,
        )
        return
    print(
        f"Warning: AIWatcher local state at {path} was unreadable and has been "
        f"backed up to {backup_path}. Starting from a fresh, empty state.",
        file=sys.stderr,
    )


def _load() -> dict[str, Any]:
    path = state_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return _empty_state()
    except OSError as exc:
        # A real read failure (permissions, I/O error, etc). Never silently
        # return empty state here: doing so would let a later _save() call
        # overwrite a ledger this process never actually managed to read.
        raise StateReadError(f"Could not read AIWatcher state at {path}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        _quarantine_corrupt_state(path)
        return _empty_state()

    if not isinstance(data, dict):
        _quarantine_corrupt_state(path)
        return _empty_state()

    data.setdefault("version", STATE_VERSION)
    data.setdefault("interventions", [])
    data.setdefault("outcomes", [])
    data.setdefault("hook_events", [])
    data.setdefault("evidence_snapshots", [])
    data.setdefault("decisions", [])
    data.setdefault("baselines", {})
    data.setdefault("survival_summary", {})
    data.setdefault("command_decisions", [])
    data.setdefault("command_gate_allowlist", [])
    data.setdefault("brief_tokens", [])
    data.setdefault("watch_notifications", [])
    data.setdefault("handoff_decisions", [])
    data.setdefault("optimize_decisions", [])
    data.setdefault("companion_skips", [])
    data.setdefault("ambient_interventions", [])
    data.setdefault("sent_notification_keys", [])
    data.setdefault("active_prompt_gate", None)
    data.setdefault("active_command_gate", None)
    data.setdefault("ui_server", None)
    data.setdefault("watcher_heartbeat", None)
    data.setdefault("session_waiting", {})
    data.setdefault("first_run_dismissed_at", None)
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


PROMPT_MODIFIED_DECISIONS = frozenset({"brief_accepted", "brief_edited", "auto_brief_headless", "context_added"})
# The decisions where what actually ran differs from the raw original prompt --
# an explicit safer brief (brief_accepted/brief_edited), an automatic one when
# no interactive gate was available (auto_brief_headless), or S-03's silent
# medium-risk guardrail context (context_added). Excludes allowed_original
# (nothing changed), cancelled (nothing ran), and blocked/auto_block_headless
# (stopped entirely, not modified).


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
    selected_risk: str | None = None,
    selected_score: int | None = None,
    session_id: str | None = None,
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
            "selected_risk": selected_risk,
            "selected_score": selected_score,
            "risk_points_reduced": max(0, score - selected_score) if selected_score is not None else None,
            "findings": list(findings),
            "original_prompt_hash": hash_prompt(original_prompt),
            "suggested_prompt_hash": hash_prompt(suggested_prompt),
            "selected_prompt_hash": hash_prompt(selected_prompt) if selected_prompt else None,
            "decision": decision,
            "predicted_impact": _safe_impact(estimated_impact),
            # A hook-provided session_id (see _extract_session_meta in cli.py) is
            # ground truth from the tool itself. Anything left None here still
            # gets a best-effort retroactive match from
            # correlate.link_recent_interventions_to_sessions() -- see its
            # `if intervention.get("session_id"): continue` guard, which skips
            # interventions that already have a real id instead of overwriting them.
            "session_id": session_id,
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
    session_id: str | None = None,
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
            "session_id": session_id,
        })
        data["hook_events"] = data["hook_events"][-50:]
        _save(data)


# A signal older than this is not evidence of anything: the session was closed,
# the machine slept, or the reader was never running. Pruned on write so the
# store cannot grow on a machine where nothing reads it.
WAITING_SIGNAL_TTL_SECONDS = 24 * 3600
MAX_WAITING_SIGNALS = 200


def record_session_waiting(
    *,
    session_id: str,
    tool: str,
    kind: str,
    cwd: str | None = None,
    wants: str | None = None,
) -> None:
    """Note that a session asked for the developer's attention.

    Called from a hook the tool runs when it needs permission or has been left
    waiting for input. Deliberately stores no message text: what a session is
    asking for can quote a file path, a command, or a prompt, and this product's
    claim is that prompt content is analyzed locally and not persisted. `kind` is
    a classification of that message, not the message.
    """
    now = datetime.now(timezone.utc)
    with _locked_state():
        data = _load()
        waiting = data.get("session_waiting")
        if not isinstance(waiting, dict):
            waiting = {}
        fresh: dict[str, Any] = {}
        for key, value in waiting.items():
            if not isinstance(value, dict):
                continue
            stamp = _parse_waiting_stamp(value.get("at"))
            if stamp and (now - stamp).total_seconds() <= WAITING_SIGNAL_TTL_SECONDS:
                fresh[key] = value
        fresh[session_id] = {
            "at": now.isoformat(),
            "tool": tool,
            "kind": kind,
            "cwd": cwd,
            # A closed-vocabulary phrase from the hook's classifier ("run
            # Bash", "edit files", ...), never message text -- the same
            # privacy contract as `kind`.
            "wants": wants or "",
        }
        if len(fresh) > MAX_WAITING_SIGNALS:
            fresh = dict(
                sorted(
                    fresh.items(),
                    key=lambda item: str(item[1].get("at") or ""),
                    reverse=True,
                )[:MAX_WAITING_SIGNALS]
            )
        data["session_waiting"] = fresh
        _save(data)


def _parse_waiting_stamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def session_waiting_signals() -> dict[str, dict[str, Any]]:
    """Latest attention signal per session, newest value only.

    Returns an empty mapping rather than raising when the store cannot be read:
    a reader that cannot see the signals should report nothing waiting, never
    take down the surface that was going to display them.
    """
    try:
        data = _load()
    except (StateReadError, OSError):
        return {}
    waiting = data.get("session_waiting")
    if not isinstance(waiting, dict):
        return {}
    return {
        str(key): value
        for key, value in waiting.items()
        if isinstance(value, dict) and value.get("at")
    }


def record_active_prompt_gate(
    *,
    gate_id: str,
    tool: str,
    cwd: str,
    risk: str,
    score: int,
    url: str,
    expires_at: datetime,
    session_id: str | None = None,
    workflow_mode: str | None = None,
    workflow_label: str | None = None,
    workflow_reward: str | None = None,
) -> None:
    with _locked_state():
        data = _load()
        data["active_prompt_gate"] = {
            "id": gate_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
            "tool": tool,
            "cwd": cwd,
            "risk": risk,
            "score": score,
            "url": url,
            "session_id": session_id,
            "workflow_mode": workflow_mode,
            "workflow_label": workflow_label,
            "workflow_reward": workflow_reward,
            "companion_seen_at": None,
        }
        _save(data)


def clear_active_prompt_gate(gate_id: str | None = None) -> None:
    with _locked_state():
        data = _load()
        gate = data.get("active_prompt_gate")
        if not isinstance(gate, dict):
            data["active_prompt_gate"] = None
            _save(data)
            return
        if gate_id is not None and gate.get("id") != gate_id:
            return
        data["active_prompt_gate"] = None
        _save(data)


def active_prompt_gate() -> dict[str, Any] | None:
    with _locked_state():
        data = _load()
        gate = data.get("active_prompt_gate")
        if not isinstance(gate, dict):
            return None
        try:
            expires_at = datetime.fromisoformat(str(gate.get("expires_at")))
        except ValueError:
            expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at.astimezone(timezone.utc) < datetime.now(timezone.utc):
            data["active_prompt_gate"] = None
            _save(data)
            return None
        return dict(gate)


def mark_active_prompt_gate_seen(gate_id: str) -> None:
    with _locked_state():
        data = _load()
        gate = data.get("active_prompt_gate")
        if not isinstance(gate, dict) or gate.get("id") != gate_id:
            return
        gate["companion_seen_at"] = datetime.now(timezone.utc).isoformat()
        data["active_prompt_gate"] = gate
        _save(data)


def active_prompt_gate_seen(gate_id: str) -> bool:
    gate = active_prompt_gate()
    return bool(isinstance(gate, dict) and gate.get("id") == gate_id and gate.get("companion_seen_at"))


def record_active_command_gate(
    *,
    gate_id: str,
    tool: str,
    command_preview: str,
    pattern_id: str,
    reason: str,
    url: str,
    expires_at: datetime,
) -> None:
    with _locked_state():
        data = _load()
        data["active_command_gate"] = {
            "id": gate_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
            "tool": tool,
            "command_preview": command_preview.strip()[:500],
            "pattern_id": pattern_id.strip()[:120],
            "reason": reason.strip()[:500],
            "url": url,
            "companion_seen_at": None,
        }
        _save(data)


def clear_active_command_gate(gate_id: str | None = None) -> None:
    with _locked_state():
        data = _load()
        gate = data.get("active_command_gate")
        if not isinstance(gate, dict):
            data["active_command_gate"] = None
            _save(data)
            return
        if gate_id is not None and gate.get("id") != gate_id:
            return
        data["active_command_gate"] = None
        _save(data)


def active_command_gate() -> dict[str, Any] | None:
    with _locked_state():
        data = _load()
        gate = data.get("active_command_gate")
        if not isinstance(gate, dict):
            return None
        try:
            expires_at = datetime.fromisoformat(str(gate.get("expires_at")))
        except ValueError:
            expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at.astimezone(timezone.utc) < datetime.now(timezone.utc):
            data["active_command_gate"] = None
            _save(data)
            return None
        return dict(gate)


def mark_active_command_gate_seen(gate_id: str) -> None:
    with _locked_state():
        data = _load()
        gate = data.get("active_command_gate")
        if not isinstance(gate, dict) or gate.get("id") != gate_id:
            return
        gate["companion_seen_at"] = datetime.now(timezone.utc).isoformat()
        data["active_command_gate"] = gate
        _save(data)


def active_command_gate_seen(gate_id: str) -> bool:
    gate = active_command_gate()
    return bool(isinstance(gate, dict) and gate.get("id") == gate_id and gate.get("companion_seen_at"))


def _prune_companion_skips(data: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    kept: list[dict[str, Any]] = []
    for row in data.get("companion_skips", []):
        if not isinstance(row, dict):
            continue
        try:
            expires_at = datetime.fromisoformat(str(row.get("expires_at")))
        except ValueError:
            continue
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at.astimezone(timezone.utc) > now:
            kept.append(row)
    data["companion_skips"] = kept[-50:]


def record_companion_skip(
    *,
    key: str,
    reason: str = "",
    minutes: int = 60,
) -> dict[str, Any]:
    """Quiet a non-blocking Companion attention state without deleting evidence."""
    key = key.strip()[:160]
    if not key:
        raise ValueError("key is required")
    now = datetime.now(timezone.utc)
    record = {
        "id": str(uuid.uuid4()),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=max(1, minutes))).isoformat(),
        "key": key,
        "reason": reason.strip()[:500],
    }
    with _locked_state():
        data = _load()
        _prune_companion_skips(data)
        data["companion_skips"].append(record)
        data["companion_skips"] = data["companion_skips"][-50:]
        _save(data)
    return record


def companion_skip_active(key: str) -> bool:
    key = key.strip()[:160]
    if not key:
        return False
    try:
        with _locked_state():
            data = _load()
            _prune_companion_skips(data)
            active = any(row.get("key") == key for row in data.get("companion_skips", []) if isinstance(row, dict))
            if active:
                _save(data)
            return active
    except OSError:
        return False


def _prune_brief_tokens(data: dict[str, Any]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=BRIEF_TOKEN_TTL_SECONDS)
    kept = []
    for row in data.get("brief_tokens", []):
        issued_at = row.get("issued_at") if isinstance(row, dict) else None
        try:
            issued = datetime.fromisoformat(issued_at) if issued_at else None
        except ValueError:
            issued = None
        if issued is not None and issued >= cutoff:
            kept.append(row)
    data["brief_tokens"] = kept


def issue_brief_token(kind: str) -> str:
    """Issue a random, single-use token proving AIWatcher itself generated a brief/capsule.

    Recognizing our own generated text by a static phrase is spoofable by anyone
    who reads this open-source repo. A per-instance token recorded here and
    required by consume_brief_token() cannot be guessed from the source alone.
    """
    token = uuid.uuid4().hex
    try:
        with _locked_state():
            data = _load()
            _prune_brief_tokens(data)
            data["brief_tokens"].append({
                "token": token,
                "kind": kind,
                "issued_at": datetime.now(timezone.utc).isoformat(),
            })
            data["brief_tokens"] = data["brief_tokens"][-200:]
            _save(data)
    except OSError:
        return f"unverified-{token}"
    return token


def consume_brief_token(token: str | None, kind: str) -> bool:
    """Validate and single-use-consume a brief/capsule token.

    Returns False for an unknown, wrong-kind, expired, or already-consumed
    token -- callers must treat that as "not proven to be AIWatcher-generated",
    not as a soft warning.
    """
    if not token:
        return False
    with _locked_state():
        data = _load()
        _prune_brief_tokens(data)
        for index, row in enumerate(data["brief_tokens"]):
            if isinstance(row, dict) and row.get("token") == token and row.get("kind") == kind:
                data["brief_tokens"].pop(index)
                _save(data)
                return True
        _save(data)
    return False


def recent_hook_events(limit: int = 10) -> list[dict[str, Any]]:
    with _locked_state():
        data = _load()
    rows = [row for row in data["hook_events"] if isinstance(row, dict)]
    return list(reversed(rows[-max(1, limit):]))


def record_watch_notification(
    *,
    session_id: str,
    tool: str,
    action: str,
    reason: str,
    sent: bool,
    detail: str,
    url: str | None = None,
) -> None:
    """Record an ambient `watch --notify` firing so it survives the watch process exiting.

    Issue #31 (S-32, Ambient Watch delivery) requires notification/intervention
    metadata to be recorded locally, not just deduped in the watch loop's
    in-memory state.
    """
    with _locked_state():
        data = _load()
        data["watch_notifications"].append({
            "created_at": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "tool": tool,
            "action": action,
            "reason": reason,
            "sent": sent,
            "detail": detail,
            "url": url,
        })
        data["watch_notifications"] = data["watch_notifications"][-50:]
        _save(data)


def record_ui_server(host: str, port: int) -> None:
    """Remember where the local dashboard last actually bound.

    `aiwatcher ui` falls back to the next free port when its default is
    taken, so a notification built in a separate `watch` process can't just
    assume the default port -- it has to look this up instead.
    """
    try:
        with _locked_state():
            data = _load()
            data["ui_server"] = {
                "host": host,
                "port": port,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            _save(data)
    except OSError:
        pass


def get_ui_server() -> dict[str, Any] | None:
    try:
        with _locked_state():
            data = _load()
    except OSError:
        return None
    server = data.get("ui_server")
    return server if isinstance(server, dict) and server.get("port") else None


def record_watcher_heartbeat(*, pid: int, mode: str, interval_seconds: int, notify: bool, overlay: bool) -> None:
    """Record that `aiwatcher watch` is alive.

    This is intentionally just a heartbeat, not a daemon supervisor. The UI can
    use it to show whether ambient Watch is running without starting processes
    behind the user's back.
    """
    try:
        with _locked_state():
            data = _load()
            data["watcher_heartbeat"] = {
                "pid": int(pid),
                "mode": mode,
                "interval_seconds": int(interval_seconds),
                "notify": bool(notify),
                "overlay": bool(overlay),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _save(data)
    except OSError:
        pass


def get_watcher_status(max_age_seconds: int = 120) -> dict[str, Any]:
    """Return a quiet, honest status for the ambient watch loop."""
    try:
        with _locked_state():
            data = _load()
    except OSError:
        return {
            "running": False,
            "status": "unknown",
            "label": "Watcher status unavailable",
            "detail": "AIWatcher could not read local state.",
        }
    heartbeat = data.get("watcher_heartbeat")
    command = "aiwatcher companion start"
    if not isinstance(heartbeat, dict):
        return {
            "running": False,
            "status": "stopped",
            "label": "Companion stopped",
            "detail": "Start the local companion to surface handoff and outcome nudges while you work.",
            "command": command,
        }
    updated_at = heartbeat.get("updated_at")
    try:
        updated = datetime.fromisoformat(str(updated_at)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        updated = None
    age_seconds = (datetime.now(timezone.utc) - updated).total_seconds() if updated else None
    running = age_seconds is not None and age_seconds <= max_age_seconds
    mode = str(heartbeat.get("mode") or "watch")
    process_label = "Companion" if mode == "companion" else "Watcher"
    return {
        "running": running,
        "status": "running" if running else "stale",
        "label": f"{process_label} running" if running else f"{process_label} not recently seen",
        "detail": (
            f"{process_label} is checking local sessions for context pressure and handoff opportunities."
            if running
            else f"The last {process_label.lower()} heartbeat is stale. Restart it to catch new session pressure."
        ),
        "command": command,
        "updated_at": updated.isoformat() if updated else None,
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "pid": heartbeat.get("pid"),
        "mode": mode,
        "notify": bool(heartbeat.get("notify")),
        "overlay": bool(heartbeat.get("overlay")),
        "interval_seconds": heartbeat.get("interval_seconds"),
    }


def clear_watcher_heartbeat(*, pid: int | None = None) -> None:
    """Clear the watcher heartbeat, optionally only for the matching process."""
    try:
        with _locked_state():
            data = _load()
            heartbeat = data.get("watcher_heartbeat")
            if pid is not None and isinstance(heartbeat, dict) and heartbeat.get("pid") != pid:
                return
            data.pop("watcher_heartbeat", None)
            _save(data)
    except OSError:
        pass


def recent_watch_notifications(limit: int = 10) -> list[dict[str, Any]]:
    try:
        with _locked_state():
            data = _load()
    except OSError:
        return []
    rows = [row for row in data["watch_notifications"] if isinstance(row, dict)]
    return list(reversed(rows[-max(1, limit):]))


VALID_HANDOFF_DECISIONS = {"new_chat", "continue_here", "copy_handoff", "dismissed"}
MAX_HANDOFF_DECISIONS_STORED = 200
MAX_OPTIMIZE_DECISIONS_STORED = 200


def record_handoff_decision(
    *,
    session_id: str,
    decision: str,
    reason: str,
    expected_saved_context_tokens: int | None = None,
    action_channel: str | None = None,
    source_project_path: str | None = None,
) -> dict[str, Any]:
    """Record a local, privacy-safe Fresh Start companion decision.

    The Fresh Start companion is a Control-phase intervention: AIWatcher suggests
    continuing in a fresh session when context pressure is likely to waste
    turns. Store only metadata and the user's choice, not prompt/source text.
    """
    if decision not in VALID_HANDOFF_DECISIONS:
        raise ValueError(f"decision must be one of: {', '.join(sorted(VALID_HANDOFF_DECISIONS))}")
    if not session_id.strip():
        raise ValueError("session_id is required")
    with _locked_state():
        data = _load()
        actionable_fresh_start = decision in {"new_chat", "copy_handoff"}
        record = {
            "id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "phase": "control",
            "intervention_type": "handoff_bubble",
            "receipt_kind": "fresh_start" if actionable_fresh_start else "handoff_decision",
            "session_id": session_id,
            "source_session_id": session_id,
            "decision": decision,
            "reason": reason.strip()[:500],
            "action_channel": (action_channel or "dashboard").strip()[:80],
            "source_project_path": source_project_path.strip()[:1000] if isinstance(source_project_path, str) else None,
            "expected_saved_context_tokens": (
                int(expected_saved_context_tokens)
                if isinstance(expected_saved_context_tokens, int) and expected_saved_context_tokens > 0
                else None
            ),
        }
        if actionable_fresh_start:
            record.update({
                "next_session_id": None,
                "next_session_linked_at": None,
                "next_session_correlation": {
                    "status": "waiting",
                    "method": "first_following_local_session",
                    "window_hours": 24,
                    "confidence": None,
                    "reason": "Waiting for a later local session in the same project.",
                },
            })
        data["handoff_decisions"].append(record)
        data["handoff_decisions"] = data["handoff_decisions"][-MAX_HANDOFF_DECISIONS_STORED:]
        _save(data)
    return record


def recent_handoff_decisions(limit: int = 10) -> list[dict[str, Any]]:
    try:
        with _locked_state():
            data = _load()
    except OSError:
        return []
    rows = [row for row in data["handoff_decisions"] if isinstance(row, dict)]
    return list(reversed(rows[-max(1, limit):]))


def record_optimize_decision(
    *,
    decision: str,
    reason: str,
    project_path: str | None = None,
    evidence: dict[str, Any] | None = None,
    action_channel: str | None = None,
) -> dict[str, Any]:
    """Record a local Optimize Workspace decision without deleting anything.

    Optimize is a Control-phase action for stale forks, completed Fresh Starts,
    old AI worktrees, and orphaned runtimes. The receipt is intentionally
    metadata-only: evidence labels, counts, and paths, never prompt/source text.
    """
    allowed = {"marked_done", "checklist_copied", "skipped", "reviewed"}
    if decision not in allowed:
        raise ValueError(f"decision must be one of: {', '.join(sorted(allowed))}")
    payload = evidence if isinstance(evidence, dict) else {}
    record = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "phase": "control",
        "intervention_type": "optimize_workspace",
        "receipt_kind": "optimize_workspace",
        "decision": decision,
        "reason": reason.strip()[:500],
        "project_path": project_path.strip()[:1000] if isinstance(project_path, str) else None,
        "action_channel": (action_channel or "dashboard").strip()[:80],
        "evidence": payload,
    }
    with _locked_state():
        data = _load()
        data["optimize_decisions"].append(record)
        data["optimize_decisions"] = data["optimize_decisions"][-MAX_OPTIMIZE_DECISIONS_STORED:]
        _save(data)
    return record


def recent_optimize_decisions(limit: int = 10) -> list[dict[str, Any]]:
    try:
        with _locked_state():
            data = _load()
    except OSError:
        return []
    rows = [row for row in data["optimize_decisions"] if isinstance(row, dict)]
    return list(reversed(rows[-max(1, limit):]))


def mark_recent_handoff_receipts_viewed(limit: int = 20) -> int:
    """Mark recent Fresh Start receipts as reviewed by the user.

    A reviewed receipt can still be proof-pending; this only means the Companion
    should stop blinking for that already-seen receipt while Evidence keeps the
    pending proof visible.
    """
    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    with _locked_state():
        data = _load()
        for row in reversed(data["handoff_decisions"]):
            if updated >= max(1, limit):
                break
            if not isinstance(row, dict):
                continue
            if row.get("receipt_kind") != "fresh_start":
                continue
            if row.get("receipt_viewed_at"):
                continue
            row["receipt_viewed_at"] = now
            updated += 1
        if updated:
            _save(data)
    return updated


def link_handoff_decision_next_session(
    decision_id: str,
    *,
    next_session_id: str | None = None,
    correlation: dict[str, Any] | None = None,
) -> bool:
    """Attach Fresh Start proof metadata to an existing handoff decision."""
    if not decision_id.strip():
        return False
    with _locked_state():
        data = _load()
        for row in reversed(data["handoff_decisions"]):
            if not isinstance(row, dict) or row.get("id") != decision_id:
                continue
            if next_session_id:
                row["next_session_id"] = next_session_id
                row["next_session_linked_at"] = datetime.now(timezone.utc).isoformat()
            if correlation is not None:
                row["next_session_correlation"] = correlation
            _save(data)
            return True
    return False


VALID_AMBIENT_INTERVENTION_STATES = {
    "detected",
    "delivered",
    "displayed",
    "acted",
    "snoozed",
    "dismissed",
    "failed",
}
TERMINAL_AMBIENT_INTERVENTION_STATES = {"acted", "snoozed", "dismissed"}
AMBIENT_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
MAX_AMBIENT_INTERVENTIONS_STORED = 500
MAX_AMBIENT_INTERVENTION_EVENTS = 25


def ambient_intervention_fingerprint(*, session_id: str, signal_kind: str, action: str) -> str:
    """Return the stable identity shared by every presentation channel.

    Session activity and severity intentionally are not part of the fingerprint:
    those values change as one underlying intervention evolves. Delivery policy
    compares them with the last presentation to decide whether a re-alert is
    warranted.
    """
    identity = {
        "session_id": session_id.strip(),
        "signal_kind": signal_kind.strip().lower(),
        "action": action.strip().lower(),
    }
    if not all(identity.values()):
        raise ValueError("session_id, signal_kind, and action are required")
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _ambient_severity_rank(severity: str | None) -> int:
    return AMBIENT_SEVERITY_RANK.get(str(severity or "").strip().lower(), -1)


def _safe_ambient_urls(urls: dict[str, str] | None) -> dict[str, str]:
    if not isinstance(urls, dict):
        return {}
    return {
        str(key).strip()[:80]: value.strip()[:2000]
        for key, value in urls.items()
        if str(key).strip() and isinstance(value, str) and value.strip()
    }


def _safe_expected_savings(savings: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(savings, dict):
        return None
    allowed = {
        "context_tokens",
        "tokens",
        "model_calls",
        "tool_calls",
        "api_value_usd",
        "confidence",
        "basis",
    }
    safe: dict[str, Any] = {}
    for key, value in savings.items():
        if key not in allowed:
            continue
        if isinstance(value, (int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, str):
            safe[key] = value[:500]
        elif isinstance(value, list) and all(isinstance(item, (int, float)) for item in value):
            safe[key] = value[:2]
    return safe or None


def _ambient_event(state: str, *, channel: str | None = None, detail: str | None = None) -> dict[str, Any]:
    event = {
        "state": state,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if channel:
        event["channel"] = channel.strip()[:80]
    if detail and detail.strip():
        event["detail"] = detail.strip()[:500]
    return event


def upsert_ambient_intervention(
    *,
    session_id: str,
    signal_kind: str,
    action: str,
    severity: str,
    session_stamp: str,
    reason: str,
    urls: dict[str, str] | None = None,
    expected_savings: dict[str, Any] | None = None,
    required_observations: int = 1,
) -> dict[str, Any] | None:
    """Create or refresh the durable record shared by notification and overlay.

    Prompt or source content does not belong here. The record contains only the
    signal, recommended action, delivery metadata, and optional local URLs and
    planning estimates.
    """
    normalized_severity = severity.strip().lower()
    if normalized_severity not in AMBIENT_SEVERITY_RANK:
        raise ValueError("severity must be one of: info, warning, critical")
    fingerprint = ambient_intervention_fingerprint(
        session_id=session_id,
        signal_kind=signal_kind,
        action=action,
    )
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _locked_state():
            data = _load()
            rows = data.setdefault("ambient_interventions", [])
            record = next(
                (row for row in reversed(rows) if isinstance(row, dict) and row.get("fingerprint") == fingerprint),
                None,
            )
            if record is None:
                record = {
                    "id": str(uuid.uuid4()),
                    "fingerprint": fingerprint,
                    "created_at": now,
                    "state": "detected",
                    "events": [_ambient_event("detected")],
                    "channels": {},
                    "observation_count": 1,
                }
                rows.append(record)
            else:
                record["observation_count"] = max(1, int(record.get("observation_count") or 1)) + 1
                prior_stamp = record.get("session_stamp")
                prior_severity = record.get("severity")
                if prior_stamp != session_stamp or prior_severity != normalized_severity:
                    events = record.setdefault("events", [])
                    events.append(_ambient_event("detected", detail="Signal activity changed."))
                    record["events"] = events[-MAX_AMBIENT_INTERVENTION_EVENTS:]

            record.update({
                "updated_at": now,
                "session_id": session_id.strip(),
                "signal_kind": signal_kind.strip().lower(),
                "action": action.strip(),
                "severity": normalized_severity,
                "session_stamp": str(session_stamp).strip()[:200],
                "reason": reason.strip()[:500],
                "urls": _safe_ambient_urls(urls),
                "expected_savings": _safe_expected_savings(expected_savings),
                "required_observations": max(1, int(required_observations)),
            })
            data["ambient_interventions"] = rows[-MAX_AMBIENT_INTERVENTIONS_STORED:]
            _save(data)
            return dict(record)
    except OSError:
        return None


def get_ambient_intervention(fingerprint: str) -> dict[str, Any] | None:
    try:
        with _locked_state():
            rows = list(_load().get("ambient_interventions", []))
    except OSError:
        return None
    return next(
        (dict(row) for row in reversed(rows) if isinstance(row, dict) and row.get("fingerprint") == fingerprint),
        None,
    )


def recent_ambient_interventions(limit: int = 20) -> list[dict[str, Any]]:
    try:
        with _locked_state():
            rows = list(_load().get("ambient_interventions", []))
    except OSError:
        return []
    valid = [dict(row) for row in rows if isinstance(row, dict)]
    return list(reversed(valid[-max(1, limit):]))


def ambient_intervention_delivery_allowed(fingerprint: str, *, channel: str) -> bool:
    """Return whether this channel may present the intervention now.

    A user choice applies across notification and overlay. Delivery is calm by
    default: new log activity alone does not reopen the same warning every poll.
    Re-alert only after a snooze expires or the signal severity worsens.
    """
    record = get_ambient_intervention(fingerprint)
    if not record:
        return False
    if int(record.get("observation_count") or 0) < int(record.get("required_observations") or 1):
        return False
    current_severity = record.get("severity")

    decision_severity = record.get("decision_severity")
    worsened_since_decision = (
        decision_severity is not None
        and _ambient_severity_rank(current_severity) > _ambient_severity_rank(str(decision_severity))
    )
    if record.get("state") in {"acted", "dismissed", "snoozed"}:
        if not worsened_since_decision:
            if record.get("state") != "snoozed":
                return False
            snoozed_until = record.get("snoozed_until")
            try:
                if snoozed_until and datetime.fromisoformat(str(snoozed_until)) > datetime.now(timezone.utc):
                    return False
                # Snooze is deliberately temporary. Once it expires, allow
                # exactly one new presentation even if the session stamp has
                # not changed; the subsequent channel receipt will dedupe it.
                return True
            except (TypeError, ValueError):
                return False

    channels = record.get("channels", {})
    if not isinstance(channels, dict):
        channels = {}
    for other_channel, other_state in channels.items():
        if other_channel == channel or not isinstance(other_state, dict):
            continue
        if other_state.get("state") not in {"delivered", "displayed"}:
            continue
        if _ambient_severity_rank(current_severity) <= _ambient_severity_rank(other_state.get("severity")):
            return False

    channel_state = channels.get(channel, {})
    if not isinstance(channel_state, dict) or not channel_state:
        return True
    if channel_state.get("state") == "failed":
        updated_at = channel_state.get("updated_at")
        try:
            failed_at = datetime.fromisoformat(str(updated_at))
            if failed_at.tzinfo is None:
                failed_at = failed_at.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - failed_at.astimezone(timezone.utc)).total_seconds() >= 5 * 60
        except (TypeError, ValueError):
            return True
    return _ambient_severity_rank(current_severity) > _ambient_severity_rank(channel_state.get("severity"))


def record_ambient_intervention_action(
    fingerprint: str,
    *,
    state: str,
    channel: str | None = None,
    snoozed_until: str | None = None,
    detail: str | None = None,
) -> dict[str, Any] | None:
    """Advance an ambient intervention and append an auditable lifecycle event."""
    if state not in VALID_AMBIENT_INTERVENTION_STATES:
        raise ValueError(
            f"state must be one of: {', '.join(sorted(VALID_AMBIENT_INTERVENTION_STATES))}"
        )
    if state == "snoozed":
        try:
            parsed_snooze = datetime.fromisoformat(str(snoozed_until))
            if parsed_snooze.tzinfo is None:
                parsed_snooze = parsed_snooze.replace(tzinfo=timezone.utc)
            snoozed_until = parsed_snooze.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("snoozed_until must be an ISO-8601 timestamp") from exc

    try:
        with _locked_state():
            data = _load()
            record = next(
                (
                    row for row in reversed(data.get("ambient_interventions", []))
                    if isinstance(row, dict) and row.get("fingerprint") == fingerprint
                ),
                None,
            )
            if record is None:
                return None
            now = datetime.now(timezone.utc).isoformat()
            prior_state = str(record.get("state") or "")
            incoming_terminal = state in TERMINAL_AMBIENT_INTERVENTION_STATES
            sticky_terminal = prior_state in TERMINAL_AMBIENT_INTERVENTION_STATES and not incoming_terminal
            if not sticky_terminal:
                record["state"] = state
            record["updated_at"] = now
            if state in {"delivered", "displayed", "failed"} and channel:
                record.setdefault("channels", {})[channel] = {
                    "state": state,
                    "updated_at": now,
                    "session_stamp": record.get("session_stamp"),
                    "severity": record.get("severity"),
                }
            if state in {"acted", "snoozed", "dismissed"}:
                record["decision_session_stamp"] = record.get("session_stamp")
                record["decision_severity"] = record.get("severity")
            if state == "snoozed":
                record["snoozed_until"] = snoozed_until
            elif state in {"acted", "dismissed"}:
                record.pop("snoozed_until", None)
            events = record.setdefault("events", [])
            events.append(_ambient_event(state, channel=channel, detail=detail))
            record["events"] = events[-MAX_AMBIENT_INTERVENTION_EVENTS:]
            _save(data)
            return dict(record)
    except OSError:
        return None


MAX_NOTIFICATION_KEYS_SENT = 500


def has_sent_notification(signal_key: str) -> bool:
    """Has a local notification already fired for `signal_key`?

    Persistent, unlike command_watch's in-memory notification_seen/
    critical_capsule_seen dicts (which only dedupe within one process run).
    Shared by two notification families that both need "don't repeat this
    until something actually changes" to survive a `watch` restart or a
    one-shot `--once` invocation:
      - issue #32's outcome-review signals (survival/churn, same-file
        re-prompt, cost-per-surviving-change), keyed `{session_id}:{signal}`
        -- each fires at most once, ever, since the signal itself doesn't
        change once resolved.
      - the watch-status recommendation (issue #31's "narrow scope"/"create
        handoff capsule now"/etc.), keyed `{session_id}:{action}:{stamp}`
        where `stamp` is the session's last-updated timestamp -- so a new
        stamp (new activity) is treated as a fresh state worth re-notifying,
        while an unchanged stamp across repeated `--once` runs is not.
    """
    try:
        with _locked_state():
            data = _load()
    except OSError:
        return False
    return signal_key in data.get("sent_notification_keys", [])


def record_notification_sent(signal_key: str) -> None:
    with _locked_state():
        data = _load()
        sent = data.setdefault("sent_notification_keys", [])
        if signal_key not in sent:
            sent.append(signal_key)
        data["sent_notification_keys"] = sent[-MAX_NOTIFICATION_KEYS_SENT:]
        _save(data)


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


def record_evidence_snapshot(session_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Store a privacy-safe evidence snapshot for a session.

    This keeps enough local history to support later survival/outcome analysis
    without storing prompt text, source diffs, commit subjects, or file content.

    `recorded_at` and `survival` (S-22's day-bucket churn history) are
    preserved across re-snapshots of the same session -- re-opening a session
    in the dashboard days later must not reset the clock survival tracking
    measures from, or erase churn checks already recorded.
    """
    commits = evidence.get("commits") if isinstance(evidence.get("commits"), list) else []
    changed_files = evidence.get("changed_files") if isinstance(evidence.get("changed_files"), list) else []
    tests = evidence.get("tests") if isinstance(evidence.get("tests"), list) else []
    repo_root = evidence.get("repo_root") if isinstance(evidence.get("repo_root"), str) else None
    with _locked_state():
        data = _load()
        existing = next(
            (row for row in data["evidence_snapshots"] if row.get("session_id") == session_id),
            None,
        )
        existing_survival = existing.get("survival") if existing and isinstance(existing.get("survival"), dict) else {}
        record = {
            "session_id": session_id,
            "recorded_at": (existing.get("recorded_at") if existing and existing.get("recorded_at") else None)
                or datetime.now(timezone.utc).isoformat(),
            "repo_root_hash": hash_prompt(repo_root)[:16] if repo_root else None,
            "commit_shas": [
                str(item.get("sha"))[:12]
                for item in commits
                if isinstance(item, dict) and item.get("sha")
            ][:20],
            "changed_file_hashes": [
                hash_prompt(str(path))[:16]
                for path in changed_files
                if isinstance(path, str)
            ][:100],
            "test_artifact_hashes": [
                hash_prompt(str(item.get("artifact")))[:16]
                for item in tests
                if isinstance(item, dict) and item.get("artifact")
            ][:30],
            "inferred_outcome": evidence.get("inferred_outcome") if isinstance(evidence.get("inferred_outcome"), str) else None,
            "confidence": evidence.get("confidence") if isinstance(evidence.get("confidence"), str) else None,
            "survival": existing_survival,
        }
        data["evidence_snapshots"] = [
            row for row in data["evidence_snapshots"]
            if row.get("session_id") != session_id
        ]
        data["evidence_snapshots"].append(record)
        data["evidence_snapshots"] = data["evidence_snapshots"][-500:]
        _save(data)
    return record


VALID_SURVIVAL_BUCKETS = ("7", "14", "30")
VALID_SURVIVAL_STATUSES = {"survived", "churned", "unknown"}


def record_survival_check(session_id: str, day_bucket: str, status: str) -> bool:
    """Record a churn/survival check result for a snapshot's day-bucket (S-22).

    Returns False if no snapshot exists yet for this session -- there's
    nothing to attach the check to (the session was never viewed/marked).
    """
    if day_bucket not in VALID_SURVIVAL_BUCKETS or status not in VALID_SURVIVAL_STATUSES:
        raise ValueError(f"Invalid survival check: bucket={day_bucket!r} status={status!r}")
    with _locked_state():
        data = _load()
        for row in data["evidence_snapshots"]:
            if row.get("session_id") == session_id:
                survival = row.get("survival")
                if not isinstance(survival, dict):
                    survival = {}
                survival[day_bucket] = {
                    "status": status,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                row["survival"] = survival
                _save(data)
                return True
    return False


def evidence_snapshots_for_sessions(session_ids: set[str] | None = None) -> dict[str, dict[str, Any]]:
    with _locked_state():
        rows = list(_load()["evidence_snapshots"])
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        session_id = row.get("session_id")
        if not isinstance(session_id, str):
            continue
        if session_ids is not None and session_id not in session_ids:
            continue
        result[session_id] = row
    return result


MAX_DECISION_SUMMARY_LENGTH = 200
MAX_DECISION_REASONING_LENGTH = 500
MAX_DECISIONS_STORED = 500


# What a month of second opinions may cost before the product stops spawning
# them. Low by design: a run measured at $0.037 means this is roughly 135 of
# them, which is far more than the gate fires on in a month of real use, and the
# point of the number is to bound a runaway, not to ration ordinary use.
ANALYST_MONTHLY_CAP_USD = 5.0
# And a ceiling on runs, because the dollar cap cannot bind on every host.
# Codex reports no machine-readable cost -- AIWatcher prices its sessions at $0
# by design, and a subscription user's really is -- so a dollar-only cap would
# quietly stop limiting anything the moment Codex became a host. 150 is what the
# dollar cap buys at the measured price of a run (~$0.035), so the two ceilings
# mean roughly the same thing and whichever is reached first stops the spawning.
ANALYST_MONTHLY_RUN_CAP = 150
MAX_ANALYST_RUNS_STORED = 2000


def analyst_consent(project_path: str) -> dict[str, Any] | None:
    """Whether this project has agreed to pay for second opinions.

    Per project, and asked once. A modal on every prompt would be the kind of
    consent nobody reads, and this is a decision about one repository's budget
    rather than about the machine.
    """
    key = (project_path or "").strip()
    if not key:
        return None
    try:
        with _locked_state():
            granted = _load().get("analyst_consent") or {}
    except OSError:
        return None
    record = granted.get(key)
    return record if isinstance(record, dict) else None


def record_analyst_consent(project_path: str, *, allowed: bool) -> dict[str, Any]:
    key = (project_path or "").strip()
    if not key:
        raise ValueError("project_path is required")
    record = {
        "allowed": bool(allowed),
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    with _locked_state():
        data = _load()
        consent = data.get("analyst_consent")
        if not isinstance(consent, dict):
            consent = {}
        consent[key] = record
        data["analyst_consent"] = consent
        _save(data)
    return record


def analyst_contents_allowed(project_path: str) -> bool:
    """Whether this project lets the analyst read file contents. Off by default.

    Deliberately not folded into consent. Consent answers "may this spend my
    money"; this answers "may it open my files". A user can reasonably say yes
    to the first and no to the second, and the Settings copy promises exactly
    that -- "It sees your prompt and your file paths. Never file contents,
    unless you turn that on."
    """
    key = (project_path or "").strip()
    if not key:
        return False
    try:
        with _locked_state():
            allowed = _load().get("analyst_contents") or {}
    except OSError:
        return False
    record = allowed.get(key)
    return bool(record.get("allowed")) if isinstance(record, dict) else False


def record_analyst_contents(project_path: str, *, allowed: bool) -> dict[str, Any]:
    key = (project_path or "").strip()
    if not key:
        raise ValueError("project_path is required")
    record = {"allowed": bool(allowed), "decided_at": datetime.now(timezone.utc).isoformat()}
    with _locked_state():
        data = _load()
        contents = data.get("analyst_contents")
        if not isinstance(contents, dict):
            contents = {}
        contents[key] = record
        data["analyst_contents"] = contents
        _save(data)
    return record


def record_analyst_run(*, project_path: str, cost_usd: float,
                       session_id: str | None = None) -> dict[str, Any]:
    """Log what a spawn actually cost, for the cap to be enforced against.

    Recorded from the CLI's own reported cost after the run rather than
    estimated before it, so the counter the user is shown is the money that
    actually moved.
    """
    record = {
        "project_path": (project_path or "").strip()[:1000],
        "session_id": (session_id or "").strip()[:200] or None,
        "cost_usd": round(max(0.0, float(cost_usd or 0.0)), 6),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    with _locked_state():
        data = _load()
        runs = data.get("analyst_runs")
        if not isinstance(runs, list):
            runs = []
        runs.append(record)
        data["analyst_runs"] = runs[-MAX_ANALYST_RUNS_STORED:]
        _save(data)
    return record


def analyst_month_spend(now: datetime | None = None) -> dict[str, Any]:
    """This calendar month's second-opinion spend, and what is left of the cap.

    Calendar month, not a rolling 30 days, because the cap is a budget and a
    budget is something a person reasons about in months.
    """
    moment = now or datetime.now(timezone.utc)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        with _locked_state():
            runs = list(_load().get("analyst_runs") or [])
    except OSError:
        runs = []
    spent = 0.0
    count = 0
    for run in runs:
        if not isinstance(run, dict):
            continue
        try:
            ran_at = datetime.fromisoformat(str(run.get("ran_at")))
        except (TypeError, ValueError):
            continue
        if ran_at.tzinfo is None:
            ran_at = ran_at.replace(tzinfo=timezone.utc)
        if ran_at < start:
            continue
        spent += float(run.get("cost_usd") or 0.0)
        count += 1
    cap = ANALYST_MONTHLY_CAP_USD
    run_cap = ANALYST_MONTHLY_RUN_CAP
    by_cost = spent >= cap
    by_runs = count >= run_cap
    return {
        "runs": count,
        "spent_usd": round(spent, 6),
        "cap_usd": cap,
        "run_cap": run_cap,
        "remaining_usd": round(max(0.0, cap - spent), 6),
        "remaining_runs": max(0, run_cap - count),
        # Which ceiling stopped it, so the reason shown can name the real one
        # rather than quoting dollars at somebody whose host reports none.
        "capped_by": "cost" if by_cost else ("runs" if by_runs else None),
        # A hard stop, checked before spawning. Warning after the fact is the
        # thing this product exists to complain about.
        "capped": by_cost or by_runs,
    }


def record_decision(
    session_id: str,
    summary: str,
    reasoning: str | None = None,
    alternatives_rejected: list[str] | None = None,
) -> dict[str, Any]:
    """Store a self-reported decision entry for a session.

    Unlike evidence_snapshots, this intentionally stores real text -- the
    point is to capture "why" for decisions that never produce a commit
    (e.g. an approach that was seriously considered and rejected without
    ever being implemented). It is convention-based and self-reported by
    whoever/whatever calls it, not verified against anything that actually
    happened, so callers surfacing this should label it as such rather
    than presenting it as fact.
    """
    if not summary or not summary.strip():
        raise ValueError("summary is required")
    with _locked_state():
        data = _load()
        record = {
            "session_id": session_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary.strip()[:MAX_DECISION_SUMMARY_LENGTH],
            "reasoning": reasoning.strip()[:MAX_DECISION_REASONING_LENGTH] if reasoning and reasoning.strip() else None,
            "alternatives_rejected": [
                str(item).strip()[:MAX_DECISION_SUMMARY_LENGTH]
                for item in (alternatives_rejected or [])
                if str(item).strip()
            ][:5],
        }
        data["decisions"].append(record)
        data["decisions"] = data["decisions"][-MAX_DECISIONS_STORED:]
        _save(data)
    return record


def recent_decisions(session_id: str, limit: int = 5) -> list[dict[str, Any]]:
    try:
        with _locked_state():
            rows = list(_load()["decisions"])
    except OSError:
        return []
    matching = [row for row in rows if row.get("session_id") == session_id]
    return list(reversed(matching))[:limit]


def get_outcome(session_id: str) -> dict[str, Any] | None:
    try:
        with _locked_state():
            data = _load()
    except OSError:
        return None
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


def get_baselines() -> dict[str, Any]:
    """Read-only cache lookup -- never scans local session history.

    Safe to call from a hook's hot path: this only reads whatever is
    already stored, it never computes anything. Computing fresh baselines
    (scanning session history) belongs in cli.py, off the hot path -- see
    get_or_refresh_baselines() there.
    """
    try:
        with _locked_state():
            data = _load()
    except OSError:
        return {}
    baselines = data.get("baselines")
    return baselines if isinstance(baselines, dict) else {}


def save_baselines(baselines: dict[str, Any]) -> None:
    with _locked_state():
        data = _load()
        data["baselines"] = baselines
        _save(data)


def get_survival_summary() -> dict[str, Any]:
    """Read-only cache lookup for cost-per-surviving-line.

    Same contract as get_baselines: never computes. Measuring survival costs a
    blame pass per file -- about 23s for a month of history here -- so it can
    never run on a request path. cli.get_or_refresh_survival() does the work
    off the hot path and stores the result here.
    """
    try:
        with _locked_state():
            data = _load()
    except OSError:
        return {}
    summary = data.get("survival_summary")
    return summary if isinstance(summary, dict) else {}


def save_survival_summary(summary: dict[str, Any]) -> None:
    with _locked_state():
        data = _load()
        data["survival_summary"] = summary
        _save(data)


def get_receipt_baseline() -> dict[str, Any]:
    """Read-only cache of per-repo $/line medians for the commit receipt.

    Same contract as get_baselines: never computes. The receipt runs inside a
    post-commit hook, where a month of ledger history would add most of a
    second to every commit -- the exact cost that gets a hook uninstalled.
    cli.get_or_refresh_receipt_baseline() fills this in off the hot path.
    """
    try:
        with _locked_state():
            data = _load()
    except OSError:
        return {}
    baseline = data.get("receipt_baseline")
    return baseline if isinstance(baseline, dict) else {}


def save_receipt_baseline(baseline: dict[str, Any]) -> None:
    with _locked_state():
        data = _load()
        data["receipt_baseline"] = baseline
        _save(data)


MAX_COMMAND_DECISIONS_STORED = 500
COMMAND_GATE_BLOCKED_DECISIONS = frozenset({"block", "auto_block_headless", "gate_timeout_blocked"})


_CONNECTION_URI_RE = re.compile(
    r"\b(?P<scheme>postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|mssql)://(?P<userinfo>[^\s/@]+(?::[^\s/@]*)?)@",
    re.IGNORECASE,
)
_ENV_SECRET_RE = re.compile(
    r"\b(?P<name>[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*)=(?P<value>[^\s]+)",
    re.IGNORECASE,
)
_LONG_SECRET_FLAG_RE = re.compile(
    r"(?P<flag>--(?:password|passwd|token|api-key|secret|access-key|private-key))(?:=|\s+)(?P<value>[^\s]+)",
    re.IGNORECASE,
)
_COMMON_SECRET_VALUE_RE = re.compile(
    r"\b("
    r"sk-[A-Za-z0-9_-]{12,}|"
    r"AKIA[0-9A-Z]{12,}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}"
    r")\b"
)


def command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def redact_command_for_storage(command: str) -> tuple[str, bool]:
    """Return a receipt-safe command preview and whether anything was redacted.

    Command-gate receipts need enough context for a developer to recognize the
    blocked action, but shell commands can contain database URLs, API keys, or
    env-var assignments. Store a redacted preview plus a hash instead of raw
    secret-bearing command text.
    """
    preview = str(command)
    preview = _CONNECTION_URI_RE.sub(lambda m: f"{m.group('scheme')}://[redacted]@", preview)
    preview = _ENV_SECRET_RE.sub(lambda m: f"{m.group('name')}=[redacted]", preview)
    preview = _LONG_SECRET_FLAG_RE.sub(lambda m: f"{m.group('flag')} [redacted]", preview)
    preview = _COMMON_SECRET_VALUE_RE.sub("[redacted-token]", preview)
    return preview, preview != command


def record_command_decision(
    *,
    tool: str,
    command: str,
    pattern_id: str,
    reason: str,
    decision: str,
    session_id: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Store a dangerous-command gate decision (S-19).

    Receipts store a privacy-safe command preview plus a stable command hash.
    They must not persist raw secret-bearing command strings such as production
    connection URLs or API tokens.
    """
    command_preview, was_redacted = redact_command_for_storage(command)
    with _locked_state():
        data = _load()
        record = {
            "id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "cwd": cwd,
            "command": command_preview,
            "command_hash": command_hash(command),
            "command_redacted": was_redacted,
            "pattern_id": pattern_id,
            "reason": reason,
            "decision": decision,
            "session_id": session_id,
        }
        data["command_decisions"].append(record)
        data["command_decisions"] = data["command_decisions"][-MAX_COMMAND_DECISIONS_STORED:]
        _save(data)
    return record


def recent_command_decisions(limit: int = 20, days: int | None = None) -> list[dict[str, Any]]:
    try:
        with _locked_state():
            rows = list(_load()["command_decisions"])
    except OSError:
        return []
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


def is_command_pattern_always_allowed(pattern_id: str) -> bool:
    try:
        with _locked_state():
            data = _load()
    except OSError:
        return False
    return pattern_id in data.get("command_gate_allowlist", [])


def record_always_allow_command_pattern(pattern_id: str) -> None:
    with _locked_state():
        data = _load()
        allowlist = data.setdefault("command_gate_allowlist", [])
        if pattern_id not in allowlist:
            allowlist.append(pattern_id)
        _save(data)


def first_run_dismissed_at() -> str | None:
    """When the first-run screen was dismissed, if it has been."""
    try:
        return _load().get("first_run_dismissed_at")
    except StateReadError:
        # Unreadable state is not "never dismissed". Re-showing onboarding
        # because a file could not be parsed is worse than not showing it.
        return datetime.now(timezone.utc).isoformat()


def dismiss_first_run() -> str:
    """Record that the first-run screen has been seen, so it does not return."""
    now = datetime.now(timezone.utc).isoformat()
    with _locked_state():
        data = _load()
        if not data.get("first_run_dismissed_at"):
            data["first_run_dismissed_at"] = now
            _save(data)
        else:
            now = str(data["first_run_dismissed_at"])
    return now
