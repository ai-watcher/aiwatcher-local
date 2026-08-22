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

from .pricing import estimate_cost, lookup
from .scanner import _anthropic_usage, _billed_input

# Past this, spend since the last commit is worth interrupting for. Below it a
# figure on screen is just noise competing with the model's output.
UNCOMMITTED_NOTICE_USD = 5.0
UNCOMMITTED_ALARM_USD = 20.0

# Per-turn context sizes that mean something. Shared with session_health's
# thresholds by intent, kept literal here so the statusline never imports the
# analysis path.
PRESSURE_TOKENS = 150_000
CRITICAL_TOKENS = 200_000


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
    turns = 0
    model: str | None = None

    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return {
            "available": False, "total_usd": 0.0, "since_usd": 0.0,
            "latest_context": 0, "peak_context": 0, "turns": 0, "model": None,
        }

    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            usage = message.get("usage") or obj.get("usage")
            if not isinstance(usage, dict):
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

            if since is not None:
                if stamp is not None and stamp >= since:
                    since_usd += cost

    return {
        "available": turns > 0,
        "total_usd": total_usd,
        "since_usd": since_usd,
        "latest_context": latest_context,
        "peak_context": peak_context,
        "turns": turns,
        "model": model,
    }


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

    context = stats["latest_context"]
    if context >= CRITICAL_TOKENS:
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
