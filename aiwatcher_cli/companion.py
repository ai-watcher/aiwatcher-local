"""Lifecycle helpers for the dashboard-independent runtime companion."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from typing import Any

from .local_state import clear_watcher_heartbeat, get_ui_server, get_watcher_status, state_path


def companion_log_path() -> Path:
    return state_path().parent / "companion.log"


def companion_command(
    interval_seconds: int,
    *,
    presence: bool = True,
    presence_position: str = "bottom-right",
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "aiwatcher_cli",
        "companion",
        "run",
        "--interval",
        str(max(15, int(interval_seconds))),
    ]
    if presence:
        command.extend(["--presence", "--presence-position", presence_position])
    return command


def local_action_server_available() -> bool:
    server = get_ui_server()
    if not server:
        return False
    host = str(server.get("host") or "127.0.0.1")
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    try:
        with socket.create_connection((host, int(server["port"])), timeout=0.3):
            return True
    except (OSError, TypeError, ValueError):
        return False


def start_companion(
    interval_seconds: int = 30,
    *,
    presence: bool = True,
    presence_position: str = "bottom-right",
) -> dict[str, Any]:
    current = get_watcher_status(max_age_seconds=max(45, interval_seconds * 2))
    if current.get("running"):
        if current.get("mode") == "companion":
            return {"ok": True, "already_running": True, **current}
        return {
            "ok": False,
            "already_running": False,
            "message": (
                f"A legacy ambient watch is already running (PID {current.get('pid')}). "
                "Stop it before starting the companion so nudges are not duplicated."
            ),
            **current,
        }

    log_path = companion_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = companion_command(
        interval_seconds,
        presence=presence,
        presence_position=presence_position,
    )
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "start_new_session": sys.platform != "win32",
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    try:
        with log_path.open("ab") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log, **kwargs)
    except OSError as exc:
        return {"ok": False, "message": str(exc), "command": command, "log_path": str(log_path)}

    for _ in range(20):
        time.sleep(0.1)
        status = get_watcher_status(max_age_seconds=max(45, interval_seconds * 2))
        if status.get("running") and status.get("pid") == process.pid:
            return {"ok": True, "already_running": False, **status, "log_path": str(log_path)}
        if process.poll() is not None:
            break
    return {
        "ok": process.poll() is None,
        "already_running": False,
        "pid": process.pid,
        "status": "starting" if process.poll() is None else "failed",
        "log_path": str(log_path),
    }


def stop_companion() -> dict[str, Any]:
    current = get_watcher_status()
    pid = current.get("pid")
    if not isinstance(pid, int):
        clear_watcher_heartbeat()
        return {"ok": True, "stopped": False, "message": "Companion is not running."}
    if current.get("mode") != "companion":
        return {
            "ok": False,
            "stopped": False,
            "message": (
                f"PID {pid} belongs to a foreground watch, not the companion. "
                "Stop that command in its terminal."
            ),
            "pid": pid,
        }
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "stopped": False, "message": str(exc), "pid": pid}
    clear_watcher_heartbeat(pid=pid)
    return {"ok": True, "stopped": True, "message": "Companion stopped.", "pid": pid}
