"""Compact at the boundary.

A session that keeps going after a commit replays that commit's whole history
on every turn, and almost none of it is needed for what comes next. The right
moment to compact is that boundary -- not some share of the model's window,
which is where the tools' own auto-compaction fires and which lands mid-task.

Everything here is observed, not chosen: the boundary is the last commit on
HEAD, the history to shed is what the session had accumulated by then, the
floor is the session's own first-turn context (what a fresh context costs on
this machine, with this CLAUDE.md and these tools), and the estimate for the
turn after compacting is that floor plus the work since the commit.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import statusline, transcript_cache
from .git_identity import identity_for_session, resolve_git_identity
from .pricing import cache_read_cost, is_subscription_model, lookup
from .scanner import LocalSession, _codex_user_prompt_text, _parse_ts
from .session_health import CONTEXT_RESET_DROP_RATIO

_REFLOG_LINE = re.compile(
    r"^(?P<old>[0-9a-f]{40}) (?P<new>[0-9a-f]{40}) (?P<who>[^<]*)<[^>]*> (?P<ts>\d+) (?P<tz>[+-]\d{4})\t(?P<action>.*)$"
)
# Reflog actions that created the commit they point at, so their message is
# its subject. A checkout or reset moves HEAD to a commit made elsewhere and
# carries that movement's description instead.
_CREATING_ACTIONS = ("commit", "merge", "cherry-pick", "pull", "revert", "rebase")
_MAX_FILES_IN_COMMAND = 4


@dataclass
class Boundary:
    sha: str
    subject: str
    committed_at: datetime


@dataclass
class Assessment:
    session_id: str
    tool: str
    model: str | None
    sha: str
    subject: str
    committed_at: str
    turns_since_commit: int     # model calls since the commit
    prompts_since_commit: int   # things the user typed since the commit
    latest_turn_tokens: int
    context_at_commit: int
    # The session's own first call: system prompt, tools, CLAUDE.md -- what a
    # fresh context costs here. The floor a compaction cannot get under.
    first_turn_tokens: int
    dead_tokens: int          # history that predates the commit, above the floor
    since_tokens: int         # what the commit did not cover and must be kept
    after_estimate: int       # first_turn_tokens + since_tokens
    files_since: list[str]
    command: str
    priced: bool
    dead_usd_per_turn: float | None
    recommend: bool
    reason: str
    # Where the session is in the act of compacting, read from its own log:
    #   nudge       a good moment, nothing done yet (recommend is True)
    #   compacting  the user typed /compact; the tool has not finished
    #   compacted   the tool wrote the boundary; the next reply shows the size
    #   confirmed   that reply landed -- context_after_shed is the new size.
    #               Holds until the user types again: Claude Code carries on
    #               by itself after a compaction, so counting replies gave
    #               this step six seconds (2026-09-09, 14:10:56 to 14:11:02)
    #               and nobody saw it. "Again" means after that reply: a
    #               prompt typed between the boundary and the reply (the
    #               user answering a bar that says Compacted with no sizes
    #               yet) is what produces the reply, and does not end the
    #               step that shows its sizes.
    #   none        nothing to say
    # Claude Code, as observed on 2026-09-09, appends the /compact row only
    # once the compaction has finished (it carries the command's stdout), so
    # on that tool the first visible step is `compacted`, about a minute
    # after enter; `compacting` is kept for a log that does write the
    # command first. Either way the bar moves a reply earlier than it could
    # from usage alone.
    # `copied` is not here: a click is a receipt, not a fact in the log, so
    # the surface overlays it from local state.
    stage: str = "none"
    title: str | None = None            # the name the user gave the session, when the tool records one
    command_seen_at: str | None = None
    boundary_seen_at: str | None = None
    context_before_shed: int = 0        # for `confirmed`: the size the reply before the drop replayed
    context_after_shed: int = 0         # for `confirmed`: the size the first reply after it replayed

    def __post_init__(self) -> None:
        # A recommendation is a nudge unless the log already shows a later
        # step; keeps a hand-built Assessment from saying "recommend, but
        # nothing to show".
        if self.stage == "none" and self.recommend:
            self.stage = "nudge"

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def _stage(
    *,
    recommend: bool,
    shed: bool,
    prompts_after_shed: int,
    command_at: datetime | None,
    boundary_at: datetime | None,
) -> str:
    """The lifecycle rule. Each step starts on a line the tool wrote, in the
    order the tool writes them, and the last ends on a line the user wrote;
    nothing here is a timer."""
    if shed:
        return "confirmed" if prompts_after_shed == 0 else "none"
    if boundary_at is not None and (command_at is None or boundary_at >= command_at):
        return "compacted"
    if command_at is not None:
        return "compacting"
    return "nudge" if recommend else "none"


def _git_dir(repo: Path) -> Path | None:
    dot_git = repo / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        # A worktree: `.git` is a pointer to the real gitdir, which keeps its
        # own HEAD and reflog.
        try:
            text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text[len("gitdir:"):].strip())
            return target if target.is_absolute() else (repo / target).resolve()
    return None


def _subject(action: str) -> str:
    verb, _, message = action.partition(":")
    return message.strip() if verb.split(" ")[0] in _CREATING_ACTIONS else ""


def head_commit(repo: str | None) -> Boundary | None:
    """The commit HEAD points at, read from the reflog with no git subprocess.

    This runs on the Companion's three-second poll, where the standing rule is
    no per-directory shell-outs. The last reflog line names HEAD's sha and when
    it got there; the subject comes from the line that created that sha, which
    is the same line unless HEAD was moved there by a checkout or reset.
    """
    if not repo:
        return None
    git_dir = _git_dir(Path(repo))
    if git_dir is None:
        return None
    try:
        with open(git_dir / "logs" / "HEAD", "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65_536))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    entries = [m for m in (_REFLOG_LINE.match(line) for line in tail.splitlines()) if m]
    if not entries:
        return None
    last = entries[-1]
    sha = last.group("new")
    if not sha.strip("0"):
        return None
    subject = _subject(last.group("action"))
    stamp = int(last.group("ts"))
    if not subject:
        for entry in reversed(entries[:-1]):
            if entry.group("new") == sha and _subject(entry.group("action")):
                subject = _subject(entry.group("action"))
                stamp = int(entry.group("ts"))
                break
    return Boundary(sha=sha, subject=subject, committed_at=datetime.fromtimestamp(stamp, timezone.utc))


_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$", re.M)


def _codex_patch_paths(payload: dict[str, Any]) -> list[str]:
    """Files named by an apply_patch call. Shell commands are not mined for
    paths: a path-shaped token in a command is a guess, and these only feed
    the optional preservation message rather than Codex command arguments."""
    if payload.get("name") not in {"apply_patch", "applyPatch"}:
        return []
    text = payload.get("input")
    if not isinstance(text, str):
        arguments = payload.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = None
        text = (arguments or {}).get("input") if isinstance(arguments, dict) else None
    return _PATCH_FILE.findall(text) if isinstance(text, str) else []


def codex_boundary_stats(path: str, *, since: datetime | None = None) -> dict[str, Any]:
    """The boundary view of a Codex rollout, shaped like statusline.read_transcript.

    Per-call context is `last_token_usage.input_tokens` on token_count events
    -- Codex counts cached tokens inside it, so it is the whole replayed
    prompt, the same quantity Claude Code's cache buckets sum to. Repeated
    totals are one call reported twice, as in the scanner. Without `since`
    only the whole-session figures (latest, peak, first, model) are filled,
    which is what the Companion meter needs.
    """
    def initial() -> dict[str, Any]:
        return {
            "stats": {
                "available": False, "latest_context": 0, "peak_context": 0, "first_context": 0,
                "turns": 0, "model": None, "context_at_since": 0, "min_context_since": 0,
                "turns_since": 0, "prompts_since": 0, "files_since": [],
                # Codex has no observed title or in-progress compact marker;
                # the lifecycle falls back to observing a context drop.
                "title": None, "command_seen_at": None, "boundary_seen_at": None,
                "turns_after_min_since": 0, "prompts_after_min_since": 0, "context_before_min_since": 0,
            },
            "previous_total": -1, "previous_context": 0,
        }

    def consume(s: dict[str, Any], row: dict[str, Any]) -> None:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        row_type = row.get("type")
        stamp = _parse_ts(row.get("timestamp"))
        after = since is not None and stamp is not None and stamp >= since
        if row_type == "turn_context" and payload.get("model"):
            s['stats']["model"] = str(payload["model"])
        if after and row_type == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
            for file_path in _codex_patch_paths(payload):
                if file_path not in s['stats']["files_since"]:
                    s['stats']["files_since"].append(file_path)
        if after and _codex_user_prompt_text(row_type, payload):
            s['stats']["prompts_since"] += 1
            s['stats']["prompts_after_min_since"] += 1
        if row_type != "event_msg" or payload.get("type") != "token_count":
            return
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
        last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
        total_tokens = int(total.get("total_tokens") or 0)
        if not total_tokens or total_tokens == s['previous_total']:
            return
        s['previous_total'] = total_tokens
        context = int(last.get("input_tokens") or 0)
        if context <= 0:
            return
        s['stats']["turns"] += 1
        s['stats']["latest_context"] = context
        s['stats']["peak_context"] = max(s['stats']["peak_context"], context)
        if not s['stats']["first_context"]:
            s['stats']["first_context"] = context
        if after:
            s['stats']["turns_since"] += 1
            if not s['stats']["min_context_since"] or context < s['stats']["min_context_since"]:
                s['stats']["min_context_since"] = context
                s['stats']["context_before_min_since"] = s['previous_context']
                s['stats']["turns_after_min_since"] = 0
                s['stats']["prompts_after_min_since"] = 0
            else:
                s['stats']["turns_after_min_since"] += 1
        elif since is not None:
            s['stats']["context_at_since"] = context
        s['previous_context'] = context

    with transcript_cache.folded_jsonl(path, ("codex-usage", since), initial, consume) as aggregate:
        s = aggregate if aggregate is not None else initial()
        result = dict(s['stats'])
        result['files_since'] = list(result['files_since'])
        result['available'] = result['turns'] > 0
        return result


def _relative(path: str, root: str | None) -> str:
    """Repo-relative, forward-slashed: the path goes into text the model reads,
    not into a filesystem call, so it is spelled the same on every OS."""
    if not os.path.isabs(path):
        return path.replace(os.sep, "/")
    if root:
        try:
            return Path(path).relative_to(root).as_posix()
        except ValueError:
            pass
    return os.path.basename(path)


def compact_command(
    tool: str,
    boundary: Boundary,
    files: list[str],
    *,
    session_title: str | None = None,
    prompts_since: int = 0,
) -> str:
    """The text Copy puts on the clipboard.

    Claude Code's /compact takes free-text focus, so the command says what to
    keep -- the boundary and the files touched since -- and what to drop.
    For Codex, keep the native command separate rather than assume inline
    focus arguments; the preservation message is an optional prior step.
    """
    if "codex" in tool.lower():
        return "/compact"
    short = boundary.sha[:7]
    subject = " ".join(boundary.subject.split())[:72]
    named = f'commit {short} ("{subject}")' if subject else f"commit {short}"
    if files:
        shown = [" ".join(str(path).split())[:180] for path in files[:_MAX_FILES_IN_COMMAND]]
        more = len(files) - len(shown)
        work = ", ".join(shown) + (f" and {more} more file{'s' if more != 1 else ''}" if more else "")
    else:
        work = "no file paths were observed"
    title = " ".join(str(session_title or "").split())[:100]
    session_anchor = f" Session: {title}." if title else ""
    prompt_anchor = f" {prompts_since} user prompt{'s' if prompts_since != 1 else ''} followed the boundary." if prompts_since else ""
    return (
        f"/compact Create a durable continuation summary for this same session. Boundary: {named}."
        f" Treat it as a checkpoint, not proof that all relevant work was committed.{session_anchor}{prompt_anchor}"
        f" Files touched since the boundary: {work}. Preserve: the current objective and latest user request;"
        " completed work since the boundary; current implementation and uncommitted state; decisions and their reasoning;"
        " constraints, failed approaches, blockers, risks, and unresolved questions; important files, commands, tool results,"
        " and verification; the exact smallest next action and done criteria. Output sections: Objective; Completed work;"
        " Current state; Decisions and constraints; Risks and open questions; Important files and evidence; Verification;"
        " Next action; Done criteria. Summarise or drop history before the boundary unless it still explains an active"
        " requirement, decision, constraint, or failure. Do not invent missing facts; label uncertainty explicitly."
    )



def preservation_prompt(assessment: Assessment) -> str:
    """Optional same-chat checkpoint, separate from the native slash command.

    Only observed metadata is embedded; the source AI supplies intent from
    its own conversation. A commit is an anchor, never completion proof.
    """
    title = " ".join((assessment.title or assessment.session_id).split())[:160]
    files = ", ".join(" ".join(str(path).split())[:180] for path in assessment.files_since[:12]) or "No file paths observed"
    if len(assessment.files_since) > 12:
        files += f"; {len(assessment.files_since) - 12} more paths omitted"
    return (
        "Prepare a concise continuation checkpoint in this same chat before I compact it. "
        "Do not edit files, run cleanup, or start new work.\n"
        f"AIWatcher target: {title}; session {assessment.session_id}; tool {assessment.tool}.\n"
        f"Observed checkpoint: {assessment.sha[:12]} ({' '.join(assessment.subject.split())[:240]}). "
        "This commit does not prove the task is complete or all changes are committed.\n"
        f"Observed paths touched since checkpoint: {files}.\n"
        "Use this conversation to preserve the actual objective and latest requested outcome; "
        "completed work versus unfinished/uncommitted work; decisions and constraints; "
        "failed approaches and blockers; important file paths and verification results; "
        "and the smallest next action with its acceptance criteria. Label missing facts as unknown. "
        "Keep secrets out. End with what must survive compaction and wait for me. "
        "This checkpoint itself does not compact the chat; I will invoke the native /compact separately."
    )


def assess(session: LocalSession) -> Assessment | None:
    """Is now a good moment for this session to compact, and what would it shed?

    None when the question cannot be asked: no transcript, no repo, no commit
    on HEAD, or a transcript neither reader understands. Otherwise an
    Assessment whose `recommend` says whether a nudge is warranted and whose
    `reason` says why not when it is not -- both are facts the Watch card can
    show. Claude Code transcripts go through statusline.read_transcript, Codex
    rollouts through codex_boundary_stats; both yield the same fields.
    """
    if not session.source_path or not session.project_path:
        return None
    repo = None
    if session.identity_source != "identity_conflict" and session.checkout_path and session.checkout_id:
        cached = resolve_git_identity(session.checkout_path)
        if cached is not None and cached.checkout_id == session.checkout_id:
            repo = cached.checkout_path
    if repo is None:
        identity = identity_for_session(session.project_path, session.raw_cwd)
        if identity is not None and identity.identity_source == "identity_conflict":
            return None
        repo = identity.checkout_path if identity is not None else session.project_path
    boundary = head_commit(repo)
    if boundary is None:
        return None
    if "codex" in session.tool.lower():
        stats = codex_boundary_stats(session.source_path, since=boundary.committed_at)
    else:
        stats = statusline.read_transcript(session.source_path, since=boundary.committed_at)
    if not stats.get("available"):
        return None
    latest = int(stats.get("latest_context") or 0)
    first = int(stats.get("first_context") or 0)
    at_commit = int(stats.get("context_at_since") or 0)
    turns_since = int(stats.get("turns_since") or 0)
    prompts_since = int(stats.get("prompts_since") or 0)
    min_since = int(stats.get("min_context_since") or 0)
    files = [_relative(p, repo) for p in stats.get("files_since") or []]

    dead = max(0, at_commit - first)
    since = max(0, latest - at_commit) if at_commit else 0
    after = first + since
    shed = bool(min_since and at_commit and min_since < at_commit * (1.0 - CONTEXT_RESET_DROP_RATIO))
    recommend = False
    if at_commit <= 0:
        reason = "The whole session is after the last commit; there is no finished history to shed."
    elif turns_since <= 0:
        reason = "Nothing has happened since the commit yet."
    elif shed:
        reason = "The context already shed since that commit."
    elif dead <= first:
        reason = (
            f"{dead:,} tokens of finished history is no more than a fresh context costs here "
            f"({first:,}), so compacting would not shed anything."
        )
    else:
        recommend = True
        reason = ""

    # Only markers written after the commit belong to this boundary; a
    # compaction from an earlier task is history, not a step in this one.
    command_at = _after(stats.get("command_seen_at"), boundary.committed_at)
    boundary_at = _after(stats.get("boundary_seen_at"), boundary.committed_at)
    stage = _stage(
        recommend=recommend, shed=shed,
        prompts_after_shed=int(stats.get("prompts_after_min_since") or 0),
        command_at=command_at, boundary_at=boundary_at,
    )
    if stage == "compacting":
        reason = f"/compact was typed at {_clock(command_at)}; the tool has not finished yet."
    elif stage == "compacted":
        reason = f"Compacted at {_clock(boundary_at)}; the next reply will show the new size."

    priced = bool(lookup(session.model)) and not is_subscription_model(session.model)
    return Assessment(
        session_id=session.session_id,
        tool=session.tool,
        model=session.model,
        sha=boundary.sha,
        subject=boundary.subject,
        committed_at=boundary.committed_at.isoformat(),
        turns_since_commit=turns_since,
        prompts_since_commit=prompts_since,
        latest_turn_tokens=latest,
        context_at_commit=at_commit,
        first_turn_tokens=first,
        dead_tokens=dead,
        since_tokens=since,
        after_estimate=after,
        files_since=files,
        command=compact_command(
            session.tool,
            boundary,
            files,
            session_title=(str(stats.get("title")).strip() or None) if stats.get("title") else None,
            prompts_since=prompts_since,
        ),
        priced=priced,
        dead_usd_per_turn=cache_read_cost(session.model, dead) if priced else None,
        recommend=recommend,
        reason=reason,
        stage=stage,
        title=(str(stats.get("title")).strip() or None) if stats.get("title") else None,
        command_seen_at=command_at.isoformat() if command_at else None,
        boundary_seen_at=boundary_at.isoformat() if boundary_at else None,
        context_before_shed=int(stats.get("context_before_min_since") or 0) if shed else 0,
        context_after_shed=min_since if shed else 0,
    )


def _after(stamp: Any, floor: datetime) -> datetime | None:
    return stamp if isinstance(stamp, datetime) and stamp >= floor else None


def _clock(stamp: datetime | None) -> str:
    return stamp.astimezone().strftime("%H:%M") if stamp else "?"
