"""Local AI runtime process discovery and hygiene classification.

This module stays deliberately read-only: it inspects process metadata, labels
likely AI runtimes, and suggests commands the developer can run manually.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone


DEFAULT_STALE_MINUTES = 120


@dataclass
class ProcessRow:
    pid: int
    ppid: int | None
    state: str
    age_seconds: int | None
    rss_kb: int | None
    cpu_percent: float | None
    command: str


@dataclass
class RuntimeProcess:
    pid: int
    ppid: int | None
    age_seconds: int | None
    state: str
    tool: str
    command: str
    cwd: str | None = None
    session_id: str | None = None
    rss_kb: int | None = None
    cpu_percent: float | None = None
    stale_reasons: list[str] = field(default_factory=list)

    @property
    def stale(self) -> bool:
        return bool(self.stale_reasons)

    @property
    def kill_command(self) -> str:
        if os.name == "nt":
            return f"taskkill /PID {self.pid}"
        return f"kill {self.pid}"


def _parse_age_to_seconds(value: str) -> int | None:
    text = value.strip()
    if not text or text == "-":
        return None
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = [int(part) for part in parts]
        elif len(parts) == 2:
            hours = 0
            minutes, seconds = [int(part) for part in parts]
        else:
            return None
    except ValueError:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _read_ps_rows() -> list[ProcessRow]:
    if os.name == "nt":
        return []
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,stat=,etime=,rss=,%cpu=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    rows: list[ProcessRow] = []
    for line in completed.stdout.splitlines():
        parsed = parse_ps_line(line)
        if parsed:
            rows.append(parsed)
    return rows


def parse_ps_line(line: str) -> ProcessRow | None:
    parts = line.strip().split(None, 6)
    if len(parts) < 7:
        return None
    pid_text, ppid_text, state, age_text, rss_text, cpu_text, command = parts
    try:
        pid = int(pid_text)
    except ValueError:
        return None
    try:
        ppid = int(ppid_text)
    except ValueError:
        ppid = None
    try:
        rss_kb = int(rss_text)
    except ValueError:
        rss_kb = None
    try:
        cpu_percent = float(cpu_text)
    except ValueError:
        cpu_percent = None
    return ProcessRow(
        pid=pid,
        ppid=ppid,
        state=state,
        age_seconds=_parse_age_to_seconds(age_text),
        rss_kb=rss_kb,
        cpu_percent=cpu_percent,
        command=command,
    )


def _argv(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _flag_value(argv: list[str], *names: str) -> str | None:
    for index, part in enumerate(argv):
        for name in names:
            if part == name and index + 1 < len(argv):
                return argv[index + 1]
            if part.startswith(name + "="):
                return part.split("=", 1)[1]
    return None


def _path_missing(value: str | None) -> bool:
    if not value:
        return False
    expanded = os.path.expanduser(value)
    return not os.path.exists(expanded)


def _executable_label(argv: list[str]) -> str | None:
    if not argv:
        return None
    executable = argv[0]
    label = os.path.basename(executable.rstrip(os.sep))
    return label or executable


def _tool_label(command: str, argv: list[str]) -> str | None:
    executable = argv[0].lower() if argv else ""
    executable_name = os.path.basename(executable.rstrip(os.sep))
    executable_path = executable.replace("\\", "/")
    known_paths = " ".join(part.lower().replace("\\", "/") for part in argv[:3])
    has_session_kernel = any(part.endswith("kernel.js") for part in argv) and any(
        part in {"--session-id", "--session_id"} or part.startswith("--session-id=") or part.startswith("--session_id=")
        for part in argv
    )
    if (
        "skycomputeruseclient" in executable_name
        or "skycomputeruseclient" in executable_path
        or "computeruse" in executable_name
        or "computer-use" in executable_name
    ):
        return "Computer Use"
    if "node_repl" in known_paths or "node-repl" in known_paths:
        return "node_repl"
    if "cua_node" in executable_name or has_session_kernel:
        return "Codex runtime"
    if "codex.app/" in executable_path or executable_name in {"codex", "codex-cli"}:
        return "Codex"
    if "claude.app/" in executable_path or executable_name in {"claude", "claude-code"}:
        return "Claude"
    if "cursor.app/" in executable_path or executable_name == "cursor":
        return "Cursor"
    return None


def classify_process(row: ProcessRow, *, stale_minutes: int = DEFAULT_STALE_MINUTES) -> RuntimeProcess | None:
    argv = _argv(row.command)
    tool = _tool_label(row.command, argv)
    if not tool:
        return None

    cwd = _flag_value(argv, "--working-dir", "--working_dir", "--cwd", "--workspace", "--workspace-root")
    session_id = _flag_value(argv, "--session-id", "--session_id", "--conversation-id", "--conversation_id")
    kernel_path = next((part for part in argv if part.endswith("kernel.js")), None)
    reasons: list[str] = []
    age_seconds = row.age_seconds
    threshold = max(1, stale_minutes) * 60
    is_long_running = age_seconds is not None and age_seconds >= threshold

    if row.ppid == 1:
        reasons.append("orphaned parent (PPID=1)")
    if row.state.upper().startswith("T"):
        reasons.append("stopped/suspended process")
    if kernel_path and _path_missing(kernel_path):
        reasons.append("kernel path is missing")
    if cwd and _path_missing(cwd):
        reasons.append("working directory is missing")
    if (
        kernel_path
        and "tmp" in kernel_path.lower()
        and ("kernel" in kernel_path.lower() or "node_repl" in kernel_path.lower())
        and _path_missing(kernel_path)
    ):
        reasons.append("stale temporary kernel path")
    if is_long_running and reasons and tool in {"Codex runtime", "node_repl", "Computer Use"}:
        reasons.append(f"older than {stale_minutes}m")

    return RuntimeProcess(
        pid=row.pid,
        ppid=row.ppid,
        age_seconds=age_seconds,
        state=row.state,
        tool=tool,
        command=row.command,
        cwd=cwd,
        session_id=session_id,
        rss_kb=row.rss_kb,
        cpu_percent=row.cpu_percent,
        stale_reasons=sorted(set(reasons), key=reasons.index),
    )


def discover_runtime_processes(*, stale_minutes: int = DEFAULT_STALE_MINUTES) -> list[RuntimeProcess]:
    return [
        process
        for row in _read_ps_rows()
        if (process := classify_process(row, stale_minutes=stale_minutes)) is not None
    ]


def seconds_label(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    minutes = max(0, round(seconds / 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, remaining = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {remaining}m" if remaining else f"{hours}h"
    days, day_hours = divmod(hours, 24)
    return f"{days}d {day_hours}h" if day_hours else f"{days}d"


def rss_label(kb: int | None) -> str:
    if kb is None:
        return "unknown"
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:.1f}GB"
    return f"{kb / 1024:.0f}MB"


def cpu_label(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f}%"


def process_hygiene_summary(*, stale_minutes: int = DEFAULT_STALE_MINUTES) -> str | None:
    if os.name == "nt":
        return None
    processes = discover_runtime_processes(stale_minutes=stale_minutes)
    stale = [process for process in processes if process.stale]
    if not stale:
        return None
    rss_kb = sum(process.rss_kb or 0 for process in stale)
    return (
        f"Runtime hygiene: {len(stale)} likely stale local AI runtime"
        f"{'s' if len(stale) != 1 else ''} found using {rss_label(rss_kb)} RSS. "
        "Run `aiwatcher processes --stale-only` to review local CPU/RAM/battery cleanup candidates."
    )


def process_record(process: RuntimeProcess) -> dict[str, object]:
    argv = _argv(process.command)
    return {
        "pid": process.pid,
        "ppid": process.ppid,
        "age_seconds": process.age_seconds,
        "state": process.state,
        "tool": process.tool,
        "working_dir": process.cwd,
        "session_id": process.session_id,
        "rss_kb": process.rss_kb,
        "cpu_percent": process.cpu_percent,
        "stale": process.stale,
        "stale_reasons": process.stale_reasons,
        "suggested_kill_command": process.kill_command if process.stale else None,
        "executable": _executable_label(argv),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "resource_note": "RSS/CPU are local process resources, not model/API spend.",
    }


def platform_note() -> str | None:
    if os.name == "nt":
        return "Runtime process discovery is not available on Windows yet; no process scan was run."
    if sys.platform not in {"darwin", "linux"}:
        return f"Runtime process discovery is experimental on {sys.platform}."
    return None
