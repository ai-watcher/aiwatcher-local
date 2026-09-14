"""Did compacting pay?

The compact nudge (compaction.py) says when a compaction would shed history.
This module records what each compaction actually did, so that claim can be
checked against real sessions before any surface makes it. Nothing here is
shown in the dashboard or the Companion: the records sit in local state until
there are enough of them to review together (`aiwatcher compactions`).

One record per compaction, measured from the session's own transcript:

  - the size of the request just before it and just after it;
  - the next WINDOW_PROMPTS prompts the user typed after it: the token buckets
    of every request in that stretch, how many of those requests rebuilt their
    cache, and how much of what was removed Claude read back in (Read calls on
    files the removed history had already touched);
  - whether a compact nudge was showing when it happened, and what the user
    did with it.

Records hold token counts, character counts, timestamps, the model and the
session id. No prompt text, no file contents, no file paths.

Dollar figures are not stored. `figures` derives them when the records are
read, so a better formula does not need a re-measure.

What the transcript cannot show is the summarising call: Claude Code does not
log its usage. Its cost is estimated from the size before and the length of the
summary, as a range from a full cache hit to a full miss.

Claude Code only. No compaction marker has been seen in a Codex rollout (see
compaction.codex_boundary_stats), so a Codex compaction is not recorded rather
than guessed from a drop in size.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import local_state
from .pricing import CACHE_READ_MULTIPLIER, CACHE_WRITE_1H_MULTIPLIER, CACHE_WRITE_5M_MULTIPLIER, lookup
from .scanner import _anthropic_usage, _billed_input, _repeated_row, _usage_receipt_key
from .statusline import _is_compact_command_row, _is_prompt_row, _parse_stamp

RECORD_VERSION = 1
# The stretch after a compaction a record covers, in prompts the user typed.
# Ten is the figure the plan was agreed on (2026-09-14): the first real case
# paid for itself after about four requests, so ten prompts (usually dozens of
# requests) is long enough for a saving to show, and short enough that most
# sessions finish it.
WINDOW_PROMPTS = 10
# A request that writes at least this share of its context to the cache is a
# rebuild (the cache expired, or its prefix changed), not a turn adding its new
# tail. Measured on 2026-09-14 over 1,745 requests in the three local sessions
# with compactions: ordinary turns wrote at most 26% (a big tool result on a
# small context) and rebuilds at least 37% (the system prompt stays cached, so
# a rebuild never writes it all). 0.3 sits in that gap; every saving came out
# the same at 0.3 and 0.5, while 0.1 counted growth as rebuilds and 0.8 missed
# real ones.
REBUILD_WRITE_SHARE = 0.3
# Anthropic's two cache lifetimes. A session that wrote 1h cache entries keeps
# its prefix warm across a longer pause than one on the 5m default.
TTL_5M_SECONDS = 300
TTL_1H_SECONDS = 3600
# Tool output is logged as text, not tokens. Four characters per token is the
# usual rule of thumb for English and code; the re-read figure is labelled an
# estimate for this reason.
CHARS_PER_TOKEN = 4
# How long a session can be silent before an open window is reported as the
# session having stopped, rather than still going. Matches the Companion's
# live window (session_presence.LIVE_WINDOW_MINUTES).
QUIET_AFTER_SECONDS = 30 * 60

_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})


def _empty_tokens() -> dict[str, int]:
    return {"input": 0, "output": 0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0}


def _iso(stamp: datetime | None) -> str | None:
    return stamp.isoformat() if stamp else None


def _text_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(str(block.get("text") or "")) for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return 0


def _read_timeline(path: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The transcript as the events a measurement needs, in file order.

    Rows Claude Code writes again at compaction time are skipped
    (scanner._repeated_row), and one request's usage, copied onto every
    content-block line, counts once (scanner._usage_receipt_key). Returns the
    events and the output size of every Read call, by tool_use id.
    """
    timeline: list[dict[str, Any]] = []
    read_chars: dict[str, int] = {}
    read_ids: set[str] = set()
    seen_rows: set[str] = set()
    seen_requests: set[str] = set()
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or _repeated_row(obj, seen_rows) or obj.get("isSidechain"):
                continue
            stamp = _parse_stamp(obj.get("timestamp"))
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if obj.get("type") == "system" and obj.get("subtype") == "compact_boundary":
                meta = obj.get("compactMetadata") if isinstance(obj.get("compactMetadata"), dict) else {}
                timeline.append({
                    "kind": "boundary", "at": stamp, "uuid": obj.get("uuid"),
                    "trigger": meta.get("trigger"),
                    "pre_tokens": int(meta.get("preTokens") or 0),
                    "duration_ms": int(meta.get("durationMs") or 0),
                })
                continue
            if obj.get("isCompactSummary"):
                timeline.append({"kind": "summary", "chars": _text_chars(message.get("content"))})
                continue
            if obj.get("type") == "user":
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in read_ids:
                            read_chars[block["tool_use_id"]] = _text_chars(block.get("content"))
                if _is_compact_command_row(obj, message):
                    timeline.append({"kind": "command", "at": stamp})
                elif _is_prompt_row(obj, message):
                    timeline.append({"kind": "prompt", "at": stamp})
                continue
            if obj.get("type") != "assistant":
                continue
            usage = message.get("usage")
            key = _usage_receipt_key(obj, message)
            if isinstance(usage, dict) and message.get("model") != "<synthetic>" and (key is None or key not in seen_requests):
                if key is not None:
                    seen_requests.add(key)
                tokens = _anthropic_usage(usage)
                if _billed_input(tokens) > 0 or tokens["output"] > 0:
                    timeline.append({"kind": "request", "at": stamp, "model": message.get("model"), "tokens": tokens})
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") not in _FILE_TOOLS:
                    continue
                file_path = (block.get("input") or {}).get("file_path") or (block.get("input") or {}).get("notebook_path")
                if not isinstance(file_path, str) or not file_path:
                    continue
                tool_id = str(block.get("id") or "")
                if block.get("name") == "Read" and tool_id:
                    read_ids.add(tool_id)
                timeline.append({"kind": "touch", "tool": block.get("name"), "path": file_path, "id": tool_id})
    return timeline, read_chars


def _is_rebuild(tokens: dict[str, int]) -> bool:
    """A request that rewrote its context to the cache rather than adding to it
    (see REBUILD_WRITE_SHARE for where the line comes from)."""
    context = _billed_input(tokens)
    return context > 0 and tokens["cache_write_5m"] + tokens["cache_write_1h"] >= REBUILD_WRITE_SHARE * context


def measure_transcript(path: str, *, session_id: str) -> list[dict[str, Any]]:
    """One record per compaction in a Claude Code transcript, oldest first.

    A compaction with no request before it anywhere in the file has no size to
    compare against and is left out.
    """
    timeline, read_chars = _read_timeline(path)
    boundaries = [index for index, item in enumerate(timeline) if item["kind"] == "boundary"]
    if not boundaries:
        return []
    stamps = [item["at"] for item in timeline if item.get("at")]
    last_activity = max(stamps) if stamps else None
    records = []
    for number, at in enumerate(boundaries):
        start = boundaries[number - 1] + 1 if number else 0
        end = boundaries[number + 1] if number + 1 < len(boundaries) else len(timeline)
        record = _measure_one(
            timeline, read_chars, start=start, at=at, end=end,
            session_id=session_id, followed=number + 1 < len(boundaries),
        )
        if record is not None:
            record["last_activity_at"] = _iso(last_activity)
            records.append(record)
    return records


def _measure_one(
    timeline: list[dict[str, Any]],
    read_chars: dict[str, int],
    *,
    start: int,
    at: int,
    end: int,
    session_id: str,
    followed: bool,
) -> dict[str, Any] | None:
    boundary = timeline[at]
    before_requests = [item for item in timeline[:at] if item["kind"] == "request"]
    if not before_requests:
        return None
    last_before = before_requests[-1]
    segment = timeline[start:at]

    # The same number of prompts before the compaction, for comparison. Only
    # this segment: a stretch that crosses an earlier compaction is a
    # different session size.
    segment_prompts = [index for index, item in enumerate(segment) if item["kind"] == "prompt"]
    window_start = segment_prompts[-WINDOW_PROMPTS] if len(segment_prompts) >= WINDOW_PROMPTS else 0
    before_tokens = _empty_tokens()
    before_count = 0
    for item in segment[window_start:]:
        if item["kind"] == "request":
            before_count += 1
            for bucket in before_tokens:
                before_tokens[bucket] += item["tokens"][bucket]

    touched_before = {item["path"] for item in segment if item["kind"] == "touch"}
    uses_1h = any(item["tokens"]["cache_write_1h"] > 0 for item in segment if item["kind"] == "request")

    # When the user typed /compact. Claude Code writes that row once the
    # compaction has finished, after the boundary in the file but carrying
    # the earlier timestamp (seen 2026-09-09), so it is looked for on both
    # sides: after the last request before, or after the boundary before the
    # first request.
    command_at = None
    for item in segment:
        if item["kind"] == "command" and item.get("at") and (last_before.get("at") is None or item["at"] >= last_before["at"]):
            command_at = item["at"]

    summary_chars = 0
    prompts = requests = rebuilds_5m = rebuilds_1h = reads = rereads = reread_chars = 0
    after_tokens = _empty_tokens()
    first: dict[str, Any] | None = None
    closed_by: str | None = None
    for item in timeline[at + 1:end]:
        kind = item["kind"]
        if kind == "summary" and first is None:
            summary_chars = item["chars"]
        elif kind == "command" and first is None and item.get("at"):
            command_at = item["at"]
        elif kind == "prompt":
            if prompts == WINDOW_PROMPTS:
                closed_by = "prompts"
                break
            prompts += 1
        elif kind == "request":
            requests += 1
            for bucket in after_tokens:
                after_tokens[bucket] += item["tokens"][bucket]
            if first is None:
                first = item
            elif _is_rebuild(item["tokens"]):
                if item["tokens"]["cache_write_1h"] >= item["tokens"]["cache_write_5m"]:
                    rebuilds_1h += 1
                else:
                    rebuilds_5m += 1
        elif kind == "touch" and item["tool"] == "Read":
            reads += 1
            if item["path"] in touched_before:
                rereads += 1
                reread_chars += read_chars.get(item["id"], 0)
    if closed_by is None and followed:
        closed_by = "next_compaction"
    # Claude Code writes the summary row just before the boundary row on some
    # versions and just after on others.
    if not summary_chars and at > 0 and timeline[at - 1]["kind"] == "summary":
        summary_chars = timeline[at - 1]["chars"]

    boundary_at = boundary.get("at")
    first_tokens = first["tokens"] if first else None
    gap = None
    if first and first.get("at") and last_before.get("at"):
        gap = round((first["at"] - last_before["at"]).total_seconds())
    return {
        "id": f"{session_id}:{boundary.get('uuid') or _iso(boundary_at)}",
        "version": RECORD_VERSION,
        "session_id": session_id,
        "tool": "claude-code",
        "model": last_before.get("model") or (first.get("model") if first else None),
        "boundary_at": _iso(boundary_at),
        "command_at": _iso(command_at),
        "trigger": boundary.get("trigger"),
        "pre_tokens": boundary.get("pre_tokens") or 0,
        "duration_ms": boundary.get("duration_ms") or 0,
        "summary_chars": summary_chars,
        "before": {
            "context": _billed_input(last_before["tokens"]),
            "cache_read": last_before["tokens"]["cache_read"],
            "at": _iso(last_before.get("at")),
            "prompts": len(segment_prompts[-WINDOW_PROMPTS:]),
            "requests": before_count,
            "tokens": before_tokens,
        },
        "first_after": None if first_tokens is None else {
            "context": _billed_input(first_tokens),
            "cache_read": first_tokens["cache_read"],
            "cache_write_5m": first_tokens["cache_write_5m"],
            "cache_write_1h": first_tokens["cache_write_1h"],
            "at": _iso(first.get("at")),
        },
        "gap_seconds": gap,
        "cache_ttl_seconds": TTL_1H_SECONDS if uses_1h else TTL_5M_SECONDS,
        "after": {
            "prompts": prompts,
            "requests": requests,
            "tokens": after_tokens,
            "rebuilds_5m": rebuilds_5m,
            "rebuilds_1h": rebuilds_1h,
            "reads": reads,
            "rereads": rereads,
            "reread_chars": reread_chars,
        },
        "window_prompts": WINDOW_PROMPTS,
        "closed_by": closed_by,
        "nudge": None,
    }


def attach_nudges(records: list[dict[str, Any]], receipts: Iterable[dict[str, Any]]) -> None:
    """Mark each compaction with the nudge that was showing before it, if any.

    A receipt belongs to a compaction when it was opened after the session's
    previous compaction and before this one was asked for -- the /compact
    typed, when the log has it, else the boundary. A nudge the user saw and
    set aside, and then compacted anyway, still counts as nudged, with its
    decision kept so the review can tell those apart. A receipt opened after
    the command is the surface catching up with a compaction already under
    way (2026-09-09: opened half a second after /compact was typed), not a
    nudge that led to it.
    """
    by_session: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    for receipt in receipts:
        created = _parse_stamp(receipt.get("created_at")) if isinstance(receipt, dict) else None
        if created is not None:
            by_session.setdefault(str(receipt.get("session_id") or ""), []).append((created, receipt))
    previous: dict[str, datetime] = {}
    for record in sorted(records, key=lambda row: str(row.get("boundary_at") or "")):
        sid = str(record.get("session_id") or "")
        at = _parse_stamp(record.get("boundary_at"))
        asked = _parse_stamp(record.get("command_at")) or at
        chosen: tuple[datetime, dict[str, Any]] | None = None
        if at is not None and asked is not None:
            for created, receipt in by_session.get(sid, []):
                if created > asked or (sid in previous and created <= previous[sid]):
                    continue
                if chosen is None or created > chosen[0]:
                    chosen = (created, receipt)
            previous[sid] = at
        record["nudge"] = None if chosen is None else {
            "id": chosen[1].get("id"),
            "shown_at": chosen[1].get("created_at"),
            "decision": chosen[1].get("decision"),
        }


def record_session(session_id: str, path: str) -> list[dict[str, Any]]:
    """Measure one transcript's compactions and keep them in local state."""
    records = measure_transcript(path, session_id=session_id)
    if not records:
        return []
    attach_nudges(records, local_state.recent_compact_nudges(limit=local_state.MAX_COMPACT_NUDGES_STORED))
    now = datetime.now(timezone.utc).isoformat()
    for record in records:
        record["measured_at"] = now
    local_state.upsert_compaction_outcomes(records)
    return records


def backfill(projects_dirs: Iterable[Path], *, since: datetime | None = None) -> int:
    """Record every compaction in the Claude Code transcripts still on disk.

    Claude Code deletes old transcripts on its own schedule, so the records in
    local state are what outlives them; this fills in compactions that happened
    while nothing was polling. Returns how many transcripts held one.
    """
    found = 0
    for root in projects_dirs:
        if not root.is_dir():
            continue
        for path in root.glob("*/*.jsonl"):
            try:
                if since is not None and datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < since:
                    continue
                if not has_compaction(path):
                    continue
            except OSError:
                continue
            if record_session(path.stem, str(path)):
                found += 1
    return found


# The marker as it sits on disk, matched in raw bytes with its quotes, so a
# transcript that merely talks about compact_boundary does not count: inside
# logged text the quotes are escaped (\"subtype\":), which this cannot match.
# Claude Code writes JSON with no spaces; the optional space keeps a
# re-serialised file from silently reading as having no compactions.
COMPACTION_MARKER = re.compile(rb'"subtype":\s?"compact_boundary"')


def compaction_markers(path: Path | str) -> int:
    with open(path, "rb") as handle:
        return len(COMPACTION_MARKER.findall(handle.read()))


def has_compaction(path: Path | str) -> bool:
    return compaction_markers(path) > 0


def figures(record: dict[str, Any]) -> dict[str, Any]:
    """What one record says about whether compacting paid.

    The world without the compaction is this same stretch of requests, each
    also carrying what was removed as history re-sent from the cache. Two
    corrections keep that honest:

      - requests that rebuilt their cache after it (a pause past the cache
        lifetime) would have rebuilt the removed history too, at the write
        rate;
      - the first request after rebuilds the cache because of the compaction,
        so that premium is a cost of compacting -- unless the pause before it
        was past the cache lifetime anyway. All of its writes are charged to
        compacting, including the new prompt's own tokens, which leans the
        result toward understating the saving.

    Re-reads are already inside the actual cost and are not taken out of the
    world without, which also leans toward understating. The summarising call
    is not logged, so it is a range.

    Dollars are API list prices at the time of the compaction; on a
    subscription they show direction, not a bill.
    """
    after = record.get("after") or {}
    before = record.get("before") or {}
    first = record.get("first_after")
    base: dict[str, Any] = {
        "measurable": False,
        "reason": "",
        "prompts": int(after.get("prompts") or 0),
        "requests": int(after.get("requests") or 0),
        "window_complete": bool(record.get("closed_by")),
        "context_before": int(before.get("context") or 0),
        "context_after": int(first.get("context") or 0) if first else 0,
    }
    if not first:
        base["reason"] = "No request after the compaction yet."
        return base
    carried = base["context_before"] - base["context_after"]
    if carried <= 0:
        base["reason"] = "The first request after was no smaller than the last one before."
        return base
    base["carried"] = carried
    reread_tokens = int(after.get("reread_chars") or 0) // CHARS_PER_TOKEN
    base["reread_tokens_est"] = reread_tokens
    base["reread_share"] = reread_tokens / carried
    if record.get("trigger") == "auto":
        # Claude Code compacts on its own at the context window (998.6k of
        # 1M, the one case seen on 2026-08-30). The session could not have
        # carried on without it, so the stretch "without compacting" never
        # existed and a saving against it would be invented.
        base["reason"] = (
            "Claude Code compacted on its own at the context window, so the session could not have "
            "carried on without it; there is nothing to weigh a saving against."
        )
        return base
    model = record.get("model")
    rates = lookup(model, _parse_stamp(record.get("boundary_at")))
    if not rates or not float(rates.get("in") or 0):
        base["reason"] = f"No list price for {model or 'this model'}, so the saving cannot be weighed."
        return base

    price_in = float(rates["in"]) / 1_000_000
    price_out = float(rates["out"]) / 1_000_000

    def cost(tokens: dict[str, int]) -> float:
        return price_in * (
            tokens.get("input", 0)
            + CACHE_WRITE_5M_MULTIPLIER * tokens.get("cache_write_5m", 0)
            + CACHE_WRITE_1H_MULTIPLIER * tokens.get("cache_write_1h", 0)
            + CACHE_READ_MULTIPLIER * tokens.get("cache_read", 0)
        ) + price_out * tokens.get("output", 0)

    actual = cost(after.get("tokens") or {})
    ttl = int(record.get("cache_ttl_seconds") or TTL_5M_SECONDS)
    gap = record.get("gap_seconds")
    first_would_have_read = gap is not None and gap <= ttl
    write_multiplier = CACHE_WRITE_1H_MULTIPLIER if ttl >= TTL_1H_SECONDS else CACHE_WRITE_5M_MULTIPLIER
    rebuilds_5m = int(after.get("rebuilds_5m") or 0)
    rebuilds_1h = int(after.get("rebuilds_1h") or 0)
    reading = base["requests"] - rebuilds_5m - rebuilds_1h - (0 if first_would_have_read else 1)
    carried_cost = carried * price_in * (
        CACHE_READ_MULTIPLIER * max(0, reading)
        + CACHE_WRITE_5M_MULTIPLIER * rebuilds_5m
        + CACHE_WRITE_1H_MULTIPLIER * rebuilds_1h
        + (0 if first_would_have_read else write_multiplier)
    )
    rebuild = price_in * (
        first.get("cache_write_5m", 0) * (CACHE_WRITE_5M_MULTIPLIER - CACHE_READ_MULTIPLIER)
        + first.get("cache_write_1h", 0) * (CACHE_WRITE_1H_MULTIPLIER - CACHE_READ_MULTIPLIER)
    ) if first_would_have_read else 0.0
    without = actual + carried_cost - rebuild

    pre = int(record.get("pre_tokens") or 0) or base["context_before"]
    summary_out = price_out * (int(record.get("summary_chars") or 0) // CHARS_PER_TOKEN)
    summarise_low = price_in * CACHE_READ_MULTIPLIER * pre + summary_out
    summarise_high = price_in * pre + summary_out
    saved_low = without - actual - summarise_high
    saved_high = without - actual - summarise_low
    per_request = carried * price_in * CACHE_READ_MULTIPLIER
    base.update({
        "measurable": True,
        "actual_usd": actual,
        "without_usd": without,
        "rebuild_usd": rebuild,
        "summarise_usd_low": summarise_low,
        "summarise_usd_high": summarise_high,
        "saved_usd_low": saved_low,
        "saved_usd_high": saved_high,
        "saved_share_low": saved_low / without if without else 0.0,
        "saved_share_high": saved_high / without if without else 0.0,
        "break_even_requests": (rebuild + summarise_low) / per_request if per_request else None,
    })
    return base


def _tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _local(stamp: str | None) -> str:
    parsed = _parse_stamp(stamp)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M") if parsed else "?"


def render_report(records: list[dict[str, Any]], *, now: datetime | None = None) -> str:
    """The records as text for reviewing together. Not a surface: the wording
    names every estimate and every stretch that is not finished yet."""
    now = now or datetime.now(timezone.utc)
    if not records:
        return "No compactions recorded yet. Records start with the next /compact in a Claude Code session."
    lines = [
        f"Compactions recorded: {len(records)} (Claude Code only). None of this is shown in the dashboard.",
        "",
    ]
    complete = []
    for record in sorted(records, key=lambda row: str(row.get("boundary_at") or "")):
        fig = figures(record)
        nudge = record.get("nudge")
        nudged = f"nudged ({nudge.get('decision') or 'no action'})" if nudge else "not nudged"
        lines.append(
            f"{_local(record.get('boundary_at'))}  {str(record.get('session_id') or '')[:8]}  "
            f"{record.get('trigger') or '?'}  {nudged}  {record.get('model') or '?'}"
        )
        window = f"{fig['prompts']} of {record.get('window_prompts') or WINDOW_PROMPTS} prompts, {fig['requests']} requests"
        if record.get("closed_by") == "next_compaction":
            window += " (cut short by the next compaction)"
        elif not record.get("closed_by"):
            last = _parse_stamp(record.get("last_activity_at"))
            quiet = last is not None and (now - last).total_seconds() > QUIET_AFTER_SECONDS
            window += " (session went quiet)" if quiet else " (still going)"
        if not fig.get("carried"):
            lines.append(f"  {fig['reason']}")
            lines.append(f"  after it     {window}")
            lines.append("")
            continue
        lines.append(
            f"  per request  {_tokens(fig['context_before'])} -> {_tokens(fig['context_after'])} "
            f"({_tokens(fig['carried'])} no longer re-sent)"
        )
        lines.append(f"  after it     {window}")
        if fig["measurable"]:
            low, high = fig["saved_share_low"] * 100, fig["saved_share_high"] * 100
            lines.append(
                f"  usage        ${fig['actual_usd']:.2f} actual vs ~${fig['without_usd']:.2f} without compacting: "
                f"saved {low:.0f}% to {high:.0f}% (summary call estimated at "
                f"${fig['summarise_usd_low']:.2f}-${fig['summarise_usd_high']:.2f})"
            )
            if fig["break_even_requests"] is not None:
                lines.append(f"  pays off     after ~{fig['break_even_requests']:.1f} requests (if the summary call hit the cache)")
            if fig["window_complete"] and record.get("closed_by") == "prompts":
                complete.append(fig)
        else:
            lines.append(f"  {fig['reason']}")
        rereads = int((record.get("after") or {}).get("rereads") or 0)
        lines.append(
            f"  re-read      {rereads} file read{'s' if rereads != 1 else ''} of already-seen files, "
            f"~{_tokens(fig['reread_tokens_est'])} tokens ({fig['reread_share'] * 100:.0f}% of what was removed; estimate)"
        )
        lines.append("")
    if complete:
        without = sum(fig["without_usd"] for fig in complete)
        low = sum(fig["saved_usd_low"] for fig in complete)
        high = sum(fig["saved_usd_high"] for fig in complete)
        lines.append(
            f"Across the {len(complete)} with a full {WINDOW_PROMPTS}-prompt stretch: saved {low / without * 100:.0f}% to "
            f"{high / without * 100:.0f}% of what those stretches would have cost without compacting."
        )
    else:
        lines.append(f"No compaction has a full {WINDOW_PROMPTS}-prompt stretch after it yet, so there is no total.")
    lines.append("Dollars are API list prices; on a subscription they show direction, not your bill.")
    return "\n".join(lines)
