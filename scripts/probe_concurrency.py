#!/usr/bin/env python3
"""Read-only probe for the live-session feature's open numbers.

Three questions have to be answered from real data before any of the
"what is running right now" work is worth building, and none of them can be
answered from a single developer's intuition:

1. How often is more than one session actually live at the same time? If the
   answer is "almost never", a concurrency badge sorts nothing and is
   decoration -- the same failure mode that killed `tokens >= 500000` and
   `replayed_share_pct` on this dashboard.
2. How long does a *normal* mid-session pause last? Any "this session has gone
   quiet" threshold has to sit above the everyday gap between transcript
   writes, or the first long test run produces a false alarm.
3. Have two live sessions ever shared one working tree? That is the collision
   case, and if it has never happened here the radar idea should be dropped.

The script reads local transcripts only. It opens nothing over the network,
writes nothing, and prints no prompt text or file content -- only timestamps,
session ids, and (by default) the *basename* of a working directory. Pass
--full-paths if you want the whole path in the collision section.

Usage:
    python3 scripts/probe_concurrency.py            # last 30 days
    python3 scripts/probe_concurrency.py --days 90
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CODEX_DB = HOME / ".codex" / "state_5.sqlite"

# A session is treated as "still live" for this long after its last write. The
# right value is exactly what question 2 is trying to establish, so the report
# runs the concurrency numbers at all three rather than picking one up front.
IDLE_WINDOWS_SECONDS = (60, 120, 300)


# --------------------------------------------------------------------------
# Loading. Every loader returns rows of (tool, session_key, epoch_seconds) plus
# a cwd per session where the source records one.
# --------------------------------------------------------------------------

def _epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _task_calls(message: object) -> int:
    """Count Task tool_use blocks -- one per subagent this turn spawned."""
    if not isinstance(message, dict):
        return 0
    content = message.get("content")
    if not isinstance(content, list):
        return 0
    return sum(
        1 for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") == "Task"
    )


def load_claude(cutoff: float) -> tuple[dict, dict, dict]:
    writes: dict[tuple[str, str], list[float]] = defaultdict(list)
    cwds: dict[tuple[str, str], str] = {}
    stats = {"files": 0, "lines": 0, "sidechain_lines": 0,
             "task_calls": 0, "sessions_with_subagents": set()}
    if not CLAUDE_PROJECTS.is_dir():
        return writes, cwds, stats

    for path in sorted(CLAUDE_PROJECTS.glob("*/*.jsonl")):
        # Cheap pre-filter: a file untouched since the cutoff has nothing in
        # window. Saves parsing years of history to print a 30-day report.
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        stats["files"] += 1
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    when = _epoch(row.get("timestamp"))
                    if when is None or when < cutoff:
                        continue
                    session_id = str(row.get("sessionId") or path.stem)
                    key = ("claude", session_id)
                    stats["lines"] += 1
                    # Subagent turns are written into the parent's transcript.
                    # They are not separate sessions and must not be counted as
                    # concurrency -- that would inflate the headline number with
                    # the very thing it is meant to be distinguished from.
                    if row.get("isSidechain") is True:
                        stats["sidechain_lines"] += 1
                        stats["sessions_with_subagents"].add(session_id)
                        continue
                    writes[key].append(when)
                    spawned = _task_calls(row.get("message"))
                    if spawned:
                        stats["task_calls"] += spawned
                        stats["sessions_with_subagents"].add(session_id)
                    cwd = row.get("cwd")
                    if isinstance(cwd, str) and cwd and key not in cwds:
                        cwds[key] = cwd
        except OSError:
            continue
    return writes, cwds, stats


def load_codex_rollouts(cutoff: float) -> tuple[dict, dict, int]:
    writes: dict[tuple[str, str], list[float]] = defaultdict(list)
    cwds: dict[tuple[str, str], str] = {}
    files = 0
    if not CODEX_SESSIONS.is_dir():
        return writes, cwds, files

    for path in sorted(CODEX_SESSIONS.rglob("*.jsonl")):
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        files += 1
        key = ("codex", path.stem)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    when = _epoch(row.get("timestamp"))
                    if when is None or when < cutoff:
                        continue
                    writes[key].append(when)
                    payload = row.get("payload")
                    if isinstance(payload, dict):
                        cwd = payload.get("cwd")
                        if isinstance(cwd, str) and cwd and key not in cwds:
                            cwds[key] = cwd
        except OSError:
            continue
    return writes, cwds, files


def codex_db_threads(cutoff: float) -> list[tuple[str, float, float]]:
    """Thread create/update stamps. Two points per thread, so this is only good
    enough to say a thread was touched -- not to measure gaps within it."""
    if not CODEX_DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{CODEX_DB}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return []
    out: list[tuple[str, float, float]] = []
    try:
        for row in conn.execute(
            "SELECT id, created_at_ms, updated_at_ms FROM threads"
        ):
            updated = (row[2] or 0) / 1000.0
            if updated < cutoff:
                continue
            out.append((str(row[0]), (row[1] or 0) / 1000.0, updated))
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return out


# --------------------------------------------------------------------------
# Analysis.
# --------------------------------------------------------------------------

def active_minutes(times: list[float], window: int) -> set[int]:
    """Minutes in which this session counts as live, given an idle window."""
    intervals: list[list[float]] = []
    for stamp in sorted(times):
        if intervals and stamp <= intervals[-1][1]:
            intervals[-1][1] = stamp + window
        else:
            intervals.append([stamp, stamp + window])
    minutes: set[int] = set()
    for start, end in intervals:
        minutes.update(range(int(start // 60), int(end // 60) + 1))
    return minutes


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def human_gap(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def episodes(minutes: list[int]) -> int:
    """Collapse runs of consecutive minutes into one incident."""
    if not minutes:
        return 0
    ordered = sorted(minutes)
    count = 1
    for previous, current in zip(ordered, ordered[1:]):
        if current - previous > 5:   # a 5-minute break starts a new incident
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="window to analyse (default 30)")
    parser.add_argument("--full-paths", action="store_true",
                        help="print whole working directories instead of basenames")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=args.days)).timestamp()

    claude_writes, claude_cwds, claude_stats = load_claude(cutoff)
    codex_writes, codex_cwds, codex_files = load_codex_rollouts(cutoff)
    db_threads = codex_db_threads(cutoff)

    writes = {**claude_writes, **codex_writes}
    cwds = {**claude_cwds, **codex_cwds}

    print()
    print("AIWatcher — live-session probe (read-only)")
    print(f"Window: last {args.days} days, to {now:%Y-%m-%d %H:%M} UTC")
    print(f"Claude: {claude_stats['files']} transcripts, {len(claude_writes)} sessions, "
          f"{claude_stats['lines']} entries")
    print(f"Codex:  {codex_files} rollouts, {len(codex_writes)} sessions, "
          f"{len(db_threads)} DB threads touched")

    if not writes:
        print()
        print("No local session data in this window. Nothing to conclude — rerun with")
        print("a longer --days, or on the machine where you actually code.")
        return 0

    all_stamps = [stamp for times in writes.values() for stamp in times]
    first, last = min(all_stamps), max(all_stamps)
    print(f"Data spans {datetime.fromtimestamp(first, timezone.utc):%Y-%m-%d} to "
          f"{datetime.fromtimestamp(last, timezone.utc):%Y-%m-%d}")

    # -- 1. concurrency -----------------------------------------------------
    print()
    print("1. CONCURRENCY — does more than one session ever run at once?")
    print("   Counted over minutes when at least one session was live, so nights")
    print("   and weekends do not pad the 'just one' bucket.")
    per_window_minutes: dict[int, dict[int, set]] = {}
    for window in IDLE_WINDOWS_SECONDS:
        minute_keys: dict[int, set] = defaultdict(set)
        for key, times in writes.items():
            for minute in active_minutes(times, window):
                minute_keys[minute].add(key)
        per_window_minutes[window] = minute_keys
        histogram = Counter(len(keys) for keys in minute_keys.values())
        total = sum(histogram.values())
        multi = sum(count for size, count in histogram.items() if size >= 2)
        three = sum(count for size, count in histogram.items() if size >= 3)
        peak = max(histogram) if histogram else 0
        print()
        print(f"   idle window {window}s — {total} live minutes")
        print(f"     2 or more concurrent: {multi:6d} min ({100.0 * multi / total:5.1f}%)")
        print(f"     3 or more concurrent: {three:6d} min ({100.0 * three / total:5.1f}%)")
        print(f"     peak concurrent:      {peak}")
        shape = " ".join(f"{size}:{histogram[size]}" for size in sorted(histogram))
        print(f"     full shape (sessions:minutes) {shape}")

    # -- 2. gaps ------------------------------------------------------------
    print()
    print("2. IDLE GAPS — how long is a normal pause inside a live session?")
    print("   A 'gone quiet' threshold has to sit above the everyday gap, or the")
    print("   first slow test run fires a false alarm.")
    for tool, source in (("claude", claude_writes), ("codex", codex_writes)):
        gaps: list[float] = []
        for times in source.values():
            ordered = sorted(times)
            gaps.extend(b - a for a, b in zip(ordered, ordered[1:]) if b > a)
        if not gaps:
            print(f"   {tool}: no gaps in window")
            continue
        within = [gap for gap in gaps if gap <= 3600]
        over_30 = sum(1 for gap in within if gap > 30)
        over_60 = sum(1 for gap in within if gap > 60)
        over_120 = sum(1 for gap in within if gap > 120)
        print()
        print(f"   {tool}: {len(gaps)} gaps ({len(within)} under an hour)")
        print(f"     p50 {human_gap(percentile(within, 50))}   "
              f"p90 {human_gap(percentile(within, 90))}   "
              f"p99 {human_gap(percentile(within, 99))}")
        print(f"     over 30s:  {100.0 * over_30 / len(within):5.1f}% of in-session gaps")
        print(f"     over 60s:  {100.0 * over_60 / len(within):5.1f}%")
        print(f"     over 120s: {100.0 * over_120 / len(within):5.1f}%")

    # -- 3. collisions ------------------------------------------------------
    print()
    print("3. SAME WORKING TREE — have two live sessions ever shared one directory?")
    minute_keys = per_window_minutes[120]
    hits: dict[str, list[int]] = defaultdict(list)
    for minute, keys in minute_keys.items():
        by_cwd: dict[str, set] = defaultdict(set)
        for key in keys:
            cwd = cwds.get(key)
            if cwd:
                by_cwd[cwd].add(key)
        for cwd, sharers in by_cwd.items():
            if len(sharers) >= 2:
                hits[cwd].append(minute)
    if not hits:
        print("   Never, in this window. Drop the collision warning — it would")
        print("   be a feature that only ever renders as 'nothing to report'.")
    else:
        total_incidents = sum(episodes(minutes) for minutes in hits.values())
        print(f"   {total_incidents} separate incidents across {len(hits)} directories")
        ranked = sorted(hits.items(), key=lambda item: -len(item[1]))
        for cwd, minutes in ranked[:8]:
            label = cwd if args.full_paths else Path(cwd).name or cwd
            print(f"     {label:<32} {episodes(minutes):3d} incidents, "
                  f"{len(minutes):4d} overlapping minutes")

    # -- 4. subagents -------------------------------------------------------
    print()
    print("4. SUBAGENTS — is there anything to count? (Claude only; Codex exposes none)")
    with_subagents = claude_stats["sessions_with_subagents"]
    if not with_subagents:
        print("   No Task calls and no sidechain entries in this window.")
        print("   A subagent badge would read zero every time it was looked at.")
    else:
        share = 100.0 * len(with_subagents) / max(1, len(claude_writes))
        print(f"   {len(with_subagents)} of {len(claude_writes)} Claude sessions "
              f"({share:.0f}%) spawned subagents")
        print(f"   {claude_stats['task_calls']} Task calls, "
              f"{claude_stats['sidechain_lines']} subagent transcript entries")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
