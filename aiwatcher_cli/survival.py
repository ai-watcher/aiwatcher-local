"""Line-level survival: how much of a change is still in the code.

The stored survival signal asks `git merge-base --is-ancestor <sha> HEAD` --
whether a commit is still reachable. That only reports churn when a commit is
rebased, reset or amended away, which almost never happens on a squash-and-merge
workflow. The two normal ways work stops being used both leave the commit
exactly where it was: `git revert` adds a *new* commit, and deleting the files a
commit created changes nothing about the commit itself. Both score "survived".

On this repo's own history reachability reported 16 of 16 sessions survived,
while a blame-based sample of the same work found roughly a third of the added
lines still present. That gap is the difference between "cost per success" being
a real number and being cost-per-session with extra steps.

This module measures the change instead of the commit, and does it
symmetrically:

  * an added line survives if HEAD still attributes it to that commit
  * a removed line survives if it has *not* come back

The second half matters more than it looks. A change whose whole job was
deleting dead code adds nothing, so an additions-only measure would score it 0%
-- exactly backwards. Measuring both directions means a cleanup that stuck reads
as a success, which it was.

Deliberately honest about its own limits: reformatting reattributes lines to the
formatting commit, so survival is a floor rather than an exact figure, and a
change with nothing measurable reports `measurable: False` rather than 0%.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

GIT_TIMEOUT_SECONDS = 30

# Blame is the expensive call -- one subprocess per file. These caps keep a
# single change bounded; a commit that trips them is reported as partially
# measured rather than silently truncated.
MAX_FILES_PER_CHANGE = 40
MAX_LINES_PER_FILE = 20_000

# Survival decays with age, steeply. Measured on this repo: 95% of lines from
# the last 3 days are still standing, 83% at 3-14 days, 67% at 14-45 days.
# Measuring a fresh commit therefore reports how *recent* it is, not whether it
# stuck -- so a change younger than this is reported as not-yet-judgeable
# rather than as a near-perfect score. Matches the day buckets the existing
# reachability recheck already uses.
MIN_AGE_DAYS = 7


@dataclass
class ChangeSurvival:
    sha: str
    repo: str
    lines_added: int = 0
    lines_surviving: int = 0
    lines_removed: int = 0
    removals_holding: int = 0
    files_measured: int = 0
    files_skipped: int = 0
    age_days: float | None = None
    measurable: bool = False
    reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def lines_touched(self) -> int:
        return self.lines_added + self.lines_removed

    @property
    def lines_intact(self) -> int:
        return self.lines_surviving + self.removals_holding

    @property
    def survival_pct(self) -> float | None:
        if not self.measurable or self.lines_touched == 0:
            return None
        return 100.0 * self.lines_intact / self.lines_touched

    def to_json(self) -> dict[str, Any]:
        pct = self.survival_pct
        return {
            "sha": self.sha,
            "repo": self.repo,
            "lines_added": self.lines_added,
            "lines_surviving": self.lines_surviving,
            "lines_removed": self.lines_removed,
            "removals_holding": self.removals_holding,
            "lines_touched": self.lines_touched,
            "lines_intact": self.lines_intact,
            "survival_pct": round(pct, 1) if pct is not None else None,
            "files_measured": self.files_measured,
            "files_skipped": self.files_skipped,
            "age_days": round(self.age_days, 1) if self.age_days is not None else None,
            "measurable": self.measurable,
            "reason": self.reason,
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
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _commit_age_days(repo: str, sha: str, *, now: datetime | None = None) -> float | None:
    result = _git(repo, ["show", "-s", "--format=%ct", sha])
    if not result or result.returncode != 0:
        return None
    try:
        committed_at = datetime.fromtimestamp(int(result.stdout.strip()), timezone.utc)
    except (TypeError, ValueError):
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return max(0.0, (now - committed_at).total_seconds() / 86400)


def _numstat(repo: str, sha: str) -> list[tuple[int, int, str]]:
    """(added, removed, path) per file. Binary files report "-" and are skipped."""
    result = _git(repo, ["show", "--numstat", "--format=", sha])
    if not result or result.returncode != 0:
        return []
    rows: list[tuple[int, int, str]] = []
    for line in result.stdout.splitlines():
        cells = line.split("\t")
        if len(cells) != 3:
            continue
        added, removed, path = cells
        if not (added.isdigit() and removed.isdigit()):
            continue
        rows.append((int(added), int(removed), path.strip()))
    return rows


def _surviving_added_lines(repo: str, sha: str, path: str) -> int | None:
    """Lines at HEAD still attributed to `sha`.

    `-w` ignores whitespace-only changes and `-C` follows code moved between
    files, which together absorb most reformatting. They do not absorb all of
    it, which is why the result is documented as a floor.
    """
    result = _git(repo, ["blame", "-l", "-w", "-C", "HEAD", "--", path])
    if not result or result.returncode != 0:
        return None
    prefix = sha[:12]
    alive = 0
    for line in result.stdout.splitlines():
        # A boundary commit -- one at the edge of the examined range, which
        # includes a repo's first commit -- is printed as "^<sha>", shifting
        # every following character by one. Without stripping it, exactly the
        # oldest work reads as 0% survived.
        if line.startswith("^"):
            line = line[1:]
        if line[:12] == prefix:
            alive += 1
    return alive


def _removed_lines(repo: str, sha: str, path: str) -> list[str]:
    result = _git(repo, ["show", "--format=", "--unified=0", sha, "--", path])
    if not result or result.returncode != 0:
        return []
    removed: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("---") or not line.startswith("-"):
            continue
        text = line[1:].strip()
        # Blank lines carry no signal: one always "exists" at HEAD somewhere.
        if text:
            removed.append(text)
    return removed


def _head_lines(repo: str, path: str) -> set[str] | None:
    result = _git(repo, ["show", f"HEAD:{path}"])
    if not result or result.returncode != 0:
        return None  # file no longer exists at HEAD
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def measure_change_survival(
    repo: str,
    sha: str,
    *,
    max_files: int = MAX_FILES_PER_CHANGE,
    max_lines_per_file: int = MAX_LINES_PER_FILE,
    min_age_days: float = MIN_AGE_DAYS,
    now: datetime | None = None,
) -> ChangeSurvival:
    """How much of one commit's work is still standing at HEAD.

    Reports a floor, not an exact figure. Blame credits a line to whichever
    commit last touched it, so a later reformat, rename or refactor moves
    attribution away even when the work itself is intact. Under-reporting
    survival is the safer direction for a waste metric, but it means a low score
    is a prompt to look, not a verdict.
    """
    survival = ChangeSurvival(sha=sha[:12], repo=repo)
    survival.age_days = _commit_age_days(repo, sha, now=now)
    if survival.age_days is not None and survival.age_days < min_age_days:
        survival.reason = (
            f"Too recent to judge: {survival.age_days:.1f} days old, needs {min_age_days:.0f}. "
            "Almost nothing has been rewritten yet, so a score now would just say it is new."
        )
        return survival
    files = _numstat(repo, sha)
    if not files:
        survival.reason = "No text changes to measure (empty, binary-only, or unreadable commit)."
        return survival

    if len(files) > max_files:
        survival.notes.append(
            f"Measured the {max_files} largest of {len(files)} files; the rest were skipped."
        )
        files = sorted(files, key=lambda row: row[0] + row[1], reverse=True)[:max_files]

    for added, removed, path in files:
        if added + removed > max_lines_per_file:
            survival.files_skipped += 1
            continue

        head_lines = _head_lines(repo, path)
        measured_file = False

        if added:
            survival.lines_added += added
            if head_lines is None:
                # The file is gone from HEAD, so none of its added lines survive.
                measured_file = True
            else:
                alive = _surviving_added_lines(repo, sha, path)
                if alive is None:
                    survival.lines_added -= added
                else:
                    # Blame can attribute more lines to a commit than it added
                    # (a `-C` move credits copied code), so cap at what it added.
                    survival.lines_surviving += min(alive, added)
                    measured_file = True

        if removed:
            survival.lines_removed += removed
            if head_lines is None:
                # File deleted outright: everything it removed stayed removed.
                survival.removals_holding += removed
                measured_file = True
            else:
                gone = [line for line in _removed_lines(repo, sha, path) if line not in head_lines]
                survival.removals_holding += min(len(gone), removed)
                measured_file = True

        survival.files_measured += int(measured_file)
        survival.files_skipped += int(not measured_file)

    if survival.lines_touched == 0:
        survival.reason = "Nothing measurable: every file was skipped or unreadable."
        return survival

    survival.measurable = True
    return survival
