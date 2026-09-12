"""Statusline: the in-flight surface.

Every other surface reports on work that is already done. The dashboard needs
opening, the receipt fires after the commit landed. This one sits in front of
you while the money is still being spent, which is the only moment the number
can change what you do.

That constraint shapes everything here. It re-renders constantly, so it reads
exactly one file -- the transcript of the session being rendered, handed to us
on stdin -- and never scans the machine. It reuses the scanner's own token
accounting rather than reimplementing it, because a statusline that disagrees
with the dashboard about what a session cost is worse than no statusline.

It reports three things, in the order they change a decision:

  - spend since your last commit, the in-flight form of unbanked
  - what this session has cost so far
  - context pressure, which is what makes the next turn cost more than the last
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any

from .pricing import context_window, estimate_cost, lookup
from .scanner import INJECTED_ROW_PREFIXES, _anthropic_usage, _billed_input, _repeated_row

# Past this, spend since the last commit is worth interrupting for. Below it a
# figure on screen is just noise competing with the model's output.
UNCOMMITTED_NOTICE_USD = 5.0
UNCOMMITTED_ALARM_USD = 20.0


def _git_head_time(repo: str) -> datetime | None:
    """Commit time of HEAD, or None if this is not a repo with commits."""
    try:
        result = subprocess.run(
            ["git", "-C", repo, "log", "-1", "--format=%ct"],
            check=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return datetime.fromtimestamp(int(result.stdout.strip()), timezone.utc)
    except (TypeError, ValueError):
        return None


def read_transcript(path: str, *, since: datetime | None = None) -> dict[str, Any]:
    """Cost and context for one session, from its transcript alone.

    Deliberately narrow: one file, no directory walk, no state file. A full
    9.6MB transcript parses in about 13ms, which is the whole reason this can
    run on every render.

    `since` splits out the portion spent after a timestamp -- used for spend
    since the last commit, so that figure covers the session actually in front
    of you rather than everything the machine has ever done.
    """
    total_usd = 0.0
    since_usd = 0.0
    latest_context = 0
    peak_context = 0
    first_context = 0
    turns = 0
    model: str | None = None
    # The boundary view, all relative to `since`: the context the session had
    # just before it, the smallest context after it (a drop is a compaction),
    # how many calls came after, and which files those calls touched. Read by
    # compaction.assess; the statusline itself only uses since_usd.
    context_at_since = 0
    min_context_since = 0
    turns_since = 0        # model calls after `since`
    prompts_since = 0      # things the user typed after `since` -- the turn count a person means
    files_since: list[str] = []
    # The compaction itself, as Claude Code records it: the `/compact` the
    # user typed lands as a user row the moment they press enter, and the
    # finished compaction as a compact_boundary row plus its summary, about
    # a minute later. Both arrive before the next reply, which is the first
    # row whose usage shows the new size. A surface that only reads usage
    # keeps asking for a compaction that has already happened.
    title: str | None = None
    command_seen_at: datetime | None = None
    boundary_seen_at: datetime | None = None
    # Calls after the one that set min_context_since: zero means the latest
    # call is the small one, i.e. the shed has just been observed.
    turns_after_min_since = 0
    # Prompts after that call: zero means the user has not typed since the
    # shed. Claude Code carries on by itself after a compaction, several tool
    # calls in a few seconds, so "calls after" cannot tell whether a person
    # has seen the result; "typed after" can.
    prompts_after_min_since = 0
    context_before_min_since = 0
    previous_context = 0

    empty = {
        "available": False, "total_usd": 0.0, "since_usd": 0.0,
        "latest_context": 0, "peak_context": 0, "first_context": 0, "turns": 0, "model": None,
        "context_at_since": 0, "min_context_since": 0, "turns_since": 0, "prompts_since": 0,
        "files_since": [], "title": None, "command_seen_at": None, "boundary_seen_at": None,
        "turns_after_min_since": 0, "prompts_after_min_since": 0, "context_before_min_since": 0,
    }
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return empty

    # Claude Code writes earlier rows again when it compacts (see
    # scanner._repeated_row). Walking the file in order, "the last usage row"
    # was a copy from hours before, so the bar closed a receipt on the wrong
    # size and skipped its Compacted step. Each row counts once.
    seen_rows: set[str] = set()

    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _repeated_row(obj, seen_rows):
                continue
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            usage = message.get("usage") or obj.get("usage")
            if not isinstance(usage, dict):
                custom_title = obj.get("customTitle")
                if isinstance(custom_title, str) and custom_title.strip():
                    title = custom_title.strip()
                if _is_compact_boundary_row(obj):
                    boundary_seen_at = _parse_stamp(obj.get("timestamp") or obj.get("createdAt")) or boundary_seen_at
                elif _is_compact_command_row(obj, message):
                    command_seen_at = _parse_stamp(obj.get("timestamp") or obj.get("createdAt")) or command_seen_at
                elif since is not None and _is_prompt_row(obj, message):
                    prompt_stamp = _parse_stamp(obj.get("timestamp") or obj.get("createdAt"))
                    if prompt_stamp is not None and prompt_stamp >= since:
                        prompts_since += 1
                        prompts_after_min_since += 1
                continue

            # The scanner's own splitter, not a copy of it. Cached input is
            # most of the bill on a long session, and a statusline that read
            # `input_tokens` alone would understate it by roughly 11x.
            tokens = _anthropic_usage(usage)
            billed_in = _billed_input(tokens)
            if billed_in <= 0 and tokens["output"] <= 0:
                continue

            turn_model = message.get("model") or obj.get("model")
            if isinstance(turn_model, str) and turn_model:
                model = turn_model

            stamp = _parse_stamp(obj.get("timestamp") or obj.get("createdAt"))
            cost = estimate_cost(
                model, tokens["input"], tokens["output"],
                cache_write_5m=tokens["cache_write_5m"],
                cache_write_1h=tokens["cache_write_1h"],
                cache_read=tokens["cache_read"],
                when=stamp,
            )
            total_usd += cost
            turns += 1
            latest_context = billed_in
            peak_context = max(peak_context, billed_in)
            if not first_context:
                first_context = billed_in

            if since is not None:
                if stamp is not None and stamp >= since:
                    since_usd += cost
                    turns_since += 1
                    if not min_context_since or billed_in < min_context_since:
                        min_context_since = billed_in
                        context_before_min_since = previous_context
                        turns_after_min_since = 0
                        prompts_after_min_since = 0
                    else:
                        turns_after_min_since += 1
                    content = message.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict) or block.get("type") != "tool_use":
                                continue
                            if block.get("name") not in _FILE_TOOLS:
                                continue
                            file_path = (block.get("input") or {}).get("file_path")
                            if isinstance(file_path, str) and file_path and file_path not in files_since:
                                files_since.append(file_path)
                else:
                    context_at_since = billed_in
            previous_context = billed_in

    return {
        "available": turns > 0,
        "total_usd": total_usd,
        "since_usd": since_usd,
        "latest_context": latest_context,
        "peak_context": peak_context,
        "first_context": first_context,
        "turns": turns,
        "model": model,
        "context_at_since": context_at_since,
        "min_context_since": min_context_since,
        "turns_since": turns_since,
        "prompts_since": prompts_since,
        "files_since": files_since,
        "title": title,
        "command_seen_at": command_seen_at,
        "boundary_seen_at": boundary_seen_at,
        "turns_after_min_since": turns_after_min_since,
        "prompts_after_min_since": prompts_after_min_since,
        "context_before_min_since": context_before_min_since,
    }


_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})
_COMPACT_COMMAND = "<command-name>/compact</command-name>"


def _row_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text") or "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _is_compact_command_row(obj: dict[str, Any], message: dict[str, Any]) -> bool:
    """The `/compact` the user typed. Claude Code writes slash commands as a
    user row wrapping the command name, at the moment enter is pressed."""
    return obj.get("type") == "user" and _COMPACT_COMMAND in _row_text(message)[:200]


def _is_compact_boundary_row(obj: dict[str, Any]) -> bool:
    """The finished compaction: a system row marking the boundary, followed
    by the summary row that replaces the history before it."""
    if obj.get("isCompactSummary"):
        return True
    return obj.get("type") == "system" and obj.get("subtype") == "compact_boundary"


def _is_prompt_row(obj: dict[str, Any], message: dict[str, Any]) -> bool:
    """A line the user typed: a user row whose content is text, not tool results.
    Slash commands are rows too, but they are not a turn a person would count."""
    if obj.get("type") != "user" or obj.get("isMeta") or obj.get("isCompactSummary"):
        return False
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif content and isinstance(content, list):
        blocks = [block for block in content if isinstance(block, dict)]
        if any(block.get("type") == "tool_result" for block in blocks):
            return False
        # A prompt with a screenshot attached is still a prompt: the text
        # blocks carry what was typed and the image rides along. Requiring
        # every block to be text missed exactly the message that should have
        # ended the confirmed step on 2026-09-09.
        text = "\n".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text")
    else:
        return False
    text = text.lstrip()
    return bool(text) and not text.startswith(INJECTED_ROW_PREFIXES)


def _parse_stamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _money(value: float) -> str:
    if value >= 100:
        return f"${value:,.0f}"
    return f"${value:.2f}"


def _tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return str(value)


def build_statusline(payload: dict[str, Any]) -> str:
    """Render the line from Claude Code's statusLine stdin payload.

    Returns "" when there is nothing worth saying. An empty statusline is a
    better outcome than a row of zeroes: this sits under every prompt, and
    anything that is always there stops being read.
    """
    workspace = payload.get("workspace") if isinstance(payload.get("workspace"), dict) else {}
    cwd = (
        workspace.get("current_dir")
        or workspace.get("project_dir")
        or payload.get("cwd")
        or os.getcwd()
    )
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return ""

    head_time = _git_head_time(str(cwd))
    stats = read_transcript(transcript, since=head_time)
    if not stats["available"]:
        return ""

    parts: list[str] = []

    # Spend since the last commit leads: it is the only figure here with an
    # obvious action attached, and the action is "commit". Worded "since
    # commit" rather than "uncommitted" because it covers this session only --
    # a second session spending in the same repo is not counted, and the
    # broader figure belongs on the dashboard where it can be explained.
    if head_time is not None and stats["since_usd"] >= UNCOMMITTED_NOTICE_USD:
        marker = "!" if stats["since_usd"] >= UNCOMMITTED_ALARM_USD else "*"
        parts.append(f"{marker} {_money(stats['since_usd'])} since commit")

    parts.append(f"{_money(stats['total_usd'])} session")

    # "compact" only when the latest turn is at the model's own window -- the
    # same rule as session_health._context_ceiling, restated here rather than
    # imported so the statusline never pulls in the analysis path. A peak past
    # the table's figure means the table is stale, not that the session is over.
    context = stats["latest_context"]
    window = context_window(stats["model"])
    at_window = window is not None and stats["peak_context"] <= window and context >= window
    if at_window:
        parts.append(f"{_tokens(context)}/turn compact")
    elif context > 0:
        parts.append(f"{_tokens(context)}/turn")

    # ASCII only, and deliberately. This string is printed by a short-lived
    # process whose stdout encoding is whatever the host terminal reports; a
    # middle dot raises UnicodeEncodeError under cp1252 and takes the whole
    # status line with it.
    return " | ".join(parts)


def statusline_from_stdin(raw: str) -> str:
    """Parse Claude Code's payload and render, never raising.

    A statusline that throws replaces itself with an error on every prompt.
    Silence is the only acceptable failure mode.
    """
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    try:
        return build_statusline(payload)
    except Exception:  # noqa: BLE001 - see docstring
        return ""


def statusline_settings_snippet(command: str) -> dict[str, Any]:
    return {"statusLine": {"type": "command", "command": f"{command} statusline"}}
