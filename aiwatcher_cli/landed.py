"""Work that is on a branch but not in main.

The expensive failure this looks for is a change that everything *says* shipped
and that is not in the code. It happens on stacked branches: PR B targets branch
A, A merges to main, and only then does B merge into A. GitHub records B as
merged -- purple badge, branch deleted, PR closed -- and B's commits sit on a
branch nobody will merge again. Every signal reads "done" and the code is not
there.

Measured on this repository: of 186 merged pull requests, 17 targeted a branch
other than main, and 13 of those targeted a branch whose own merge to main had
already happened or never came. Most survived anyway, because the parent branch
was squash-merged and carried the content along. One did not. PR #63 (August)
added a fixture that stopped the test suite writing into the developer's real
~/.aiwatcher state; none of it reached main, nobody noticed, and the same job was
done again from scratch in November as PR #207. The cost of not detecting this
is paying twice for the same work, months apart.

Four stages, cheapest first, because the honest answer needs all of them and the
first three are wrong on their own:

  1. Ancestry (`git branch --no-merged`). Removes every branch whose commits are
     literally reachable from main. Cheap and exact, but it clears nothing that
     was squashed, rebased or cherry-picked, so on this repo it only narrows 181
     remote branches to 60.

  2. Patch identity (`git cherry`). Asks whether main holds a commit with the
     same change, which is how a cherry-pick or a rebase is recognised. This is
     what correctly clears PR #211, whose commit reached main under a different
     SHA. It is not sufficient either: a squash merge combines several commits
     into one new change, so every squashed commit still looks absent. On this
     repo it reports PRs #175-#178 as missing when their content is in fact in
     main.

  3. Line presence. For the commits stage 2 still suspects, read main's current
     files and count how many of the commit's added lines are actually there.
     This is the stage that separates the two cases, and the separation measured
     here is wide: work that landed via squash reads 88-100% present, work that
     genuinely never landed reads 0-21%. PRESENT_PCT_THRESHOLD sits in the gap.

  4. Move probe. Stage 3 reads each file at the path the commit touched, so a
     function that moved to another file reads as entirely absent. Before a
     change is reported, a sample of its absent lines is looked for anywhere in
     main. PR #185's one-line Windows hook fix needed this: the line is on main,
     in delivery_tracking.py, after the function left cli.py. Only changes that
     would actually be shown are probed, so the tally of commits no branch
     merged is not move-checked -- those are counted, never listed.

What this cannot do, stated plainly because the surface above it must not
overclaim:

  * It separates stranded work from an abandoned experiment only in one
    direction. When another branch still contains the commit, that branch merged
    the work and main never received it: the stranded shape, and the reason
    findings are ranked by that before size. When no branch contains it, the
    commit may have been dropped on purpose or may have been orphaned by a
    branch that was rebased after absorbing the merge -- PRs #185 and #192 here
    are the second kind, and look identical to the first. Saying which would mean
    knowing whether a pull request merged, which lives on GitHub, and this tool
    does not talk to GitHub. So a finding is reported as worth checking and never
    as proven loss.
  * Line presence is a floor, not a measurement. A later rename or reformat of a
    line that did land reads as absent. It fails towards "look at this", which
    is the safer direction for a warning, but it means a low score is a reason
    to look and never a verdict.
  * A branch deleted both locally and on the remote is invisible to git, and
    this finds nothing. It works today because this workflow keeps branches.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

GIT_TIMEOUT_SECONDS = 30

# A full stage-1 sweep of this repo's 60 unmerged branches takes about 4.7s, so
# the scan is bounded and belongs behind the Optimize packet's existing cache
# rather than on a page render.
MAX_BRANCHES = 80
MAX_COMMITS_PER_BRANCH = 40
MAX_FILES_PER_COMMIT = 25
MAX_LINES_PER_FILE = 4_000

# Below this share of its added lines present in main, a change is worth
# surfacing. Measured on this repo: squash-landed work reads 88-100% present,
# work that never landed reads 0-21%. The threshold sits in the empty middle, so
# both sides clear it by a wide margin rather than by a tuned hair.
PRESENT_PCT_THRESHOLD = 50.0

# Short lines ("return None", "else:") recur everywhere, so finding one in main
# proves nothing about this commit. Only lines longer than this are counted.
MIN_SIGNIFICANT_LINE_LENGTH = 12

# Code that moved to another file reads as absent, because presence is measured
# against the path the commit touched. Before reporting a change as missing, a
# sample of its absent lines is looked for anywhere in main. A sample is enough
# to tell "this moved" from "this is gone" and the cost is bounded: git grep
# answers one fixed string in about 0.07s but takes twelve seconds for a hundred
# of them at once, so these run as separate single-pattern searches.
MOVE_PROBE_LINES = 12
MOVE_PROBE_SHARE = 0.5

# Unmerged commits on a branch somebody is still working on are not lost work,
# they are work in progress. A branch is only judged once it has gone quiet for
# this long, which also matches the age floor survival.py uses before it is
# willing to score a change at all.
MIN_BRANCH_IDLE_DAYS = 14

# Paths whose absence from main says nothing useful about lost work.
SKIP_PATH_SUFFIXES = (".lock", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".pyc")


@dataclass
class UnlandedChange:
    """One commit whose content main does not appear to have."""

    sha: str
    branch: str
    subject: str = ""
    committed_at: datetime | None = None
    lines_added: int = 0
    lines_present: int = 0
    files_measured: int = 0
    files_skipped: int = 0
    measurable: bool = False
    reason: str | None = None
    merged_into: tuple[str, ...] = ()
    moved_in_main: bool = False
    probes: int = 0
    probe_hits: int = 0
    # Kept only so a move probe can run later without re-reading every file,
    # and capped because the report is cached: the probe reads the longest few.
    absent_lines: tuple[str, ...] = field(default=(), repr=False)

    @property
    def merged_elsewhere(self) -> bool:
        """Some other branch took this commit in, and main still does not have it.

        This is the stranded shape rather than the abandoned one, and it is the
        difference between work somebody shipped and work somebody dropped. It
        is reliable when true and says nothing when false: if the branch that
        absorbed the merge was later rebased, the containing ref is gone and an
        orphaned commit looks exactly like one that was never merged at all.
        """
        return bool(self.merged_into)

    @property
    def present_pct(self) -> float | None:
        if not self.measurable or self.lines_added == 0:
            return None
        return 100.0 * self.lines_present / self.lines_added

    @property
    def lines_absent(self) -> int:
        return max(0, self.lines_added - self.lines_present)

    @property
    def looks_unlanded(self) -> bool:
        if self.moved_in_main:
            return False
        pct = self.present_pct
        return pct is not None and pct < PRESENT_PCT_THRESHOLD

    def to_json(self) -> dict[str, Any]:
        pct = self.present_pct
        return {
            "sha": self.sha,
            "branch": self.branch,
            "subject": self.subject,
            "committed_at": self.committed_at.isoformat() if self.committed_at else None,
            "lines_added": self.lines_added,
            "lines_present": self.lines_present,
            "lines_absent": self.lines_absent,
            "present_pct": round(pct, 1) if pct is not None else None,
            "files_measured": self.files_measured,
            "files_skipped": self.files_skipped,
            "measurable": self.measurable,
            "looks_unlanded": self.looks_unlanded,
            "merged_into": list(self.merged_into),
            "merged_elsewhere": self.merged_elsewhere,
            "moved_in_main": self.moved_in_main,
            "probes": self.probes,
            "probe_hits": self.probe_hits,
            "confidence": "merged elsewhere, not in main" if self.merged_elsewhere else "never merged anywhere",
            "reason": self.reason,
        }


@dataclass
class LandingReport:
    """What a scan of one repository found."""

    repo: str
    main_ref: str = ""
    branches_scanned: int = 0
    branches_skipped_active: int = 0
    branches_omitted: int = 0
    commits_examined: int = 0
    changes: list[UnlandedChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def unlanded(self) -> list[UnlandedChange]:
        """The changes worth showing, likeliest loss first.

        Ordered by shape before size. A commit another branch merged and main
        never received is the expensive case whatever its size, so it outranks a
        larger commit on a branch nobody ever merged -- which is more often an
        experiment that was dropped on purpose.
        """
        found = [change for change in self.changes if change.looks_unlanded]
        return sorted(found, key=lambda change: (change.merged_elsewhere, change.lines_absent), reverse=True)

    @property
    def stranded(self) -> list[UnlandedChange]:
        """Changes another branch took in that main never received."""
        return [change for change in self.unlanded if change.merged_elsewhere]

    @property
    def unmerged_anywhere(self) -> list[UnlandedChange]:
        """Changes no branch ever merged: usually abandoned, occasionally orphaned."""
        return [change for change in self.unlanded if not change.merged_elsewhere]

    @property
    def lines_at_stake(self) -> int:
        return sum(change.lines_absent for change in self.unlanded)

    def to_json(self) -> dict[str, Any]:
        found = self.unlanded
        return {
            "repo": self.repo,
            "main_ref": self.main_ref,
            "branches_scanned": self.branches_scanned,
            "branches_skipped_active": self.branches_skipped_active,
            "branches_omitted": self.branches_omitted,
            "commits_examined": self.commits_examined,
            "unlanded_count": len(found),
            "stranded_count": len(self.stranded),
            "unmerged_anywhere_count": len(self.unmerged_anywhere),
            "lines_at_stake": self.lines_at_stake,
            "changes": [change.to_json() for change in found],
            "evidence": (
                "Branch commits with no patch-equivalent on main and most added lines "
                "absent from main's current files. A commit another branch merged is the "
                "stranded case; one no branch merged is more often abandoned on purpose. "
                "Verify before acting."
            ),
            "notes": self.notes,
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
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_lines(repo: str, args: list[str]) -> list[str]:
    result = _git(repo, args)
    if not result or result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def resolve_main_ref(repo: str, preferred: str | None = None) -> str | None:
    """The ref standing for "shipped", preferring the remote over a stale local main."""
    candidates = [preferred] if preferred else []
    candidates += ["origin/main", "origin/master", "main", "master"]
    for candidate in candidates:
        if not candidate:
            continue
        result = _git(repo, ["rev-parse", "--verify", "-q", candidate])
        if result and result.returncode == 0:
            return candidate
    return None


def candidate_branches(repo: str, main_ref: str) -> list[str]:
    """Branches holding commits main cannot reach, remote first and newest first.

    Stage 1. `--no-merged` is pure ancestry, so this keeps every branch that was
    squashed or cherry-picked as well as every branch genuinely left behind. The
    later stages are what tell those apart.
    """
    seen: dict[str, tuple[str, float]] = {}
    for scope in (["branch", "-r"], ["branch"]):
        rows = _git_lines(repo, [*scope, "--no-merged", main_ref,
                                 "--format=%(refname:short)%09%(committerdate:unix)"])
        for row in rows:
            name, _, stamp = row.partition("\t")
            name = name.strip()
            if not name or "HEAD" in name or name == main_ref:
                continue
            try:
                when = float(stamp.strip())
            except ValueError:
                when = 0.0
            # A branch and its remote copy hold the same work; keep one.
            key = name.split("/", 1)[1] if name.startswith("origin/") else name
            if key not in seen or when > seen[key][1]:
                seen[key] = (name, when)
    ordered = sorted(seen.values(), key=lambda pair: pair[1], reverse=True)
    return [name for name, _ in ordered]


def _branch_idle_days(repo: str, ref: str, *, now: datetime | None = None) -> float | None:
    result = _git(repo, ["log", "-1", "--format=%ct", ref])
    if not result or result.returncode != 0:
        return None
    try:
        tip = datetime.fromtimestamp(int(result.stdout.strip()), timezone.utc)
    except (TypeError, ValueError):
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return max(0.0, (now - tip).total_seconds() / 86400)


def unmatched_commits(repo: str, ref: str, main_ref: str) -> list[str]:
    """Commits on `ref` with no patch-equivalent on main, oldest first.

    Stage 2. `git cherry` marks a commit `-` when main holds the same change
    under any SHA, which is how a cherry-pick or rebase is cleared. A squash
    merge rewrites several commits into one new change, so squashed work still
    comes back marked `+` and has to be settled by reading the files.
    """
    shas: list[str] = []
    for line in _git_lines(repo, ["cherry", main_ref, ref]):
        mark, _, sha = line.partition(" ")
        if mark == "+" and sha.strip():
            shas.append(sha.strip())
    return shas


def refs_containing(repo: str, sha: str, *, main_ref: str, branch: str) -> tuple[str, ...]:
    """Branches other than main and `branch` that hold this commit.

    Only worth asking once a commit already looks absent from main, since it
    costs a subprocess per commit. A hit means another branch merged the work and
    main still never got it, which is the shape that cost real time here.
    """
    rows = _git_lines(repo, ["branch", "-a", "--contains", sha, "--format=%(refname:short)"])
    own = {branch, f"origin/{branch}", branch.removeprefix("origin/")}
    found: list[str] = []
    for name in rows:
        name = name.strip().removeprefix("* ").strip()
        if not name or "HEAD" in name or name in own or name == main_ref:
            continue
        if name.removeprefix("origin/") in {value.removeprefix("origin/") for value in own}:
            continue
        found.append(name)
    return tuple(sorted(set(found)))


def looks_moved(repo: str, absent: Sequence[str], *, main_ref: str,
                probe_lines: int = MOVE_PROBE_LINES) -> tuple[bool, int, int]:
    """Whether main holds these lines somewhere other than where they were added.

    Presence is measured against the path a commit touched, so a function that
    moved to another file reads as entirely absent. Here that was PR #185's
    one-line Windows hook fix: the line is on main, in delivery_tracking.py,
    after the function left cli.py.

    Returns (moved, hits, probed). The longest absent lines are probed first
    because a long line is the least likely to appear in main by coincidence,
    and only a sample is probed: deciding "moved" against "gone" does not need
    every line, and git grep costs far more for many patterns at once than for
    the same patterns one at a time.
    """
    candidates = sorted({line for line in absent if len(line) > MIN_SIGNIFICANT_LINE_LENGTH},
                        key=len, reverse=True)[:probe_lines]
    if not candidates:
        return (False, 0, 0)
    total = len(candidates)
    needed = total * MOVE_PROBE_SHARE
    hits = 0
    probed = 0
    for line in candidates:
        result = _git(repo, ["grep", "-l", "-F", "-e", line, main_ref])
        probed += 1
        if result and result.returncode == 0 and result.stdout.strip():
            hits += 1
        # Stop as soon as the remaining probes cannot change the answer. A
        # change that really is gone misses every probe, which is the common
        # case and the one worth leaving early.
        if hits >= needed or hits + (total - probed) < needed:
            break
    return (hits >= needed, hits, probed)


def _added_lines(repo: str, sha: str) -> dict[str, list[str]]:
    """Significant lines each file gained in this commit."""
    result = _git(repo, ["show", "--format=", "-U0", "--no-color", sha])
    if not result or result.returncode != 0:
        return {}
    per_file: dict[str, list[str]] = {}
    current: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        if current is None or not line.startswith("+"):
            continue
        text = line[1:].strip()
        if len(text) <= MIN_SIGNIFICANT_LINE_LENGTH:
            continue
        per_file.setdefault(current, []).append(text)
    return per_file


def measure_unlanded_change(
    repo: str,
    sha: str,
    *,
    branch: str,
    main_ref: str,
    max_files: int = MAX_FILES_PER_COMMIT,
    max_lines_per_file: int = MAX_LINES_PER_FILE,
    probe_moves: bool = True,
) -> UnlandedChange:
    """How much of one commit's added work is readable in main today.

    Stage 3. Compares against main's current file contents rather than asking
    blame who owns a line: the question here is whether the content is in main at
    all, not which commit earned the credit for it. That also means this works
    against an explicit ref instead of whatever HEAD happens to be.
    """
    change = UnlandedChange(sha=sha[:12], branch=branch)
    details = _git(repo, ["show", "-s", "--format=%s%n%ct", sha])
    if details and details.returncode == 0:
        parts = details.stdout.splitlines()
        if parts:
            change.subject = parts[0].strip()
        if len(parts) > 1:
            try:
                change.committed_at = datetime.fromtimestamp(int(parts[1].strip()), timezone.utc)
            except (TypeError, ValueError):
                pass

    per_file = _added_lines(repo, sha)
    if not per_file:
        change.reason = "No significant added lines to measure."
        return change

    absent: list[str] = []
    for path, lines in sorted(per_file.items()):
        if change.files_measured >= max_files:
            change.files_skipped += 1
            continue
        if path.lower().endswith(SKIP_PATH_SUFFIXES):
            change.files_skipped += 1
            continue
        if len(lines) > max_lines_per_file:
            lines = lines[:max_lines_per_file]
            change.files_skipped += 1
        blob = _git(repo, ["show", f"{main_ref}:{path}"])
        # A file main never had contributes its lines as absent, which is the
        # whole point: a commit that created a file nobody merged scores zero.
        current = set()
        if blob and blob.returncode == 0:
            current = {text.strip() for text in blob.stdout.splitlines()}
        change.files_measured += 1
        change.lines_added += len(lines)
        change.lines_present += sum(1 for text in lines if text in current)
        absent.extend(text for text in lines if text not in current)

    change.measurable = change.lines_added > 0
    if not change.measurable:
        change.reason = "Nothing measurable after skipping binary and trivial lines."
        return change

    change.absent_lines = tuple(sorted(set(absent), key=len, reverse=True)[:MOVE_PROBE_LINES * 2])
    # Only worth asking once the same-path answer is "missing", and only then is
    # the cost paid: a change that is clearly present never probes at all.
    if probe_moves and change.looks_unlanded:
        apply_move_probe(repo, change, main_ref=main_ref)
    return change


def apply_move_probe(repo: str, change: UnlandedChange, *, main_ref: str) -> UnlandedChange:
    """Settle whether an apparently missing change simply moved, in place."""
    if not change.absent_lines:
        return change
    moved, hits, probed = looks_moved(repo, change.absent_lines, main_ref=main_ref)
    change.moved_in_main = moved
    change.probe_hits = hits
    change.probes = probed
    if moved:
        change.reason = (
            f"{hits} of {probed} sampled lines are on {main_ref} in another file; "
            "this change moved rather than went missing."
        )
    return change


def find_unlanded_work(
    repo: str,
    *,
    main_ref: str | None = None,
    max_branches: int = MAX_BRANCHES,
    max_commits_per_branch: int = MAX_COMMITS_PER_BRANCH,
    min_idle_days: float = MIN_BRANCH_IDLE_DAYS,
    now: datetime | None = None,
) -> LandingReport:
    """Find work sitting on a branch that main does not have.

    Runs the three stages in order and stops early wherever a branch can be
    cleared cheaply. Branches still being worked on are left alone: unmerged
    commits there are work in progress, not work that went missing.
    """
    resolved = resolve_main_ref(repo, main_ref)
    report = LandingReport(repo=repo, main_ref=resolved or "")
    if not resolved:
        report.notes.append("No main or master ref found; nothing to compare against.")
        return report

    branches = candidate_branches(repo, resolved)
    if len(branches) > max_branches:
        report.branches_omitted = len(branches) - max_branches
        report.notes.append(
            f"Scanned the {max_branches} most recent unmerged branches; "
            f"{report.branches_omitted} older ones were not examined."
        )
        branches = branches[:max_branches]

    seen_shas: set[str] = set()
    for branch in branches:
        idle = _branch_idle_days(repo, branch, now=now)
        if idle is not None and idle < min_idle_days:
            report.branches_skipped_active += 1
            continue
        report.branches_scanned += 1
        shas = unmatched_commits(repo, branch, resolved)
        if len(shas) > max_commits_per_branch:
            shas = shas[-max_commits_per_branch:]
            report.notes.append(f"{branch}: examined its {max_commits_per_branch} newest commits only.")
        for sha in shas:
            if sha in seen_shas:
                continue
            seen_shas.add(sha)
            report.commits_examined += 1
            # Measured without the move probe first: only a change that another
            # branch merged is ever shown, and probing the rest cost more than
            # the whole scan. The unshown tally is therefore not move-checked.
            change = measure_unlanded_change(repo, sha, branch=branch, main_ref=resolved,
                                             probe_moves=False)
            if change.looks_unlanded:
                change.merged_into = refs_containing(repo, sha, main_ref=resolved, branch=branch)
                if change.merged_elsewhere:
                    apply_move_probe(repo, change, main_ref=resolved)
            report.changes.append(change)

    if report.branches_skipped_active:
        report.notes.append(
            f"{report.branches_skipped_active} branch(es) active in the last "
            f"{int(min_idle_days)} days were left alone as work in progress."
        )
    return report
