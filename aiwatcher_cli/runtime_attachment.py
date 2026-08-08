"""Runtime attachment hints for returning from AIWatcher to active AI work.

This module is deliberately honest about platform limits. Some tools expose
local logs but no stable URL for a specific chat. In those cases AIWatcher can
bring the app or workspace forward, but must not claim exact session return.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

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
    status = str((state or {}).get("status") or "unknown")
    project_path = _project_path(session.project_path)
    process_rows = processes if processes is not None else []
    process = _matching_process(session, process_rows)
    app_name = _app_name(session.tool, session.surface)

    if process:
        process_project = _project_path(process.cwd) or project_path
        mode, reason, available = _workspace_mode(session.tool, process_project)
        action_label = "Open workspace" if available else "Copy handoff"
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
        )

    if status not in {"active", "recent"}:
        return RuntimeAttachment(
            session_id=session.session_id,
            level="historical",
            mode="none",
            label="Historical session",
            action_label="Copy handoff",
            available=False,
            confidence="low",
            reason="This session is not fresh enough for live return. Use it for evidence, prompt optimization, or a handoff brief.",
            tool=session.tool,
            surface=session.surface,
            project_path=project_path,
            app_name=app_name,
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
        )

    mode, reason, available = _workspace_mode(session.tool, project_path)
    return RuntimeAttachment(
        session_id=session.session_id,
        level="workspace" if available else "unavailable",
        mode=mode,
        label="Workspace return available" if available else "No live return target",
        action_label="Open workspace" if available else "Copy handoff",
        available=available,
        confidence="medium" if available else "low",
        reason=reason,
        tool=session.tool,
        surface=session.surface,
        project_path=project_path,
        app_name=app_name,
    )


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
            return {"ok": True, "message": "Opened project folder. Use the handoff brief if you need a fresh AI chat.", "attachment": attachment.to_json()}
    except OSError as exc:
        return {"ok": False, "message": f"Could not open return target: {exc}", "attachment": attachment.to_json()}

    return {
        "ok": False,
        "message": "No supported return action is available on this platform yet.",
        "attachment": attachment.to_json(),
    }

