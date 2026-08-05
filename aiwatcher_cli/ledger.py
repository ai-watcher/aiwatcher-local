"""Change ledger: what each commit cost, and what never reached one.

A session is a unit of tooling, not a unit of work -- it does not end, it
spans unrelated tasks, and it maps many-to-many onto commits. Measuring
outcomes per session is what made "cost per success" unanswerable: several
sessions claim the same commits, so their costs double-count.

This module changes the unit. A *change* is a commit, and its cost is the AI
spend in that repo since the previous change. That is a rule, not a heuristic:
there is no matching step to get wrong, and two sessions running in parallel on
one repo both count toward the commit they preceded -- which is correct, not a
compromise.

Attribution is per *event*, not per session. Every event carries its own
timestamp, cost and project path, and event costs sum exactly to session costs,
so a long session spanning several commits splits across them at the turn it
actually happened rather than dumping its whole cost on whichever commit came
last.

Spend that never reached a commit -- exploration that went nowhere, or work
still uncommitted -- is not forced onto the next commit. It is reported
separately as `unbanked`, which is the most direct measure of waste in the
product: money spent with nothing to show for it.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from .outcome_evidence import _repo_root
from .scanner import LocalEvent
from .survival import MIN_AGE_DAYS, measure_change_survival

# `git log --numstat` over a month of history is far heavier than the small
# point queries outcome_evidence makes, so this gets its own budget rather than
# borrowing that module's 2s one.
GIT_TIMEOUT_SECONDS = 20

# How far back a commit may reach for spend. Without a cap, a trivial commit
# made after a week away would inherit every dollar spent before the gap. Spend
# older than this stays unbanked instead of being misattributed.
DEFAULT_MAX_LOOKBACK_HOURS = 12.0


@dataclass
class Change:
    sha: str
    repo: str
    subject: str
    committed_at: datetime
    cost_usd: float = 0.0
    lines_added: int = 0
    lines_removed: int = 0
    files_changed: int = 0
    event_count: int = 0
    session_ids: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    @property
    def lines_changed(self) -> int:
        return self.lines_added + self.lines_removed

    @property
    def usd_per_line(self) -> float | None:
        return self.cost_usd / self.lines_changed if self.lines_changed else None

    def to_json(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "repo": self.repo,
            "subject": self.subject,
            "committed_at": self.committed_at.isoformat(),
            "cost_usd": round(self.cost_usd, 6),
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "lines_changed": self.lines_changed,
            "files_changed": self.files_changed,
            "event_count": self.event_count,
            "session_ids": self.session_ids,
            "tools": self.tools,
            "models": self.models,
            "usd_per_line": round(self.usd_per_line, 6) if self.usd_per_line is not None else None,
        }


@dataclass
class Ledger:
    changes: list[Change] = field(default_factory=list)
    banked_usd: float = 0.0
    unbanked_usd: float = 0.0
    unbanked_events: int = 0
    repos: list[str] = field(default_factory=list)
    window_days: int = 7
    max_lookback_hours: float = DEFAULT_MAX_LOOKBACK_HOURS

    @property
    def total_usd(self) -> float:
        return self.banked_usd + self.unbanked_usd

    @property
    def unbanked_pct(self) -> float:
        return 100.0 * self.unbanked_usd / self.total_usd if self.total_usd > 0 else 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "changes": [change.to_json() for change in self.changes],
            "banked_usd": round(self.banked_usd, 6),
            "unbanked_usd": round(self.unbanked_usd, 6),
            "unbanked_events": self.unbanked_events,
            "unbanked_pct": round(self.unbanked_pct, 1),
            "total_usd": round(self.total_usd, 6),
            "repos": self.repos,
            "window_days": self.window_days,
            "max_lookback_hours": self.max_lookback_hours,
        }


def _git(repo: str, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", repo, *args],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def commits_since(repo: str, since: datetime) -> list[Change]:
    """Commits on the current branch since `since`, oldest first, with line counts.

    One `git log` call carries both the commit headers and the per-file numstat
    rows, so a month of history costs one subprocess rather than one per commit.
    Merge commits are skipped (`--no-merges`): their diff is the union of work
    already counted on the commits they merge, so including them would
    double-count every line.
    """
    result = _git(repo, [
        "log", "--no-merges", "--numstat",
        f"--since={since.astimezone(timezone.utc).isoformat()}",
        "--pretty=format:\x1e%H\x1f%ct\x1f%s",
    ])
    if not result or result.returncode != 0:
        return []

    changes: list[Change] = []
    for record in result.stdout.split("\x1e"):
        if not record.strip():
            continue
        header, _, body = record.partition("\n")
        parts = header.split("\x1f", 2)
        if len(parts) != 3:
            continue
        sha, stamp, subject = parts
        try:
            committed_at = datetime.fromtimestamp(int(stamp), timezone.utc)
        except (TypeError, ValueError):
            continue
        change = Change(
            sha=sha[:12],
            repo=repo,
            subject=subject.strip(),
            committed_at=committed_at,
        )
        for line in body.splitlines():
            cells = line.split("\t")
            if len(cells) != 3:
                continue
            added, removed, _path = cells
            # "-" marks a binary file; it has no line count to add.
            if added.isdigit():
                change.lines_added += int(added)
            if removed.isdigit():
                change.lines_removed += int(removed)
            change.files_changed += 1
        changes.append(change)

    changes.sort(key=lambda item: item.committed_at)
    return changes


def _event_repo(event: LocalEvent, cache: dict[str, str | None]) -> str | None:
    path = event.project_path
    if not path:
        return None
    if path not in cache:
        try:
            cache[path] = _repo_root(path)
        except OSError:
            cache[path] = None
    return cache[path]


def build_ledger(
    events: Sequence[LocalEvent],
    *,
    days: int = 7,
    max_lookback_hours: float = DEFAULT_MAX_LOOKBACK_HOURS,
    now: datetime | None = None,
) -> Ledger:
    """Attribute AI spend to the commits it preceded.

    Each costed event is banked against the first commit at or after it in the
    same repo, provided that commit lands within `max_lookback_hours`. Events
    with no such commit -- because none followed, or the next one came too long
    afterwards -- are unbanked.

    Events with no cost are ignored entirely rather than counted as free work:
    they are metadata (titles, mode switches) and would inflate event counts
    without moving a dollar.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = now - timedelta(days=days)
    cutoff = timedelta(hours=max_lookback_hours)

    repo_cache: dict[str, str | None] = {}
    by_repo: dict[str, list[LocalEvent]] = defaultdict(list)
    unbanked_usd = 0.0
    unbanked_events = 0

    for event in events:
        if event.cost_usd <= 0 or not event.timestamp:
            continue
        stamp = event.timestamp.astimezone(timezone.utc)
        if stamp < since:
            continue
        repo = _event_repo(event, repo_cache)
        if not repo:
            # Spend outside any git repo can never be banked against a change.
            unbanked_usd += event.cost_usd
            unbanked_events += 1
            continue
        by_repo[repo].append(event)

    all_changes: list[Change] = []
    for repo, repo_events in by_repo.items():
        # Look back a little before the window so an event early in it can bank
        # against a commit that the window itself would have excluded.
        changes = commits_since(repo, since - cutoff)
        if not changes:
            unbanked_usd += sum(event.cost_usd for event in repo_events)
            unbanked_events += len(repo_events)
            continue

        seen_sessions: dict[str, set[str]] = defaultdict(set)
        seen_tools: dict[str, set[str]] = defaultdict(set)
        seen_models: dict[str, set[str]] = defaultdict(set)

        for event in repo_events:
            stamp = event.timestamp.astimezone(timezone.utc)
            target = _first_commit_at_or_after(changes, stamp)
            if target is None or target.committed_at - stamp > cutoff:
                unbanked_usd += event.cost_usd
                unbanked_events += 1
                continue
            target.cost_usd += event.cost_usd
            target.event_count += 1
            seen_sessions[target.sha].add(event.session_id)
            if event.tool:
                seen_tools[target.sha].add(event.tool)
            if event.model:
                seen_models[target.sha].add(event.model)

        for change in changes:
            if change.committed_at < since:
                # Only in range to catch spillover; not part of this window.
                continue
            change.session_ids = sorted(seen_sessions.get(change.sha, ()))
            change.tools = sorted(seen_tools.get(change.sha, ()))
            change.models = sorted(seen_models.get(change.sha, ()))
            all_changes.append(change)

    all_changes.sort(key=lambda change: change.cost_usd, reverse=True)
    return Ledger(
        changes=all_changes,
        banked_usd=sum(change.cost_usd for change in all_changes),
        unbanked_usd=unbanked_usd,
        unbanked_events=unbanked_events,
        repos=sorted(by_repo),
        window_days=days,
        max_lookback_hours=max_lookback_hours,
    )


MIN_MEASURED_CHANGES = 3

# Survival costs roughly half a second per change (a blame pass per file), so a
# month of history cannot be measured on a page load. Cap by *spend covered*
# rather than change count: a fixed count of 25 looked reasonable but left 28%
# of local spend unmeasured, because cost per change is long-tailed. Walking
# down the cost ranking until most of the money is accounted for gives a figure
# that represents the window instead of just its biggest commits.
TARGET_COST_COVERAGE = 0.8
MAX_CHANGES_MEASURED = 40  # backstop, so a pathological window still terminates


def cost_per_surviving_line(
    ledger: Ledger,
    *,
    max_changes: int = MAX_CHANGES_MEASURED,
    min_changes: int = MIN_MEASURED_CHANGES,
    target_coverage: float = TARGET_COST_COVERAGE,
    min_age_days: float = MIN_AGE_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What your surviving code actually cost.

    Replaces the survived/churned split that was built on git reachability.
    That asked whether a commit was still in history, which stays true after a
    revert or a full rewrite, so locally it reported 16 of 16 changes surviving
    and the resulting "cost per surviving change" was cost-per-change wearing a
    different label.

    Survival is continuous here rather than a verdict: a change that is 60%
    still standing counts as 60%, not as a coin flip against some threshold.
    So the headline is cost per surviving *line* -- total spend over lines that
    are still there.

    Changes too recent to judge are excluded and counted separately rather than
    scored. Including them would drag the figure toward flattery, since almost
    nothing has had time to be rewritten yet.
    """
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = stamp - timedelta(days=min_age_days)

    # Age is filtered BEFORE the coverage walk, using the commit time the ledger
    # already carries. Selecting first and dropping recent changes afterwards
    # let them consume the budget while contributing nothing -- aiming at 80%
    # coverage actually landed at 52%.
    paid = [change for change in ledger.changes if change.cost_usd > 0]
    too_recent_changes = [change for change in paid if change.committed_at > cutoff]
    judgeable = sorted(
        (change for change in paid if change.committed_at <= cutoff),
        key=lambda change: change.cost_usd,
        reverse=True,
    )

    total_cost = sum(change.cost_usd for change in judgeable)
    capped: list[Change] = []
    running = 0.0
    for change in judgeable:
        if len(capped) >= max_changes:
            break
        capped.append(change)
        running += change.cost_usd
        if total_cost > 0 and running / total_cost >= target_coverage:
            break

    measured = 0
    too_recent = len(too_recent_changes)
    unmeasurable = 0
    cost = 0.0
    touched = 0
    intact = 0

    for change in capped:
        result = measure_change_survival(
            change.repo, change.sha, min_age_days=min_age_days, now=now
        )
        if not result.measurable:
            unmeasurable += 1
            continue
        measured += 1
        cost += change.cost_usd
        touched += result.lines_touched
        intact += result.lines_intact

    if measured < min_changes:
        return {
            "available": False,
            "reason": (
                f"Only {measured} change{'s' if measured != 1 else ''} old enough to judge; "
                f"need {min_changes}. Survival needs about a week to mean anything."
            ),
            "changes_measured": measured,
            "changes_too_recent": too_recent,
            "changes_unmeasurable": unmeasurable,
        }

    survival_pct = 100.0 * intact / touched if touched else None
    return {
        "available": True,
        "reason": None,
        "changes_measured": measured,
        "changes_too_recent": too_recent,
        "changes_unmeasurable": unmeasurable,
        "changes_considered": len(judgeable),
        "changes_skipped": max(0, len(judgeable) - len(capped)),
        "too_recent_usd": round(sum(c.cost_usd for c in too_recent_changes), 6),
        # What share of the window's banked spend the measured changes account
        # for. Without this the ratio looks authoritative while quietly
        # describing only the biggest commits.
        "cost_coverage_pct": round(100.0 * cost / total_cost, 1) if total_cost > 0 else None,
        "cost_usd": round(cost, 6),
        "lines_touched": touched,
        "lines_intact": intact,
        "survival_pct": round(survival_pct, 1) if survival_pct is not None else None,
        "usd_per_line": round(cost / touched, 6) if touched else None,
        "usd_per_surviving_line": round(cost / intact, 6) if intact else None,
        # A floor, for the same reason measure_change_survival reports one:
        # blame moves attribution on reformats and refactors.
        "is_floor": True,
    }


def _first_commit_at_or_after(changes: Sequence[Change], stamp: datetime) -> Change | None:
    """Earliest commit not before `stamp`.

    Linear rather than bisecting: the commit list for a window is small, and a
    plain scan keeps the "which change did this turn feed into" rule obvious.
    """
    for change in changes:
        if change.committed_at >= stamp:
            return change
    return None
