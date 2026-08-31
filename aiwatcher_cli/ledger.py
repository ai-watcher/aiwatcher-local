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


# A commit whose committer date runs this far ahead of its author date was
# rewritten -- rebased, cherry-picked, or amended long after the fact. Used only
# to label the row; attribution keys off the author date either way.
REWRITE_SKEW = timedelta(hours=1)


@dataclass
class Change:
    sha: str
    repo: str
    subject: str
    committed_at: datetime
    # When the work was actually done. `git rebase` rewrites committed_at to
    # the moment of the rebase but leaves this alone, which is why attribution
    # keys off it -- see `landed_at`.
    authored_at: datetime | None = None
    author_email: str = ""
    cost_usd: float = 0.0
    lines_added: int = 0
    lines_removed: int = 0
    files_changed: int = 0
    event_count: int = 0
    session_ids: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    @property
    def landed_at(self) -> datetime:
        """The timestamp attribution uses: when this work was committed.

        Author date, not committer date. Rebasing rewrites the committer date
        to the rebase moment, which pushed the original work outside the 12h
        lookback and stranded its spend -- 30 of 32 rewritten commits locally
        read as free while $537 read as waste, when both records were correct
        and simply could not find each other.

        Clamped to the committer date for the pathological case where a clock
        skew or an explicit `--date` puts the author date in the future.
        """
        if self.authored_at is None:
            return self.committed_at
        return min(self.authored_at, self.committed_at)

    @property
    def was_rewritten(self) -> bool:
        if self.authored_at is None:
            return False
        return self.committed_at - self.authored_at > REWRITE_SKEW

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
            "authored_at": self.authored_at.isoformat() if self.authored_at else None,
            "landed_at": self.landed_at.isoformat(),
            "was_rewritten": self.was_rewritten,
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


# Why a dollar never reached a commit. Kept apart because they call for
# different responses: work outside a repo may simply belong somewhere git
# cannot see, while spend in a repo that never committed is the real signal.
UNBANKED_OUTSIDE_REPO = "outside_repo"
UNBANKED_NO_COMMIT = "no_commit_followed"


@dataclass
class Ledger:
    changes: list[Change] = field(default_factory=list)
    banked_usd: float = 0.0
    unbanked_usd: float = 0.0
    unbanked_events: int = 0
    unbanked_by_reason: dict[str, float] = field(default_factory=dict)
    unbanked_by_repo: dict[str, float] = field(default_factory=dict)
    # Spend git could not answer for -- an unreadable repo, or a `git log` that
    # timed out. Held apart from unbanked deliberately: treating a git failure
    # as "money with nothing to show for it" would inflate the one number this
    # module exists to report, and it would do so silently.
    unresolved_usd: float = 0.0
    unresolved_events: int = 0
    unresolved_repos: list[str] = field(default_factory=list)
    # Commits dropped because someone else authored them. Reported so the
    # change table can say the window held other people's work rather than
    # looking like it silently lost commits.
    foreign_changes: int = 0
    repos: list[str] = field(default_factory=list)
    window_days: int = 7
    max_lookback_hours: float = DEFAULT_MAX_LOOKBACK_HOURS

    @property
    def total_usd(self) -> float:
        return self.banked_usd + self.unbanked_usd + self.unresolved_usd

    @property
    def classified_usd(self) -> float:
        """Spend the ledger could actually place — banked or provably unbanked."""
        return self.banked_usd + self.unbanked_usd

    @property
    def unbanked_pct(self) -> float:
        # Share of what could be classified, not of everything: spend git could
        # not answer for is neither banked nor unbanked, and folding it into the
        # denominator would quietly understate the rate.
        base = self.classified_usd
        return 100.0 * self.unbanked_usd / base if base > 0 else 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "changes": [change.to_json() for change in self.changes],
            "banked_usd": round(self.banked_usd, 6),
            "unbanked_usd": round(self.unbanked_usd, 6),
            "unbanked_events": self.unbanked_events,
            "unbanked_pct": round(self.unbanked_pct, 1),
            "unbanked_by_reason": {
                key: round(value, 6) for key, value in sorted(self.unbanked_by_reason.items())
            },
            "unbanked_by_repo": {
                key: round(value, 6) for key, value in sorted(
                    self.unbanked_by_repo.items(), key=lambda item: item[1], reverse=True
                )
            },
            "unresolved_usd": round(self.unresolved_usd, 6),
            "unresolved_events": self.unresolved_events,
            "unresolved_repos": self.unresolved_repos,
            "foreign_changes": self.foreign_changes,
            "classified_usd": round(self.classified_usd, 6),
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
            # git emits UTF-8; without this, text=True decodes with the locale
            # codec (cp1252 on Windows) and any non-ASCII commit subject comes
            # back mangled -- an em dash renders as "â€”".
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def commits_since(repo: str, since: datetime) -> list[Change] | None:
    """Commits on the current branch since `since`, oldest first, with line counts.

    One `git log` call carries both the commit headers and the per-file numstat
    rows, so a month of history costs one subprocess rather than one per commit.
    Merge commits are skipped (`--no-merges`): their diff is the union of work
    already counted on the commits they merge, so including them would
    double-count every line.

    Returns None when git could not answer at all -- an unreadable path, or a
    call that timed out. That is distinct from an empty list, which means the
    repo genuinely has no commits in the window. Callers must not conflate the
    two: an empty list makes every dollar in that repo unbanked, and a git
    failure would otherwise fake exactly that result.
    """
    result = _git(repo, [
        "log", "--no-merges", "--numstat",
        # --since filters on committer date, so it is deliberately left wider
        # than the caller's window: a commit authored inside the window but
        # rebased later still has a committer date inside it, and one authored
        # before the window is dropped by the landed_at check below.
        f"--since={since.astimezone(timezone.utc).isoformat()}",
        "--pretty=format:\x1e%H\x1f%ct\x1f%at\x1f%ae\x1f%s",
    ])
    if not result or result.returncode != 0:
        # `git log` also fails on a valid repo whose HEAD is unborn -- a fresh
        # `git init` with nothing committed yet. That repo genuinely has no
        # commits, so its spend really is unbanked; only an unreadable repo is
        # unresolved. One extra call, and only ever on the failure path.
        probe = _git(repo, ["rev-parse", "--git-dir"])
        if probe and probe.returncode == 0:
            return []
        return None

    changes: list[Change] = []
    for record in result.stdout.split("\x1e"):
        if not record.strip():
            continue
        header, _, body = record.partition("\n")
        parts = header.split("\x1f", 4)
        if len(parts) != 5:
            continue
        sha, stamp, author_stamp, author_email, subject = parts
        try:
            committed_at = datetime.fromtimestamp(int(stamp), timezone.utc)
        except (TypeError, ValueError):
            continue
        try:
            authored_at = datetime.fromtimestamp(int(author_stamp), timezone.utc)
        except (TypeError, ValueError):
            authored_at = None
        change = Change(
            sha=sha[:12],
            repo=repo,
            subject=subject.strip(),
            committed_at=committed_at,
            authored_at=authored_at,
            author_email=author_email.strip().lower(),
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

    changes.sort(key=lambda item: item.landed_at)
    return changes


# A repo's root commit cannot change, so this is cached for the life of the
# process. The global git identity is cached too -- it is one value shared by
# every repo, and looking it up once per path was most of the added git calls.
# Per-repo `user.email` is deliberately NOT cached: it is the value most likely
# to be edited while a dashboard is running.
_IDENTITY_CACHE: dict[str, str] = {}
_GLOBAL_EMAIL: list[str | None] = [None]


def repo_identity(repo: str) -> str:
    """A key that is the same for every clone of one repository.

    Repos were keyed by filesystem path, so a second checkout of the same
    project counted as a separate repo and every one of its commits appeared
    again as a distinct change -- 93 phantom rows out of 259 locally, all of
    them showing "no spend observed" because the first copy had already
    claimed the money.

    The root commit's sha is the identity: it is stable across clones, remotes
    and renames, and needs no network. A repo with several roots (history
    grafted from another project) sorts them so the key stays deterministic.
    Falls back to the path when git cannot answer, which keeps the old
    behaviour rather than merging two repos that might be unrelated.
    """
    if repo in _IDENTITY_CACHE:
        return _IDENTITY_CACHE[repo]
    result = _git(repo, ["rev-list", "--max-parents=0", "HEAD"])
    if not result or result.returncode != 0:
        # Not cached: an unreadable repo may just be an unmounted drive, and
        # pinning it to its path for the process lifetime would outlive that.
        return repo
    roots = sorted(line.strip() for line in result.stdout.split() if line.strip())
    identity = roots[0][:16] if roots else repo
    _IDENTITY_CACHE[repo] = identity
    return identity


def local_author_emails(repo: str) -> set[str]:
    """Emails that count as "work done on this machine" for this repo.

    Fetching brings teammates' commits into the local repo, and the ledger was
    counting them: 53 of 166 unattributed commits locally were authored by
    someone else. Worse, the attribution rule (spend before a commit belongs to
    it) meant a teammate's commit arriving just after a local session absorbed
    that session's spend -- 10 commits were showing another developer's dollars.

    Returns an empty set when git has no configured identity, and callers treat
    that as "cannot tell, count everything" rather than silently dropping every
    commit.
    """
    emails: set[str] = set()
    result = _git(repo, ["config", "user.email"])
    if result and result.returncode == 0 and result.stdout.strip():
        emails.add(result.stdout.strip().lower())
    if _GLOBAL_EMAIL[0] is None:
        result = _git(repo, ["config", "--global", "user.email"])
        _GLOBAL_EMAIL[0] = (
            result.stdout.strip().lower() if result and result.returncode == 0 else ""
        )
    if _GLOBAL_EMAIL[0]:
        emails.add(_GLOBAL_EMAIL[0])
    return emails


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


def repos_matching(events: Sequence[LocalEvent], needle: str) -> list[str]:
    """Repo roots with spend whose path contains `needle`, one entry per repository.

    `aiwatcher changes --repo` takes a substring because typing a full path is
    miserable, but `build_ledger(only_repo=...)` needs a path it can resolve to
    a repo identity. Resolving up front is what keeps a drill-down internally
    consistent: filtering the change rows after the ledger was built left the
    unbanked total and the foreign-commit count still describing every repo on
    the machine, so a scoped table sat under machine-wide totals.

    Clones of one repository collapse to a single entry -- two checkouts of the
    same project are one repo to the ledger, so matching both is not ambiguous.
    Only repos with costed events are considered, matching what `build_ledger`
    itself will walk: a repo with no spend in the window has no ledger row to
    scope to either way.
    """
    wanted = needle.lower()
    cache: dict[str, str | None] = {}
    by_identity: dict[str, str] = {}
    for event in events:
        if event.cost_usd <= 0:
            continue
        repo = _event_repo(event, cache)
        if not repo or wanted not in repo.lower():
            continue
        by_identity.setdefault(repo_identity(repo), repo)
    return sorted(by_identity.values())


def build_ledger(
    events: Sequence[LocalEvent],
    *,
    days: int = 7,
    max_lookback_hours: float = DEFAULT_MAX_LOOKBACK_HOURS,
    now: datetime | None = None,
    only_repo: str | None = None,
) -> Ledger:
    """Attribute AI spend to the commits it preceded.

    Each costed event is banked against the first commit at or after it in the
    same repo, provided that commit lands within `max_lookback_hours`. Events
    with no such commit -- because none followed, or the next one came too long
    afterwards -- are unbanked.

    Events with no cost are ignored entirely rather than counted as free work:
    they are metadata (titles, mode switches) and would inflate event counts
    without moving a dollar.

    Two things are deliberately NOT in scope. Clones of one repository are
    merged, so the same commit cannot appear twice under two paths. And
    commits authored by someone else are dropped, because they arrived by
    fetch: this machine can have no spend for them, and leaving them in let
    them absorb spend that belonged to the local commit behind them.

    `only_repo` restricts the whole ledger to one repository, matched by
    identity so any clone of it counts. The post-commit receipt uses this: it
    has one repo to report on, and walking git history for every other repo
    with spend would be work thrown away on a path that has to stay fast.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    wanted = repo_identity(only_repo) if only_repo else None
    since = now - timedelta(days=days)
    cutoff = timedelta(hours=max_lookback_hours)

    repo_cache: dict[str, str | None] = {}
    by_repo: dict[str, list[LocalEvent]] = defaultdict(list)
    unbanked_usd = 0.0
    unbanked_events = 0
    unbanked_by_reason: dict[str, float] = defaultdict(float)
    unbanked_by_repo: dict[str, float] = defaultdict(float)
    unresolved_usd = 0.0
    unresolved_events = 0
    unresolved_repos: list[str] = []

    for event in events:
        if event.cost_usd <= 0 or not event.timestamp:
            continue
        stamp = event.timestamp.astimezone(timezone.utc)
        if stamp < since:
            continue
        repo = _event_repo(event, repo_cache)
        if not repo:
            if wanted is not None:
                # Scoped to one repo: spend that belongs to no repo is somebody
                # else's problem, not this receipt's.
                continue
            # Spend outside any git repo can never be banked against a change.
            unbanked_usd += event.cost_usd
            unbanked_events += 1
            unbanked_by_reason[UNBANKED_OUTSIDE_REPO] += event.cost_usd
            continue
        by_repo[repo].append(event)

    # Identity is resolved per repo, never per event. Filtering inside the loop
    # above cost a subprocess for every event whose repo could not be resolved
    # -- 10,698 calls and 2.2s on a month of local history.
    if wanted is not None:
        by_repo = defaultdict(list, {
            repo: repo_events for repo, repo_events in by_repo.items()
            if repo_identity(repo) == wanted
        })

    # Collapse clones of one repository into a single unit of attribution. Both
    # paths are still scanned for commits, because two checkouts can sit on
    # different branches and scanning only one would lose the other's work; the
    # commits they share are deduplicated by sha afterwards.
    clones: dict[str, list[str]] = defaultdict(list)
    for repo in by_repo:
        clones[repo_identity(repo)].append(repo)

    all_changes: list[Change] = []
    foreign_changes = 0
    for paths in clones.values():
        paths.sort()
        primary = paths[0]
        repo_events = [event for path in paths for event in by_repo[path]]

        # Look back a little before the window so an event early in it can bank
        # against a commit that the window itself would have excluded.
        gathered: dict[str, Change] = {}
        readable = False
        for path in paths:
            found = commits_since(path, since - cutoff)
            if found is None:
                continue
            readable = True
            for change in found:
                gathered.setdefault(change.sha, change)

        if not readable:
            # git could not answer. Say so rather than calling it waste.
            unresolved_usd += sum(event.cost_usd for event in repo_events)
            unresolved_events += len(repo_events)
            unresolved_repos.append(primary)
            continue

        # Commits that arrived by fetch were made on another machine, so no
        # local spend can belong to them. With no configured identity there is
        # nothing to compare against, and dropping everything would be worse
        # than counting everything.
        mine: set[str] = set()
        for path in paths:
            mine |= local_author_emails(path)
        if mine:
            before = len(gathered)
            gathered = {
                sha: change for sha, change in gathered.items()
                if not change.author_email or change.author_email in mine
            }
            foreign_changes += before - len(gathered)

        changes = sorted(gathered.values(), key=lambda item: item.landed_at)
        repo = primary
        if not changes:
            spend = sum(event.cost_usd for event in repo_events)
            unbanked_usd += spend
            unbanked_events += len(repo_events)
            unbanked_by_reason[UNBANKED_NO_COMMIT] += spend
            unbanked_by_repo[repo] += spend
            continue

        seen_sessions: dict[str, set[str]] = defaultdict(set)
        seen_tools: dict[str, set[str]] = defaultdict(set)
        seen_models: dict[str, set[str]] = defaultdict(set)

        for event in repo_events:
            stamp = event.timestamp.astimezone(timezone.utc)
            target = _first_commit_at_or_after(changes, stamp)
            if target is None or target.landed_at - stamp > cutoff:
                unbanked_usd += event.cost_usd
                unbanked_events += 1
                unbanked_by_reason[UNBANKED_NO_COMMIT] += event.cost_usd
                unbanked_by_repo[repo] += event.cost_usd
                continue
            target.cost_usd += event.cost_usd
            target.event_count += 1
            seen_sessions[target.sha].add(event.session_id)
            if event.tool:
                seen_tools[target.sha].add(event.tool)
            if event.model:
                seen_models[target.sha].add(event.model)

        for change in changes:
            if change.landed_at < since:
                # Only in range to catch spillover; not part of this window.
                # Keyed off landed_at so a commit and the spend that produced
                # it fall inside or outside the window together -- a rebase
                # must not drag old work into a recent window.
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
        unbanked_by_reason=dict(unbanked_by_reason),
        unbanked_by_repo=dict(unbanked_by_repo),
        unresolved_usd=unresolved_usd,
        unresolved_events=unresolved_events,
        unresolved_repos=sorted(unresolved_repos),
        foreign_changes=foreign_changes,
        repos=sorted(by_repo),
        window_days=days,
        max_lookback_hours=max_lookback_hours,
    )


# Under this there is no story to tell -- a window with $0.40 unbanked is
# rounding, not waste, and a card claiming otherwise trains people to ignore it.
MIN_UNBANKED_USD = 1.0


def unbanked_summary(ledger: Ledger, *, top_repos: int = 3) -> dict[str, Any]:
    """Spend in the window that never reached a commit.

    The most direct measure of waste the product has: it needs no outcome
    inference, no survival pass and no matching heuristic -- just the absence of
    a commit behind the money. It is not all waste, though, and the wording must
    not pretend otherwise: work still uncommitted on disk looks identical to
    exploration that went nowhere until it lands.
    """
    if ledger.classified_usd <= 0:
        return _unbanked_unavailable("No costed AI spend in this window to attribute.", ledger)
    if ledger.unbanked_usd < MIN_UNBANKED_USD:
        return _unbanked_unavailable(
            f"Under ${MIN_UNBANKED_USD:.0f} of spend went unbanked in this window.", ledger
        )

    repos = sorted(ledger.unbanked_by_repo.items(), key=lambda item: item[1], reverse=True)
    outside = ledger.unbanked_by_reason.get(UNBANKED_OUTSIDE_REPO, 0.0)
    no_commit = ledger.unbanked_by_reason.get(UNBANKED_NO_COMMIT, 0.0)
    return {
        "available": True,
        "reason": None,
        "unbanked_usd": round(ledger.unbanked_usd, 6),
        "banked_usd": round(ledger.banked_usd, 6),
        "classified_usd": round(ledger.classified_usd, 6),
        "unbanked_pct": round(ledger.unbanked_pct, 1),
        "unbanked_events": ledger.unbanked_events,
        "changes": len(ledger.changes),
        "outside_repo_usd": round(outside, 6),
        "no_commit_usd": round(no_commit, 6),
        "top_repos": [
            {"repo": repo, "unbanked_usd": round(spend, 6)}
            for repo, spend in repos[:top_repos]
        ],
        "unresolved_usd": round(ledger.unresolved_usd, 6),
        "unresolved_repos": ledger.unresolved_repos,
        "window_days": ledger.window_days,
        "max_lookback_hours": ledger.max_lookback_hours,
    }


def _unbanked_unavailable(reason: str, ledger: Ledger) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "unbanked_usd": round(ledger.unbanked_usd, 6),
        "banked_usd": round(ledger.banked_usd, 6),
        "unresolved_usd": round(ledger.unresolved_usd, 6),
        "window_days": ledger.window_days,
    }


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
    #
    # This deliberately uses committed_at where attribution uses landed_at (the
    # author date). The two questions are different: attribution asks when the
    # work was done, survival asks how long the lines have been exposed to
    # being rewritten, and a commit rebased onto this branch yesterday has been
    # exposed for a day no matter how long ago it was authored. survival.py's
    # own age check reads %ct for the same reason.
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
    # Keyed by sha so a request-time ledger over a different window can join
    # against it. The blame pass runs here either way; discarding the per-change
    # results only meant the change table had to say "unknown" for work this
    # function had already measured.
    by_change: dict[str, dict[str, Any]] = {}

    for change in capped:
        result = measure_change_survival(
            change.repo, change.sha, min_age_days=min_age_days, now=now
        )
        if not result.measurable:
            unmeasurable += 1
            by_change[change.sha] = {"measurable": False, "reason": result.reason}
            continue
        measured += 1
        cost += change.cost_usd
        touched += result.lines_touched
        intact += result.lines_intact
        by_change[change.sha] = {
            "measurable": True,
            "reason": None,
            "lines_touched": result.lines_touched,
            "lines_intact": result.lines_intact,
            "survival_pct": (
                round(100.0 * result.lines_intact / result.lines_touched, 1)
                if result.lines_touched else None
            ),
            "usd_per_surviving_line": (
                round(change.cost_usd / result.lines_intact, 6) if result.lines_intact else None
            ),
        }

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
            "by_change": by_change,
        }

    survival_pct = 100.0 * intact / touched if touched else None
    return {
        "available": True,
        "reason": None,
        "changes_measured": measured,
        "changes_too_recent": too_recent,
        "changes_unmeasurable": unmeasurable,
        "by_change": by_change,
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
    """Earliest commit not before `stamp`, by the time the work was committed.

    Linear rather than bisecting: the commit list for a window is small, and a
    plain scan keeps the "which change did this turn feed into" rule obvious.
    """
    for change in changes:
        if change.landed_at >= stamp:
            return change
    return None


# A median needs enough points to be a median. Below this it is one commit
# divided by another, which is the failure MIN_SESSIONS_PER_MODEL exists to
# prevent one module over.
MIN_CHECKPOINT_BASELINE = 4


def checkpoint_distance(
    ledger: Ledger,
    events: Sequence[LocalEvent],
    repo: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """How far this repo is from its last commit, in time and in money.

    The honest present-tense question. "Is this spend wasted" cannot be answered
    while it is happening -- build_ledger banks an event against the *first
    commit at or after it*, so everything from the last max_lookback_hours is
    provisionally unbanked and may become banked the moment you commit. A live
    "unbanked" figure would therefore fire on every developer every afternoon
    for the ordinary state of having written code you have not landed yet, and
    a signal that fires for everyone sorts nothing.

    "How far am I from a checkpoint" is answerable right now and carries no
    verdict: it is a distance, not an accusation. The action is obvious and the
    reader supplies their own judgment about whether the distance is fine.

    The threshold is the developer's own median spend between commits, because
    provider quota is not visible locally and a fixed dollar figure would be a
    number someone picked. Each change banks the spend since the one before it,
    so the per-change costs already *are* the between-commit distribution --
    no separate history to keep. Median rather than mean: one 300-dollar
    afternoon should not move the bar the other days are judged against.
    """
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    wanted = repo_identity(repo)

    mine = [c for c in ledger.changes if repo_identity(c.repo) == wanted]
    if not mine:
        return {
            "available": False,
            "reason": "No commits from you in this repo in the window, so there is no checkpoint to measure from.",
        }

    last = max(mine, key=lambda c: c.landed_at)
    # _event_repo, not the event's raw path: it resolves to the repo root and
    # caches, which is what makes a clone of the same repository count as the
    # same repository here and in build_ledger.
    cache: dict[str, str | None] = {}
    since = []
    for event in events:
        if event.cost_usd <= 0 or not event.timestamp:
            continue
        if event.timestamp.astimezone(timezone.utc) <= last.landed_at:
            continue
        root = _event_repo(event, cache)
        if root and repo_identity(root) == wanted:
            since.append(event)
    spend = round(sum(event.cost_usd for event in since), 6)
    elapsed = stamp - last.landed_at

    card: dict[str, Any] = {
        "available": True,
        "reason": None,
        "repo": repo,
        "last_commit_sha": last.sha[:12],
        "last_commit_subject": last.subject,
        "hours_since": round(elapsed.total_seconds() / 3600.0, 2),
        "spend_usd": spend,
        "events_since": len(since),
    }

    # Compare to the owner, not to a round number.
    costs = sorted(c.cost_usd for c in ledger.changes if c.cost_usd > 0)
    if len(costs) < MIN_CHECKPOINT_BASELINE:
        card["baseline"] = {
            "available": False,
            "reason": (
                f"Need {MIN_CHECKPOINT_BASELINE}+ costed commits to know what your usual "
                f"distance between checkpoints is; there are {len(costs)}."
            ),
            "changes": len(costs),
        }
        return card

    middle = len(costs) // 2
    median = costs[middle] if len(costs) % 2 else (costs[middle - 1] + costs[middle]) / 2.0
    card["baseline"] = {
        "available": median > 0,
        "reason": None if median > 0 else "Median spend between commits is zero.",
        "median_usd": round(median, 6),
        "changes": len(costs),
        "ratio": round(spend / median, 2) if median > 0 else None,
    }
    return card
