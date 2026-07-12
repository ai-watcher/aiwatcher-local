"""Local outcome evidence for AIWatcher sessions.

This module keeps the OSS moat narrow and honest: it reads local git/test
signals around a session and turns them into personal evidence. It does not
upload source, prompt text, diffs, or team data.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .scanner import LocalSession


GIT_TIMEOUT_SECONDS = 2
COMMIT_LOOKAHEAD_HOURS = 24


@dataclass
class OutcomeEvidence:
    session_id: str
    project_path: str | None
    repo_root: str | None = None
    commits: list[dict[str, Any]] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    inferred_outcome: str | None = None
    confidence: str = "low"
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "project_path": self.project_path,
            "repo_root": self.repo_root,
            "commits": self.commits,
            "changed_files": self.changed_files,
            "tests": self.tests,
            "inferred_outcome": self.inferred_outcome,
            "confidence": self.confidence,
            "reasons": self.reasons,
        }


def _run_git(repo: str, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", repo, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _repo_root(path: str | None) -> str | None:
    if not path:
        return None
    candidate = Path(path).expanduser()
    if not candidate.exists():
        return None
    if candidate.is_file():
        candidate = candidate.parent
    result = _run_git(str(candidate), ["rev-parse", "--show-toplevel"])
    if not result or result.returncode != 0:
        return None
    root = result.stdout.strip()
    return root or None


def _session_window(session: LocalSession) -> tuple[datetime | None, datetime | None]:
    start = session.started_at or session.updated_at
    end = session.updated_at or session.started_at
    if start and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start, end


def _safe_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _recent_commits(repo: str, session: LocalSession) -> list[dict[str, Any]]:
    start, end = _session_window(session)
    if not start:
        return []
    until = (end or start) + timedelta(hours=COMMIT_LOOKAHEAD_HOURS)
    result = _run_git(
        repo,
        [
            "log",
            "--date=iso-strict",
            "--pretty=format:%H%x1f%ad%x1f%s",
            f"--since={start.isoformat()}",
            f"--until={until.isoformat()}",
            "--",
        ],
    )
    if not result or result.returncode != 0:
        return []
    commits: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) != 3:
            continue
        sha, stamp, subject = parts
        commits.append({
            "sha": sha[:12],
            "subject_hash": _safe_hash(subject),
            "committed_at": stamp,
        })
    return commits[:10]


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


def build_outcome_evidence(session: LocalSession) -> OutcomeEvidence:
    repo = _repo_root(session.project_path)
    evidence = OutcomeEvidence(session_id=session.session_id, project_path=session.project_path, repo_root=repo)
    if not repo:
        evidence.reasons.append("No git repository was detected for this session.")
        return evidence

    evidence.commits = _recent_commits(repo, session)
    evidence.changed_files = _changed_files(repo)
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
    return evidence


def evidence_for_sessions(sessions: Iterable[LocalSession]) -> dict[str, OutcomeEvidence]:
    return {session.session_id: build_outcome_evidence(session) for session in sessions}
