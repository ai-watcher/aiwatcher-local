"""
Context growth and session health analysis.

Detects the pattern behind 325M-token sessions: a long-lived agentic session
that never restarts, so each turn resends the entire conversation history.
The per-turn token count climbs linearly; the "bloat ratio" measures what
share of the session's bill went on replaying that history rather than on new
work.

Bloat is measured in dollars, not tokens, and deliberately so. As a share of
*tokens*, replayed history is 78-99% of billed input for every non-trivial
session -- that is simply how prompt caching works, and a metric that reads
"96%" for everything ranks nothing. Cached reads are also billed at a tenth of
the input rate, so a high replayed-token share is the cheap case, not the
expensive one. As a share of *cost* the same sessions spread across 16-83%,
which is both discriminating and directly actionable.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .pricing import cache_read_cost
from .scanner import LocalEvent, LocalSession


# Thresholds — adjustable via config later
PRESSURE_TOKENS_PER_TURN: int = 150_000   # yellow: worth watching
CRITICAL_TOKENS_PER_TURN: int = 200_000   # red: action needed
STALE_WARN_HOURS: float       = 24.0      # yellow: session older than 1 day
CRITICAL_STALE_DAYS: float    = 7.0       # red: session older than 1 week
# Tuned against the observed spread of replayed-context cost share (16-83%,
# median 47%): HIGH is roughly the top quartile, EXTREME the tail.
HIGH_BLOAT_RATIO: float       = 0.60      # 60% of the session's spend was replay
EXTREME_BLOAT_RATIO: float    = 0.75      # replay dominates; a restart pays for itself
MIN_EVENTS_FOR_CONTEXT_ANALYSIS: int = 3  # need enough turns to detect meaningful growth
MIN_TOKENS_FOR_CONTEXT_ANALYSIS: int = 5_000  # ignore sessions with trivial token counts
ACTIVE_SESSION_DAYS: int = 30             # only surface sessions active in last 30 days


@dataclass
class ContextHealth:
    session_id: str
    tool: str
    project_path: str | None
    age_hours: float
    age_days: float

    # Event-level token stats
    event_count: int              # model_usage events analyzed
    total_input_tokens: int       # sum of tokens_in across all events
    total_output_tokens: int      # sum of tokens_out across all events
    latest_turn_tokens: int       # most recent event's tokens_in
    peak_turn_tokens: int         # max tokens_in across all events
    avg_turn_tokens: float        # mean tokens_in per event
    growth_rate: float            # average Δtokens_in per turn (positive = growing)

    # Bloat — measured in dollars; see the module docstring for why not tokens.
    bloat_ratio: float            # replayed_cost_usd / analyzed_cost_usd
    efficiency_pct: float         # share of spend that bought new work: (1 - bloat) * 100
    # False when the source reports no cache buckets, or the model is priced at
    # zero (subscription plans). Then bloat_ratio is 0.0 because it is unknown,
    # not because the session replayed nothing — callers must not render it.
    bloat_measurable: bool
    replayed_cost_usd: float      # spend attributable to re-sent history
    analyzed_cost_usd: float      # spend across the events analyzed here
    latest_turn_replayed_tokens: int  # cache reads on the most recent turn

    # Flags
    is_stale: bool
    is_critical_stale: bool
    is_context_pressure: bool     # latest/peak > PRESSURE threshold
    is_context_critical: bool     # latest/peak > CRITICAL threshold
    is_high_bloat: bool
    is_extreme_bloat: bool

    severity: str                 # "healthy" | "warning" | "critical"
    recommendations: list[str] = field(default_factory=list)


def _age_hours(session: LocalSession) -> float:
    stamp = session.updated_at or session.started_at
    if not stamp:
        return 0.0
    now = datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (now - stamp).total_seconds() / 3600)


def analyze_session_health(
    session: LocalSession,
    events: Sequence[LocalEvent],
) -> ContextHealth | None:
    """
    Return a ContextHealth for one session, or None if there are no
    model_usage events with token data to analyze.
    """
    relevant = sorted(
        [
            e for e in events
            if (
                e.session_id == session.session_id
                and e.tokens_in >= MIN_TOKENS_FOR_CONTEXT_ANALYSIS
            )
        ],
        key=lambda e: e.timestamp or datetime.min.replace(tzinfo=timezone.utc),
    )
    # Need meaningful per-turn token data to produce useful context analysis
    if len(relevant) < MIN_EVENTS_FOR_CONTEXT_ANALYSIS:
        return None

    total_in  = sum(e.tokens_in  for e in relevant)
    total_out = sum(e.tokens_out for e in relevant)
    peak      = max(e.tokens_in  for e in relevant)
    latest    = relevant[-1].tokens_in
    avg       = statistics.mean(e.tokens_in for e in relevant)

    # Growth rate: average delta of tokens_in between consecutive events
    deltas = [
        relevant[i].tokens_in - relevant[i - 1].tokens_in
        for i in range(1, len(relevant))
        if relevant[i].tokens_in > 0 and relevant[i - 1].tokens_in > 0
    ]
    growth_rate = statistics.mean(deltas) if deltas else 0.0

    # Bloat ratio: what share of this session's bill was re-sent history.
    # cache_read_tokens is the replayed portion as the provider counted it, and
    # it is billed at the discounted cache rate, so price it that way.
    replayed_usd  = sum(cache_read_cost(e.model, e.cache_read_tokens) for e in relevant)
    analyzed_usd  = sum(e.cost_usd for e in relevant)
    # A source that reports cache buckets at all will show writes even when a
    # session gets no read hits; all-zero across both means "not reported".
    reports_cache = any(e.cache_read_tokens or e.cache_write_tokens for e in relevant)
    measurable    = reports_cache and analyzed_usd > 0

    if measurable:
        bloat_ratio = max(0.0, min(1.0, replayed_usd / analyzed_usd))
        efficiency  = (1.0 - bloat_ratio) * 100
    else:
        bloat_ratio = 0.0
        efficiency  = 0.0

    age_hours     = _age_hours(session)
    age_days      = age_hours / 24.0

    is_stale           = age_hours > STALE_WARN_HOURS
    is_critical_stale  = age_days  > CRITICAL_STALE_DAYS
    is_pressure        = latest > PRESSURE_TOKENS_PER_TURN or peak > PRESSURE_TOKENS_PER_TURN
    is_critical        = latest > CRITICAL_TOKENS_PER_TURN or peak > CRITICAL_TOKENS_PER_TURN
    is_high_bloat      = measurable and bloat_ratio > HIGH_BLOAT_RATIO
    is_extreme_bloat   = measurable and bloat_ratio > EXTREME_BLOAT_RATIO

    # Severity — stale alone is a warning, not critical; context pressure or extreme bloat = critical
    if is_critical or is_extreme_bloat or (is_critical_stale and (is_pressure or is_high_bloat)):
        severity = "critical"
    elif is_pressure or is_stale or is_high_bloat or is_critical_stale:
        severity = "warning"
    else:
        severity = "healthy"

    # Recommendations
    recs: list[str] = []
    if is_critical_stale:
        recs.append(
            f"Session is {age_days:.0f} days old. Start a new session per task "
            "to prevent context from accumulating across unrelated work."
        )
    elif is_stale:
        recs.append(
            f"Session is {age_hours:.0f}h old. Consider restarting for a fresh context."
        )
    if is_critical or is_pressure:
        recs.append(
            f"Context is {latest:,} tokens/turn "
            f"({'critical' if is_critical else 'elevated'}). "
            "Run /compact (Codex/Claude) to compress history, or start a new session."
        )
    if is_extreme_bloat:
        recs.append(
            f"{bloat_ratio * 100:.0f}% of this session's ${analyzed_usd:.2f} went on "
            f"re-sending history it had already sent (${replayed_usd:.2f}). "
            f"Only {efficiency:.0f}c in the dollar bought new work — restart or /compact."
        )
    elif is_high_bloat:
        recs.append(
            f"${replayed_usd:.2f} of this session's ${analyzed_usd:.2f} "
            f"({bloat_ratio * 100:.0f}%) was replayed history. "
            "Use /compact to compress older turns before they compound further."
        )
    if not recs:
        recs.append("Context is healthy.")

    return ContextHealth(
        session_id=session.session_id,
        tool=session.tool,
        project_path=session.project_path,
        age_hours=age_hours,
        age_days=age_days,
        event_count=len(relevant),
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        latest_turn_tokens=latest,
        peak_turn_tokens=peak,
        avg_turn_tokens=avg,
        growth_rate=growth_rate,
        bloat_ratio=bloat_ratio,
        efficiency_pct=efficiency,
        bloat_measurable=measurable,
        replayed_cost_usd=replayed_usd,
        analyzed_cost_usd=analyzed_usd,
        latest_turn_replayed_tokens=relevant[-1].cache_read_tokens,
        is_stale=is_stale,
        is_critical_stale=is_critical_stale,
        is_context_pressure=is_pressure,
        is_context_critical=is_critical,
        is_high_bloat=is_high_bloat,
        is_extreme_bloat=is_extreme_bloat,
        severity=severity,
        recommendations=recs,
    )


def analyze_all_sessions(
    sessions: Sequence[LocalSession],
    events: Sequence[LocalEvent],
    *,
    active_days: int = ACTIVE_SESSION_DAYS,
) -> list[ContextHealth]:
    """
    Analyze context health for sessions active within the last `active_days` days.
    Older sessions are skipped — they are historical record, not actionable alerts.
    """
    cutoff_hours = active_days * 24.0
    results: list[ContextHealth] = []
    for session in sessions:
        h = analyze_session_health(session, events)
        if h is not None and h.age_hours <= cutoff_hours:
            results.append(h)
    # Most severe first
    order = {"critical": 0, "warning": 1, "healthy": 2}
    results.sort(key=lambda h: (order.get(h.severity, 9), -h.total_input_tokens))
    return results


def _compact_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _short_project(path: str | None) -> str:
    if not path:
        return "(unknown)"
    try:
        parts = Path(path).parts
        return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    except Exception:
        return path


def format_health_row(h: ContextHealth) -> str:
    """One-line summary for the health leaderboard."""
    icon = {"critical": "[CRIT]", "warning": "[WARN]", "healthy": "[OK]  "}[h.severity]
    project = _short_project(h.project_path)
    age = f"{h.age_days:.0f}d" if h.age_days >= 1 else f"{h.age_hours:.0f}h"
    ctx = f"{_compact_tokens(h.latest_turn_tokens)}/turn"
    eff = f"{h.efficiency_pct:.0f}% new work" if h.bloat_measurable else "replay n/a"
    return f"{icon} {project:<32} {h.tool:<12} age={age:<5} ctx={ctx:<10} {eff}"


def format_health_block(h: ContextHealth) -> str:
    """Multi-line detail for a single session."""
    lines: list[str] = []
    project = _short_project(h.project_path)
    age_str = (
        f"{h.age_days:.1f} days"
        if h.age_days >= 1
        else f"{h.age_hours:.1f} hours"
    )

    lines.append(f"Context health: {h.severity.upper()}")
    lines.append(f"  Project : {project}  ({h.tool})")
    lines.append(f"  Age     : {age_str}")
    lines.append(f"  Context : {_compact_tokens(h.latest_turn_tokens)} tokens/turn  "
                 f"(peak {_compact_tokens(h.peak_turn_tokens)},  "
                 f"healthy: <{_compact_tokens(PRESSURE_TOKENS_PER_TURN)})")
    lines.append(f"  Total   : {_compact_tokens(h.total_input_tokens)} input  /  "
                 f"{_compact_tokens(h.total_output_tokens)} output")
    if h.bloat_measurable:
        lines.append(f"  Replay  : ${h.replayed_cost_usd:.2f} of ${h.analyzed_cost_usd:.2f}  "
                     f"({h.bloat_ratio * 100:.0f}% of spend re-sent history,  "
                     f"healthy: <{HIGH_BLOAT_RATIO * 100:.0f}%)")
    else:
        lines.append("  Replay  : not measurable (source reports no cache buckets)")
    if h.growth_rate > 1000:
        lines.append(f"  Growth  : +{_compact_tokens(int(h.growth_rate))}/turn  (context accumulating)")
    lines.append(f"  Events  : {h.event_count} model calls analyzed")
    lines.append("")
    for rec in h.recommendations:
        lines.append(f"  → {rec}")
    return "\n".join(lines)


def format_health_section(healths: list[ContextHealth]) -> str:
    """
    Full context health section for `aiwatcher today`.
    Shows the leaderboard and expands critical/warning sessions.
    """
    if not healths:
        return ""
    lines: list[str] = ["", "Context health"]
    lines.append("-" * 64)
    for h in healths:
        lines.append(format_health_row(h))
    critical = [h for h in healths if h.severity == "critical"]
    warnings = [h for h in healths if h.severity == "warning"]
    if critical or warnings:
        lines.append("")
        for h in (critical + warnings)[:3]:  # cap at 3 expanded details
            lines.append("")
            for line in format_health_block(h).splitlines():
                lines.append("  " + line)
    return "\n".join(lines)


def gate_health_warning(
    sessions: Sequence[LocalSession],
    events: Sequence[LocalEvent],
    tool: str,
    cwd: str | None = None,
) -> str | None:
    """
    Return a short warning string if the most-recently-updated session for this
    tool has a context health problem. Used by the preflight gate.
    Returns None if context is healthy.
    """
    tool_map = {
        "claude": {"claude-code", "claude"},
        "codex":  {"codex-cli", "codex"},
        "cursor": {"cursor"},
    }
    allowed = tool_map.get(tool.lower(), {tool.lower()})
    candidates = [s for s in sessions if s.tool.lower() in allowed]
    if not candidates:
        return None
    # Most recently updated session
    def _stamp(s: LocalSession) -> datetime:
        t = s.updated_at or s.started_at
        if t and t.tzinfo is None:
            return t.replace(tzinfo=timezone.utc)
        return t or datetime.min.replace(tzinfo=timezone.utc)

    active = max(candidates, key=_stamp)
    h = analyze_session_health(active, events)
    if h is None or h.severity == "healthy":
        return None

    lines: list[str] = []
    if h.is_context_critical:
        lines.append(
            f"⚠ Context pressure: active {tool} session has "
            f"{_compact_tokens(h.latest_turn_tokens)} tokens/turn context"
            + (f" ({h.efficiency_pct:.0f}% of spend on new work)."
               if h.bloat_measurable else ".")
        )
    elif h.is_context_pressure:
        lines.append(
            f"⚠ Context elevated: active {tool} session at "
            f"{_compact_tokens(h.latest_turn_tokens)} tokens/turn."
        )
    if h.is_critical_stale:
        lines.append(
            f"  Session is {h.age_days:.0f} days old — "
            "start a new session to prevent further context growth."
        )
    if h.is_extreme_bloat or h.is_high_bloat:
        lines.append(
            f"  {int(h.bloat_ratio * 100)}% of this session's spend so far "
            f"(${h.replayed_cost_usd:.2f}) went on re-sending history."
        )
        lines.append("  Recommendation: /compact or restart before proceeding.")
    return "\n".join(lines) if lines else None
