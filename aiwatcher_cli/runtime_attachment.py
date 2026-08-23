"""Runtime attachment hints for returning from AIWatcher to active AI work.

This module is deliberately honest about platform limits. Some tools expose
local logs but no stable URL for a specific chat. In those cases AIWatcher can
bring the app or workspace forward, but must not claim exact session return.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

from .processes import RuntimeProcess, discover_runtime_processes
from .scanner import LocalSession, _normalize_project_path


@dataclass
class RuntimeAttachment:
    session_id: str
    level: str
    mode: str
    label: str
    action_label: str
    available: bool
    confidence: str
    reason: str
    tool: str
    surface: str | None = None
    project_path: str | None = None
    pid: int | None = None
    app_name: str | None = None
    deep_link: str | None = None
    exact_return_available: bool = False
    exact_return_label: str = "Exact chat unavailable"
    exact_return_reason: str = (
        "Local history does not include a verified app window, terminal pane, or chat deep link for this exact session."
    )
    native_companion_required: bool = True
    identity_level: str = "historical_log"
    identity_label: str = "Historical log only"
    identity_reason: str = "AIWatcher found local history, but no verified live runtime for this exact AI session."
    # Set by runtime_attachment_for_session from the tool + session id alone.
    # Independent of every `available`/level decision above -- see the
    # "Tool-native resume" section below for why it must not be gated on them.
    resume_command: list[str] | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "level": self.level,
            "mode": self.mode,
            "label": self.label,
            "action_label": self.action_label,
            "available": self.available,
            "confidence": self.confidence,
            "reason": self.reason,
            "tool": self.tool,
            "surface": self.surface,
            "project_path": self.project_path,
            "pid": self.pid,
            "app_name": self.app_name,
            "deep_link": self.deep_link,
            "exact_return_available": self.exact_return_available,
            "exact_return_label": self.exact_return_label,
            "exact_return_reason": self.exact_return_reason,
            "native_companion_required": self.native_companion_required,
            "identity_level": self.identity_level,
            "identity_label": self.identity_label,
            "identity_reason": self.identity_reason,
            "resume_available": bool(self.resume_command),
            "resume_command": format_resume_command(self.resume_command, cwd=resolve_resume_cwd(self.project_path))
            if self.resume_command
            else None,
            "resume_label": "Resume in terminal" if self.resume_command else "Resume unavailable",
            "resume_reason": (
                "Reopens this conversation with its full history in a new terminal. It does not "
                "focus the window the session originally ran in — no local tool can do that."
                if self.resume_command
                else resume_unavailable_reason(self.tool, self.session_id)
            ),
        }


def _project_path(path: str | None) -> str | None:
    return _normalize_project_path(path)


def _app_name(tool: str, surface: str | None = None) -> str | None:
    key = tool.lower()
    if key in {"claude", "claude-code"}:
        return "Claude"
    if key in {"codex", "codex-cli"}:
        return "Codex"
    if key == "cursor":
        return "Cursor"
    if key in {"vscode", "vs-code", "code"}:
        return "Visual Studio Code"
    return None


def _workspace_mode(tool: str, project_path: str | None) -> tuple[str, str, bool]:
    if not project_path:
        return "none", "No reliable project/workspace path is available for this session.", False
    key = tool.lower()
    if key == "cursor" and shutil.which("cursor"):
        return "cursor", "Open the project in Cursor. Exact composer thread return depends on Cursor platform support.", True
    if key in {"vscode", "vs-code", "code"} and shutil.which("code"):
        return "vscode", "Open the project in VS Code. Exact assistant thread return depends on editor support.", True
    if shutil.which("code"):
        return "vscode", "Open the project in VS Code as the safest workspace return path.", True
    if sys.platform == "darwin":
        return "folder", "Open the project folder. Exact AI chat return is not available from local logs alone.", True
    return "none", "Install the Cursor or VS Code command-line launcher to open this workspace from AIWatcher.", False


def _safe_deep_link(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() in {"http", "https", "claude", "codex", "cursor", "vscode"}:
        return value.strip()
    return None


def _matching_process(session: LocalSession, processes: list[RuntimeProcess]) -> RuntimeProcess | None:
    if not session.session_id:
        return None
    return next((process for process in processes if process.session_id == session.session_id), None)


def runtime_attachment_for_session(
    session: LocalSession,
    *,
    state: dict[str, object] | None = None,
    processes: list[RuntimeProcess] | None = None,
) -> RuntimeAttachment:
    attachment = _live_attachment_for_session(session, state=state, processes=processes)
    # Applied to whichever tier came back, including "historical" -- resume is
    # the one return path that does not depend on the session still being live.
    attachment.resume_command = resume_command_for_session(session.tool, session.session_id)
    return attachment


def _live_attachment_for_session(
    session: LocalSession,
    *,
    state: dict[str, object] | None = None,
    processes: list[RuntimeProcess] | None = None,
) -> RuntimeAttachment:
    status = str((state or {}).get("status") or "unknown")
    project_path = _project_path(session.project_path)
    process_rows = processes if processes is not None else []
    process = _matching_process(session, process_rows)
    app_name = _app_name(session.tool, session.surface)

    if process:
        process_project = _project_path(process.cwd) or project_path
        deep_link = _safe_deep_link(process.deep_link)
        if deep_link:
            return RuntimeAttachment(
                session_id=session.session_id,
                level="exact_deep_link",
                mode="deep_link",
                label="Exact chat link captured",
                action_label="Return to exact chat",
                available=True,
                confidence="verified",
                reason=(
                    f"Matched a live {process.tool} process (PID {process.pid}) with a host-provided deep link. "
                    "AIWatcher can open that link without guessing."
                ),
                tool=session.tool,
                surface=session.surface,
                project_path=process_project,
                pid=process.pid,
                app_name=app_name,
                deep_link=deep_link,
                exact_return_available=True,
                exact_return_label="Return to exact chat",
                exact_return_reason="The host/runtime exposed a trusted deep link for this exact session.",
                native_companion_required=False,
                identity_level="exact_session",
                identity_label="Exact active session",
                identity_reason=f"Matched this AIWatcher session to a live {process.tool} process and a trusted host deep link.",
            )
        mode, reason, available = _workspace_mode(session.tool, process_project)
        action_label = "Open workspace" if available else "No live return"
        return RuntimeAttachment(
            session_id=session.session_id,
            level="active_process",
            mode=mode,
            label="Active process attached",
            action_label=action_label,
            available=available,
            confidence="high",
            reason=(
                f"Matched a live {process.tool} process (PID {process.pid}). "
                f"{reason} Exact terminal/chat focus needs a host-provided window or session handle."
            ),
            tool=session.tool,
            surface=session.surface,
            project_path=process_project,
            pid=process.pid,
            app_name=app_name,
            exact_return_label="Needs native companion",
            exact_return_reason=(
                "AIWatcher matched a live process, but returning to the exact chat needs an opt-in native companion "
                "that records a trusted window, terminal pane, or host deep link."
            ),
            identity_level="exact_session",
            identity_label="Exact active session",
            identity_reason=(
                f"Matched this AIWatcher session to a live {process.tool} process. Exact chat focus still depends "
                "on the host exposing a window, terminal pane, or deep link."
            ),
        )

    if status not in {"active", "recent"}:
        return RuntimeAttachment(
            session_id=session.session_id,
            level="historical",
            mode="none",
            label="Historical session",
            action_label="No live return",
            available=False,
            confidence="low",
            reason="This session is not fresh enough for live return. Use it for evidence, prompt optimization, or a Fresh Start brief.",
            tool=session.tool,
            surface=session.surface,
            project_path=project_path,
            app_name=app_name,
            native_companion_required=False,
            identity_level="historical_log",
            identity_label="Historical log only",
            identity_reason="This is local history, not a verified in-progress AI chat.",
        )

    if session.surface == "desktop" and app_name:
        return RuntimeAttachment(
            session_id=session.session_id,
            level="app",
            mode="app",
            label=f"{app_name} app detected",
            action_label=f"Open {app_name}",
            available=sys.platform == "darwin",
            confidence="medium",
            reason=(
                f"AIWatcher can bring {app_name} forward, but this desktop surface does not expose "
                "a stable deep link to the exact chat yet."
            ),
            tool=session.tool,
            surface=session.surface,
            project_path=project_path,
            app_name=app_name,
            exact_return_label="App focus only",
            exact_return_reason=(
                f"AIWatcher can bring {app_name} forward. Returning to the exact desktop chat needs a platform "
                "API, extension, or explicit native companion with accessibility permission."
            ),
            identity_level="likely_workspace",
            identity_label="Likely active app",
            identity_reason=(
                f"The {app_name} app surface is recent, but AIWatcher has not verified the exact desktop chat."
            ),
        )

    mode, reason, available = _workspace_mode(session.tool, project_path)
    return RuntimeAttachment(
        session_id=session.session_id,
        level="workspace" if available else "unavailable",
        mode=mode,
        label="Workspace return available" if available else "No live return target",
        action_label="Open workspace" if available else "No live return",
        available=available,
        confidence="medium" if available else "low",
        reason=reason,
        tool=session.tool,
        surface=session.surface,
        project_path=project_path,
        app_name=app_name,
        exact_return_label="Workspace only" if available else "Exact chat unavailable",
        exact_return_reason=(
            "AIWatcher can open the workspace, but exact chat return is unavailable without a live process/window "
            "attachment or platform deep link."
            if available
            else "AIWatcher only found a local session log; no safe return target is available."
        ),
        native_companion_required=available,
        identity_level="likely_workspace",
        identity_label="Likely workspace" if available else "Likely active session",
        identity_reason=(
            "AIWatcher can identify the workspace, but not the exact running AI chat."
            if available
            else "AIWatcher sees a recent local session, but cannot open a safe workspace or exact chat target on this platform."
        ),
    )


# --- Tool-native resume ---------------------------------------------------
#
# Everything above answers "can AIWatcher reach the running tool?" and depends
# on a live process, a window handle, or a host deep link. Resume answers a
# different question -- "can the reader get this conversation back?" -- and
# needs none of that. Claude Code and Codex both reopen a past conversation
# from their own session id, and the id AIWatcher stores IS that id: Claude's
# transcript filename stem, Codex's rollout payload id. Both were checked
# against real local logs and are plain UUIDs.
#
# So this is deliberately NOT gated on RuntimeAttachment.available. A session
# that ended three days ago in a terminal that has since been closed still
# resumes; that is the whole point, and gating it on freshness would throw
# away the only return path that survives a closed terminal. It is also the
# only one that works on Windows, where processes._read_ps_rows() returns []
# unconditionally and no attachment tier above "historical" can ever fire.
#
# What it does NOT do: focus the window the session came from. It reopens the
# conversation in a NEW terminal. Claude Code's claude-cli:// URL handler
# accepts only repo/cwd/prefill -- there is no session route -- so nothing
# local can raise the original window. Every label here therefore says
# "Resume", never "Return to"; the button must answer the question it asks.
#
# Cursor is deliberately absent: scanner builds those ids as f"cursor-{dir}"
# from a log directory name, so there is no id Cursor would recognise.
_RESUME_COMMANDS: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "--resume"),
    "claude-code": ("claude", "--resume"),
    "codex": ("codex", "resume"),
    "codex-cli": ("codex", "resume"),
}

_RESUME_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def _valid_resume_session_id(session_id: str | None) -> bool:
    return bool(_RESUME_SESSION_ID_RE.fullmatch((session_id or "").strip()))


def resume_command_for_session(tool: str | None, session_id: str | None) -> list[str] | None:
    """The tool's own resume command for this session, or None if it has none."""
    prefix = _RESUME_COMMANDS.get((tool or "").strip().lower())
    session_id = (session_id or "").strip()
    if not prefix or not _valid_resume_session_id(session_id):
        return None
    return [*prefix, session_id]


def resume_unavailable_reason(tool: str | None, session_id: str | None = None) -> str:
    if session_id and not _valid_resume_session_id(session_id):
        return (
            "AIWatcher will only resume Claude/Codex conversations with a verified UUID session id. "
            "Use the Fresh Start brief instead of running an untrusted resume command."
        )
    label = (tool or "").strip() or "This tool"
    return (
        f"{label} does not expose a local resume command, so AIWatcher cannot reopen this "
        "conversation. Use the Fresh Start brief to carry the context into a new session."
    )


def resolve_resume_cwd(path: str | None) -> str | None:
    if not path:
        return None
    normalized = os.path.expanduser(path.strip())
    if not normalized:
        return None
    if sys.platform != "win32":
        normalized = os.path.abspath(normalized)
    if os.path.isdir(normalized):
        return normalized
    return None


def format_resume_command(
    command: list[str] | None,
    *,
    cwd: str | None = None,
    platform: str | None = None,
) -> str | None:
    if not command:
        return None
    platform = platform or sys.platform
    if platform == "win32":
        display = subprocess.list2cmdline(command)
        if cwd:
            return f'cd /d "{cwd}" && {display}'
        return display
    display = shlex.join(command)
    if cwd:
        return f"cd {shlex.quote(cwd)} && {display}"
    return display


def launch_resume_command(command: list[str], *, cwd: str | None = None) -> dict[str, object]:
    """Open the resume command in a new terminal window.

    Copying the command is the guaranteed path and always stays offered --
    this is only the convenience on top. It must fail loudly enough for the
    caller to fall back to copying, rather than leave the reader believing a
    terminal opened somewhere off screen.
    """
    if not command:
        return {"ok": False, "message": "No resume command is available for this session."}
    cwd = resolve_resume_cwd(cwd)
    display = format_resume_command(command, cwd=cwd) or ""
    if not shutil.which(command[0]):
        return {
            "ok": False,
            "message": f"`{command[0]}` is not on this machine's PATH. Copy the command and run it where it is.",
        }
    try:
        if sys.platform == "win32":
            # A new console rather than `start`: the child owns its own window
            # and there is no cmd quoting layer between us and the argv.
            subprocess.Popen(command, cwd=cwd, creationflags=_NEW_CONSOLE)
            return {"ok": True, "message": f"Opened a new terminal running `{display}`."}
        if sys.platform == "darwin":
            script = f"tell application \"Terminal\" to do script {json.dumps(display)}"
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {"ok": True, "message": f"Opened Terminal running `{display}`."}
    except OSError as exc:
        return {"ok": False, "message": f"Could not open a terminal: {exc}. Copy the command instead."}
    return {
        "ok": False,
        "message": "AIWatcher cannot open a terminal for you on this platform. Copy the command instead.",
    }


def safe_runtime_processes() -> list[RuntimeProcess]:
    try:
        return discover_runtime_processes()
    except OSError:
        return []


def perform_runtime_return(attachment: RuntimeAttachment) -> dict[str, object]:
    if not attachment.available:
        return {
            "ok": False,
            "message": attachment.reason,
            "attachment": attachment.to_json(),
        }

    try:
        if attachment.mode == "deep_link" and attachment.deep_link:
            if sys.platform == "darwin":
                subprocess.Popen(["open", attachment.deep_link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif sys.platform == "win32":
                os.startfile(attachment.deep_link)  # type: ignore[attr-defined]
            elif shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", attachment.deep_link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                return {"ok": False, "message": "No opener is available for this exact chat link.", "attachment": attachment.to_json()}
            return {"ok": True, "message": "Opened exact chat link.", "attachment": attachment.to_json()}
        if attachment.mode == "app" and attachment.app_name and sys.platform == "darwin":
            subprocess.Popen(["open", "-a", attachment.app_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "message": f"Opened {attachment.app_name}. Exact chat return is not available yet.", "attachment": attachment.to_json()}
        if attachment.mode == "cursor" and attachment.project_path:
            subprocess.Popen(["cursor", attachment.project_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "message": "Opened workspace in Cursor.", "attachment": attachment.to_json()}
        if attachment.mode == "vscode" and attachment.project_path:
            subprocess.Popen(["code", attachment.project_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "message": "Opened workspace in VS Code.", "attachment": attachment.to_json()}
        if attachment.mode == "folder" and attachment.project_path and sys.platform == "darwin":
            subprocess.Popen(["open", attachment.project_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "message": "Opened project folder. Use the Fresh Start brief if you need a fresh AI chat.", "attachment": attachment.to_json()}
    except OSError as exc:
        return {"ok": False, "message": f"Could not open return target: {exc}", "attachment": attachment.to_json()}

    return {
        "ok": False,
        "message": "No supported return action is available on this platform yet.",
        "attachment": attachment.to_json(),
    }
