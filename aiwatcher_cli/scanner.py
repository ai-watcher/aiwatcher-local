"""Read-only local scanners for AIWatcher Local."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .local_state import recent_hook_events
from .pricing import estimate_cost


HOME_DIR = Path.home().resolve()
TEMP_DIRS = {
    Path("/tmp").resolve(),
    Path("/private/tmp").resolve(),
    Path(tempfile.gettempdir()).resolve(),
}
COMMON_NON_PROJECT_DIRS = {
    HOME_DIR / "Desktop",
    HOME_DIR / "Documents",
    HOME_DIR / "Downloads",
    *TEMP_DIRS,
}


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
CODEX_SESSIONS_DIRS = _path_candidates(
    HOME_DIR / ".codex" / "sessions",
    _env_path("APPDATA", "Codex", "sessions"),
    _env_path("LOCALAPPDATA", "Codex", "sessions"),
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
CODEX_ROLLOUT_CACHE: tuple[
    tuple[tuple[str, int, int], ...],
    list["LocalSession"],
    list["LocalEvent"],
] | None = None


def _first_existing(paths: list[Path]) -> Path | None:
    return next((path for path in paths if path.exists()), None)


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _agent_worktree_owner(path: Path) -> Path | None:
    """The repository an agent worktree was cut from, if this is one.

    Claude Code runs isolated agents in a throwaway worktree at
    `<repo>/.claude/worktrees/agent-<id>`, then deletes it when the agent
    finishes. Because the directory is gone, the git-root lookup below cannot
    fold it back into its repository, so each one would otherwise rank as its
    own project -- splitting the real repo's cost across entries that no longer
    exist on disk.

    The owning repository is the path up to `.claude`, so recover it rather than
    dropping the session as unattributed.
    """
    parts = path.parts
    for index in range(1, len(parts) - 1):
        if parts[index] == ".claude" and parts[index + 1] == "worktrees":
            return Path(*parts[:index])
    return None


def _is_agent_scratch_path(path: Path) -> bool:
    """Scratch space a local AI tool created for itself under a temp root.

    Deliberately narrow: a temp subdirectory can be somebody's real project, so
    only agent-owned directory names are rejected, not everything under temp.
    """
    if not any(_is_inside(path, temp_dir) for temp_dir in TEMP_DIRS):
        return False
    return any(part == "claude" or part.startswith("claude-") for part in path.parts)


def _is_tool_storage_path(path: Path) -> bool:
    if _is_agent_scratch_path(path):
        return True
    storage_roots = [
        *CLAUDE_PROJECTS_DIRS,
        *CURSOR_LOGS_DIRS,
        *CURSOR_STATE_DIRS,
        *CODEX_DIRS,
        *CODEX_SESSIONS_DIRS,
        *CLINE_DIRS,
        *WINDSURF_DIRS,
    ]
    return any(_is_inside(path, root.resolve()) for root in storage_roots if root.exists())


@dataclass
class LocalSession:
    session_id: str
    tool: str
    project_path: str | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    # The last timestamp that came from an actual message, never the file mtime
    # that updated_at falls back to. Anything asking "was this session really
    # active recently?" must use this: a transcript whose trailing lines are
    # untimestamped housekeeping takes its updated_at from the mtime, so a file
    # merely touched by a backup or re-index reads as active today when its last
    # real message was weeks ago. None when the transcript carried no timestamps.
    last_message_at: datetime | None = None
    model: str | None = None
    # tokens_in counts EVERY input token the provider billed for, including the
    # cached ones. cache_read_tokens/cache_write_tokens break out how much of it
    # was replayed conversation history rather than new content -- the two are a
    # subset of tokens_in, not an addition to it, so don't sum all three.
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    agent_calls: int = 0
    tool_calls: int = 0
    source_path: str | None = None
    notes: list[str] = field(default_factory=list)
    # "cli" | "desktop" | None (host did not report which surface was used).
    surface: str | None = None
    # Per-model usage within this session: {model_name: {tokens_in, tokens_out,
    # cost_usd, agent_calls, tool_calls}}. `model` above is only the highest-usage
    # model for backward compatibility — a session that used more than one model
    # (e.g. Fable then Sonnet) is fully represented here, not just by its last model.
    model_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)

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
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "agent_calls": self.agent_calls,
            "tool_calls": self.tool_calls,
            "source_path": self.source_path,
            "notes": self.notes,
            "surface": self.surface,
            "model_breakdown": self.model_breakdown,
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
    # Same convention as LocalSession: tokens_in is all billed input, and the
    # two cache counters are a subset of it.
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    content_hash: str | None = None
    source_path: str | None = None
    notes: list[str] = field(default_factory=list)
    turn: int = 0

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
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "content_hash": self.content_hash,
            "source_path": self.source_path,
            "notes": self.notes,
            "turn": self.turn,
        }


@dataclass
class SurfaceCoverage:
    surface_id: str
    label: str
    status: str
    status_label: str
    detected: bool
    automatic_gate: str
    history: str
    action: str
    detail: str
    session_count: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "surface_id": self.surface_id,
            "label": self.label,
            "status": self.status,
            "status_label": self.status_label,
            "detected": self.detected,
            "automatic_gate": self.automatic_gate,
            "history": self.history,
            "action": self.action,
            "detail": self.detail,
            "session_count": self.session_count,
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


def _user_prompt_text(content: Any) -> str | None:
    """Pull natural-language text from a user message's content, or None if it is not a real prompt."""
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_result":
                return None  # user-role message that is actually a tool result, not a prompt
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "\n".join(parts).strip()
    else:
        return None
    if not text:
        return None
    # Skip slash-command wrappers and injected reminders; they are not the user's real ask.
    if text.startswith(("<command", "<local-command", "<system-reminder>", "Caveat:")):
        return None
    return text


def segment_session_by_prompt(source_path: str | None, *, max_chars: int = 2000) -> list[dict[str, object]]:
    """Split a Claude Code session into prompt-bounded turns.

    Each real user prompt opens a turn; all following assistant/tool work (until the
    next real prompt) is attributed to it. Returns one dict per turn with the prompt
    text and the cost/tokens/tool-calls/events accumulated during that turn.
    Reads prompt/text content on demand; the event scan itself stores only hashes.
    """
    if not source_path or not source_path.endswith(".jsonl"):
        return []
    segments: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    try:
        with Path(source_path).open(errors="replace") as handle:
            for index, line in enumerate(handle):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                if obj.get("type") == "user" and not obj.get("isMeta"):
                    text = _user_prompt_text(message.get("content"))
                    if text:
                        current = {
                            "prompt": text[:max_chars],
                            "turn": len(segments) + 1,
                            "cost_usd": 0.0,
                            "tokens": 0,
                            "tool_calls": 0,
                            "events": 0,
                        }
                        segments.append(current)
                        continue
                if current is None:
                    continue
                tokens = _anthropic_usage(message.get("usage") or obj.get("usage") or {})
                model = message.get("model") or obj.get("model")
                current["cost_usd"] = float(current["cost_usd"]) + estimate_cost(
                    model,
                    tokens["input"],
                    tokens["output"],
                    cache_write_5m=tokens["cache_write_5m"],
                    cache_write_1h=tokens["cache_write_1h"],
                    cache_read=tokens["cache_read"],
                    when=_parse_ts(obj.get("timestamp") or obj.get("createdAt")),
                )
                current["tokens"] = int(current["tokens"]) + _billed_input(tokens) + tokens["output"]
                current["events"] = int(current["events"]) + 1
                content = message.get("content")
                if isinstance(content, list):
                    current["tool_calls"] = int(current["tool_calls"]) + sum(
                        1 for item in content if isinstance(item, dict) and item.get("type") == "tool_use"
                    )
    except OSError:
        return []
    return segments


def extract_opening_prompt(source_path: str | None, *, max_chars: int = 4000) -> str | None:
    """Return the first genuine user prompt from a Claude Code .jsonl session file.

    Reads prompt content only on demand (the event scan itself stores hashes, not text).
    Returns None when the source is unavailable or holds no readable user prompt.
    """
    if not source_path or not source_path.endswith(".jsonl"):
        return None
    try:
        with Path(source_path).open(errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user" or obj.get("isMeta"):
                    continue
                message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                text = _user_prompt_text(message.get("content"))
                if text:
                    return text[:max_chars]
    except OSError:
        return None
    return None


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

    owner = _agent_worktree_owner(resolved)
    if owner is not None:
        # Re-normalize the owning repo so it still faces every check below --
        # a worktree under ~/.claude must resolve to home, and so to None.
        # The owner never contains .claude/worktrees, so this cannot recurse.
        PROJECT_PATH_CACHE[path] = _normalize_project_path(str(owner))
        return PROJECT_PATH_CACHE[path]

    if resolved == HOME_DIR or resolved in COMMON_NON_PROJECT_DIRS or resolved.parent == resolved or _is_tool_storage_path(resolved):
        PROJECT_PATH_CACHE[path] = None
        return None

    raw = str(resolved)
    PROJECT_PATH_CACHE[path] = _git_root(raw) or raw
    return PROJECT_PATH_CACHE[path]


_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w.-])(?:"
    r"[`'\"](?P<quoted>(?:~|/|[A-Za-z]:[\\/])[^`'\"\r\n]+)[`'\"]"
    r"|(?P<plain>(?:~|/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)+)"
    r"|(?:[A-Za-z]:[\\/][^\s:*?\"<>|`\r\n]+))"
    r")"
)
_JSON_TIMESTAMP_PREFIX_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')
CODEX_TAIL_INITIAL_BYTES = 8 * 1024 * 1024
CODEX_TAIL_MAX_BYTES = 128 * 1024 * 1024
CODEX_TAIL_MIN_FILE_BYTES = 16 * 1024 * 1024
CODEX_MAX_WINDOW_JSON_LINE_BYTES = 2 * 1024 * 1024


def _normalize_project_hint(path: str | None) -> str | None:
    """Normalize an explicit path mentioned by the user into a project root.

    This is intentionally conservative: the path must be absolute-ish and
    resolve to something on this machine, either directly or through an
    existing parent directory. It lets a prompt like "work in /repo/aiwatcher"
    override a stale/wrong tool cwd, without trying to infer projects from
    fuzzy topic words.
    """
    if not path:
        return None
    cleaned = path.strip().strip("`'\"()[]{}<>,.;:")
    if not cleaned:
        return None
    # Quoted prose can contain an absolute-looking token and then continue for
    # hundreds of characters. Treat that as prose, not a filesystem path; it is
    # both slow to normalize and likely to pollute project attribution.
    if len(cleaned) > 512 or "\n" in cleaned or "\r" in cleaned:
        return None
    try:
        candidate = Path(cleaned).expanduser()
    except RuntimeError:
        return None
    if any(ch.isspace() for ch in cleaned):
        try:
            if not candidate.exists():
                return None
        except OSError:
            return None
    if not candidate.is_absolute():
        return None

    probe = candidate
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.exists():
        return None
    if probe.is_file():
        probe = probe.parent

    return _normalize_project_path(str(probe))


def _project_hints_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    hints: list[str] = []
    seen: set[str] = set()
    for match in _ABSOLUTE_PATH_RE.finditer(text):
        normalized = _normalize_project_hint(match.group("quoted") or match.group("plain"))
        if normalized and normalized not in seen:
            hints.append(normalized)
            seen.add(normalized)
    return hints


_PROJECT_TRANSITION_RE = re.compile(
    r"(?:"
    r"\b(?:work|continue|resume|switch|move|implement|build|fix|edit|change)\b"
    r"[^\n.!?]{0,80}\b(?:in|inside|under|from|at)\s*"
    r"|\b(?:workspace|project|repo|repository)\s*(?::|is|=)\s*"
    r")$",
    re.IGNORECASE,
)


def _intentional_project_hints_from_text(text: str | None) -> list[str]:
    """Paths that are part of an explicit workspace-transition instruction.

    A path mention is weak evidence: developers routinely ask about configs,
    logs, or sibling repositories while remaining in the current project. An
    instruction such as "continue the work in /repo/app" is different: it is
    direct evidence that the recorded host cwd may be stale. Keep those two
    signals separate so neither one wins unconditionally.
    """
    if not text:
        return []
    hints: list[str] = []
    seen: set[str] = set()
    for match in _ABSOLUTE_PATH_RE.finditer(text):
        prefix = text[max(0, match.start() - 120):match.start()]
        if not _PROJECT_TRANSITION_RE.search(prefix):
            continue
        normalized = _normalize_project_hint(match.group("quoted") or match.group("plain"))
        if normalized and normalized not in seen:
            hints.append(normalized)
            seen.add(normalized)
    return hints


def _line_timestamp_from_prefix(line: str) -> datetime | None:
    stamp_match = _JSON_TIMESTAMP_PREFIX_RE.search(line[:160])
    if not stamp_match:
        return None
    return _parse_ts(stamp_match.group(1))


def _codex_rollout_lines(path: Path, since: datetime | None) -> Iterable[tuple[int, str]]:
    """Yield rollout lines, reading only the recent tail for windowed scans.

    Codex transcripts can grow to hundreds of megabytes and occasionally get a
    fresh mtime even when the session started months ago. For `since` scans,
    rollout rows are chronological and timestamp-prefixed, so we can seek near
    the end and expand backward until the chunk begins before the requested
    window. Full scans still read the whole file.
    """
    if since is None:
        with path.open(errors="replace") as handle:
            for index, line in enumerate(handle):
                yield index, line
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < CODEX_TAIL_MIN_FILE_BYTES:
        with path.open(errors="replace") as handle:
            for index, line in enumerate(handle):
                yield index, line
        return

    threshold = since.astimezone(timezone.utc) - MTIME_SAFETY_MARGIN
    window = min(CODEX_TAIL_INITIAL_BYTES, size)
    selected_start = 0
    selected_lines: list[str] = []
    while True:
        start = max(0, size - window)
        with path.open("rb") as handle:
            handle.seek(start)
            if start > 0:
                handle.readline()
            chunk = handle.read()
        lines = [line.decode("utf-8", errors="replace") for line in chunk.splitlines(keepends=True)]
        selected_start = start
        selected_lines = lines
        first_timestamp = next(
            (stamp for line in lines for stamp in [_line_timestamp_from_prefix(line)] if stamp is not None),
            None,
        )
        if start == 0 or (first_timestamp and first_timestamp.astimezone(timezone.utc) <= threshold):
            break
        if window >= min(CODEX_TAIL_MAX_BYTES, size):
            break
        window = min(window * 2, CODEX_TAIL_MAX_BYTES, size)
    approx_index = max(0, selected_start)
    for offset, line in enumerate(selected_lines):
        yield approx_index + offset, line


def _codex_window_line_is_essential(line: str) -> bool:
    prefix = line[:512]
    return (
        '"type":"token_count"' in prefix
        or '"type": "token_count"' in prefix
        or '"type":"session_meta"' in prefix
        or '"type": "session_meta"' in prefix
        or '"type":"turn_context"' in prefix
        or '"type": "turn_context"' in prefix
    )


def _codex_user_prompt_text(row_type: str | None, payload: dict[str, Any]) -> str | None:
    """Best-effort extraction of real user prompt text from Codex rollout rows.

    Codex rollout schemas have changed over time, so this accepts the common
    message/user_input shapes while avoiding assistant/tool payloads.
    """
    role = str(payload.get("role") or "").lower()
    payload_type = str(payload.get("type") or "").lower()
    if row_type in {"user_message", "user_prompt"}:
        candidates = [payload.get("text"), payload.get("message"), payload.get("prompt")]
    elif row_type == "event_msg" and payload_type in {"user_message", "user_prompt", "user_input"}:
        candidates = [payload.get("text"), payload.get("message"), payload.get("prompt")]
    elif row_type == "response_item" and role == "user":
        candidates = [payload.get("content"), payload.get("text")]
    else:
        return None

    parts: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, str):
            parts.append(candidate)
        elif isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("input_text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(item, str):
                    parts.append(item)
    text = "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    return text or None


def _choose_project_path(
    fallback_path: str,
    cwd_counts: dict[str, int],
    cwd_costs: dict[str, float],
    hint_counts: dict[str, int] | None = None,
    hint_costs: dict[str, float] | None = None,
    intentional_hint_counts: dict[str, int] | None = None,
) -> str:
    """Attribute a session using observed cwd plus explicit transition intent.

    A recorded cwd is an observation: the tool writes it on every event, so a
    single session carries hundreds of them. A path mentioned in a prompt is an
    inference from one line of text, and prompts mention paths for all sorts of
    reasons -- "does the setting in /etc/nginx/nginx.conf matter here?" is not a
    statement about which project the session belongs to.

    Ordinary path hints are fallback-only. A path in an explicit workspace
    transition may override a usable cwd because desktop tools can keep logging
    the workspace where a chat started after the user deliberately moves the
    task to another repository.
    """
    candidates: dict[str, tuple[float, int]] = {}
    for cwd, count in cwd_counts.items():
        normalized = _normalize_project_path(cwd)
        if not normalized:
            continue
        cost, existing_count = candidates.get(normalized, (0.0, 0))
        candidates[normalized] = (cost + cwd_costs.get(cwd, 0.0), existing_count + count)

    observed = max(candidates, key=lambda path: (candidates[path][0], candidates[path][1])) if candidates else None

    intentional_candidates: dict[str, int] = {}
    for hint, count in (intentional_hint_counts or {}).items():
        normalized = _normalize_project_path(hint)
        if normalized:
            intentional_candidates[normalized] = intentional_candidates.get(normalized, 0) + count
    if intentional_candidates:
        ranked = sorted(intentional_candidates.items(), key=lambda item: item[1], reverse=True)
        # Do not invent certainty when two explicit workspace instructions tie.
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            return ranked[0][0]

    if observed:
        return observed

    hint_candidates: dict[str, tuple[int, float]] = {}
    for hint, count in (hint_counts or {}).items():
        normalized = _normalize_project_path(hint)
        if not normalized:
            continue
        existing_count, existing_cost = hint_candidates.get(normalized, (0, 0.0))
        hint_candidates[normalized] = (
            existing_count + count,
            existing_cost + (hint_costs or {}).get(hint, 0.0),
        )
    if hint_candidates:
        return max(hint_candidates, key=lambda path: (hint_candidates[path][0], hint_candidates[path][1]))

    normalized_fallback = _normalize_project_path(fallback_path)
    return normalized_fallback or "unknown"


CLIP_FALLBACK_NOTE = "Windowed whole-session: this tool reports no per-turn events to clip by."


def session_in_window(session: LocalSession, since: datetime, until: datetime | None = None) -> bool:
    stamp = session.updated_at or session.started_at
    if not stamp:
        return False
    stamp = stamp.astimezone()
    if stamp < since.astimezone():
        return False
    return until is None or stamp <= until.astimezone()


def clip_sessions_to_window(
    rows: Sequence[LocalSession],
    events: Sequence[LocalEvent],
    since: datetime,
    *,
    until: datetime | None = None,
) -> list[LocalSession]:
    """Reduce each session to the spend that actually happened inside the window.

    The original rule was all-or-nothing on `updated_at`: a session touched once
    this week contributed every dollar it had ever cost, including turns from
    weeks earlier. With long-running sessions that badly overstates a window --
    on this repo's own history roughly half of a "last 7 days" total had
    happened before those 7 days ($372 of $766).

    Clipping is exact rather than apportioned: each event carries its own
    timestamp and cost, and event costs sum to session costs, so the in-window
    subset is simply summed.

    Sessions whose scanner emits no per-turn events -- Cursor, and the Codex
    sqlite path -- cannot be clipped. They keep the old whole-session rule and
    carry CLIP_FALLBACK_NOTE, so the imprecision stays visible instead of
    either vanishing from the window or being silently overstated.
    """
    by_session: dict[str, list[LocalEvent]] = defaultdict(list)
    for event in events:
        by_session[event.session_id].append(event)

    since_local = since.astimezone()
    until_local = until.astimezone() if until else None
    clipped: list[LocalSession] = []

    for row in rows:
        row_events = by_session.get(row.session_id)
        if not row_events:
            if session_in_window(row, since, until):
                fallback = replace(row, notes=[*row.notes, CLIP_FALLBACK_NOTE])
                clipped.append(fallback)
            continue

        in_window_events = []
        for event in row_events:
            if not event.timestamp:
                continue
            stamp = event.timestamp.astimezone()
            if stamp < since_local:
                continue
            if until_local is not None and stamp > until_local:
                continue
            in_window_events.append(event)
        if not in_window_events:
            continue

        model_totals: dict[str, dict[str, float]] = defaultdict(
            lambda: {"tokens_in": 0.0, "tokens_out": 0.0, "cost_usd": 0.0, "agent_calls": 0.0, "tool_calls": 0.0}
        )
        agent_calls = tool_calls = 0
        for event in in_window_events:
            # Mirrors how scan_claude_code classifies a turn, so a clipped
            # session's call counts stay comparable with an unclipped one.
            is_agent_call = event.event_type.startswith("assistant") or bool(event.model)
            is_tool_call = event.event_type == "tool_result"
            agent_calls += int(is_agent_call)
            tool_calls += int(is_tool_call)
            key = event.model or row.model
            if key:
                bucket = model_totals[key]
                bucket["tokens_in"] += event.tokens_in
                bucket["tokens_out"] += event.tokens_out
                bucket["cost_usd"] += event.cost_usd
                bucket["agent_calls"] += int(is_agent_call)
                bucket["tool_calls"] += int(is_tool_call)

        clipped.append(replace(
            row,
            tokens_in=sum(event.tokens_in for event in in_window_events),
            tokens_out=sum(event.tokens_out for event in in_window_events),
            cache_read_tokens=sum(event.cache_read_tokens for event in in_window_events),
            cache_write_tokens=sum(event.cache_write_tokens for event in in_window_events),
            cost_usd=sum(event.cost_usd for event in in_window_events),
            agent_calls=agent_calls,
            tool_calls=tool_calls,
            model_breakdown={key: dict(value) for key, value in model_totals.items()},
        ))

    return clipped


def _usage_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _anthropic_usage(usage: Any) -> dict[str, int]:
    """Split an Anthropic usage block into its separately-billed token buckets.

    With prompt caching on, `input_tokens` is only the *uncached remainder* --
    routinely single digits on a long session, while the prompt that was
    actually processed sits in `cache_creation_input_tokens` and
    `cache_read_input_tokens`. Reading `input_tokens` alone (which this scanner
    did until these buckets were added) understated observed cost by roughly
    11x across this repo's own history: every turn re-sends the whole
    conversation, and cached input is discounted but never free.

    Anthropic-shaped only. Codex uses the opposite convention -- its
    `input_tokens` already *includes* `cached_input_tokens` -- so passing a
    Codex usage block through here would double-count the cached portion.
    """
    if not isinstance(usage, dict):
        return {"input": 0, "output": 0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0}
    creation = usage.get("cache_creation")
    if isinstance(creation, dict):
        write_5m = _usage_int(creation.get("ephemeral_5m_input_tokens"))
        write_1h = _usage_int(creation.get("ephemeral_1h_input_tokens"))
    else:
        # Older logs report only the combined total with no TTL breakdown.
        # Anthropic's default TTL is 5m, so attribute it to that bucket rather
        # than to the pricier 1h one -- this under-estimates rather than over.
        write_5m = _usage_int(usage.get("cache_creation_input_tokens"))
        write_1h = 0
    return {
        "input": _usage_int(usage.get("input_tokens") or usage.get("prompt_tokens")),
        "output": _usage_int(usage.get("output_tokens") or usage.get("completion_tokens")),
        "cache_write_5m": write_5m,
        "cache_write_1h": write_1h,
        "cache_read": _usage_int(usage.get("cache_read_input_tokens")),
    }


def _usage_receipt_key(obj: Any, message: Any) -> str | None:
    """Identify the API request a transcript line's usage block belongs to.

    Claude Code writes one line per *content block*, not per API call: a reply
    containing text plus three tool_use blocks becomes four lines, milliseconds
    apart, each carrying an identical copy of the message-level `usage`. Usage
    is reported per request, so summing it per line counts one request's tokens
    once per block.

    Locally that inflated a single session from $71.57 to $127.19 -- 180 of 427
    requests over-counted, some four times over -- which is why the figure
    disagreed with Claude Code's own `/cost`.

    `requestId` is the primary key because it is exactly what it claims to be;
    `message.id` is the fallback for older transcripts that predate it. A line
    with neither returns None and is counted on its own, which is the safe
    direction: a missed dedup over-counts by one, while a bad key merge would
    silently discard a real request.
    """
    request_id = obj.get("requestId") if isinstance(obj, dict) else None
    if isinstance(request_id, str) and request_id:
        return request_id
    message_id = message.get("id") if isinstance(message, dict) else None
    if isinstance(message_id, str) and message_id:
        return message_id
    return None


def _billed_input(usage: dict[str, int]) -> int:
    """Every input token the provider charged for, cached or not."""
    return usage["input"] + usage["cache_write_5m"] + usage["cache_write_1h"] + usage["cache_read"]


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


@dataclass
class HookLiveness:
    """Whether a surface's hook is actually firing, judged only from evidence.

    state is one of:
      "working"  -- work happened on this surface and hooks fired for it
      "silent"   -- work happened on this surface and no hook fired for any of it
      "unproven" -- nothing recent enough to judge; claim nothing either way
    """
    state: str
    judged: int = 0
    hooked: int = 0
    last_hooked_at: datetime | None = None


def hook_liveness(
    sessions: Sequence[LocalSession],
    hook_events: Sequence[dict[str, Any]],
    *,
    session_tool: str,
    hook_tool: str,
    surface: str,
) -> HookLiveness:
    """Match hook events to sessions by id to prove a surface is really hooked.

    Deliberately not "did any hook fire lately": hook_events is a small ring
    buffer shared by every tool, so a run of Codex work evicts the Claude
    evidence and that test would flip a healthy surface to unverified for no
    reason. Matching ids instead asks the question that actually matters --
    when you worked on this surface, did AIWatcher see it? -- which needs no
    timer, and self-heals in both directions.

    Sessions older than the oldest event we still hold are excluded rather than
    counted as unhooked: their evidence may simply have aged out of the buffer,
    and "we forgot" must never be reported as "it failed".
    """
    stamped = [
        (row, _parse_ts(row.get("created_at")))
        for row in hook_events
        if isinstance(row, dict)
    ]
    stamped = [(row, ts) for row, ts in stamped if ts is not None]
    if not stamped:
        return HookLiveness(state="unproven")

    # The floor spans every tool: eviction is global, so a Codex event is just
    # as much proof that we still remember this far back as a Claude one.
    evidence_floor = min(ts for _, ts in stamped)
    hooked_ids = {
        str(row.get("session_id"))
        for row, _ in stamped
        if row.get("tool") == hook_tool and row.get("session_id")
    }

    judged = [
        row for row in sessions
        if row.tool == session_tool
        and row.surface == surface
        and row.last_message_at is not None
        and row.last_message_at >= evidence_floor
    ]
    if not judged:
        return HookLiveness(state="unproven")

    hooked = [row for row in judged if row.session_id in hooked_ids]
    last_hooked_at = max(
        (ts for row, ts in stamped
         if row.get("tool") == hook_tool and str(row.get("session_id")) in {r.session_id for r in hooked}),
        default=None,
    )
    return HookLiveness(
        # One match is enough: a session where you never submitted a prompt
        # fires no UserPromptSubmit, so partial coverage is normal and healthy.
        # Only zero-out-of-N means the hook is not reaching this surface.
        state="working" if hooked else "silent",
        judged=len(judged),
        hooked=len(hooked),
        last_hooked_at=last_hooked_at,
    )


def surface_coverage(sessions: Iterable[LocalSession] | None = None) -> list[SurfaceCoverage]:
    """Explain what AIWatcher can and cannot protect for each local surface.

    This is intentionally separate from `discover_tools()`: detection only says
    something exists on disk. Coverage tells the user whether AIWatcher can gate
    prompts automatically, read history, or only offer a manual companion flow.
    """
    detected = discover_tools()
    rows = list(sessions or [])

    def count(tool: str, surface: str | None = None) -> int:
        return sum(
            1
            for row in rows
            if row.tool == tool and (surface is None or row.surface == surface)
        )

    claude_sessions = count("claude-code")
    claude_cli_sessions = count("claude-code", "cli")
    claude_desktop_sessions = count("claude-code", "desktop")
    codex_sessions = count("codex-cli")
    codex_desktop_sessions = count("codex-cli", "desktop")
    cursor_sessions = count("cursor")

    try:
        hook_events = recent_hook_events(limit=50)
    except OSError:
        hook_events = []
    hooked_tools = {str(e.get("tool", "")) for e in hook_events if isinstance(e, dict)}
    claude_hook_seen = "claude" in hooked_tools
    codex_hook_seen = "codex" in hooked_tools
    desktop_code = hook_liveness(
        rows, hook_events, session_tool="claude-code", hook_tool="claude", surface="desktop",
    )
    cli_code = hook_liveness(
        rows, hook_events, session_tool="claude-code", hook_tool="claude", surface="cli",
    )
    codex_desktop = hook_liveness(
        rows, hook_events, session_tool="codex-cli", hook_tool="codex", surface="desktop",
    )

    return [
        SurfaceCoverage(
            surface_id="claude-code-cli",
            label="Claude Code CLI",
            # Held to the same evidence bar as the Desktop row below. This used
            # to report "automatic" whenever ~/.claude/projects existed, which
            # only proves Claude Code ran here once -- never that a hook is
            # installed, trusted, or firing. Uninstalling the hook left the row
            # claiming full protection.
            status=(
                "automatic" if cli_code.state == "working"
                else "silent" if cli_code.state == "silent"
                else "limited" if detected.get("claude-code")
                else "not_detected"
            ),
            status_label=(
                "Automatic gate + history" if cli_code.state == "working"
                else "Hook not firing" if cli_code.state == "silent"
                else "Hook-capable, verify locally" if detected.get("claude-code")
                else "Not detected"
            ),
            detected=bool(detected.get("claude-code")),
            automatic_gate="UserPromptSubmit and command gates when installed/trusted",
            history="Full local JSONL session and token history",
            action=(
                "Nothing to do." if cli_code.state == "working"
                else "Reinstall with `aiwatcher install-claude-hook`, then run `aiwatcher hook-status`."
                if cli_code.state == "silent"
                else "Submit a test prompt, then run `aiwatcher hook-status`."
            ),
            detail=(
                f"Verified: hooks fired for {cli_code.hooked} of {cli_code.judged} recent CLI sessions. "
                "Prompt/source content stays local."
                if cli_code.state == "working"
                else f"{cli_code.judged} recent CLI session(s) and no hook fired for any of them. "
                "The hook is not reaching this surface, so those prompts were ungated."
                if cli_code.state == "silent"
                # Deliberately not alarming: no recent CLI work to check is a
                # different thing from a hook that stopped working.
                else "No recent CLI sessions inside the retained evidence window, so coverage is "
                "unproven rather than broken. Prompt/source content stays local."
            ),
            session_count=claude_cli_sessions,
        ),
        SurfaceCoverage(
            surface_id="claude-desktop-code",
            label="Claude Desktop Code tab",
            status=(
                "automatic" if desktop_code.state == "working"
                else "silent" if desktop_code.state == "silent"
                else "limited" if detected.get("claude-code")
                else "unknown"
            ),
            status_label=(
                "Auto gate + history" if desktop_code.state == "working"
                else "Hook not firing" if desktop_code.state == "silent"
                else "Hook-capable, verify locally"
            ),
            detected=bool(detected.get("claude-code") or claude_desktop_sessions),
            automatic_gate="UserPromptSubmit hook fires from both Claude Code CLI and the Desktop Code tab",
            history="Visible when the host writes Claude Code JSONL",
            action=(
                "Nothing to do." if desktop_code.state == "working"
                else "Reinstall with `aiwatcher install-claude-hook`, then run `aiwatcher hook-status`."
                if desktop_code.state == "silent"
                else "Submit a test prompt, then run `aiwatcher hook-status`."
            ),
            detail=(
                f"Verified: hooks fired for {desktop_code.hooked} of {desktop_code.judged} recent Desktop "
                "sessions. Sessions with no prompt submitted fire no hook, so partial is normal."
                if desktop_code.state == "working"
                else f"{desktop_code.judged} recent Desktop session(s) and no hook fired for any of them. "
                "The hook is installed but not reaching this surface, so those prompts were ungated."
                if desktop_code.state == "silent"
                else "The user-level hook covers both Claude Code CLI and the Desktop Code tab."
            ),
            session_count=claude_desktop_sessions,
        ),
        SurfaceCoverage(
            surface_id="claude-desktop-chat",
            label="Claude Desktop general chat",
            status="companion",
            status_label="Companion only",
            detected=False,
            automatic_gate="No verified local hook interception",
            history="No reliable local token/cost scanner yet",
            action="Use the Prompt tab or MCP/manual preflight before sending risky prompts.",
            detail="AIWatcher should not claim automatic protection for general chat.",
        ),
        SurfaceCoverage(
            surface_id="claude-ai-browser",
            label="claude.ai browser",
            status="companion",
            status_label="Extension/companion",
            detected=False,
            automatic_gate="Browser companion can preflight when installed; no desktop hook",
            history="No local session history scanner",
            action="Use Prompt Companion or the browser extension when available.",
            detail="Browser surfaces are protected only by explicit local companion tooling.",
        ),
        SurfaceCoverage(
            surface_id="codex-cli",
            label="Codex CLI/TUI",
            status="automatic" if detected.get("codex-cli") else "not_detected",
            status_label="Hook-capable + partial history" if detected.get("codex-cli") else "Not detected",
            detected=bool(detected.get("codex-cli")),
            automatic_gate="UserPromptSubmit when the host invokes and trusts the hook",
            history="SQLite/JSONL history; subscription cost is observed usage, not invoice spend",
            action="Use `/hooks` and `aiwatcher hook-status` to verify invocation.",
            detail="Codex local token totals can be cumulative, so AIWatcher labels estimates carefully.",
            session_count=codex_sessions,
        ),
        SurfaceCoverage(
            surface_id="codex-desktop",
            label="Codex Desktop",
            # Was "is there any codex hook event in the buffer", which proves
            # nothing about the Desktop surface and was in practice satisfied by
            # test-written events. Same session-id matching as the Claude rows.
            status=(
                "limited" if codex_desktop.state == "working"
                else "silent" if codex_desktop.state == "silent"
                else "unverified" if codex_desktop_sessions
                else "companion"
            ),
            status_label=(
                "Hook active + history" if codex_desktop.state == "working"
                else "Hook not firing" if codex_desktop.state == "silent"
                else "Unverified automatic gate" if codex_desktop_sessions
                else "Companion only"
            ),
            detected=bool(codex_desktop_sessions),
            automatic_gate=(
                "Hook fires on this surface, matched by session id"
                if codex_desktop.state == "working"
                else "Do not assume Desktop conversation prompts invoke hooks"
            ),
            history="Visible only when Codex writes readable local sessions",
            action=(
                "Nothing to do." if codex_desktop.state == "working"
                else "Reinstall with `aiwatcher install-codex-hook`, then run `aiwatcher hook-status`."
                if codex_desktop.state == "silent"
                else "Use Prompt Companion unless `hook-status` proves the hook fired."
            ),
            detail=(
                f"Verified: hooks fired for {codex_desktop.hooked} of {codex_desktop.judged} recent "
                "Desktop session(s), matched by session id."
                if codex_desktop.state == "working"
                else f"{codex_desktop.judged} recent Desktop session(s) and no hook fired for any of "
                "them, so those prompts were ungated."
                if codex_desktop.state == "silent"
                # Codex tops out at "limited" even when proven: token totals here
                # are cumulative and subscription-based, so history stays weaker
                # than Claude's regardless of how well the gate is working.
                else "This surface needs real-device verification before stronger claims."
            ),
            session_count=codex_desktop_sessions,
        ),
        SurfaceCoverage(
            surface_id="cursor",
            label="Cursor",
            status="limited" if detected.get("cursor") else "not_detected",
            status_label="Limited local history" if detected.get("cursor") else "Not detected",
            detected=bool(detected.get("cursor")),
            automatic_gate="Prompt hook can pause and return a resubmittable brief when configured",
            history="Presence/log detection only; token/cost details may be unavailable",
            action="Treat Cursor numbers as coverage-limited until session fixtures improve.",
            detail="AIWatcher should be honest when Cursor is installed but not measurable.",
            session_count=cursor_sessions,
        ),
        SurfaceCoverage(
            surface_id="cline",
            label="Cline",
            status="unsupported" if detected.get("cline") else "not_detected",
            status_label="Detected, not scanned" if detected.get("cline") else "Not detected",
            detected=bool(detected.get("cline")),
            automatic_gate="No verified hook",
            history="Not scanned yet",
            action="No local cost/session claims yet.",
            detail="Detection is not coverage; this avoids a false green check.",
        ),
        SurfaceCoverage(
            surface_id="windsurf",
            label="Windsurf",
            status="unsupported" if detected.get("windsurf") else "not_detected",
            status_label="Detected, not scanned" if detected.get("windsurf") else "Not detected",
            detected=bool(detected.get("windsurf")),
            automatic_gate="No verified hook",
            history="Not scanned yet",
            action="No local cost/session claims yet.",
            detail="Detection is not coverage; this avoids a false green check.",
        ),
    ]


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
                # One usage block per API request, however many transcript
                # lines that request produced. See _usage_receipt_key.
                counted_requests: set[str] = set()
                events_seen = 0
                agent_calls = 0
                tool_calls = 0
                tokens_in = 0
                tokens_out = 0
                cache_read_tokens = 0
                cache_write_tokens = 0
                cost = 0.0
                model: str | None = None
                surface: str | None = None
                model_totals: dict[str, dict[str, float]] = defaultdict(
                    lambda: {"tokens_in": 0.0, "tokens_out": 0.0, "cost_usd": 0.0, "agent_calls": 0.0, "tool_calls": 0.0}
                )
                started_at: datetime | None = None
                updated_at: datetime | None = None
                trailing_untimestamped = False
                cwd_counts: dict[str, int] = defaultdict(int)
                cwd_costs: dict[str, float] = defaultdict(float)
                hint_counts: dict[str, int] = defaultdict(int)
                hint_costs: dict[str, float] = defaultdict(float)
                intentional_hint_counts: dict[str, int] = defaultdict(int)
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
                            if ts is not None:
                                updated_at = _max_dt(updated_at, ts)
                                trailing_untimestamped = False
                            else:
                                trailing_untimestamped = True
                            cwd = obj.get("cwd")
                            if isinstance(cwd, str) and cwd:
                                cwd_counts[cwd] += 1
                            if surface is None:
                                entrypoint = obj.get("entrypoint")
                                if entrypoint == "cli":
                                    surface = "cli"
                                elif entrypoint == "claude-desktop":
                                    surface = "desktop"

                            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                            msg_type = obj.get("type") or message.get("role")
                            tokens = _anthropic_usage(message.get("usage") or obj.get("usage") or {})
                            # See scan_claude_code_events: a multi-block reply
                            # repeats one request's usage on every line.
                            receipt = _usage_receipt_key(obj, message)
                            if receipt is not None:
                                if receipt in counted_requests:
                                    tokens = _anthropic_usage({})
                                else:
                                    counted_requests.add(receipt)
                            input_tokens = _billed_input(tokens)
                            output_tokens = tokens["output"]
                            event_model = message.get("model") or obj.get("model")
                            event_cost = estimate_cost(
                                event_model,
                                tokens["input"],
                                output_tokens,
                                cache_write_5m=tokens["cache_write_5m"],
                                cache_write_1h=tokens["cache_write_1h"],
                                cache_read=tokens["cache_read"],
                                when=ts,
                            )
                            if isinstance(cwd, str) and cwd:
                                cwd_costs[cwd] += event_cost
                            tokens_in += input_tokens
                            tokens_out += output_tokens
                            cache_read_tokens += tokens["cache_read"]
                            cache_write_tokens += tokens["cache_write_5m"] + tokens["cache_write_1h"]
                            cost += event_cost
                            if event_model:
                                model = event_model
                            prompt_text = _user_prompt_text(message.get("content")) if msg_type == "user" and not obj.get("isMeta") else None
                            for hint in _project_hints_from_text(prompt_text):
                                hint_counts[hint] += 1
                                hint_costs[hint] += event_cost
                            for hint in _intentional_project_hints_from_text(prompt_text):
                                intentional_hint_counts[hint] += 1
                            is_agent_call = bool(msg_type == "assistant" or event_model)
                            is_tool_call = bool(
                                obj.get("toolUseResult") is not None or obj.get("toolUseID") or msg_type == "tool_result"
                            )
                            if is_agent_call:
                                agent_calls += 1
                            if is_tool_call:
                                tool_calls += 1
                            # Attribute this event to whichever model is active (its own
                            # model if reported, else the last model seen) so a session
                            # that used more than one model keeps every model's usage
                            # visible instead of only the last model overwriting the rest.
                            model_key = event_model or model
                            if model_key:
                                bucket = model_totals[model_key]
                                bucket["tokens_in"] += input_tokens
                                bucket["tokens_out"] += output_tokens
                                bucket["cost_usd"] += event_cost
                                if is_agent_call:
                                    bucket["agent_calls"] += 1
                                if is_tool_call:
                                    bucket["tool_calls"] += 1
                            events_seen += 1
                except OSError:
                    continue

                if events_seen == 0:
                    continue
                # Capture before the mtime fallback below overwrites it.
                last_message_at = updated_at
                if trailing_untimestamped:
                    updated_at = _max_dt(updated_at, _mtime(fpath))
                primary_model = model or "claude-code"
                if model_totals:
                    primary_model = max(
                        model_totals.items(),
                        key=lambda item: item[1]["tokens_in"] + item[1]["tokens_out"],
                    )[0]
                sessions.append(LocalSession(
                    session_id=session_id,
                    tool="claude-code",
                    project_path=fallback_project_path,
                    started_at=started_at or _mtime(fpath),
                    updated_at=updated_at or _mtime(fpath),
                    last_message_at=last_message_at,
                    model=primary_model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    cost_usd=cost,
                    agent_calls=agent_calls,
                    tool_calls=tool_calls,
                    surface=surface,
                    model_breakdown={key: dict(value) for key, value in model_totals.items()},
                    source_path=str(fpath),
                ))

                session = sessions[-1]
                session.project_path = _choose_project_path(
                    fallback_project_path,
                    cwd_counts,
                    cwd_costs,
                    hint_counts,
                    hint_costs,
                    intentional_hint_counts,
                )

    return sessions


def scan_claude_code_events(since: datetime | None = None) -> list[LocalEvent]:
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
                if _too_old_to_matter(fpath, since):
                    continue
                session_id = fpath.stem
                turn = 0
                # One usage block per API request, however many transcript
                # lines that request produced. See _usage_receipt_key.
                counted_requests: set[str] = set()
                hinted_project_path: str | None = None
                intentional_project_path: str | None = None
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
                            resolved_project_path = (
                                _normalize_project_path(cwd if isinstance(cwd, str) else None)
                                or _normalize_project_path(fallback_project_path)
                            )
                            project_path = resolved_project_path or fallback_project_path

                            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                            msg_type = obj.get("type") or "unknown"
                            content = message.get("content")
                            prompt_text = _user_prompt_text(content) if msg_type == "user" and not obj.get("isMeta") else None
                            hints = _project_hints_from_text(prompt_text)
                            if hints:
                                hinted_project_path = hints[0]
                            intentional_hints = _intentional_project_hints_from_text(prompt_text)
                            if intentional_hints:
                                intentional_project_path = intentional_hints[0]
                            # Same precedence as _choose_project_path: ordinary
                            # references are fallback-only; an explicit
                            # workspace transition can correct a stale host cwd.
                            if intentional_project_path:
                                project_path = intentional_project_path
                            elif hinted_project_path and not resolved_project_path:
                                project_path = hinted_project_path
                            model = message.get("model") or obj.get("model")
                            tokens = _anthropic_usage(message.get("usage") or obj.get("usage") or {})
                            # Every line of a multi-block reply repeats the same
                            # usage; charge it to the first line only. The later
                            # lines still become events -- they are real content
                            # blocks -- they just carry no second copy of the bill.
                            receipt = _usage_receipt_key(obj, message)
                            if receipt is not None:
                                if receipt in counted_requests:
                                    tokens = _anthropic_usage({})
                                else:
                                    counted_requests.add(receipt)
                            input_tokens = _billed_input(tokens)
                            output_tokens = tokens["output"]
                            event_cost = estimate_cost(
                                model,
                                tokens["input"],
                                output_tokens,
                                cache_write_5m=tokens["cache_write_5m"],
                                cache_write_1h=tokens["cache_write_1h"],
                                cache_read=tokens["cache_read"],
                                when=ts,
                            )

                            content_hash = None
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

                            # A real user prompt opens a new turn; every following event belongs to it.
                            # Same boundary test as segment_session_by_prompt() so turn numbers align.
                            if msg_type == "user" and not obj.get("isMeta") and prompt_text:
                                turn += 1

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
                                cache_read_tokens=tokens["cache_read"],
                                cache_write_tokens=tokens["cache_write_5m"] + tokens["cache_write_1h"],
                                cost_usd=event_cost,
                                content_hash=content_hash,
                                source_path=str(fpath),
                                turn=turn,
                            ))
                except OSError:
                    continue

    return events


def scan_codex_cli(since: datetime | None = None) -> list[LocalSession]:
    rollout_sessions, _ = scan_codex_rollouts(since=since)
    codex_db = _first_existing(CODEX_DB_PATHS)
    if not codex_db:
        return rollout_sessions

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
            ),
            *rollout_sessions,
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
                    # Codex's own recorded timestamp, never a file mtime, so it
                    # is a sound basis for judging real activity.
                    last_message_at=_parse_ts(row["updated_at_ms"]),
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
    by_id = {row.session_id: row for row in sessions}
    for rollout in rollout_sessions:
        by_id[rollout.session_id] = rollout
    return sorted(
        by_id.values(),
        key=lambda row: row.updated_at or row.started_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def scan_codex_rollouts(since: datetime | None = None) -> tuple[list[LocalSession], list[LocalEvent]]:
    global CODEX_ROLLOUT_CACHE
    sessions: list[LocalSession] = []
    events: list[LocalEvent] = []
    paths: list[Path] = []
    for root in CODEX_SESSIONS_DIRS:
        if not root.exists():
            continue
        paths.extend(
            path for path in root.rglob("*.jsonl")
            if not _too_old_to_matter(path, since)
        )
    signature_rows: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature_rows.append((str(path), stat.st_mtime_ns, stat.st_size))
    signature = tuple(sorted(signature_rows))
    if CODEX_ROLLOUT_CACHE and CODEX_ROLLOUT_CACHE[0] == signature:
        return list(CODEX_ROLLOUT_CACHE[1]), list(CODEX_ROLLOUT_CACHE[2])

    for path in paths:
        session_id = path.stem
        project_path: str | None = None
        model: str | None = None
        surface: str | None = None
        started_at: datetime | None = None
        updated_at: datetime | None = None
        final_input = 0
        final_output = 0
        agent_calls = 0
        tool_calls = 0
        previous_total = -1
        hint_counts: dict[str, int] = defaultdict(int)
        hint_costs: dict[str, float] = defaultdict(float)
        intentional_hint_counts: dict[str, int] = defaultdict(int)
        model_totals: dict[str, dict[str, float]] = defaultdict(
            lambda: {"tokens_in": 0.0, "tokens_out": 0.0, "cost_usd": 0.0, "agent_calls": 0.0, "tool_calls": 0.0}
        )
        try:
            for index, line in _codex_rollout_lines(path, since):
                    if not line or line == "\n":
                        continue
                    if since is not None:
                        line_timestamp = _line_timestamp_from_prefix(line)
                        if line_timestamp and line_timestamp < since - MTIME_SAFETY_MARGIN:
                            continue
                        if (
                            len(line) > CODEX_MAX_WINDOW_JSON_LINE_BYTES
                            and not _codex_window_line_is_essential(line)
                        ):
                            continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    timestamp = _parse_ts(row.get("timestamp"))
                    started_at = _min_dt(started_at, timestamp)
                    updated_at = _max_dt(updated_at, timestamp)
                    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                    row_type = row.get("type")
                    if row_type == "session_meta":
                        session_id = str(payload.get("id") or payload.get("session_id") or session_id)
                        project_path = _normalize_project_path(str(payload.get("cwd") or "")) or project_path
                        if surface is None:
                            originator = str(payload.get("originator") or "").lower()
                            if "desktop" in originator:
                                surface = "desktop"
                            elif "cli" in originator or "tui" in originator:
                                surface = "cli"
                    elif row_type == "turn_context":
                        project_path = _normalize_project_path(str(payload.get("cwd") or "")) or project_path
                        model = str(payload.get("model") or model or "codex")
                    elif row_type == "response_item" and payload.get("type") in {
                        "function_call", "custom_tool_call", "local_shell_call"
                    }:
                        tool_calls += 1
                        if model:
                            model_totals[model]["tool_calls"] += 1
                    prompt_text = _codex_user_prompt_text(row_type, payload)
                    for hint in _project_hints_from_text(prompt_text):
                        hint_counts[hint] += 1
                        hint_costs[hint] += estimate_cost(model, 0, 0)
                    for hint in _intentional_project_hints_from_text(prompt_text):
                        intentional_hint_counts[hint] += 1
                    if hint_counts:
                        # project_path here is the cwd Codex recorded in
                        # session_meta/turn_context. Pass it as observed evidence
                        # rather than a bare fallback, so prompt hints only take
                        # over when it did not resolve to a usable project.
                        observed_cwd = {project_path: 1} if project_path else {}
                        project_path = _choose_project_path(
                            project_path or "",
                            observed_cwd,
                            {},
                            hint_counts,
                            hint_costs,
                            intentional_hint_counts,
                        )
                    if row_type != "event_msg" or payload.get("type") != "token_count":
                        continue
                    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                    total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
                    last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
                    total_tokens = int(total.get("total_tokens") or 0)
                    if not total_tokens or total_tokens == previous_total:
                        continue
                    previous_total = total_tokens
                    final_input = int(total.get("input_tokens") or 0)
                    final_output = int(total.get("output_tokens") or 0)
                    agent_calls += 1
                    event_input = int(last.get("input_tokens") or 0)
                    event_output = int(last.get("output_tokens") or 0)
                    event_cost = estimate_cost(model, event_input, event_output, when=timestamp)
                    # Attribute each incremental turn's tokens/cost to whichever model
                    # was active for that turn — total_token_usage is cumulative and
                    # priced with only the final model, but these per-turn deltas let
                    # a session that switched models keep every model's share visible.
                    model_key = model or "codex"
                    bucket = model_totals[model_key]
                    bucket["tokens_in"] += event_input
                    bucket["tokens_out"] += event_output
                    bucket["cost_usd"] += event_cost
                    bucket["agent_calls"] += 1
                    events.append(LocalEvent(
                        event_id=_event_id(session_id, index, "model_usage", timestamp),
                        session_id=session_id,
                        tool="codex-cli",
                        event_type="model_usage",
                        timestamp=timestamp,
                        project_path=project_path,
                        model=model or "codex",
                        tokens_in=event_input,
                        tokens_out=event_output,
                        cost_usd=event_cost,
                        source_path=str(path),
                        notes=["Measured from Codex rollout token_count event"],
                    ))
        except OSError:
            continue
        if not final_input and not final_output:
            continue
        if hint_counts:
            observed_cwd = {project_path: 1} if project_path else {}
            project_path = _choose_project_path(
                project_path or "",
                observed_cwd,
                {},
                hint_counts,
                hint_costs,
                intentional_hint_counts,
            )
        sessions.append(LocalSession(
            session_id=session_id,
            tool="codex-cli",
            project_path=project_path,
            started_at=started_at or _mtime(path),
            updated_at=updated_at or _mtime(path),
            # Content-derived only, so surface coverage can tell real activity
            # from a file the OS merely touched. See LocalSession.last_message_at.
            last_message_at=updated_at,
            model=model or "codex",
            tokens_in=final_input,
            tokens_out=final_output,
            # Session-level total, so it is dated by the session's last turn:
            # a rollup has no single moment, and the newest turn is the closest
            # honest answer for which rate card applied.
            cost_usd=estimate_cost(model, final_input, final_output, when=updated_at),
            agent_calls=agent_calls,
            tool_calls=tool_calls,
            source_path=str(path),
            surface=surface,
            model_breakdown={key: dict(value) for key, value in model_totals.items()},
            notes=[
                "Measured from Codex rollout token_count events",
                "Codex cost is subscription/plan-based, not invoice spend",
            ],
        ))
    CODEX_ROLLOUT_CACHE = (signature, list(sessions), list(events))
    return sessions, events


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


# Display-only relabeling for model identifiers that aren't really models.
# "<synthetic>" is Claude Code/Desktop's own marker for a client-injected
# message (e.g. a rate-limit notice) with zero tokens and zero cost — not an
# actual model response. Kept as the raw dict key everywhere internally;
# only the label shown to the user changes.
MODEL_DISPLAY_NAMES: dict[str, str] = {
    "<synthetic>": "Session limit model",
}


def display_model_name(model: str | None) -> str:
    if not model:
        return "unknown"
    return MODEL_DISPLAY_NAMES.get(model, model)


def model_usage_totals(sessions: Iterable[LocalSession]) -> dict[str, dict[str, float]]:
    """Flatten each session's model_breakdown into a global per-model total.

    A session that used more than one model contributes to every model's bucket
    here, instead of collapsing to whichever single model the session's `model`
    field happened to record last.
    """
    totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"tokens_in": 0.0, "tokens_out": 0.0, "cost_usd": 0.0, "agent_calls": 0.0, "tool_calls": 0.0, "sessions": 0.0}
    )
    session_counts: dict[str, set[str]] = defaultdict(set)
    for row in sessions:
        breakdown = row.model_breakdown or {
            (row.model or "unknown"): {
                "tokens_in": row.tokens_in,
                "tokens_out": row.tokens_out,
                "cost_usd": row.cost_usd,
                "agent_calls": row.agent_calls,
                "tool_calls": row.tool_calls,
            }
        }
        for model_name, stats in breakdown.items():
            key = model_name or "unknown"
            bucket = totals[key]
            bucket["tokens_in"] += float(stats.get("tokens_in", 0))
            bucket["tokens_out"] += float(stats.get("tokens_out", 0))
            bucket["cost_usd"] += float(stats.get("cost_usd", 0))
            bucket["agent_calls"] += float(stats.get("agent_calls", 0))
            bucket["tool_calls"] += float(stats.get("tool_calls", 0))
            session_counts[key].add(row.session_id)
    for key, ids in session_counts.items():
        totals[key]["sessions"] = float(len(ids))
    return dict(totals)


def scan_all(since: datetime | None = None) -> list[LocalSession]:
    return [*scan_claude_code(), *scan_codex_cli(since=since), *scan_cursor_limited()]


# How far before a caller's `since` a transcript file may have been last
# written and still be worth reading. Generous on purpose: mtime is the only
# cheap signal available, and a copied or restored file can carry a stale one.
# Reading a file needlessly costs milliseconds; skipping one loses events.
MTIME_SAFETY_MARGIN = timedelta(days=2)


def _too_old_to_matter(path: Path | str, since: datetime | None) -> bool:
    """True when a transcript cannot hold events at or after `since`.

    Transcripts are append-only, so a file untouched since before the window
    has nothing in it for that window. This is what lets the post-commit
    receipt read two days of history instead of every session ever recorded.
    """
    if since is None:
        return False
    try:
        mtime = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc)
    except OSError:
        return False
    return mtime < since - MTIME_SAFETY_MARGIN


def scan_all_events(since: datetime | None = None) -> list[LocalEvent]:
    """Every model-usage event, optionally only those a window could contain.

    `since` is a read optimisation, not a filter: it skips transcript files
    whose last write predates the window, and callers still window the events
    themselves. Passing it never adds events, and on a machine with a long
    history it turns a ~1s scan into a fraction of that.
    """
    _, codex_events = scan_codex_rollouts(since=since)
    return [*scan_claude_code_events(since=since), *codex_events]
