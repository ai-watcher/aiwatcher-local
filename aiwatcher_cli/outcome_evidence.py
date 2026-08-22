"""Local outcome evidence for AIWatcher sessions.

This module keeps the local privacy boundary narrow and honest: it reads local
git/test signals around a session and turns them into personal evidence. It
does not upload source, prompt text, diffs, or team data.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .scanner import LocalSession


GIT_TIMEOUT_SECONDS = 2

# Repo root for a given path does not change while the process lives, but the
# companion re-derives it for every session on every scan tick -- which meant a
# git process spawned several times a second in a long-running daemon. scanner
# caches the same lookup for the same reason.
_REPO_ROOT_CACHE: dict[str, str | None] = {}

# A commit's file list cannot change once the commit exists, but the companion
# re-derives it for every session on every scan tick -- ten `git show` processes
# per tick, forever, all returning the same answer. Keyed by (repo, sha).
_COMMIT_FILES_CACHE: dict[tuple[str, str], list[str]] = {}
COMMIT_LOOKAHEAD_HOURS = 24
REPROMPT_WINDOW_HOURS = 72.0  # a later session touching the same file(s) within this window is a rework signal

VALID_EVIDENCE_OUTCOMES = {"useful", "needs_review", "churned"}  # OutcomeEvidence.inferred_outcome's non-None values


@dataclass
class OutcomeEvidence:
    session_id: str
    project_path: str | None
    repo_root: str | None = None
    commits: list[dict[str, Any]] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)  # files touched by this session's own commits
    tests: list[dict[str, Any]] = field(default_factory=list)
    inferred_outcome: str | None = None
    confidence: str = "low"
    reasons: list[str] = field(default_factory=list)
    same_file_reprompt: bool = False
    survival: dict[str, Any] | None = None  # {"7": "survived"|"churned"|"unknown", ...}, from stored history

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "project_path": self.project_path,
            "repo_root": self.repo_root,
            "commits": self.commits,
            "changed_files": self.changed_files,
            "files_touched": self.files_touched,
            "tests": self.tests,
            "inferred_outcome": self.inferred_outcome,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "same_file_reprompt": self.same_file_reprompt,
            "survival": self.survival,
        }


def _run_git(repo: str, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", repo, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _repo_root(path: str | None) -> str | None:
    if not path:
        return None
    if path in _REPO_ROOT_CACHE:
        return _REPO_ROOT_CACHE[path]
    candidate = Path(path).expanduser()
    if not candidate.exists():
        return None
    if candidate.is_file():
        candidate = candidate.parent
    result = _run_git(str(candidate), ["rev-parse", "--show-toplevel"])
    if not result or result.returncode != 0:
        # A timeout or a transient git failure is not cached: the next tick
        # should be free to ask again rather than mark the repo dead for the
        # life of the daemon.
        if result is None:
            return None
        _REPO_ROOT_CACHE[path] = None
        return None
    root = result.stdout.strip() or None
    _REPO_ROOT_CACHE[path] = root
    return root


def _session_window(session: LocalSession) -> tuple[datetime | None, datetime | None]:
    start = session.started_at or session.updated_at
    end = session.updated_at or session.started_at
    if start and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start, end


def _recent_commits(repo: str, session: LocalSession) -> list[dict[str, Any]]:
    """Commit subject/body are captured as real text, not hashed.

    Unlike prompts, a commit message is written by whoever made the change
    specifically to explain it to a future reader -- it is not private the
    way prompt text is, and it is the strongest local signal for "why" a
    change happened. The record separator (%x1e) is required because %b
    can itself contain newlines, which would otherwise break line-based
    parsing of `git log` output.
    """
    start, end = _session_window(session)
    if not start:
        return []
    until = (end or start) + timedelta(hours=COMMIT_LOOKAHEAD_HOURS)
    result = _run_git(
        repo,
        [
            "log",
            "--date=iso-strict",
            "--pretty=format:%H%x1f%ad%x1f%s%x1f%b%x1e",
            f"--since={start.isoformat()}",
            f"--until={until.isoformat()}",
            "--",
        ],
    )
    if not result or result.returncode != 0:
        return []
    commits: list[dict[str, Any]] = []
    for record in result.stdout.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f", 3)
        if len(parts) != 4:
            continue
        sha, stamp, subject, body = parts
        commits.append({
            "sha": sha[:12],
            "subject": subject.strip(),
            "body": body.strip(),
            "committed_at": stamp,
        })
    return commits[:10]


def _files_in_commit(repo: str, sha: str) -> list[str]:
    """Paths touched by one commit. Empty for a merge commit, which
    `git show --name-only` reports no files for."""
    key = (repo, sha)
    if key in _COMMIT_FILES_CACHE:
        return list(_COMMIT_FILES_CACHE[key])
    result = _run_git(repo, ["show", "--name-only", "--format=", sha])
    if not result or result.returncode != 0:
        # Not cached: a timeout or transient git failure should not pin an
        # empty file list to a commit for the life of the daemon.
        return []
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    _COMMIT_FILES_CACHE[key] = files
    return list(files)


def _files_touched(repo: str, commits: list[dict[str, Any]]) -> list[str]:
    """Union of file paths touched by this session's own commits (not uncommitted status).

    Used for the same-file re-prompt signal (S-24): whether a *later* session
    in the same project touches the same file(s) again soon after, which
    changed_files (current `git status`, always "now") can't answer since it
    reflects the live working tree, not what a past session actually changed.
    """
    touched: list[str] = []
    seen: set[str] = set()
    for commit in commits[:10]:
        sha = str(commit.get("sha") or "")
        if not sha:
            continue
        for path in _files_in_commit(repo, sha):
            if path and path not in seen:
                seen.add(path)
                touched.append(path)
            if len(touched) >= 50:
                return touched
    return touched


def repo_root_for_session(session: LocalSession) -> str | None:
    """Public wrapper so survival re-checks (cli.py) can re-derive a session's real repo path
    fresh from its live project_path, instead of ever needing to read it back from persisted
    (deliberately hashed) evidence_snapshot storage."""
    return _repo_root(session.project_path)


def check_commit_survival(repo: str, sha: str) -> str:
    """Is `sha` still reachable from the current branch? "survived" | "churned" | "unknown".

    Uses `merge-base --is-ancestor` (reachability from HEAD), not `cat-file -e`
    (object existence) -- a rebased-away or reset-away commit can still exist
    as a dangling object for a while before GC, which `cat-file -e` would
    wrongly call "survived". Reachability from HEAD is what "did this change
    stick" actually means.
    """
    result = _run_git(repo, ["merge-base", "--is-ancestor", sha, "HEAD"])
    if result is None:
        return "unknown"
    if result.returncode == 0:
        return "survived"
    if result.returncode == 1:
        return "churned"
    return "unknown"  # sha not found / repo error -- e.g. shallow clone, gc'd object


def _tracked_paths_at_head(repo: str) -> set[str] | None:
    """Every path tracked at HEAD, or None if the repo can't be read.

    One git call per repo so a sweep across many sessions doesn't shell out
    once per file.
    """
    result = _run_git(repo, ["ls-tree", "-r", "HEAD", "--name-only"])
    if not result or result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def find_reverts_of(repo: str, sha: str) -> list[str]:
    """SHAs of later commits reachable from HEAD whose message cites `sha`.

    `git revert` records "This reverts commit <sha>." in the body and leaves
    the original commit exactly where it was, so matching on the sha text is
    the only way to see it. `--fixed-strings` because --grep is a regex by
    default; a hex sha is harmless either way, but this stops a malformed
    stored value from being interpreted as a pattern.
    """
    result = _run_git(
        repo,
        ["log", f"{sha}..HEAD", "--fixed-strings", f"--grep={sha}", "--pretty=format:%H"],
    )
    if not result or result.returncode != 0:
        return []
    return [line.strip()[:12] for line in result.stdout.splitlines() if line.strip()]


def check_commit_undone(repo: str, sha: str, *, tracked_paths: set[str] | None = None) -> dict[str, Any]:
    """Explicit-undo signals for a commit that is still reachable from HEAD.

    check_commit_survival only reports "churned" once a commit becomes
    unreachable -- rebased, reset, or amended away. The two most common ways
    work actually stops being used leave the original commit untouched:
    `git revert` adds a *new* commit, and deleting the files a commit created
    changes nothing about the commit itself. Both score "survived" today.

    Deliberately reports signals rather than a verdict. A missing file may
    have been renamed rather than deleted, so `files_missing` is evidence, not
    proof, and only a full revert or a total wipe of the commit's files sets
    `undone`. Partial rewrites are what line-level survival is for; this is
    the cheap version that answers whether churn exists at all.

    `tracked_paths` lets a caller sweeping many sessions in one repo pay for
    the `git ls-tree` once instead of per commit.
    """
    reverted_by = find_reverts_of(repo, sha)
    files = _files_in_commit(repo, sha)
    if tracked_paths is None:
        tracked_paths = _tracked_paths_at_head(repo)
    missing = [path for path in files if path not in tracked_paths] if tracked_paths is not None else []
    reasons: list[str] = []
    if reverted_by:
        reasons.append(f"Later commit(s) {', '.join(reverted_by[:3])} revert this change.")
    if files and len(missing) == len(files):
        reasons.append(f"All {len(files)} file(s) this commit touched are gone from HEAD.")
    elif missing:
        reasons.append(f"{len(missing)} of {len(files)} file(s) this commit touched are gone from HEAD.")
    return {
        "undone": bool(reverted_by) or bool(files and len(missing) == len(files)),
        "reverted_by": reverted_by,
        "files_total": len(files),
        "files_missing": len(missing),
        "reasons": reasons,
    }


def _changed_files(repo: str) -> list[str]:
    result = _run_git(repo, ["diff", "--name-only", "HEAD"])
    if result and result.returncode == 0:
        files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    else:
        status = _run_git(repo, ["status", "--porcelain"])
        if not status or status.returncode != 0:
            return []
        files = []
        for line in status.stdout.splitlines():
            if not line.strip():
                continue
            # Porcelain format uses two status chars, a space, then the path.
            files.append(line[3:].strip() or line.strip())
    return files[:50]


def _detect_test_artifacts(repo: str, session: LocalSession) -> list[dict[str, Any]]:
    """Find local test result artifacts touched near the session.

    This intentionally avoids running tests. It only observes files common test
    runners create, and stores file names/timestamps rather than test content.
    """
    _, end = _session_window(session)
    if not end:
        return []
    cutoff = end - timedelta(hours=2)
    patterns = [
        "junit*.xml",
        "test-results/**/*.xml",
        "coverage/**/*.xml",
        ".pytest_cache/v/cache/lastfailed",
        ".tox/**/log/*",
    ]
    results: list[dict[str, Any]] = []
    root = Path(repo)
    for pattern in patterns:
        for path in root.glob(pattern):
            try:
                stat = path.stat()
            except OSError:
                continue
            stamp = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            if stamp < cutoff:
                continue
            try:
                relative = str(path.relative_to(root))
            except ValueError:
                relative = path.name
            results.append({
                "artifact": relative,
                "updated_at": stamp.isoformat(),
            })
            if len(results) >= 10:
                return results
    return results


def _earliest_survival_status(survival: dict[str, str] | None) -> str | None:
    if not survival:
        return None
    for bucket in ("7", "14", "30"):
        status = survival.get(bucket)
        if status:
            return status
    return None


def build_outcome_evidence(session: LocalSession, *, survival: dict[str, str] | None = None) -> OutcomeEvidence:
    """Build local evidence for a session.

    `survival` (optional) is prior churn-check history for this exact
    session, keyed by day-bucket ("7"/"14"/"30") -- see check_commit_survival
    and local_state.py's evidence_snapshots["survival"]. Passing it in lets a
    commit that looked "useful" get downgraded to "churned" once it's known
    not to have stuck around, without this function needing to know how or
    where that history is stored.
    """
    repo = _repo_root(session.project_path)
    evidence = OutcomeEvidence(session_id=session.session_id, project_path=session.project_path, repo_root=repo, survival=survival)
    if not repo:
        evidence.reasons.append("No git repository was detected for this session.")
        return evidence

    evidence.commits = _recent_commits(repo, session)
    evidence.changed_files = _changed_files(repo)
    evidence.files_touched = _files_touched(repo, evidence.commits)
    evidence.tests = _detect_test_artifacts(repo, session)

    if evidence.commits and evidence.tests:
        evidence.inferred_outcome = "useful"
        evidence.confidence = "medium"
        evidence.reasons.append("A nearby commit and recent test artifacts were detected.")
    elif evidence.commits:
        evidence.inferred_outcome = "useful"
        evidence.confidence = "low"
        evidence.reasons.append("A nearby commit was detected; mark the outcome to confirm usefulness.")
    elif evidence.changed_files:
        evidence.inferred_outcome = "needs_review"
        evidence.confidence = "low"
        evidence.reasons.append("Uncommitted changed files were detected after this session.")
    else:
        evidence.reasons.append("No nearby commit, changed file, or test artifact was detected.")

    if _earliest_survival_status(survival) == "churned" and evidence.inferred_outcome == "useful":
        evidence.inferred_outcome = "churned"
        evidence.confidence = "medium"
        evidence.reasons.append(
            "The commit that looked useful did not survive on the current branch -- "
            "it was likely reverted or rewritten."
        )
    return evidence


def evidence_for_sessions(
    sessions: Iterable[LocalSession], *, survival_by_session: dict[str, dict[str, str]] | None = None
) -> dict[str, OutcomeEvidence]:
    survival_by_session = survival_by_session or {}
    result = {
        session.session_id: build_outcome_evidence(session, survival=survival_by_session.get(session.session_id))
        for session in sessions
    }
    annotate_same_file_reprompt(list(zip(sessions, result.values())))
    return result


def annotate_same_file_reprompt(sessions_with_evidence: list[tuple[LocalSession, OutcomeEvidence]]) -> None:
    """S-24: flag a session whose files get touched again by a later same-project
    session within REPROMPT_WINDOW_HOURS -- a signal the first attempt needed rework.

    Mutates the OutcomeEvidence objects in place. Compares files_touched (this
    session's own commits), not changed_files (always today's live working
    tree, so identical for every session and useless for this comparison).
    """
    by_project: dict[str, list[tuple[LocalSession, OutcomeEvidence]]] = {}
    for session, evidence in sessions_with_evidence:
        if not session.project_path or not evidence.files_touched:
            continue
        by_project.setdefault(session.project_path, []).append((session, evidence))

    for items in by_project.values():
        items.sort(key=lambda pair: pair[0].updated_at or pair[0].started_at or datetime.min.replace(tzinfo=timezone.utc))
        for index, (session, evidence) in enumerate(items):
            _, end = _session_window(session)
            if not end:
                continue
            touched = set(evidence.files_touched)
            for later_session, later_evidence in items[index + 1:]:
                later_start = later_session.started_at or later_session.updated_at
                if not later_start:
                    continue
                if later_start.tzinfo is None:
                    later_start = later_start.replace(tzinfo=timezone.utc)
                gap_hours = (later_start - end).total_seconds() / 3600
                if gap_hours < 0:
                    continue
                if gap_hours > REPROMPT_WINDOW_HOURS:
                    break  # sorted by time -- no later session in this project will be closer
                if touched & set(later_evidence.files_touched):
                    evidence.same_file_reprompt = True
                    evidence.reasons.append(
                        f"A later session touched the same file(s) again within {gap_hours:.0f}h -- "
                        "this attempt may not have fully resolved the task."
                    )
                    break
