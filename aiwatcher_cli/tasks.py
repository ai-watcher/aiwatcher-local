"""Tasks: prompt-bounded runs of work inside a session.

A session is a unit of tooling. The unit of intent is smaller: "review the PR",
then "now fix the pricing table", often in the same thread. This module splits a
session's turns into those runs so that turns, tokens, cost and tool calls can be
counted per piece of work, and so the things AIWatcher did (an accepted brief, a
Fresh Start) and the things the work produced (commits) can be attached to the
task they belong to.

Boundaries are found by plain rules over the prompt text -- no model. Every task
carries how its boundary was found and how sure the rules were, and the user's
own merge/split corrections override the rules by turn number. Tasks are derived
on every build from the transcripts, like sessions; only the corrections and the
user's verdicts are persisted.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from .scanner import (
    LocalSession,
    _parse_ts,
    extract_session_title,
    segment_codex_session_by_prompt,
    segment_session_by_prompt,
)

CLAUDE_TOOLS = {"claude-code", "claude"}
CODEX_TOOLS = {"codex-cli", "codex"}

# A prompt that opens with an instruction rather than a reaction.
FRESH_OPENER_RE = re.compile(
    r"^(now|next|ok(ay)?[,.]?\s+now|separately|new task|let'?s|also[,]?\s|switching|"
    r"different (thing|topic)|can you|could you|please|i want|i need|help me|build|add|fix|write|"
    r"review|implement|refactor|create|make|update|remove|delete|investigate|look into|check|"
    r"go through|spin up|run|scope|design|explore|compare|migrate|rename|set up|setup)\b",
    re.IGNORECASE,
)
# A prompt that leans on what was just said cannot be starting something new.
ANAPHORA_RE = re.compile(
    r"\b(that|it|this|those|these|the above|same|again|still|and then|why|what about|yes|no|ok|"
    r"okay|go ahead|do it|continue|proceed|try|instead|too|either|the other)\b",
    re.IGNORECASE,
)
# Things a prompt can point at: files, PRs, issues, code identifiers.
REFERENCE_RE = re.compile(
    r"(#\d+|\bPRs?\s*#?\d+|\bpull request\b|[\w./-]+\.(?:py|js|ts|tsx|md|css|html|swift|json|yml|yaml|toml)\b|`[^`\n]+`)",
    re.IGNORECASE,
)
ATTACHMENT_RE = re.compile(r'^@"(?P<path>[^"]+)"')

SHORT_PROMPT_CHARS = 12          # "yes", "go ahead", "ok" -- never a task
LONG_FRESH_PROMPT_CHARS = 400    # a long prompt with no back-reference is a brief, not a follow-up
GAP_BOUNDARY = timedelta(hours=6)  # a pause this long, then a non-reactive prompt, is new work
COMMIT_ATTACH_LOOKBACK = timedelta(hours=12)  # same window the ledger banks events with
TWIN_SESSION_WINDOW = timedelta(minutes=2)    # Claude Desktop forks a session under a second id
OPEN_TASK_WINDOW = timedelta(minutes=30)      # matches the presence "gone" threshold
LABEL_WORDS = 8
MIN_TASKS_FOR_SIZING = 6

VALID_VERDICTS = {"done", "not_done"}
PROMPT_MODIFIED_DECISIONS = {"brief_accepted", "brief_edited", "auto_brief_headless", "context_added"}
FRESH_START_TAKEN = {"new_chat", "copy_handoff"}


def task_id_for(session_id: str, start_turn: int) -> str:
    return hashlib.sha256(f"{session_id}|{start_turn}".encode("utf-8")).hexdigest()[:12]


def label_for(prompt: str) -> str:
    """A few words a human recognises: the opening line, trimmed."""
    first = prompt.strip().splitlines()[0] if prompt.strip() else ""
    attached = ATTACHMENT_RE.match(first)
    if attached:
        return "Attached " + attached.group("path").replace("\\", "/").rstrip("/").split("/")[-1]
    first = re.sub(r"\s+", " ", first).strip(" .:-")
    words = first.split(" ")
    return " ".join(words[:LABEL_WORDS]) + ("…" if len(words) > LABEL_WORDS else "")


def _references(text: str) -> set[str]:
    return {match.group(0).lower() for match in REFERENCE_RE.finditer(text)}


def detect_boundary(
    prompt: str,
    *,
    index: int,
    previous_references: set[str],
    gap: timedelta | None,
) -> tuple[bool, str, str]:
    """Decide whether `prompt` starts a new task. Returns (boundary, method, confidence)."""
    text = prompt.strip()
    if index == 0:
        return True, "session_start", "high"
    if len(text) < SHORT_PROMPT_CHARS:
        return False, "rules", "high"
    head = text[:160]
    opener = bool(FRESH_OPENER_RE.match(head))
    anaphoric = bool(ANAPHORA_RE.search(head[:60]))
    references = _references(text)
    new_reference = bool(references) and not (references & previous_references)
    long_fresh = len(text) >= LONG_FRESH_PROMPT_CHARS and not anaphoric
    if (opener and new_reference) or long_fresh:
        return True, "rules", "high"
    if opener and not anaphoric:
        return True, "rules", "medium"
    if gap is not None and gap >= GAP_BOUNDARY and not anaphoric:
        return True, "rules", "medium"
    if new_reference and not anaphoric:
        return True, "rules", "low"
    return False, "rules", "high"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _stamp(segment: dict[str, object]) -> datetime | None:
    return _parse_ts(segment.get("at"))


def build_session_tasks(
    session: LocalSession,
    segments: Sequence[dict[str, object]],
    *,
    overrides: dict[int, bool] | None = None,
    session_title: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Split one session's prompt segments into tasks.

    `overrides` maps a turn number to a user decision: True forces a boundary at
    that turn (a split), False suppresses one (a merge). A user decision beats
    the rules and is labelled as such.
    """
    now = now or datetime.now(timezone.utc)
    overrides = overrides or {}
    tasks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    previous_references: set[str] = set()
    previous_stamp: datetime | None = None
    for index, segment in enumerate(segments):
        prompt = str(segment.get("prompt") or "")
        turn = int(segment.get("turn") or (index + 1))
        stamp = _stamp(segment)
        gap = (stamp - previous_stamp) if (stamp and previous_stamp) else None
        boundary, method, confidence = detect_boundary(
            prompt, index=index, previous_references=previous_references, gap=gap
        )
        corrected_here = turn in overrides and index > 0
        if corrected_here:
            boundary, method, confidence = overrides[turn], "user", "confirmed"
        if boundary or current is None:
            if current is not None:
                current["ended_at"] = _iso(stamp) or current["ended_at"]
            current = {
                "id": task_id_for(session.session_id, turn),
                "session_id": session.session_id,
                "tool": session.tool,
                "surface": session.surface,
                "project_path": session.project_path,
                "session_title": session_title,
                "label": label_for(prompt),
                "start_turn": turn,
                "end_turn": turn,
                "turns": 0,
                "tokens": 0,
                "cost_usd": 0.0,
                "tool_calls": 0,
                "started_at": _iso(stamp) or _iso(session.started_at),
                "ended_at": _iso(session.updated_at),
                "boundary_method": method,
                "confidence": confidence,
                # True when the user's own split/merge shaped this task -- a
                # merge lives inside the task it produced, not at its start.
                "corrected": method == "user",
                "turn_details": [],
                "commits": [],
                "pull_requests": [],
                "interventions": [],
                "verdict": None,
                "status": "ended",
                "size": "unsized",
            }
            tasks.append(current)
            previous_references = set()
        if corrected_here and not boundary:
            current["corrected"] = True
        current["turns"] += 1
        current["end_turn"] = turn
        current["tokens"] += int(segment.get("tokens") or 0)
        current["cost_usd"] += float(segment.get("cost_usd") or 0.0)
        current["tool_calls"] += int(segment.get("tool_calls") or 0)
        current["turn_details"].append(
            {"turn": turn, "label": label_for(prompt), "tokens": int(segment.get("tokens") or 0), "at": _iso(stamp)}
        )
        previous_references |= _references(prompt)
        previous_stamp = stamp or previous_stamp
    for task in tasks:
        task["cost_usd"] = round(task["cost_usd"], 4)
    if tasks and session.updated_at and (now - session.updated_at) <= OPEN_TASK_WINDOW:
        tasks[-1]["status"] = "open"
    return tasks


FRESH_BOUNDARY_WINDOW = timedelta(minutes=10)


def _segments_for(row: LocalSession) -> list[dict[str, object]]:
    transcript = bool(row.source_path) and str(row.source_path).endswith(".jsonl")
    if transcript and row.tool in CLAUDE_TOOLS:
        return segment_session_by_prompt(row.source_path)
    if transcript and row.tool in CODEX_TOOLS:
        return segment_codex_session_by_prompt(row.source_path)
    return []


def find_fresh_boundaries(
    rows: Sequence[LocalSession],
    *,
    overrides: dict[str, dict[int, bool]] | None = None,
    baseline: dict[str, int],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Tasks that just closed because a new one opened in the same session.

    `baseline` is the task count per session from the previous look; it is
    updated in place. A session seen for the first time sets its baseline and
    asks nothing -- history is not a reason to interrupt anyone. A count that
    grew because the user split old work in the dashboard is filtered out by
    requiring the new task to have started within FRESH_BOUNDARY_WINDOW.
    """
    now = now or datetime.now(timezone.utc)
    overrides = overrides or {}
    asks: list[dict[str, Any]] = []
    for row in rows:
        segments = _segments_for(row)
        if not segments:
            continue
        session_tasks = build_session_tasks(row, segments, overrides=overrides.get(row.session_id), now=now)
        count = len(session_tasks)
        previous = baseline.get(row.session_id)
        baseline[row.session_id] = count
        if previous is None or count <= previous or count < 2:
            continue
        closed, opened = session_tasks[-2], session_tasks[-1]
        started = _parse_ts(opened.get("started_at"))
        if not started or (now - started) > FRESH_BOUNDARY_WINDOW:
            continue
        asks.append(
            {
                "task_id": closed["id"],
                "session_id": row.session_id,
                "tool": row.tool,
                "project_path": row.project_path,
                "label": closed["label"],
                "turns": closed["turns"],
                "tokens": closed["tokens"],
                "cost_usd": closed["cost_usd"],
                "tool_calls": closed["tool_calls"],
                "boundary_turn": opened["start_turn"],
                "confidence": opened["confidence"],
            }
        )
    return asks


def _twin_alias_map(rows: Sequence[LocalSession], openers: dict[str, str]) -> dict[str, str]:
    """Map a forked copy of a session (same opening prompt, same start minute) to the canonical id.

    Claude Desktop writes a resumed session under a new UUID with the same history.
    Counting both doubles every number, and interventions can be recorded against
    either id, so the later-updated copy wins and the other becomes an alias.
    """
    groups: dict[tuple[str, str], list[LocalSession]] = {}
    for row in rows:
        opener = openers.get(row.session_id)
        if not opener or not row.started_at:
            continue
        key = (opener, row.started_at.replace(second=0, microsecond=0).isoformat())
        groups.setdefault(key, []).append(row)
    alias: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        canonical = max(members, key=lambda row: row.updated_at or row.started_at or datetime.min.replace(tzinfo=timezone.utc))
        for row in members:
            if row.session_id != canonical.session_id:
                alias[row.session_id] = canonical.session_id
    return alias


def _size_tasks(tasks: list[dict[str, Any]]) -> None:
    """Small / medium / large by terciles of this user's own tasks, or unsized when too few.

    Self-referential on purpose: there is no external notion of a "big task", and a
    round token threshold would fire the same way for every user. Below
    MIN_TASKS_FOR_SIZING the buckets would be noise, so nothing is claimed.
    """
    sized = [task for task in tasks if task["tokens"] > 0]
    if len(sized) < MIN_TASKS_FOR_SIZING:
        return
    ordered = sorted(task["tokens"] for task in sized)
    lower = ordered[len(ordered) // 3]
    upper = ordered[(2 * len(ordered)) // 3]
    for task in sized:
        task["size"] = "small" if task["tokens"] < lower else "large" if task["tokens"] >= upper else "medium"


def _within(task: dict[str, Any], when: datetime, *, after_end: timedelta) -> bool:
    started = _parse_ts(task.get("started_at"))
    ended = _parse_ts(task.get("ended_at"))
    if not started:
        return False
    return started <= when <= ((ended or started) + after_end)


def _attach_to_task(tasks: list[dict[str, Any]], when: datetime, *, after_end: timedelta) -> dict[str, Any] | None:
    active = [task for task in tasks if _within(task, when, after_end=after_end)]
    if not active:
        return None
    return max(active, key=lambda task: _parse_ts(task.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc))


def attach_commits(tasks: list[dict[str, Any]], changes: Iterable[Any], alias: dict[str, str]) -> None:
    """Bank each ledger Change on exactly one task: the one open when it landed."""
    by_session: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        by_session.setdefault(task["session_id"], []).append(task)
    for change in changes:
        landed = getattr(change, "landed_at", None)
        if not landed:
            continue
        candidates: list[dict[str, Any]] = []
        for session_id in getattr(change, "session_ids", []) or []:
            candidates.extend(by_session.get(alias.get(session_id, session_id), []))
        target = _attach_to_task(candidates, landed, after_end=COMMIT_ATTACH_LOOKBACK)
        if target is None:
            continue
        target["commits"].append(
            {
                "sha": str(getattr(change, "sha", ""))[:12],
                "subject": str(getattr(change, "subject", ""))[:120],
                "landed_at": _iso(landed),
                "repo": getattr(change, "repo", None),
            }
        )


def attach_pull_requests(tasks: list[dict[str, Any]], pull_requests: Iterable[dict[str, Any]]) -> None:
    """Bank each PR on the task open in that repository when it was opened.

    A PR is matched by repository and time, not by branch: the task that was
    running when you pressed "create pull request" is the one that produced it.
    """
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        root = task.get("repo_root") or task.get("project_path")
        if root:
            by_repo.setdefault(str(root), []).append(task)
    for pull_request in pull_requests:
        opened = _parse_ts(pull_request.get("opened_at"))
        root = str(pull_request.get("repo_root") or "")
        if not opened or root not in by_repo:
            continue
        target = _attach_to_task(by_repo[root], opened, after_end=COMMIT_ATTACH_LOOKBACK)
        if target is None:
            continue
        target.setdefault("pull_requests", []).append(
            {
                "number": pull_request.get("number"),
                "title": pull_request.get("title"),
                "url": pull_request.get("url"),
                "state": pull_request.get("state"),
                "opened_at": pull_request.get("opened_at"),
                "merged_at": pull_request.get("merged_at"),
            }
        )


def attach_interventions(
    tasks: list[dict[str, Any]],
    interventions: Iterable[dict[str, Any]],
    handoffs: Iterable[dict[str, Any]],
    alias: dict[str, str],
) -> None:
    """Attach what AIWatcher actually changed: an applied brief, or a Fresh Start the user took.

    A gate that fired and was ignored is not an intervention and is not attached.
    """
    by_session: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        by_session.setdefault(task["session_id"], []).append(task)
    for row in interventions:
        if row.get("decision") not in PROMPT_MODIFIED_DECISIONS or not row.get("session_id"):
            continue
        when = _parse_ts(row.get("created_at"))
        if not when:
            continue
        session_tasks = by_session.get(alias.get(str(row["session_id"]), str(row["session_id"])), [])
        # The hook fires just before the prompt line is written, hence the small lead.
        target = _attach_to_task(session_tasks, when + timedelta(minutes=2), after_end=timedelta(0))
        if target is None:
            continue
        target["interventions"].append(
            {
                "kind": "prompt_brief",
                "decision": row.get("decision"),
                "at": row.get("created_at"),
                "score": row.get("score"),
                "selected_score": row.get("selected_score"),
            }
        )
    for row in handoffs:
        if row.get("decision") not in FRESH_START_TAKEN or not row.get("source_session_id"):
            continue
        when = _parse_ts(row.get("created_at"))
        session_tasks = by_session.get(alias.get(str(row["source_session_id"]), str(row["source_session_id"])), [])
        target = (_attach_to_task(session_tasks, when, after_end=COMMIT_ATTACH_LOOKBACK) if when else None) or (
            session_tasks[-1] if session_tasks else None
        )
        if target is None:
            continue
        correlation = row.get("next_session_correlation") or {}
        target["interventions"].append(
            {
                "kind": "fresh_start",
                "decision": row.get("decision"),
                "at": row.get("created_at"),
                "next_session_id": row.get("next_session_id"),
                "link_status": correlation.get("status"),
            }
        )


def build_tasks(
    rows: Sequence[LocalSession],
    *,
    overrides: dict[str, dict[int, bool]] | None = None,
    verdicts: dict[str, str] | None = None,
    changes: Iterable[Any] = (),
    pull_requests: Iterable[dict[str, Any]] = (),
    interventions: Iterable[dict[str, Any]] = (),
    handoffs: Iterable[dict[str, Any]] = (),
    repo_roots: dict[str, str] | None = None,
    turn_ends: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Derive tasks for every session that has a readable transcript.

    Sessions without one (Codex threads from the sqlite fallback, Cursor) are
    reported in `unmeasurable` with the reason rather than counted as zero tasks.
    """
    now = now or datetime.now(timezone.utc)
    overrides = overrides or {}
    verdicts = verdicts or {}
    segments_by_session: dict[str, list[dict[str, object]]] = {}
    openers: dict[str, str] = {}
    unmeasurable: list[dict[str, str]] = []
    for row in rows:
        transcript = bool(row.source_path) and str(row.source_path).endswith(".jsonl")
        if not transcript or row.tool not in CLAUDE_TOOLS | CODEX_TOOLS:
            reason = "Codex thread from the sqlite fallback: no per-prompt rollout" if row.tool in CODEX_TOOLS else "no per-prompt transcript to split"
            unmeasurable.append({"session_id": row.session_id, "tool": row.tool, "reason": reason})
            continue
        segments = _segments_for(row)
        if not segments:
            unmeasurable.append({"session_id": row.session_id, "tool": row.tool, "reason": "no readable user prompt"})
            continue
        segments_by_session[row.session_id] = segments
        openers[row.session_id] = label_for(str(segments[0].get("prompt") or ""))
    alias = _twin_alias_map([row for row in rows if row.session_id in segments_by_session], openers)
    tasks: list[dict[str, Any]] = []
    ordered = sorted(
        (row for row in rows if row.session_id in segments_by_session and row.session_id not in alias),
        key=lambda row: row.updated_at or row.started_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for row in ordered:
        tasks.extend(
            build_session_tasks(
                row,
                segments_by_session[row.session_id],
                overrides=overrides.get(row.session_id),
                session_title=extract_session_title(row.source_path) if row.tool in CLAUDE_TOOLS else None,
                now=now,
            )
        )
    for task in tasks:
        task["repo_root"] = (repo_roots or {}).get(str(task["project_path"] or ""), task["project_path"])
    attach_commits(tasks, changes, alias)
    attach_pull_requests(tasks, pull_requests)
    attach_interventions(tasks, interventions, handoffs, alias)
    _size_tasks(tasks)
    for task in tasks:
        verdict = verdicts.get(task["id"])
        task["verdict"] = verdict if verdict in VALID_VERDICTS else None
        if task["verdict"] == "done":
            task["status"] = "done"
        elif task["verdict"] == "not_done" and task["status"] != "open":
            task["status"] = "kept_open"
        elif task["status"] == "open" and turn_ends:
            # The Stop hook says the last prompt has been answered: the task is
            # open but nobody is working on it right now.
            ended = _parse_ts((turn_ends.get(task["session_id"]) or {}).get("at"))
            last_prompt = _parse_ts((task["turn_details"] or [{}])[-1].get("at"))
            if ended and last_prompt and ended > last_prompt:
                task["status"] = "idle"
    return {
        "tasks": tasks,
        "session_count": len(ordered),
        "twin_sessions_folded": len(alias),
        "unmeasurable": unmeasurable,
        "sized": any(task["size"] != "unsized" for task in tasks),
    }
