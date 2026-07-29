"""Private local state for AIWatcher interventions and outcomes."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
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
        "command_decisions": [],
        "command_gate_allowlist": [],
        "brief_tokens": [],
        "watch_notifications": [],
        "sent_notification_keys": [],
        "ui_server": None,
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
    data.setdefault("command_decisions", [])
    data.setdefault("command_gate_allowlist", [])
    data.setdefault("brief_tokens", [])
    data.setdefault("watch_notifications", [])
    data.setdefault("sent_notification_keys", [])
    data.setdefault("ui_server", None)
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


def recent_watch_notifications(limit: int = 10) -> list[dict[str, Any]]:
    try:
        with _locked_state():
            data = _load()
    except OSError:
        return []
    rows = [row for row in data["watch_notifications"] if isinstance(row, dict)]
    return list(reversed(rows[-max(1, limit):]))


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
    with _locked_state():
        rows = list(_load()["decisions"])
    matching = [row for row in rows if row.get("session_id") == session_id]
    return list(reversed(matching))[:limit]


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


MAX_COMMAND_DECISIONS_STORED = 500
COMMAND_GATE_BLOCKED_DECISIONS = frozenset({"block", "auto_block_headless", "gate_timeout_blocked"})


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

    Unlike prompt interventions (which store only a hash of the prompt),
    this intentionally stores the real command text -- the scenario spec
    requires "decision recorded with full command text", and a shell
    command is not private prompt/source content the same way a prompt is.
    record_decision() above already sets this precedent for commit-message-
    style text.
    """
    with _locked_state():
        data = _load()
        record = {
            "id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "cwd": cwd,
            "command": command,
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
