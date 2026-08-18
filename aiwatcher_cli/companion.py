"""Lifecycle helpers for the dashboard-independent runtime companion."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import signal
import shutil
import socket
import subprocess
import sys
import time
from typing import Any

from .local_state import clear_watcher_heartbeat, get_ui_server, get_watcher_status, state_path


AUTOSTART_LABEL = "com.aiwatcher.local.companion"


def companion_log_path() -> Path:
    return state_path().parent / "companion.log"


def companion_autostart_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{AUTOSTART_LABEL}.plist"
    if sys.platform == "win32":
        startup = os.environ.get("APPDATA")
        if startup:
            return (
                Path(startup)
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
                / "Startup"
                / "AIWatcher Companion.cmd"
            )
    return state_path().parent / "aiwatcher-companion-autostart.unsupported"


def companion_command(
    interval_seconds: int,
    *,
    presence: bool = True,
    presence_position: str = "bottom-right",
    presence_visibility: str = "always",
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
        command.extend(
            [
                "--presence",
                "--presence-position",
                presence_position,
                "--presence-visibility",
                presence_visibility,
            ]
        )
    return command


def tray_command(interval_seconds: int = 30) -> list[str]:
    return [
        sys.executable,
        "-m",
        "aiwatcher_cli",
        "companion",
        "tray",
        "start",
        "--interval",
        str(max(15, int(interval_seconds))),
    ]


def install_login_autostart(
    interval_seconds: int = 30,
    *,
    presence: bool = True,
    presence_position: str = "bottom-right",
    presence_visibility: str = "always",
    tray: bool = False,
) -> dict[str, Any]:
    """Install best-effort login autostart for the local Companion.

    This writes only user-level startup entries. It does not install a service,
    daemon, login item entitlement, or anything requiring admin privileges.
    """
    command = tray_command(interval_seconds) if tray else companion_command(
        interval_seconds,
        presence=presence,
        presence_position=presence_position,
        presence_visibility=presence_visibility,
    )
    target = companion_autostart_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            payload = {
                "Label": AUTOSTART_LABEL,
                "ProgramArguments": command,
                "RunAtLoad": True,
                "KeepAlive": False,
                "StandardOutPath": str(companion_log_path()),
                "StandardErrorPath": str(companion_log_path()),
                "WorkingDirectory": str(Path.cwd()),
            }
            with target.open("wb") as handle:
                plistlib.dump(payload, handle)
            return {"ok": True, "path": str(target), "platform": "macos", "command": command}
        if sys.platform == "win32":
            quoted = " ".join(f'"{part}"' if " " in part else part for part in command)
            target.write_text(f"@echo off\r\nstart \"AIWatcher Companion\" /min {quoted}\r\n", encoding="utf-8")
            return {"ok": True, "path": str(target), "platform": "windows", "command": command}
        return {
            "ok": False,
            "path": str(target),
            "platform": sys.platform,
            "message": "Login autostart is currently implemented for macOS LaunchAgent and Windows Startup only.",
        }
    except OSError as exc:
        return {"ok": False, "path": str(target), "message": str(exc), "command": command}


def uninstall_login_autostart() -> dict[str, Any]:
    target = companion_autostart_path()
    try:
        target.unlink()
        return {"ok": True, "removed": True, "path": str(target)}
    except FileNotFoundError:
        return {"ok": True, "removed": False, "path": str(target), "message": "Login autostart is not installed."}
    except OSError as exc:
        return {"ok": False, "removed": False, "path": str(target), "message": str(exc)}


def login_autostart_status() -> dict[str, Any]:
    target = companion_autostart_path()
    return {
        "installed": target.exists(),
        "path": str(target),
        "platform": "macos" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else sys.platform,
        "supported": sys.platform in {"darwin", "win32"},
    }


def tray_status() -> dict[str, Any]:
    """Return the honest tray/menu-bar support level for this build."""
    if sys.platform == "darwin":
        return {
            "supported": True,
            "mode": "native_menu_bar",
            "label": "macOS menu-bar item",
            "detail": "Adds an AIW menu-bar item with Console, Plan Prompt, Scan Now, and Quit. Rich live nudges still use the floating Companion.",
        }
    if sys.platform == "win32":
        return {
            "supported": True,
            "mode": "native_system_tray",
            "label": "Windows system-tray icon",
            "detail": "Adds an AIWatcher notification-area icon with Console, Plan Prompt, Scan Now, and Quit. Rich live nudges still use the floating Companion.",
        }
    return {
        "supported": False,
        "mode": "fallback",
        "label": "No native tray support",
        "detail": "Use `aiwatcher ui` or `aiwatcher companion start --no-presence` on this platform.",
    }


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


def _terminate_pid(pid: int, *, force: bool = False) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, sig)
        return True
    except (OSError, ProcessLookupError):
        return False


def _pids_matching(pattern: str) -> set[int]:
    if sys.platform == "win32" or not shutil.which("pgrep"):
        return set()
    try:
        output = subprocess.check_output(["pgrep", "-f", pattern], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return set()
    pids: set[int] = set()
    current = {os.getpid(), os.getppid()}
    for line in output.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid > 0 and pid not in current:
            pids.add(pid)
    return pids


def _orphan_companion_pids() -> set[int]:
    patterns = (
        r"aiwatcher_cli companion run",
        r"aiwatcher_cli\.native_overlay .*--presence",
        r"aiwatcher-native-presence\.swift",
    )
    pids: set[int] = set()
    for pattern in patterns:
        pids.update(_pids_matching(pattern))
    return pids


def cleanup_orphan_companion_processes(*, exclude_pid: int | None = None) -> list[int]:
    stopped: list[int] = []
    for pid in sorted(_orphan_companion_pids()):
        if exclude_pid is not None and pid == exclude_pid:
            continue
        if _terminate_pid(pid):
            stopped.append(pid)
    return stopped


def start_companion(
    interval_seconds: int = 30,
    *,
    presence: bool = True,
    presence_position: str = "bottom-right",
    presence_visibility: str = "always",
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

    cleanup_orphan_companion_processes()
    log_path = companion_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = companion_command(
        interval_seconds,
        presence=presence,
        presence_position=presence_position,
        presence_visibility=presence_visibility,
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
        stopped_orphans = cleanup_orphan_companion_processes()
        clear_watcher_heartbeat()
        if stopped_orphans:
            return {
                "ok": True,
                "stopped": True,
                "message": f"Cleaned {len(stopped_orphans)} orphan presence process(es).",
                "orphan_pids": stopped_orphans,
            }
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
    stopped_primary = _terminate_pid(pid)
    stopped_orphans = cleanup_orphan_companion_processes(exclude_pid=pid)
    clear_watcher_heartbeat(pid=pid)
    if not stopped_primary and not stopped_orphans:
        return {"ok": True, "stopped": False, "message": "Companion was already stopped; cleared stale state.", "pid": pid}
    message = "Companion stopped."
    if stopped_orphans:
        message += f" Cleaned {len(stopped_orphans)} orphan presence process(es)."
    return {
        "ok": True,
        "stopped": True,
        "message": message,
        "pid": pid,
        "orphan_pids": stopped_orphans,
    }
