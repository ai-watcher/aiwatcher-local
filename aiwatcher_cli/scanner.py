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
from typing import Any, Iterable

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


@dataclass
class LocalSession:
    session_id: str
    tool: str
    project_path: str | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
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
    claude_desktop_sessions = count("claude-code", "desktop")
    codex_sessions = count("codex-cli")
    codex_desktop_sessions = count("codex-cli", "desktop")
    cursor_sessions = count("cursor")

    return [
        SurfaceCoverage(
            surface_id="claude-code-cli",
            label="Claude Code CLI",
            status="automatic" if detected.get("claude-code") else "not_detected",
            status_label="Automatic gate + history" if detected.get("claude-code") else "Not detected",
            detected=bool(detected.get("claude-code")),
            automatic_gate="UserPromptSubmit and command gates when installed/trusted",
            history="Full local JSONL session and token history",
            action="Verify with `aiwatcher hook-status`.",
            detail="Best-covered Claude surface. Prompt/source content stays local.",
            session_count=claude_sessions,
        ),
        SurfaceCoverage(
            surface_id="claude-desktop-code",
            label="Claude Desktop Code tab",
            status="limited" if detected.get("claude-code") else "unknown",
            status_label="Hook-capable, verify locally",
            detected=bool(detected.get("claude-code") or claude_desktop_sessions),
            automatic_gate="Works only when the Desktop Code tab invokes Claude Code hooks",
            history="Visible when the host writes Claude Code JSONL",
            action="Submit a test prompt, then run `aiwatcher hook-status`.",
            detail="Do not assume every Claude Desktop surface behaves the same.",
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
            status="unverified" if codex_desktop_sessions else "companion",
            status_label="Unverified automatic gate",
            detected=bool(codex_desktop_sessions),
            automatic_gate="Do not assume Desktop conversation prompts invoke hooks",
            history="Visible only when Codex writes readable local sessions",
            action="Use Prompt Companion unless `hook-status` proves the hook fired.",
            detail="This surface needs real-device verification before stronger claims.",
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
                turn = 0
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
                            tokens = _anthropic_usage(message.get("usage") or obj.get("usage") or {})
                            input_tokens = _billed_input(tokens)
                            output_tokens = tokens["output"]
                            event_cost = estimate_cost(
                                model,
                                tokens["input"],
                                output_tokens,
                                cache_write_5m=tokens["cache_write_5m"],
                                cache_write_1h=tokens["cache_write_1h"],
                                cache_read=tokens["cache_read"],
                            )

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

                            # A real user prompt opens a new turn; every following event belongs to it.
                            # Same boundary test as segment_session_by_prompt() so turn numbers align.
                            if msg_type == "user" and not obj.get("isMeta") and _user_prompt_text(content):
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


def scan_codex_cli() -> list[LocalSession]:
    rollout_sessions, _ = scan_codex_rollouts()
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


def scan_codex_rollouts() -> tuple[list[LocalSession], list[LocalEvent]]:
    global CODEX_ROLLOUT_CACHE
    sessions: list[LocalSession] = []
    events: list[LocalEvent] = []
    paths: list[Path] = []
    for root in CODEX_SESSIONS_DIRS:
        if not root.exists():
            continue
        paths.extend(root.rglob("*.jsonl"))
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
        model_totals: dict[str, dict[str, float]] = defaultdict(
            lambda: {"tokens_in": 0.0, "tokens_out": 0.0, "cost_usd": 0.0, "agent_calls": 0.0, "tool_calls": 0.0}
        )
        try:
            with path.open(errors="replace") as handle:
                for index, line in enumerate(handle):
                    if not line.strip():
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
                    event_cost = estimate_cost(model, event_input, event_output)
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
        sessions.append(LocalSession(
            session_id=session_id,
            tool="codex-cli",
            project_path=project_path,
            started_at=started_at or _mtime(path),
            updated_at=updated_at or _mtime(path),
            model=model or "codex",
            tokens_in=final_input,
            tokens_out=final_output,
            cost_usd=estimate_cost(model, final_input, final_output),
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


def scan_all() -> list[LocalSession]:
    return [*scan_claude_code(), *scan_codex_cli(), *scan_cursor_limited()]


def scan_all_events() -> list[LocalEvent]:
    _, codex_events = scan_codex_rollouts()
    return [*scan_claude_code_events(), *codex_events]
