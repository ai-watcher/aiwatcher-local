"""Read-only local scanners for AIWatcher Local."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat as stat_module
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .local_state import recent_hook_events
from .pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    cache_read_cost,
    estimate_cost,
    lookup,
)


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
CODEX_ARCHIVED_SESSIONS_DIRS = _path_candidates(
    HOME_DIR / ".codex" / "archived_sessions",
    _env_path("APPDATA", "Codex", "archived_sessions"),
    _env_path("LOCALAPPDATA", "Codex", "archived_sessions"),
)
CODEX_AGENT_RUNNING_FRESHNESS = timedelta(minutes=5)
CODEX_AGENT_CLOCK_SKEW_TOLERANCE = timedelta(minutes=2)
CODEX_ROLLOUT_MAX_LINE_BYTES = 1024 * 1024
CODEX_ROLLOUT_SCAN_MAX_BYTES = 4 * 1024 * 1024
CODEX_ROLLOUT_SCAN_MAX_RECORDS = 10_000
CODEX_ROLLOUT_REQUEST_MAX_BYTES = 64 * 1024 * 1024
CODEX_ROLLOUT_REQUEST_MAX_FILES = 2048
CODEX_ROLLOUT_REQUEST_MAX_RECORDS = 100_000
CODEX_LIFECYCLE_CACHE_MAX_ENTRIES = 4096
_CODEX_LIFECYCLE_CACHE: dict[str, tuple[int, int, dict[str, Any] | None]] = {}


@dataclass
class _CodexRolloutScanBudget:
    bytes_remaining: int = field(default_factory=lambda: CODEX_ROLLOUT_REQUEST_MAX_BYTES)
    files_remaining: int = field(default_factory=lambda: CODEX_ROLLOUT_REQUEST_MAX_FILES)
    records_remaining: int = field(default_factory=lambda: CODEX_ROLLOUT_REQUEST_MAX_RECORDS)

    def begin_file(self) -> bool:
        if self.files_remaining <= 0:
            return False
        self.files_remaining -= 1
        return True

    def take_bytes(self, requested: int) -> int:
        allowed = min(requested, self.bytes_remaining)
        self.bytes_remaining -= allowed
        return allowed

    def take_record(self) -> bool:
        if self.records_remaining <= 0:
            return False
        self.records_remaining -= 1
        return True
CLINE_DIRS = _path_candidates(
    HOME_DIR / ".cline",
    _env_path("APPDATA", "Cline"),
)
WINDSURF_DIRS = _path_candidates(
    HOME_DIR / "Library" / "Application Support" / "Windsurf",
    _env_path("APPDATA", "Windsurf"),
)
OLLAMA_DIRS = _path_candidates(
    HOME_DIR / ".ollama",
    Path("/Applications/Ollama.app"),
    _env_path("LOCALAPPDATA", "Ollama"),
    _env_path("APPDATA", "Ollama"),
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
    # The working directory the tool actually logged, before project_path
    # folded it to a git root. Kept because that folding is lossy in a way
    # that matters: <project>/.aiwatcher/analyst normalises to <project>,
    # which would leave AIWatcher unable to tell its own analyst runs from
    # the user's work and quietly inflate every number it reports.
    raw_cwd: str | None = None
    notes: list[str] = field(default_factory=list)
    # "cli" | "desktop" | None (host did not report which surface was used).
    surface: str | None = None
    # What the chat is called: the name the user gave it, else the one the tool
    # generated (Claude Code's customTitle / aiTitle rows, Codex's threads.title).
    # None when the tool recorded neither.
    title: str | None = None
    # Per-model usage within this session: {model_name: {tokens_in, tokens_out,
    # cost_usd, agent_calls, tool_calls}}. `model` above is only the highest-usage
    # model for backward compatibility — a session that used more than one model
    # (e.g. Fable then Sonnet) is fully represented here, not just by its last model.
    model_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def analyst_run(self) -> bool:
        """A Second Opinion analyst spawn, not work the user did.

        Reported rather than excluded: hiding it would be dishonest, and it
        would fail the first time somebody asks what the feature costs.
        """
        from . import analyst
        return analyst.is_analyst_cwd(self.raw_cwd)

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
            "raw_cwd": self.raw_cwd,
            "analyst_run": self.analyst_run,
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
            "title": self.title,
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
    command_protection: str = "unknown"
    command_protection_label: str = "Unknown"
    command_protection_detail: str = "Command-level protection has not been classified for this surface yet."

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
            "command_protection": self.command_protection,
            "command_protection_label": self.command_protection_label,
            "command_protection_detail": self.command_protection_detail,
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
    # The Claude desktop app writes "<!-- attach -->" ahead of a quoted
    # selection. It is not something typed, so it goes; the quote stays.
    text = _LEADING_HTML_COMMENTS.sub("", text).strip()
    if not text:
        return None
    if text.startswith(INJECTED_ROW_PREFIXES):
        return None
    return text


# User-role rows the tool writes itself: slash-command wrappers and their
# output, injected reminders, and background-task notifications. None of them
# is a thing the person typed, so none counts as a prompt -- here and in
# statusline.read_transcript, which reads the same log.
INJECTED_ROW_PREFIXES = ("<command", "<local-command", "<system-reminder>", "<task-notification>", "Caveat:")
_LEADING_HTML_COMMENTS = re.compile(r"^(?:\s*<!--.*?-->)+", re.S)


def segment_session_by_prompt(source_path: str | None, *, max_chars: int = 2000) -> list[dict[str, object]]:
    """Split a Claude Code session into prompt-bounded turns.

    Each real user prompt opens a turn; all following assistant/tool work (until the
    next real prompt) is attributed to it. Returns one dict per turn with the prompt
    text and the cost/tokens/tool-calls/events accumulated during that turn.
    Reads prompt/text content on demand; the event scan itself stores only hashes.

    Counted the way the event scan counts, so turn numbers and costs agree with
    it: a row Claude Code wrote again at a compaction is skipped
    (_repeated_row), and one request's usage, copied onto every content-block
    line, is counted once (_usage_receipt_key). Until 2026-09-15 this function
    did neither, and on this machine a session the event scan put at $44 summed
    to $270 across its turns, with 21 prompts counted twice after compactions.

    Each turn also carries what a receipt for that prompt needs, all from the
    requests themselves:

      at               when the prompt was sent
      requests         model calls it caused
      context_before   the chat's size on the last request before it (None for the first prompt)
      context_after    the chat's size on its last request
      took_seconds     prompt to last request
      gap_seconds      last request before it to the prompt: how long the chat sat idle
      cost_resent_usd  the chat read back from the cache on each request
      cost_recached_usd  the existing chat written into the cache again -- cache
                       writes beyond what that request added, which is what a
                       cache that expired during a pause costs
      priced           every request had a list price; False means cost_usd
                       leaves some of it out
    """
    if not source_path or not source_path.endswith(".jsonl"):
        return []
    if _is_codex_rollout(source_path):
        return _segment_codex_rollout(source_path, max_chars=max_chars)
    segments: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    seen_rows: set[str] = set()
    counted_requests: set[str] = set()
    last_context: int | None = None
    last_request_at: datetime | None = None
    # Whether the chat has written 1-hour cache entries yet. A cache that holds
    # for an hour does not expire in a 13-minute pause, so what counts as a
    # break long enough to explain a re-cache depends on it.
    writes_1h = False
    try:
        with Path(source_path).open(errors="replace") as handle:
            for index, line in enumerate(handle):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _repeated_row(obj, seen_rows):
                    continue
                message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                stamp = _parse_ts(obj.get("timestamp") or obj.get("createdAt"))
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
                            "at": stamp.isoformat() if stamp else None,
                            "requests": 0,
                            "context_before": last_context,
                            "context_after": None,
                            "took_seconds": None,
                            "gap_seconds": (
                                round((stamp - last_request_at).total_seconds())
                                if stamp and last_request_at else None
                            ),
                            "cost_resent_usd": 0.0,
                            "cost_recached_usd": 0.0,
                            "priced": True,
                            # Claude Code's own summary after a compaction arrives as a
                            # user row and opens a turn here and in the event scan alike;
                            # it is flagged rather than dropped so turn numbers still agree.
                            "compact_summary": bool(obj.get("isCompactSummary")),
                            "cache_lifetime_seconds": 3600 if writes_1h else 300,
                        }
                        segments.append(current)
                        continue
                if current is None:
                    continue
                content = message.get("content")
                if isinstance(content, list):
                    current["tool_calls"] = int(current["tool_calls"]) + sum(
                        1 for item in content if isinstance(item, dict) and item.get("type") == "tool_use"
                    )
                current["events"] = int(current["events"]) + 1
                receipt = _usage_receipt_key(obj, message)
                if receipt is not None:
                    if receipt in counted_requests:
                        continue
                    counted_requests.add(receipt)
                tokens = _anthropic_usage(message.get("usage") or obj.get("usage") or {})
                context = _billed_input(tokens)
                if context <= 0 and tokens["output"] <= 0:
                    continue
                model = message.get("model") or obj.get("model")
                current["cost_usd"] = float(current["cost_usd"]) + estimate_cost(
                    model,
                    tokens["input"],
                    tokens["output"],
                    cache_write_5m=tokens["cache_write_5m"],
                    cache_write_1h=tokens["cache_write_1h"],
                    cache_read=tokens["cache_read"],
                    when=stamp,
                )
                current["tokens"] = int(current["tokens"]) + context + tokens["output"]
                if context <= 0:
                    continue
                resent, recached, priced = _request_cost_split(model, tokens, last_context, stamp)
                current["cost_resent_usd"] = float(current["cost_resent_usd"]) + resent
                current["cost_recached_usd"] = float(current["cost_recached_usd"]) + recached
                current["priced"] = bool(current["priced"]) and priced
                current["requests"] = int(current["requests"]) + 1
                current["context_after"] = context
                if stamp and current.get("at"):
                    current["took_seconds"] = round((stamp - datetime.fromisoformat(str(current["at"]))).total_seconds())
                last_context = context
                if tokens["cache_write_1h"] > 0:
                    writes_1h = True
                if stamp:
                    last_request_at = stamp
    except OSError:
        return []
    return segments


def _request_cost_split(
    model: str | None, tokens: dict[str, int], previous_context: int | None, when: datetime | None,
) -> tuple[float, float, bool]:
    """(re-sent, re-cached, priced) for one request, in list-price dollars.

    Re-sent is the cache read. Re-cached is the part of the cache write that
    is not this request's own growth: writing 600K when the chat grew by 2K
    means the existing chat went back into the cache, which is what an
    expired cache costs. The first request of a session has nothing earlier
    to re-cache. Subscription and unknown models are not priced.
    """
    rates = lookup(model, when)
    if not rates or rates.get("subscription"):
        return 0.0, 0.0, False
    price_in = float(rates["in"]) / 1_000_000
    resent = tokens["cache_read"] * price_in * CACHE_READ_MULTIPLIER
    written = tokens["cache_write_5m"] + tokens["cache_write_1h"]
    if previous_context is None or written <= 0:
        return resent, 0.0, True
    growth = max(0, _billed_input(tokens) - previous_context)
    rewritten = max(0, written - growth)
    multiplier = (
        tokens["cache_write_5m"] * CACHE_WRITE_5M_MULTIPLIER + tokens["cache_write_1h"] * CACHE_WRITE_1H_MULTIPLIER
    ) / written
    return resent, rewritten * price_in * multiplier, True


def current_prompt_segment(segments: list[dict[str, object]]) -> dict[str, object] | None:
    """The prompt a session is on now, from `segment_session_by_prompt`.

    The last turn, unless it is a row Claude Code wrote rather than the user
    and it caused nothing: an interrupted-reply marker with no requests is not
    what the session is doing. A prompt just sent, with no reply yet, is --
    that is the moment "working" matters most. Shared by the Companion and the
    statusline so both name the same prompt.
    """
    for segment in reversed(segments):
        if int(segment.get("requests") or 0) > 0:
            return segment
        text = str(segment.get("prompt") or "")
        if not segment.get("compact_summary") and not text.startswith("[Request interrupted"):
            return segment
    return None


_CODEX_ROW_TYPES = frozenset({"session_meta", "turn_context", "response_item", "event_msg", "compacted"})


def _is_codex_rollout(path: str) -> bool:
    """A Codex rollout, told from a Claude Code transcript by its first row."""
    try:
        with Path(path).open(errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    return False
                return isinstance(obj, dict) and obj.get("type") in _CODEX_ROW_TYPES
    except OSError:
        return False
    return False


def _segment_codex_rollout(path: str, *, max_chars: int = 2000) -> list[dict[str, object]]:
    """`segment_session_by_prompt` for a Codex rollout: the same keys, so the
    session review and the Companion treat both tools alike.

    Built from Codex's own records, and not yet checked against real rollouts
    on a machine that runs Codex daily (none on the one this was written on):

      - A prompt is the typed row: `event_msg` user_message before Codex
        0.149.1, `item_completed` UserMessage after. Codex also echoes each
        prompt as a user-role `response_item`, and injects environment and
        AGENTS.md rows the same way, so those count only in a rollout that has
        no typed rows at all (older formats), and never when injected.
      - A request is a `token_count` event, one call reported once (a repeated
        total is the same call, as in the scanner). `last_token_usage.input_tokens`
        is the whole prompt sent, cached tokens included; `cached_input_tokens`
        bill at the cached rate; `output_tokens` is taken to include reasoning.
      - Re-sent is the cached input. OpenAI charges no premium to write the
        cache, so nothing is ever re-cached: that part is always zero.
    """
    rows: list[tuple[datetime | None, str, dict[str, Any], str | None]] = []
    try:
        with Path(path).open(errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                row_type = str(obj.get("type") or "")
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                rows.append((_parse_ts(obj.get("timestamp")), row_type, payload, _codex_user_prompt_text(row_type, payload)))
    except OSError:
        return []
    typed_rows = any(text and row_type != "response_item" for _, row_type, _, text in rows)

    segments: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    model: str | None = None
    last_context: int | None = None
    last_request_at: datetime | None = None
    previous_total = -1
    for stamp, row_type, payload, text in rows:
        if row_type == "turn_context" and payload.get("model"):
            model = str(payload["model"])
        if text and (row_type != "response_item" or not typed_rows):
            current = {
                "prompt": text[:max_chars],
                "turn": len(segments) + 1,
                "cost_usd": 0.0,
                "tokens": 0,
                "tool_calls": 0,
                "events": 0,
                "at": stamp.isoformat() if stamp else None,
                "requests": 0,
                "context_before": last_context,
                "context_after": None,
                "took_seconds": None,
                "gap_seconds": round((stamp - last_request_at).total_seconds()) if stamp and last_request_at else None,
                "cost_resent_usd": 0.0,
                "cost_recached_usd": 0.0,
                "priced": True,
                "compact_summary": False,
                "cache_lifetime_seconds": 300,
            }
            segments.append(current)
            continue
        if current is not None:
            current["events"] = int(current["events"]) + 1
            if row_type == "response_item" and payload.get("type") in {"function_call", "custom_tool_call", "local_shell_call"}:
                current["tool_calls"] = int(current["tool_calls"]) + 1
        if row_type != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
        last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
        total_tokens = int(total.get("total_tokens") or 0)
        if not total_tokens or total_tokens == previous_total:
            continue
        previous_total = total_tokens
        context = int(last.get("input_tokens") or 0)
        if context <= 0:
            continue
        cached = min(context, int(last.get("cached_input_tokens") or 0))
        output = int(last.get("output_tokens") or 0)
        if current is not None:
            name = model or "codex"
            rates = lookup(name, stamp)
            current["cost_usd"] = float(current["cost_usd"]) + estimate_cost(
                name, context - cached, output, cache_read=cached, when=stamp,
            )
            current["cost_resent_usd"] = float(current["cost_resent_usd"]) + cache_read_cost(name, cached, stamp)
            current["priced"] = bool(current["priced"]) and bool(rates) and not rates.get("subscription")
            current["requests"] = int(current["requests"]) + 1
            current["context_after"] = context
            current["tokens"] = int(current["tokens"]) + context + output
            if stamp and current.get("at"):
                current["took_seconds"] = round((stamp - datetime.fromisoformat(str(current["at"]))).total_seconds())
        last_context = context
        if stamp:
            last_request_at = stamp
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
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
    elif row_type == "event_msg" and payload_type == "item_completed":
        # Codex 0.149.1 (~2026-08-25) moved the typed prompt into an envelope:
        # {"type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":...}]}}
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        if str(item.get("type") or "") != "UserMessage":
            return None
        candidates = [item.get("content")]
    elif row_type == "response_item" and role == "user":
        candidates = [payload.get("content"), payload.get("text")]
    else:
        return None

    parts: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, str):
            parts.append(candidate)
        elif isinstance(candidate, list):
            for part in candidate:
                if isinstance(part, dict):
                    # Text parts only: a "skill" or image part is not what was typed.
                    if part.get("type") not in (None, "text", "input_text"):
                        continue
                    text = part.get("text") or part.get("input_text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(part, str):
                    parts.append(part)
    text = "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    if text.startswith(CODEX_INJECTED_PREFIXES):
        return None
    return text or None


# User-role rows Codex writes itself at the start of a session: the environment
# it runs in and the AGENTS.md instructions it loaded. Not something typed.
CODEX_INJECTED_PREFIXES = ("<environment_context>", "<user_instructions>", "# AGENTS.md instructions")


def _dominant_cwd(cwd_counts: dict[str, int], cwd_costs: dict[str, float]) -> str | None:
    """The working directory this session mostly ran in, unnormalised.

    Ranked the way _choose_project_path ranks its candidates -- by spend
    first, then by event count -- so the raw path and the attributed project
    describe the same directory rather than two different ones.
    """
    if not cwd_counts:
        return None
    return max(cwd_counts, key=lambda cwd: (cwd_costs.get(cwd, 0.0), cwd_counts[cwd]))


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


def _repeated_row(obj: Any, seen: set[str]) -> bool:
    """A transcript row the tool has written before.

    Claude Code appends copies of earlier rows when it compacts -- observed
    2026-09-09: 149 rows at one compaction, 1,333 at the next, the same
    uuids and timestamps as the originals, placed after the live tail and
    just before the compact_boundary row. A reader that walks the file in
    order and trusts the last row it meets then takes a morning-old context
    size for the current one, right at the moment that number matters. So a
    row counts once, on its first appearance. Rows without a uuid (the
    tool's own bookkeeping lines) are never repeats.
    """
    row_uuid = obj.get("uuid") if isinstance(obj, dict) else None
    if not isinstance(row_uuid, str) or not row_uuid:
        return False
    if row_uuid in seen:
        return True
    seen.add(row_uuid)
    return False


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
    runtime_tools: set[str] = set()
    try:
        from .processes import discover_runtime_processes

        runtime_tools = {process.tool.lower() for process in discover_runtime_processes()}
    except OSError:
        runtime_tools = set()
    return {
        "claude-code": any(path.exists() for path in CLAUDE_PROJECTS_DIRS),
        "cursor": any(path.exists() for path in [*CURSOR_STATE_DIRS, *CURSOR_LOGS_DIRS]) or "cursor" in runtime_tools,
        "codex-cli": any(path.exists() for path in [*CODEX_DB_PATHS, *CODEX_DIRS]),
        "cline": any(path.exists() for path in CLINE_DIRS),
        "ollama": bool(shutil.which("ollama")) or any(path.exists() for path in OLLAMA_DIRS) or "ollama" in runtime_tools,
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

    try:
        hook_events = recent_hook_events(limit=50)
    except OSError:
        hook_events = []
    hooked_tools = {str(e.get("tool", "")) for e in hook_events if isinstance(e, dict)}
    claude_hook_seen = "claude" in hooked_tools
    codex_hook_seen = "codex" in hooked_tools

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
            command_protection="block" if detected.get("claude-code") else "not_detected",
            command_protection_label=(
                "Can block risky commands" if detected.get("claude-code") else "Not detected"
            ),
            command_protection_detail=(
                "Claude Code PreToolUse lets AIWatcher pause risky Bash commands before they run."
                if detected.get("claude-code") else
                "Install Claude Code before enabling command protection."
            ),
        ),
        SurfaceCoverage(
            surface_id="claude-desktop-code",
            label="Claude Desktop Code tab",
            status=(
                "limited" if (claude_desktop_sessions and detected.get("claude-code") and claude_hook_seen)
                else "limited" if detected.get("claude-code")
                else "unknown"
            ),
            status_label=(
                "History seen; hook unverified" if (claude_desktop_sessions and detected.get("claude-code") and claude_hook_seen)
                else "Hook-capable, verify locally"
            ),
            detected=bool(detected.get("claude-code") or claude_desktop_sessions),
            automatic_gate="UserPromptSubmit may fire from the Desktop Code tab, but verify this exact surface",
            history="Visible when the host writes Claude Code JSONL",
            action=(
                "Verify with `aiwatcher hook-status` after a Desktop Code-tab prompt." if (claude_desktop_sessions and claude_hook_seen)
                else "Submit a test prompt, then run `aiwatcher hook-status`."
            ),
            detail="Claude hook events were observed somewhere on this machine; Desktop interception still needs same-surface proof.",
            session_count=claude_desktop_sessions,
            command_protection="verify_host" if detected.get("claude-code") else "not_detected",
            command_protection_label=(
                "Verify command gate" if detected.get("claude-code") else "Not detected"
            ),
            command_protection_detail=(
                "If this Desktop Code build invokes Claude PreToolUse, AIWatcher can block risky Bash commands; otherwise use prompt preflight plus Watch."
                if detected.get("claude-code") else
                "No Claude Code surface was detected on this machine."
            ),
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
            command_protection="manual",
            command_protection_label="Manual only",
            command_protection_detail="General chat does not expose a verified pre-tool command lifecycle to AIWatcher.",
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
            command_protection="manual",
            command_protection_label="Manual only",
            command_protection_detail="Use prompt planning before execution; command calls are not locally interceptable yet.",
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
            command_protection="warn_observe" if detected.get("codex-cli") else "not_detected",
            command_protection_label=(
                "Warn + observe" if detected.get("codex-cli") else "Not detected"
            ),
            command_protection_detail=(
                "No verified Codex pre-tool command hook yet; AIWatcher uses prompt intent gating and local history evidence."
                if detected.get("codex-cli") else
                "Install Codex before enabling prompt or command awareness."
            ),
        ),
        SurfaceCoverage(
            surface_id="codex-desktop",
            label="Codex Desktop",
            status=(
                "limited" if (codex_desktop_sessions and detected.get("codex-cli") and codex_hook_seen)
                else "unverified" if codex_desktop_sessions
                else "companion"
            ),
            status_label=(
                "History seen; hook unverified" if (codex_desktop_sessions and detected.get("codex-cli") and codex_hook_seen)
                else "Unverified automatic gate" if codex_desktop_sessions
                else "Companion only"
            ),
            detected=bool(codex_desktop_sessions),
            automatic_gate=(
                "Do not assume Desktop conversation prompts invoke hooks; verify this exact surface"
                if (codex_desktop_sessions and codex_hook_seen)
                else "Do not assume Desktop conversation prompts invoke hooks"
            ),
            history="Visible only when Codex writes readable local sessions",
            action=(
                "Verify with `aiwatcher hook-status` after a Desktop prompt."
                if (codex_desktop_sessions and codex_hook_seen)
                else "Use Prompt Companion unless `hook-status` proves the hook fired."
            ),
            detail=(
                "Codex hook events were observed somewhere on this machine; Desktop interception still needs same-surface proof."
                if (codex_desktop_sessions and codex_hook_seen)
                else "This surface needs real-device verification before stronger claims."
            ),
            session_count=codex_desktop_sessions,
            command_protection="warn_observe" if codex_desktop_sessions else "manual",
            command_protection_label=(
                "Warn + observe" if codex_desktop_sessions else "Manual until verified"
            ),
            command_protection_detail=(
                "Prompt hooks can catch risky intent when this Desktop build invokes them; command execution is observed from local evidence where available."
                if codex_desktop_sessions else
                "Use Companion Plan/Scan until this surface writes readable history or invokes hooks."
            ),
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
            command_protection="warn_observe" if detected.get("cursor") else "not_detected",
            command_protection_label=(
                "Warn + observe" if detected.get("cursor") else "Not detected"
            ),
            command_protection_detail=(
                "Prompt hooks can warn before risky intent; command-level blocking needs a verified host lifecycle event."
                if detected.get("cursor") else
                "Install Cursor before checking its local coverage."
            ),
        ),
        SurfaceCoverage(
            surface_id="ollama",
            label="Ollama",
            status="limited" if detected.get("ollama") else "not_detected",
            status_label="Runtime/local model detected" if detected.get("ollama") else "Not detected",
            detected=bool(detected.get("ollama")),
            automatic_gate="No verified prompt hook for Ollama surfaces",
            history="Runtime/model presence only; no local prompt, token, or cost scanner yet",
            action="Use Prompt Companion before expensive local-agent work; treat Ollama as detected but unmeasured.",
            detail="Local model runtime presence is useful coverage context, but AIWatcher should not claim spend or outcome evidence without an integration.",
            command_protection="observe_only" if detected.get("ollama") else "not_detected",
            command_protection_label=(
                "Observed only" if detected.get("ollama") else "Not detected"
            ),
            command_protection_detail=(
                "AIWatcher can see local runtime presence, but cannot intercept the commands a separate agent sends to it."
                if detected.get("ollama") else
                "No local model runtime was detected."
            ),
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
            command_protection="unsupported" if detected.get("cline") else "not_detected",
            command_protection_label="Not supported" if detected.get("cline") else "Not detected",
            command_protection_detail="AIWatcher has no verified Cline command lifecycle integration yet.",
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
            command_protection="unsupported" if detected.get("windsurf") else "not_detected",
            command_protection_label="Not supported" if detected.get("windsurf") else "Not detected",
            command_protection_detail="AIWatcher has no verified Windsurf command lifecycle integration yet.",
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
                seen_rows: set[str] = set()
                custom_title: str | None = None
                generated_title: str | None = None
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
                            if _repeated_row(obj, seen_rows):
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
                            # The latest of each wins: a chat can be renamed.
                            if isinstance(obj.get("customTitle"), str) and obj["customTitle"].strip():
                                custom_title = obj["customTitle"].strip()
                            if isinstance(obj.get("aiTitle"), str) and obj["aiTitle"].strip():
                                generated_title = obj["aiTitle"].strip()

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
                    title=custom_title or generated_title,
                ))

                session = sessions[-1]
                session.raw_cwd = _dominant_cwd(cwd_counts, cwd_costs)
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
                seen_rows: set[str] = set()
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
                            if _repeated_row(obj, seen_rows):
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
                    title=str(row["title"]).strip() or None if row["title"] else None,
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
        # The rollout is the better record of what the thread did, but only the
        # database knows what it is called.
        known = by_id.get(rollout.session_id)
        if known is not None and known.title and not rollout.title:
            rollout.title = known.title
        by_id[rollout.session_id] = rollout
    return sorted(
        by_id.values(),
        key=lambda row: row.updated_at or row.started_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


AGENT_HIERARCHY_EDGE_LIMIT = 1000
AGENT_HIERARCHY_ENTRY_LIMIT = 5000


def scan_codex_agent_hierarchy(since: datetime | None = None) -> dict[str, Any]:
    """Read Codex's spawn graph and structural rollout lifecycle events."""
    generated_at_value = datetime.now(timezone.utc)
    generated_at = generated_at_value.isoformat()
    launch_thread_id = str(
        os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID") or ""
    ).strip()
    codex_db = _first_existing(CODEX_DB_PATHS)
    if not codex_db:
        return {
            "available": False,
            "source": "codex-sqlite-spawn-edges",
            "generated_at": generated_at,
            "reason": "Codex local state was not found.",
            "sessions": [],
        }

    try:
        # mode=ro remains read-only while still observing Codex's WAL updates.
        conn = sqlite3.connect(f"{Path(codex_db).resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        # Bound database work as well as returned rows; no indexes are written
        # into another application's database.
        progress_calls = 0
        def stop_expensive_query() -> int:
            nonlocal progress_calls
            progress_calls += 1
            return int(progress_calls > 2000)
        conn.set_progress_handler(stop_expensive_query, 1000)
    except sqlite3.Error as exc:
        return {
            "available": False,
            "source": "codex-sqlite-spawn-edges",
            "generated_at": generated_at,
            "reason": f"Codex local state could not be opened read-only: {exc}",
            "sessions": [],
        }

    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "threads" not in tables or "thread_spawn_edges" not in tables:
            return {
                "available": False,
                "source": "codex-sqlite-spawn-edges",
                "generated_at": generated_at,
                "reason": "This Codex version does not expose agent spawn relationships.",
                "sessions": [],
            }

        thread_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(threads)").fetchall()
        }
        edge_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(thread_spawn_edges)").fetchall()
        }
        if not {"id"}.issubset(thread_columns) or not {
            "parent_thread_id", "child_thread_id", "status"
        }.issubset(edge_columns):
            return {
                "available": False,
                "source": "codex-sqlite-spawn-edges",
                "generated_at": generated_at,
                "reason": "Codex agent relationship metadata is incomplete.",
                "sessions": [],
            }

        safe_columns = ["id", "cwd", "agent_nickname", "agent_role", "archived", "rollout_path"]
        timestamp_columns = ["created_at_ms", "created_at", "updated_at_ms", "updated_at"]
        select_columns = [
            column if column in thread_columns else f"NULL AS {column}"
            for column in [*safe_columns, *timestamp_columns]
        ]
        def timestamp_value(primary: Any, fallback: Any) -> float:
            value = _parse_codex_hierarchy_ts(primary) or _parse_codex_hierarchy_ts(fallback)
            return value.timestamp() if value else 0
        conn.create_function("aiw_timestamp", 2, timestamp_value)
        def updated_sql(alias: str) -> str:
            columns = [f"{alias}.{column}" if column in thread_columns else "NULL" for column in ("updated_at_ms", "updated_at")]
            return f"aiw_timestamp({', '.join(columns)})"
        ordering = f"MAX({updated_sql('child')}, {updated_sql('parent')}) DESC,"
        edge_rows = conn.execute(
            "SELECT edge.parent_thread_id, edge.child_thread_id, edge.status FROM thread_spawn_edges edge "
            "LEFT JOIN threads child ON child.id = edge.child_thread_id "
            "LEFT JOIN threads parent ON parent.id = edge.parent_thread_id "
            f"ORDER BY {ordering} edge.child_thread_id LIMIT ?",
            (AGENT_HIERARCHY_EDGE_LIMIT + 1,),
        ).fetchall()
        truncated = len(edge_rows) > AGENT_HIERARCHY_EDGE_LIMIT
        edge_rows = edge_rows[:AGENT_HIERARCHY_EDGE_LIMIT]
        # Recover ancestors within a separate budget. A clipped subtree must
        # never be presented as a different root session.
        children = {str(row["child_thread_id"]) for row in edge_rows}
        frontier = {str(row["parent_thread_id"]) for row in edge_rows} - children
        incomplete_roots: set[str] = set()
        ancestry_budget = AGENT_HIERARCHY_EDGE_LIMIT
        while frontier:
            next_frontier: set[str] = set()
            for offset in range(0, len(frontier), 400):
                batch = sorted(frontier)[offset:offset + 400]
                ancestors = conn.execute(
                    "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges "
                    f"WHERE child_thread_id IN ({','.join('?' for _ in batch)}) LIMIT ?",
                    [*batch, ancestry_budget + 1],
                ).fetchall()
                for row in ancestors:
                    if ancestry_budget <= 0:
                        incomplete_roots.update(batch)
                        truncated = True
                        break
                    ancestry_budget -= 1
                    edge_rows.append(row)
                    children.add(str(row["child_thread_id"]))
                    next_frontier.add(str(row["parent_thread_id"]))
            frontier = next_frontier - children
        ids = sorted({str(row[key]) for row in edge_rows for key in ("parent_thread_id", "child_thread_id") if row[key]})
        thread_rows = []
        for offset in range(0, len(ids), 400):
            batch = ids[offset:offset + 400]
            thread_rows.extend(conn.execute(
                f"SELECT {', '.join(select_columns)} FROM threads WHERE id IN ({','.join('?' for _ in batch)})",
                batch,
            ).fetchall())
    except sqlite3.Error as exc:
        return {
            "available": False,
            "source": "codex-sqlite-spawn-edges",
            "generated_at": generated_at,
            "reason": f"Codex agent relationships could not be read: {exc}",
            "sessions": [],
        }
    finally:
        conn.close()

    threads: dict[str, dict[str, Any]] = {}
    for row in thread_rows:
        session_id = str(row["id"] or "")
        if not session_id:
            continue
        created_at = _parse_codex_hierarchy_ts(row["created_at_ms"]) or _parse_codex_hierarchy_ts(row["created_at"])
        updated_at = _parse_codex_hierarchy_ts(row["updated_at_ms"]) or _parse_codex_hierarchy_ts(row["updated_at"])
        threads[session_id] = {
            "agent_id": session_id,
            "project_path": str(row["cwd"] or "") or None,
            "name": str(row["agent_nickname"] or "") or None,
            "role": str(row["agent_role"] or "") or None,
            "archived": bool(row["archived"]),
            "rollout_path": str(row["rollout_path"] or "") or None,
            "created_at_value": created_at,
            "updated_at_value": updated_at,
        }

    parents: dict[str, str] = {}
    edge_status: dict[str, str] = {}
    for row in edge_rows:
        parent_id = str(row["parent_thread_id"] or "")
        child_id = str(row["child_thread_id"] or "")
        if not parent_id or not child_id or parent_id == child_id:
            continue
        parents[child_id] = parent_id
        edge_status[child_id] = str(row["status"] or "unknown").lower()
        threads.setdefault(parent_id, _missing_codex_thread(parent_id))
        threads.setdefault(child_id, _missing_codex_thread(child_id))

    roots: dict[str, set[str]] = defaultdict(set)
    resolved_roots: dict[str, str | None] = {}
    for child_id in parents:
        current = child_id
        seen: set[str] = set()
        while current in parents and current not in seen and current not in resolved_roots:
            seen.add(current)
            current = parents[current]
        root = None if current in seen else resolved_roots.get(current, current)
        for item in seen:
            resolved_roots[item] = root
        if root is None or root in incomplete_roots:
            continue
        component = roots[root]
        component.add(root)
        component.update(seen)
        component.add(child_id)

    sessions: list[dict[str, Any]] = []
    rollout_scan_budget = _CodexRolloutScanBudget()
    undated_count = 0
    for root_id, agent_ids in roots.items():
        database_updated = max(
            (
                safe_value
                for agent_id in agent_ids
                if threads[agent_id]["updated_at_value"]
                for safe_value in [
                    _codex_safe_evidence_time(
                        threads[agent_id]["updated_at_value"], generated_at_value
                    )
                ]
                if safe_value
            ),
            default=None,
        )
        rollout_updated = max(
            (
                safe_value
                for agent_id in agent_ids
                for value in [_codex_rollout_mtime(threads[agent_id].get("rollout_path"))]
                if value
                for safe_value in [_codex_safe_evidence_time(value, generated_at_value)]
                if safe_value
            ),
            default=None,
        )
        candidate_updated = max(
            (value for value in (database_updated, rollout_updated) if value),
            default=None,
        )
        if since:
            if candidate_updated is None:
                undated_count += 1
                continue
            if candidate_updated < since:
                continue
        lifecycles = {
            agent_id: _latest_codex_lifecycle_event(
                threads[agent_id].get("rollout_path"),
                request_budget=rollout_scan_budget,
            )
            for agent_id in agent_ids
        }
        lifecycle_updated = max(
            (
                safe_value
                for lifecycle in lifecycles.values()
                if lifecycle
                for value in (lifecycle.get("last_write_at"), lifecycle.get("occurred_at"))
                if value
                for safe_value in [_codex_safe_evidence_time(value, generated_at_value)]
                if safe_value
            ),
            default=None,
        )
        component_updated = max(
            (value for value in (database_updated, lifecycle_updated) if value),
            default=None,
        )
        agents: list[dict[str, Any]] = []
        for agent_id in sorted(
            agent_ids,
            key=lambda item: threads[item]["created_at_value"] or datetime.min.replace(tzinfo=timezone.utc),
        ):
            thread = threads[agent_id]
            is_root = agent_id == root_id
            raw_status = edge_status.get(agent_id)
            resolved = _resolve_codex_agent_state(
                thread,
                raw_status=raw_status,
                lifecycle=lifecycles.get(agent_id),
                is_root=is_root,
                now=generated_at_value,
            )
            display_name = "Main agent" if is_root else (thread["name"] or f"Agent {agent_id[:8]}")
            agents.append({
                "agent_id": agent_id,
                "parent_agent_id": parents.get(agent_id),
                "name": display_name,
                "role": "orchestration" if is_root else (thread["role"] or "delegated"),
                **resolved,
                "created_at": _iso_or_none(thread["created_at_value"]),
                "updated_at": _iso_or_none(
                    _codex_safe_evidence_time(thread["updated_at_value"], generated_at_value)
                ),
            })
        root_agent = next(agent for agent in agents if agent["agent_id"] == root_id)
        launch_agent_id = launch_thread_id if launch_thread_id in agent_ids else None
        active_count = sum(agent["status"] == "running" for agent in agents)
        returned_count = sum(agent["status"] == "completed" for agent in agents)
        stale_count = sum(agent["status"] == "stale" for agent in agents)
        stale_record_count = sum(
            agent["metadata_warning"] == "stale_open_edge" for agent in agents
        )
        session_status = "running" if active_count else ("stale" if stale_count else root_agent["status"])
        sessions.append({
            "session_id": root_id,
            "tool": "codex-cli",
            "is_launch_session": launch_agent_id is not None,
            "launch_agent_id": launch_agent_id,
            "project_path": threads[root_id]["project_path"],
            "updated_at": _iso_or_none(component_updated),
            "status": session_status,
            "root_status": root_agent["status"],
            "agent_count": len(agents),
            "active_count": active_count,
            "returned_count": returned_count,
            "stale_count": stale_count,
            "stale_record_count": stale_record_count,
            "agents": agents,
            "relationship_note": "Topology comes from Codex spawn records; status comes from structural rollout lifecycle events when available.",
        })

    sessions.sort(
        key=lambda item: (
            item["is_launch_session"],
            item["status"] == "running",
            item["updated_at"] or "",
        ),
        reverse=True,
    )
    return {
        "available": True,
        "source": "codex-topology-and-rollout-lifecycle",
        "generated_at": generated_at,
        "reason": "Observed from Codex topology and structural rollout lifecycle events.",
        "sessions": sessions,
        "truncated": truncated,
        "undated_count": undated_count,
    }


def scan_claude_agent_hierarchy(since: datetime | None = None) -> dict[str, Any]:
    """Observe session membership from documented subagent paths, never file bodies.

    https://code.claude.com/docs/en/sub-agents#resume-subagents
    A containing session is known; a nested agent's immediate parent is not.
    """
    sessions: list[dict[str, Any]] = []
    remaining = AGENT_HIERARCHY_ENTRY_LIMIT
    truncated = False
    unreadable = False
    available = False

    def entries(path: Path):
        nonlocal remaining, truncated, unreadable
        try:
            with os.scandir(path) as iterator:
                for entry in iterator:
                    if remaining <= 0:
                        truncated = True
                        return
                    remaining -= 1
                    if not entry.is_symlink():
                        yield entry
        except FileNotFoundError:
            return
        except OSError:
            unreadable = True

    for storage in CLAUDE_PROJECTS_DIRS:
        if not storage.is_dir() or storage.is_symlink():
            continue
        available = True
        for project in entries(storage):
            if not project.is_dir(follow_symlinks=False):
                continue
            for session in entries(Path(project.path)):
                if not session.is_dir(follow_symlinks=False):
                    continue
                subagents = Path(session.path) / "subagents"
                if subagents.is_symlink():
                    continue
                agents = []
                for child in entries(subagents):
                    if not child.name.startswith("agent-") or not child.name.endswith(".jsonl") or not child.is_file(follow_symlinks=False):
                        continue
                    try:
                        updated = datetime.fromtimestamp(child.stat(follow_symlinks=False).st_mtime, timezone.utc)
                    except (OSError, OverflowError, ValueError):
                        unreadable = True
                        continue
                    agent_id = Path(child.name).stem
                    agents.append({
                        "agent_id": agent_id, "parent_agent_id": session.name,
                        "name": f"Agent {agent_id[6:14]}", "role": "session member; parent unverified",
                        "status": "unknown", "latest_event": "unknown",
                        "relationship_status": "session_member",
                        "created_at": None, "updated_at": updated.isoformat(),
                    })
                if not agents:
                    continue
                updated = max(str(agent["updated_at"]) for agent in agents)
                if since and datetime.fromisoformat(updated) < since:
                    continue
                agents.insert(0, {
                    "agent_id": session.name, "parent_agent_id": None, "name": "Owning session",
                    "role": "session owner", "status": "unknown", "latest_event": "unknown",
                    "relationship_status": "unknown", "created_at": None, "updated_at": None,
                })
                sessions.append({
                    "session_id": session.name, "tool": "claude-code",
                    "project_path": None, "storage_project": project.name,
                    "updated_at": updated, "status": "unknown", "active_count": 0,
                    "agent_count": len(agents), "agents": agents,
                    "relationship_note": "Session membership only; nested parents and execution status are not measured. Updated is transcript file modification time.",
                })
    return {
        "available": available, "source": "claude-subagent-paths",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sessions": sessions, "truncated": truncated, "partial": unreadable,
        "reason": "Claude Code subagent file metadata." if available else "Claude Code local project storage was not found.",
    }


def scan_agent_hierarchy(since: datetime | None = None) -> dict[str, Any]:
    sources = [("codex-cli", scan_codex_agent_hierarchy(since)), ("claude-code", scan_claude_agent_hierarchy(since))]
    sessions = []
    for tool, result in sources:
        for session in result.get("sessions", []):
            sessions.append({**session, "tool": tool, "selection_id": f"{tool}:{session['session_id']}"})
    sessions.sort(
        key=lambda item: (
            bool(item.get("is_launch_session")),
            item.get("status") == "running",
            item.get("updated_at") or "",
        ),
        reverse=True,
    )
    return {
        "available": any(result["available"] for _, result in sources),
        "source": "local-agent-relationships",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reason": "Codex lifecycle evidence and recorded local agent relationships; coverage varies by tool.",
        "sessions": sessions,
        "coverage": [{"tool": tool, "available": result["available"], "reason": result.get("reason", "")} for tool, result in sources],
        "unsupported_tools": ["Cursor", "Windsurf", "Cline", "Ollama"],
        "truncated": any(result.get("truncated") for _, result in sources),
        "partial": any(result.get("partial") or not result["available"] for _, result in sources),
        "undated_count": sum(result.get("undated_count", 0) for _, result in sources),
    }


def _missing_codex_thread(session_id: str) -> dict[str, Any]:
    return {
        "agent_id": session_id,
        "project_path": None,
        "name": None,
        "role": None,
        "archived": False,
        "rollout_path": None,
        "created_at_value": None,
        "updated_at_value": None,
    }


def _latest_codex_lifecycle_event(
    rollout_path: Any,
    *,
    request_budget: _CodexRolloutScanBudget | None = None,
) -> dict[str, Any] | None:
    """Return the newest task boundary without retaining rollout content."""
    path = _validated_codex_rollout_path(rollout_path)
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    if not stat_module.S_ISREG(stat.st_mode):
        return None
    cache_key = str(path)
    cached = _CODEX_LIFECYCLE_CACHE.get(cache_key)
    signature = (stat.st_mtime_ns, stat.st_size)
    if cached and cached[:2] == signature:
        return cached[2]

    if request_budget is not None and not request_budget.begin_file():
        return {
            "event": "scan_budget_exhausted",
            "occurred_at": None,
            "last_write_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc),
        }

    result = None
    record_budget_exhausted = False
    try:
        for record_index, line in enumerate(
            _reverse_file_lines(
                path,
                max_bytes=CODEX_ROLLOUT_SCAN_MAX_BYTES,
                request_budget=request_budget,
            )
        ):
            if record_index >= CODEX_ROLLOUT_SCAN_MAX_RECORDS:
                record_budget_exhausted = True
                break
            if request_budget is not None and not request_budget.take_record():
                record_budget_exhausted = True
                break
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(item, dict) or item.get("type") != "event_msg":
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            event_name = payload.get("type")
            if event_name not in {"task_started", "task_complete"}:
                continue
            result = {
                "event": event_name,
                "occurred_at": _parse_codex_hierarchy_ts(item.get("timestamp")),
                "last_write_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            }
            break
    except OSError:
        return None
    request_budget_exhausted = request_budget is not None and (
        request_budget.bytes_remaining <= 0 or request_budget.records_remaining <= 0
    )
    if result is None and (
        record_budget_exhausted
        or stat.st_size > CODEX_ROLLOUT_SCAN_MAX_BYTES
        or request_budget_exhausted
    ):
        result = {
            "event": "scan_budget_exhausted",
            "occurred_at": None,
            "last_write_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc),
        }
    if not (result and result.get("event") == "scan_budget_exhausted" and request_budget_exhausted):
        if cache_key not in _CODEX_LIFECYCLE_CACHE and len(_CODEX_LIFECYCLE_CACHE) >= CODEX_LIFECYCLE_CACHE_MAX_ENTRIES:
            _CODEX_LIFECYCLE_CACHE.pop(next(iter(_CODEX_LIFECYCLE_CACHE)))
        _CODEX_LIFECYCLE_CACHE[cache_key] = (*signature, result)
    return result


def _codex_rollout_mtime(rollout_path: Any) -> datetime | None:
    path = _validated_codex_rollout_path(rollout_path)
    if path is None:
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def _validated_codex_rollout_path(value: Any) -> Path | None:
    if not value:
        return None
    try:
        path = Path(str(value)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if path.suffix.lower() != ".jsonl":
        return None
    for sessions_dir in [*CODEX_SESSIONS_DIRS, *CODEX_ARCHIVED_SESSIONS_DIRS]:
        try:
            path.relative_to(sessions_dir.expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            continue
        return path
    return None


def _reverse_file_lines(
    path: Path,
    block_size: int = 64 * 1024,
    max_line_size: int = CODEX_ROLLOUT_MAX_LINE_BYTES,
    max_bytes: int = CODEX_ROLLOUT_SCAN_MAX_BYTES,
    request_budget: _CodexRolloutScanBudget | None = None,
) -> Iterable[bytes]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat_module.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        scan_start = max(0, position - max_bytes)
        remainder = b""
        discarding_oversized_line = False
        while position > scan_start:
            read_size = min(block_size, position - scan_start)
            if request_budget is not None:
                read_size = request_budget.take_bytes(read_size)
                if read_size <= 0:
                    break
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            if discarding_oversized_line:
                boundary = chunk.rfind(b"\n")
                if boundary < 0:
                    continue
                chunk = chunk[:boundary]
                discarding_oversized_line = False
            chunk += remainder
            lines = chunk.split(b"\n")
            remainder = lines[0]
            for line in reversed(lines[1:]):
                if line.strip() and len(line) <= max_line_size:
                    yield line
            if len(remainder) > max_line_size:
                remainder = b""
                discarding_oversized_line = True
        if position == 0 and not discarding_oversized_line and remainder.strip():
            yield remainder


def _resolve_codex_agent_state(
    thread: dict[str, Any],
    *,
    raw_status: str | None,
    lifecycle: dict[str, Any] | None,
    is_root: bool,
    now: datetime,
) -> dict[str, Any]:
    lifecycle_event = lifecycle.get("event") if lifecycle else None
    lifecycle_at = lifecycle.get("occurred_at") if lifecycle else None
    rollout_write_at = lifecycle.get("last_write_at") if lifecycle else None
    future_lifecycle = any(
        value > now + CODEX_AGENT_CLOCK_SKEW_TOLERANCE
        for value in (rollout_write_at, lifecycle_at)
        if value
    )
    lifecycle_activity_at = max(
        (
            safe_value
            for value in (rollout_write_at, lifecycle_at)
            if value
            for safe_value in [_codex_safe_evidence_time(value, now)]
            if safe_value
        ),
        default=None,
    )
    evidence_at = lifecycle_activity_at or _codex_safe_evidence_time(thread.get("updated_at_value"), now)
    stale_after = None

    if future_lifecycle:
        status = "unknown"
        latest_event = "clock_skew"
        evidence_source = "rollout_lifecycle+clock_skew"
        confidence = "low"
    elif lifecycle_event == "task_complete":
        status = "idle" if is_root else "completed"
        latest_event = "idle" if is_root else "returned"
        evidence_source = "rollout_lifecycle"
        confidence = "high"
    elif lifecycle_event == "scan_budget_exhausted":
        status = "unknown"
        latest_event = "scan_limited"
        evidence_source = "rollout_scan_budget"
        confidence = "low"
    elif lifecycle_event == "task_started":
        if thread.get("archived") or (raw_status == "closed" and not is_root):
            status = "interrupted"
            latest_event = "interrupted"
            evidence_source = "rollout_lifecycle+thread_metadata"
            confidence = "high"
        elif lifecycle_activity_at is None:
            status = "unknown"
            latest_event = "started"
            evidence_source = "rollout_lifecycle"
            confidence = "low"
        else:
            stale_after_value = lifecycle_activity_at + CODEX_AGENT_RUNNING_FRESHNESS
            stale_after = _iso_or_none(stale_after_value)
            if now <= stale_after_value:
                status = "running"
                latest_event = "working"
                confidence = "medium"
            else:
                status = "stale"
                latest_event = "stale"
                confidence = "medium"
            evidence_source = "rollout_lifecycle+last_activity"
    elif thread.get("archived"):
        status = "unknown"
        latest_event = "archived"
        evidence_source = "thread_metadata"
        confidence = "low"
    elif not is_root and raw_status == "closed":
        status = "unknown"
        latest_event = "relationship_closed"
        evidence_source = "spawn_edge"
        confidence = "low"
    else:
        status = "unknown"
        latest_event = "unknown"
        evidence_source = "spawn_edge" if raw_status else "thread_metadata"
        confidence = "low"

    metadata_warning = None
    if not is_root and raw_status == "open" and status in {"completed", "interrupted", "stale"}:
        metadata_warning = "stale_open_edge"

    return {
        "status": status,
        "latest_event": latest_event,
        "evidence_source": evidence_source,
        "evidence_at": _iso_or_none(evidence_at),
        "confidence": confidence,
        "stale_after": stale_after,
        "relationship_status": raw_status,
        "metadata_warning": metadata_warning,
    }


def _codex_safe_evidence_time(value: datetime | None, now: datetime) -> datetime | None:
    if value is None or value > now:
        return None
    return value


def _parse_codex_hierarchy_ts(value: Any) -> datetime | None:
    try:
        parsed = _parse_ts(value)
    except (OSError, OverflowError, TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


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
        # The cwd exactly as Codex wrote it. project_path below is the same
        # value already folded to a git root, which erases the one thing that
        # tells a Second Opinion analyst run apart from the user's own work.
        recorded_cwd: str | None = None
        model: str | None = None
        surface: str | None = None
        started_at: datetime | None = None
        updated_at: datetime | None = None
        final_input = 0
        final_cached = 0
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
                        recorded_cwd = str(payload.get("cwd") or "") or recorded_cwd
                        project_path = _normalize_project_path(str(payload.get("cwd") or "")) or project_path
                        if surface is None:
                            originator = str(payload.get("originator") or "").lower()
                            if "desktop" in originator:
                                surface = "desktop"
                            elif "cli" in originator or "tui" in originator:
                                surface = "cli"
                    elif row_type == "turn_context":
                        recorded_cwd = str(payload.get("cwd") or "") or recorded_cwd
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
                    final_cached = min(final_input, int(total.get("cached_input_tokens") or 0))
                    final_output = int(total.get("output_tokens") or 0)
                    agent_calls += 1
                    event_input = int(last.get("input_tokens") or 0)
                    event_output = int(last.get("output_tokens") or 0)
                    # Codex counts cached tokens inside input_tokens; they bill at
                    # the cached rate, so they are split out rather than priced as
                    # fresh input (estimate_cost's docstring).
                    event_cached = min(event_input, int(last.get("cached_input_tokens") or 0))
                    event_cost = estimate_cost(
                        model, event_input - event_cached, event_output, cache_read=event_cached, when=timestamp,
                    )
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
            raw_cwd=recorded_cwd,
            started_at=started_at or _mtime(path),
            updated_at=updated_at or _mtime(path),
            model=model or "codex",
            tokens_in=final_input,
            tokens_out=final_output,
            # Session-level total, so it is dated by the session's last turn:
            # a rollup has no single moment, and the newest turn is the closest
            # honest answer for which rate card applied.
            cost_usd=estimate_cost(
                model, final_input - final_cached, final_output, cache_read=final_cached, when=updated_at,
            ),
            agent_calls=agent_calls,
            tool_calls=tool_calls,
            source_path=str(path),
            surface=surface,
            model_breakdown={key: dict(value) for key, value in model_totals.items()},
            notes=[
                "Measured from Codex rollout token_count events",
                "Codex cost is API-equivalent at OpenAI list prices; on a ChatGPT plan no money moves per token",
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
