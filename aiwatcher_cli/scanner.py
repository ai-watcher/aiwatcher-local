"""Read-only local scanners for AIWatcher Local."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pricing import estimate_cost


HOME_DIR = Path.home().resolve()


def _env_path(name: str, *parts: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    return Path(value).joinpath(*parts)


def _path_candidates(*paths: Path | None) -> list[Path]:
    return [path.expanduser() for path in paths if path is not None]


CLAUDE_PROJECTS_DIRS = _path_candidates(HOME_DIR / ".claude" / "projects")
CURSOR_LOGS_DIRS = _path_candidates(
    HOME_DIR / "Library" / "Application Support" / "Cursor" / "logs",
    _env_path("APPDATA", "Cursor", "logs"),
)
CURSOR_STATE_DIRS = _path_candidates(
    HOME_DIR / ".cursor",
    _env_path("APPDATA", "Cursor"),
)
CODEX_DB_PATHS = _path_candidates(
    HOME_DIR / ".codex" / "state_5.sqlite",
    _env_path("APPDATA", "Codex", "state_5.sqlite"),
    _env_path("LOCALAPPDATA", "Codex", "state_5.sqlite"),
)
CODEX_DIRS = _path_candidates(
    HOME_DIR / ".codex",
    _env_path("APPDATA", "Codex"),
    _env_path("LOCALAPPDATA", "Codex"),
)
CLINE_DIRS = _path_candidates(
    HOME_DIR / ".cline",
    _env_path("APPDATA", "Cline"),
)
WINDSURF_DIRS = _path_candidates(
    HOME_DIR / "Library" / "Application Support" / "Windsurf",
    _env_path("APPDATA", "Windsurf"),
)

AI_FILE_PATTERNS = re.compile(r"(copilot|chat|inline|ghost|predict)", re.IGNORECASE)
GIT_ROOT_CACHE: dict[str, str | None] = {}
PROJECT_PATH_CACHE: dict[str, str | None] = {}


def _first_existing(paths: list[Path]) -> Path | None:
    return next((path for path in paths if path.exists()), None)


@dataclass
class LocalSession:
    session_id: str
    tool: str
    project_path: str | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    agent_calls: int = 0
    tool_calls: int = 0
    source_path: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> int:
        if not self.started_at or not self.updated_at:
            return 0
        return max(0, int((self.updated_at - self.started_at).total_seconds()))

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "tool": self.tool,
            "project_path": self.project_path,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "agent_calls": self.agent_calls,
            "tool_calls": self.tool_calls,
            "source_path": self.source_path,
            "notes": self.notes,
        }


@dataclass
class LocalEvent:
    event_id: str
    session_id: str
    tool: str
    event_type: str
    timestamp: datetime | None = None
    project_path: str | None = None
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    content_hash: str | None = None
    source_path: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "tool": self.tool,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "project_path": self.project_path,
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "content_hash": self.content_hash,
            "source_path": self.source_path,
            "notes": self.notes,
        }


def _hash_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except TypeError:
        payload = str(value)
    if not payload:
        return None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _event_id(session_id: str, index: int, event_type: str, timestamp: datetime | None) -> str:
    raw = f"{session_id}|{index}|{event_type}|{timestamp.isoformat() if timestamp else ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _decode_claude_project_path(encoded: str) -> str:
    windows_match = re.match(r"^-?([A-Za-z])--(.*)$", encoded)
    if windows_match:
        current = Path(f"{windows_match.group(1)}:/")
        raw_parts = windows_match.group(2)
    elif encoded.startswith("-"):
        current = Path("/")
        raw_parts = encoded[1:]
    else:
        return encoded

    parts = [part for part in raw_parts.split("-") if part]
    naive_path = current.joinpath(*parts)
    if naive_path.exists():
        return str(naive_path)

    index = 0
    while index < len(parts):
        match: Path | None = None
        match_end = index
        for end in range(index + 1, len(parts) + 1):
            candidate = current / "-".join(parts[index:end])
            if candidate.exists():
                match = candidate
                match_end = end
        if match is None:
            return str(naive_path)
        current = match
        index = match_end

    return str(current)


def _git_root(path: str) -> str | None:
    if not path or not Path(path).is_dir():
        return None
    if path in GIT_ROOT_CACHE:
        return GIT_ROOT_CACHE[path]
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return None
    if result.returncode != 0:
        GIT_ROOT_CACHE[path] = None
        return None
    value = result.stdout.strip()
    GIT_ROOT_CACHE[path] = value or None
    return GIT_ROOT_CACHE[path]


def _normalize_project_path(path: str | None) -> str | None:
    if not path:
        return None
    if path in PROJECT_PATH_CACHE:
        return PROJECT_PATH_CACHE[path]
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate

    if resolved == HOME_DIR:
        PROJECT_PATH_CACHE[path] = None
        return None

    raw = str(resolved)
    PROJECT_PATH_CACHE[path] = _git_root(raw) or raw
    return PROJECT_PATH_CACHE[path]


def _choose_project_path(
    fallback_path: str,
    cwd_counts: dict[str, int],
    cwd_costs: dict[str, float],
) -> str:
    candidates: dict[str, tuple[float, int]] = {}
    for cwd, count in cwd_counts.items():
        normalized = _normalize_project_path(cwd)
        if not normalized:
            continue
        cost, existing_count = candidates.get(normalized, (0.0, 0))
        candidates[normalized] = (cost + cwd_costs.get(cwd, 0.0), existing_count + count)

    if candidates:
        return max(candidates, key=lambda path: (candidates[path][0], candidates[path][1]))

    normalized_fallback = _normalize_project_path(fallback_path)
    return normalized_fallback or fallback_path


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            value = value / 1000
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _min_dt(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _max_dt(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def discover_tools() -> dict[str, bool]:
    return {
        "claude-code": any(path.exists() for path in CLAUDE_PROJECTS_DIRS),
        "cursor": any(path.exists() for path in [*CURSOR_STATE_DIRS, *CURSOR_LOGS_DIRS]),
        "codex-cli": any(path.exists() for path in [*CODEX_DB_PATHS, *CODEX_DIRS]),
        "cline": any(path.exists() for path in CLINE_DIRS),
        "windsurf": any(path.exists() for path in WINDSURF_DIRS),
    }


def scan_claude_code() -> list[LocalSession]:
    sessions: list[LocalSession] = []
    projects_dirs = [path for path in CLAUDE_PROJECTS_DIRS if path.exists()]
    if not projects_dirs:
        return sessions

    for projects_dir in projects_dirs:
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            fallback_project_path = _decode_claude_project_path(project_dir.name)
            for fpath_raw in glob.glob(str(project_dir / "*.jsonl")):
                fpath = Path(fpath_raw)
                session_id = fpath.stem
                events_seen = 0
                agent_calls = 0
                tool_calls = 0
                tokens_in = 0
                tokens_out = 0
                cost = 0.0
                model: str | None = None
                started_at: datetime | None = None
                updated_at: datetime | None = None
                cwd_counts: dict[str, int] = defaultdict(int)
                cwd_costs: dict[str, float] = defaultdict(float)
                try:
                    with fpath.open(errors="replace") as handle:
                        for line in handle:
                            if not line.strip():
                                continue
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            ts = _parse_ts(obj.get("timestamp") or obj.get("createdAt"))
                            started_at = _min_dt(started_at, ts)
                            updated_at = _max_dt(updated_at, ts)
                            cwd = obj.get("cwd")
                            if isinstance(cwd, str) and cwd:
                                cwd_counts[cwd] += 1

                            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                            msg_type = obj.get("type") or message.get("role")
                            usage = message.get("usage") or obj.get("usage") or {}
                            input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                            output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                            event_model = message.get("model") or obj.get("model")
                            event_cost = estimate_cost(event_model, input_tokens, output_tokens)
                            if isinstance(cwd, str) and cwd:
                                cwd_costs[cwd] += event_cost
                            tokens_in += input_tokens
                            tokens_out += output_tokens
                            cost += event_cost
                            if event_model:
                                model = event_model
                            if msg_type == "assistant" or event_model:
                                agent_calls += 1
                            if obj.get("toolUseResult") is not None or obj.get("toolUseID") or msg_type == "tool_result":
                                tool_calls += 1
                            events_seen += 1
                except OSError:
                    continue

                if events_seen == 0:
                    continue
                sessions.append(LocalSession(
                    session_id=session_id,
                    tool="claude-code",
                    project_path=fallback_project_path,
                    started_at=started_at or _mtime(fpath),
                    updated_at=_max_dt(updated_at, _mtime(fpath)) or _mtime(fpath),
                    model=model or "claude-code",
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost,
                    agent_calls=agent_calls,
                    tool_calls=tool_calls,
                    source_path=str(fpath),
                ))

                session = sessions[-1]
                session.project_path = _choose_project_path(fallback_project_path, cwd_counts, cwd_costs)

    return sessions


def scan_claude_code_events() -> list[LocalEvent]:
    events: list[LocalEvent] = []
    projects_dirs = [path for path in CLAUDE_PROJECTS_DIRS if path.exists()]
    if not projects_dirs:
        return events

    for projects_dir in projects_dirs:
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            fallback_project_path = _decode_claude_project_path(project_dir.name)
            for fpath_raw in glob.glob(str(project_dir / "*.jsonl")):
                fpath = Path(fpath_raw)
                session_id = fpath.stem
                try:
                    with fpath.open(errors="replace") as handle:
                        for index, line in enumerate(handle):
                            if not line.strip():
                                continue
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            ts = _parse_ts(obj.get("timestamp") or obj.get("createdAt"))
                            cwd = obj.get("cwd")
                            project_path = _normalize_project_path(cwd if isinstance(cwd, str) else None)
                            if not project_path:
                                project_path = _normalize_project_path(fallback_project_path) or fallback_project_path

                            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                            msg_type = obj.get("type") or "unknown"
                            model = message.get("model") or obj.get("model")
                            usage = message.get("usage") or obj.get("usage") or {}
                            input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                            output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                            event_cost = estimate_cost(model, input_tokens, output_tokens)

                            content_hash = None
                            content = message.get("content")
                            if msg_type in {"user", "assistant"}:
                                content_hash = _hash_text(content)
                            elif obj.get("toolUseID") or obj.get("toolUseResult") is not None:
                                content_hash = _hash_text({
                                    "toolUseID": obj.get("toolUseID"),
                                    "hasOutput": obj.get("hasOutput"),
                                    "operation": obj.get("operation"),
                                })

                            event_type = msg_type
                            if msg_type == "assistant" and isinstance(content, list):
                                if any(isinstance(item, dict) and item.get("type") == "tool_use" for item in content):
                                    event_type = "assistant_tool_use"
                            elif msg_type == "tool_result" or obj.get("toolUseResult") is not None:
                                event_type = "tool_result"

                            events.append(LocalEvent(
                                event_id=_event_id(session_id, index, event_type, ts),
                                session_id=session_id,
                                tool="claude-code",
                                event_type=event_type,
                                timestamp=ts,
                                project_path=project_path,
                                model=model,
                                tokens_in=input_tokens,
                                tokens_out=output_tokens,
                                cost_usd=event_cost,
                                content_hash=content_hash,
                                source_path=str(fpath),
                            ))
                except OSError:
                    continue

    return events


def scan_codex_cli() -> list[LocalSession]:
    codex_db = _first_existing(CODEX_DB_PATHS)
    if not codex_db:
        return []

    try:
        conn = sqlite3.connect(f"file:{codex_db}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return [
            LocalSession(
                session_id="codex-db-unreadable",
                tool="codex-cli",
                source_path=str(codex_db),
                notes=["Codex database detected but could not be opened read-only."],
            )
        ]

    sessions: list[LocalSession] = []
    try:
        rows = conn.execute(
            "SELECT id, cwd, title, model, tokens_used, created_at_ms, updated_at_ms, archived "
            "FROM threads ORDER BY created_at_ms DESC"
        ).fetchall()
        for row in rows:
            tokens = int(row["tokens_used"] or 0)
            sessions.append(
                LocalSession(
                    session_id=row["id"],
                    tool="codex-cli",
                    project_path=row["cwd"],
                    started_at=_parse_ts(row["created_at_ms"]),
                    updated_at=_parse_ts(row["updated_at_ms"]),
                    model=row["model"] or "codex",
                    tokens_in=tokens,
                    tokens_out=0,
                    cost_usd=0.0,
                    agent_calls=1 if tokens else 0,
                    source_path=str(codex_db),
                    notes=[
                        "tokens_used is Codex's cumulative thread total",
                        "Codex cost is subscription/plan-based, not estimated as API spend",
                    ],
                )
            )
    except sqlite3.Error as exc:
        sessions.append(
            LocalSession(
                session_id="codex-db-limited",
                tool="codex-cli",
                source_path=str(codex_db),
                notes=[f"Codex database detected, but thread details could not be read: {exc}"],
            )
        )
    finally:
        conn.close()
    return sessions


def scan_cursor_limited() -> list[LocalSession]:
    sessions: list[LocalSession] = []
    cursor_logs_dir = _first_existing(CURSOR_LOGS_DIRS)
    if not (any(path.exists() for path in CURSOR_STATE_DIRS) or cursor_logs_dir):
        return sessions
    if not cursor_logs_dir:
        return [
            LocalSession(
                session_id="cursor-detected",
                tool="cursor",
                notes=["Cursor detected, but local AI usage logs were not found."],
            )
        ]

    for log_dir in cursor_logs_dir.iterdir():
        if not log_dir.is_dir():
            continue
        ai_files = [
            child for child in log_dir.iterdir()
            if child.is_file() and AI_FILE_PATTERNS.search(child.name)
        ]
        if not ai_files:
            continue
        updated = max((_mtime(path) for path in ai_files), default=None)
        sessions.append(LocalSession(
            session_id=f"cursor-{log_dir.name}",
            tool="cursor",
            project_path=str(log_dir),
            updated_at=updated,
            model="cursor-ai",
            agent_calls=len(ai_files),
            source_path=str(log_dir),
            notes=["Cursor local logs are detected, but token and cost details are limited."],
        ))
    return sessions


def scan_all() -> list[LocalSession]:
    return [*scan_claude_code(), *scan_codex_cli(), *scan_cursor_limited()]


def scan_all_events() -> list[LocalEvent]:
    return scan_claude_code_events()
