"""Shared policy for calm, session-aware runtime nudges.

Detection belongs to the scanner/watch engine. Delivery belongs to platform
adapters. This module is the contract between them so a context warning does
not become four contradictory notifications across the CLI, desktop companion,
and dashboard.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from .runtime_attachment import RuntimeAttachment
from .scanner import LocalSession


MIN_IDLE_SECONDS = 8
MAX_ACTIVE_IDLE_SECONDS = 15 * 60
REQUIRED_OBSERVATIONS = 2

# How long a session must have been waiting before this interrupts you.
#
# Every other signal here is held unless the tool it is about is in front of
# you: nagging about a session you are not in is noise. This one inverts that,
# because the whole value is that you are somewhere else and do not know. What
# replaces the foreground check is dwell time -- at the keyboard you answer a
# permission prompt in seconds, so a wait past a minute means you are not
# looking at it. That is more reliable than foreground detection, which cannot
# tell which of three Claude windows you are actually in.
#
# STOPGAP in the same sense as WORKING_SECONDS: 60s is reasoned, not measured,
# and biased long because a notification that arrives while you are mid-answer
# is the one that gets the feature muted.
WAITING_NOTIFY_SECONDS = 60


@dataclass(frozen=True)
class RuntimeNudge:
    signal_kind: str
    severity: str
    action: str
    title: str
    body: str
    primary_label: str
    primary_mode: str
    eligible: bool
    hold_reason: str | None
    required_observations: int
    idle_seconds: float
    foreground_tool: str | None


_PRESENTATIONS: dict[str, tuple[str, str, str]] = {
    "session_blocked": (
        "A session is waiting for you",
        "Return to session",
        "return_session",
    ),
    "critical_context": (
        "Context is getting expensive",
        "Copy Fresh Start brief",
        "fresh_chat",
    ),
    "loop": (
        "Possible loop detected",
        "Inspect and stop",
        "recover_loop",
    ),
    "velocity": (
        "Narrow this unusually fast run",
        "Copy focused next step",
        "continue_focused",
    ),
    "runway": (
        "Usage runway is getting low",
        "Review switch options",
        "switch_tool",
    ),
    "warning_context": (
        "This session is getting heavy",
        "Copy compact next step",
        "continue_focused",
    ),
    "usage_pressure": (
        "Focus the next checkpoint",
        "Copy focused next step",
        "continue_focused",
    ),
}


# Signal kinds the always-on-top Companion bar has a state of its own for.
#
# The bar is fed by `ui.build_companion_state`, which is a different function
# reached by a different route and never sees a RuntimeNudge. It renders
# `session_waiting` for a blocked session and the Fresh Start states for
# context pressure, so for those three kinds it really is the delivery
# surface. It has no state for a loop, a velocity spike, runway, or generic
# usage pressure -- for those, the bar sitting on "Watching quietly" is not
# coverage, it is silence.
#
# This exists because `_open_handoff_overlay` suppressed *every* signal
# whenever the bar was running, on the assumption that the bar covered them
# all. It does not, so the four kinds absent from this set reached no surface
# at all: detected, judged, phrased, and then dropped.
#
# Keep this in step with the branches in `build_companion_state`. A kind added
# here that the bar cannot draw goes silent; a kind left out that the bar does
# draw is shown twice.
COMPANION_BAR_SIGNAL_KINDS = frozenset({
    "session_blocked",
    "critical_context",
    "warning_context",
})


# What the primary button does, per action.
#
# The table above says what the button is *called*; this says what it must then
# *do*. They are one decision, kept in one place on purpose: a label and a
# handler chosen independently is exactly how "Review switch options" came to
# copy a `target=generic` Fresh Start brief, and how "Return to session" came to
# copy one too.
#
#   copy    -- put a brief on the clipboard
#   inspect -- open the session in the dashboard, to look at the evidence
#   return  -- focus the tool the session is already running in
#
# `return` exists because a blocked session is not a session you should hand
# off. It is alive and waiting on a permission prompt, and it needs an answer
# in the tool, not a brief pasted into a fresh chat that abandons it. A loop
# and runway pressure inspect instead: you cannot write a useful next step for
# a run that is repeating without looking at what it repeats, and the switch
# targets live in the session drawer.
#
# overlay.js keeps the same mapping in its own PRIMARY_MODES, because that one
# names functions on the page and cannot import this. Keep them in step.
PRIMARY_MODE_FOR_ACTION: dict[str, str] = {
    "return_session": "return",
    "recover_loop": "inspect",
    "switch_tool": "inspect",
    "fresh_chat": "copy",
    "continue_focused": "copy",
}

# Copy is the fallback because it is the only mode that needs no attachment and
# no live session: worse than the right action, never wrong enough to mislead.
DEFAULT_PRIMARY_MODE = "copy"


def primary_mode_for_action(action: str) -> str:
    return PRIMARY_MODE_FOR_ACTION.get(action, DEFAULT_PRIMARY_MODE)


def presentation_for_signal(signal_kind: str, reason: str) -> dict[str, str]:
    title, primary_label, action = _PRESENTATIONS.get(signal_kind, _PRESENTATIONS["usage_pressure"])
    return {
        "signal_kind": signal_kind,
        "title": title,
        "body": reason,
        "primary_label": primary_label,
        "action_mode": action,
    }


def _tool_family(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if "claude" in text:
        return "claude"
    if "codex" in text:
        return "codex"
    if "cursor" in text:
        return "cursor"
    if text in {"code", "visual studio code", "vscode"} or "visual studio code" in text:
        return "vscode"
    if any(name in text for name in ("terminal", "iterm", "powershell", "windows terminal", "cmd")):
        return "terminal"
    return text


def _macos_foreground_app() -> str | None:
    """Read the frontmost app without requesting Accessibility permission."""
    try:
        front = subprocess.run(
            ["/usr/bin/lsappinfo", "front"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        ).stdout.strip()
        if not front or "NULL" in front:
            return None
        info = subprocess.run(
            ["/usr/bin/lsappinfo", "info", "-only", "name", front],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        ).stdout.strip()
    # Same hole as the Windows probe below: both calls above carry a 1s timeout,
    # and TimeoutExpired is a SubprocessError rather than an OSError. A loaded
    # Mac would crash `aiwatcher watch` exactly the way a loaded Windows box did.
    except (OSError, subprocess.SubprocessError):
        return None
    if '"name"=' in info:
        return info.split('"name"=', 1)[1].strip().strip('"')
    return info or None


def _windows_foreground_app() -> str | None:
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        handle = user32.GetForegroundWindow()
        if not handle:
            return None
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        if not pid.value:
            return None
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid.value}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
            # The companion runs detached with no console of its own. Without
            # this flag Windows allocates a fresh console for tasklist, which
            # the default host renders as a real terminal window blinking on
            # screen every nudge tick.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
    # TimeoutExpired is a SubprocessError, not an OSError, so the timeout above
    # was raising straight through this handler. That crashed `aiwatcher watch`
    # on a loaded Windows box -- and #83 put foreground_tool() on the watch path,
    # which turned a rare CI flake into ten failures in one local run. This
    # function promises best-effort silence; a busy tasklist is exactly the case
    # it exists to swallow.
    except (AttributeError, OSError, subprocess.SubprocessError):
        return None
    if not result or result.startswith("INFO:"):
        return None
    return result.split(",", 1)[0].strip().strip('"')


def foreground_tool() -> str | None:
    """Best-effort foreground tool family; failures intentionally stay silent."""
    override = os.environ.get("AIWATCHER_FOREGROUND_TOOL")
    if override is not None:
        return _tool_family(override)
    if sys.platform == "darwin":
        app = _macos_foreground_app()
    elif sys.platform == "win32":
        app = _windows_foreground_app()
    else:
        app = None
    return _tool_family(app)


def _session_idle_seconds(session: LocalSession, now: datetime) -> float:
    stamp = session.updated_at or session.started_at
    if stamp is None:
        return float("inf")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (now - stamp.astimezone(timezone.utc)).total_seconds())


def build_runtime_nudge(
    session: LocalSession,
    status: Mapping[str, object],
    attachment: RuntimeAttachment,
    *,
    now: datetime | None = None,
    active_foreground_tool: str | None = None,
    enforce_pause: bool = True,
) -> RuntimeNudge:
    """Build one truthful intervention and decide whether it may interrupt.

    The dashboard can show historical findings. A desktop companion is held
    unless the work is recent, the user has reached a short pause, and the
    signal can be tied to a live process, CLI workspace, or foreground app.
    """
    current = now or datetime.now(timezone.utc)
    signal_kind = str(status.get("signal_kind") or "usage_pressure")
    reason = str(status.get("reason") or "AIWatcher found local execution pressure.")
    presentation = presentation_for_signal(signal_kind, reason)
    title = presentation["title"]
    primary_label = presentation["primary_label"]
    action = presentation["action_mode"]
    health = status.get("health")
    loop = status.get("loop")
    loop_repeats = int(loop.get("max_repeat", 0)) if isinstance(loop, Mapping) else 0
    severity = (
        str(getattr(health, "severity", ""))
        if getattr(health, "severity", None) in {"warning", "critical"}
        else "critical"
        if signal_kind == "loop" and loop_repeats >= 8
        else "warning"
    )
    idle_seconds = _session_idle_seconds(session, current)
    active_tool = _tool_family(active_foreground_tool) if active_foreground_tool is not None else foreground_tool()
    session_tool = _tool_family(session.tool)
    surface = (session.surface or "").lower()

    hold_reason: str | None = None
    if signal_kind == "session_blocked":
        # Deliberately none of the checks below. The foreground rule would hold
        # this exactly when it matters, and MAX_ACTIVE_IDLE_SECONDS would go
        # quiet at fifteen minutes -- by which point the wait is the most
        # expensive thing on the machine. The tool told us it is blocked, so
        # the only question left is whether you have had time to answer.
        if idle_seconds < WAITING_NOTIFY_SECONDS:
            hold_reason = "waiting less than a minute; you may still be answering it"
    elif enforce_pause and idle_seconds < MIN_IDLE_SECONDS:
        hold_reason = "waiting for a pause between AI turns"
    elif idle_seconds > MAX_ACTIVE_IDLE_SECONDS:
        hold_reason = "session activity is too old for an ambient interruption"
    elif attachment.level == "active_process":
        allowed_foreground = {None, session_tool}
        if surface == "cli":
            allowed_foreground.add("terminal")
        if session_tool == "cursor":
            allowed_foreground.add("vscode")
        if active_tool not in allowed_foreground:
            hold_reason = f"{active_tool} is foreground, not this active {session_tool or 'AI'} session"
    elif surface == "cli" and attachment.level not in {"historical", "unavailable"}:
        if active_tool not in {None, "terminal", session_tool}:
            hold_reason = f"{active_tool} is foreground, not this CLI session"
    elif surface in {"desktop", "editor", "ide"} or attachment.level == "app":
        if active_tool is None:
            hold_reason = "foreground app could not be verified"
        elif active_tool not in ({session_tool, "vscode"} if session_tool == "cursor" else {session_tool}):
            hold_reason = f"{active_tool} is foreground, not {session_tool or 'this AI tool'}"
    else:
        hold_reason = "no active runtime or foreground tool is attached"

    return RuntimeNudge(
        signal_kind=signal_kind,
        severity=severity,
        action=action,
        title=title,
        body=reason,
        primary_label=primary_label,
        primary_mode="inspect" if action == "recover_loop" else "copy",
        eligible=hold_reason is None,
        hold_reason=hold_reason,
        # A reported event needs no second sighting. REQUIRED_OBSERVATIONS
        # exists to confirm signals inferred from noisy timestamps; holding a
        # blocked session for a second poll only adds latency to the one
        # signal whose whole value is arriving before you notice yourself.
        required_observations=(
            1 if signal_kind == "session_blocked"
            else REQUIRED_OBSERVATIONS if enforce_pause else 1
        ),
        idle_seconds=idle_seconds,
        foreground_tool=active_tool,
    )
