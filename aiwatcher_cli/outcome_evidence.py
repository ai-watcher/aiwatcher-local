"""Local outcome evidence for AIWatcher sessions.

This module keeps the local privacy boundary narrow and honest: it reads local
git/test signals around a session and turns them into personal evidence. It
does not upload source, prompt text, diffs, or team data.
"""

from __future__ import annotations

import os
import hashlib
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .command_evidence import CommandEvidence, command_evidence_coverage, command_evidence_for_session
from .git_identity import identity_for_session, repository_identity, resolve_git_identity
from .local_state import (
    recent_commit_receipts,
    recent_verification_receipts,
    record_commit_receipt,
    record_verification_receipt,
)
from .scanner import LocalSession


GIT_TIMEOUT_SECONDS = 2
MAX_FINGERPRINT_UNTRACKED_FILES = 200
MAX_FINGERPRINT_UNTRACKED_BYTES = 64 * 1024 * 1024
MAX_FINGERPRINT_SUBMODULES = 64

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
    repository_id: str | None = None
    repository_lineage_id: str | None = None
    checkout_id: str | None = None
    checkout_path: str | None = None
    branch: str | None = None
    head: str | None = None
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    dirty: bool | None = None
    unpushed_commits: list[dict[str, Any]] = field(default_factory=list)
    commit_receipts: list[dict[str, Any]] = field(default_factory=list)
    commits: list[dict[str, Any]] = field(default_factory=list)
    commit_attribution: str = "none"
    command_evidence_coverage: str = "unavailable"
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
            "repository_id": self.repository_id,
            "repository_lineage_id": self.repository_lineage_id,
            "checkout_id": self.checkout_id,
            "checkout_path": self.checkout_path,
            "branch": self.branch,
            "head": self.head,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "dirty": self.dirty,
            "unpushed_commits": self.unpushed_commits,
            "commit_receipts": self.commit_receipts,
            "commits": self.commits,
            "commit_attribution": self.commit_attribution,
            "command_evidence_coverage": self.command_evidence_coverage,
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
            encoding="utf-8",
            errors="surrogateescape",
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
    identity = resolve_git_identity(path)
    root = identity.checkout_path if identity else None
    if root is None:
        return None
    _REPO_ROOT_CACHE[path] = root
    return root


def _git_text(repo: str, args: list[str]) -> str | None:
    result = _run_git(repo, args)
    if not result or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _working_tree_fingerprint(repo: str, status: str) -> str | None:
    """Hash the current tracked diff and bounded untracked contents.

    A porcelain status hash only identifies changed paths. Two different edits
    to the same file therefore looked identical and could make an old test
    receipt appear current. Raw content stays local; only this digest is stored.
    If the tree cannot be read within conservative bounds, return unknown so a
    verification receipt is never treated as current optimistically.
    """
    diff = _run_git(repo, ["diff", "--binary", "--no-ext-diff", "HEAD", "--"])
    untracked = _run_git(repo, ["ls-files", "--others", "--exclude-standard", "-z"])
    index = _run_git(repo, ["ls-files", "--stage", "-z"])
    if (
        not diff or diff.returncode != 0
        or not untracked or untracked.returncode != 0
        or not index or index.returncode != 0
    ):
        return None

    paths = [item for item in untracked.stdout.split("\0") if item]
    if len(paths) > MAX_FINGERPRINT_UNTRACKED_FILES:
        return None
    def frame(value: bytes) -> bytes:
        return len(value).to_bytes(8, "big") + value

    digest = hashlib.sha256()
    digest.update(frame(status.encode("utf-8", errors="surrogateescape")))
    digest.update(frame(diff.stdout.encode("utf-8", errors="surrogateescape")))
    total_bytes = 0
    root = Path(repo)
    try:
        for relative in paths:
            path = root / relative
            stat = path.lstat()
            path_bytes = relative.encode("utf-8", errors="surrogateescape")
            file_digest = hashlib.sha256()
            if path.is_symlink():
                kind = b"symlink"
                file_digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            elif path.is_file():
                kind = b"file"
                total_bytes += stat.st_size
                if total_bytes > MAX_FINGERPRINT_UNTRACKED_BYTES:
                    return None
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        file_digest.update(chunk)
            else:
                kind = f"mode:{stat.st_mode}".encode("ascii")
            digest.update(frame(path_bytes))
            digest.update(frame(kind))
            digest.update(frame(file_digest.digest()))

        submodules: list[tuple[str, str]] = []
        for entry in index.stdout.split("\0"):
            metadata, separator, relative = entry.partition("\t")
            if separator and metadata.startswith("160000 "):
                fields = metadata.split()
                if len(fields) >= 2:
                    submodules.append((relative, fields[1]))
        if len(submodules) > MAX_FINGERPRINT_SUBMODULES:
            return None
        for relative, index_head in submodules:
            digest.update(frame(relative.encode("utf-8", errors="surrogateescape")))
            digest.update(frame(b"submodule-index"))
            digest.update(frame(index_head.encode("ascii")))
            submodule = root / relative
            top_level = _run_git(str(submodule), ["rev-parse", "--show-toplevel"])
            if (
                top_level is None
                or top_level.returncode != 0
                or os.path.normcase(os.path.realpath(top_level.stdout.strip()))
                != os.path.normcase(os.path.realpath(submodule))
            ):
                # An uninitialized submodule has no mutable checkout state; its
                # recorded commit is already represented by the parent diff.
                continue
            submodule_head = _git_text(str(submodule), ["rev-parse", "HEAD"])
            if not submodule_head:
                return None
            status_result = _run_git(
                str(submodule), ["status", "--porcelain=v1", "--untracked-files=all"]
            )
            if status_result is None or status_result.returncode != 0:
                return None
            submodule_fingerprint = _working_tree_fingerprint(
                str(submodule), status_result.stdout
            )
            if submodule_fingerprint is None:
                return None
            digest.update(frame(b"submodule-head"))
            digest.update(frame(submodule_head.encode("ascii")))
            digest.update(frame(b"submodule-worktree"))
            digest.update(frame(submodule_fingerprint.encode("ascii")))
    except (OSError, ValueError):
        return None
    if not status and not diff.stdout and not paths and not submodules:
        # Preserve the established clean-tree fingerprint for repositories
        # without submodules. Older receipts lack a pre-state and therefore
        # remain historical rather than gaining authority from this value.
        return hashlib.sha256(b"").hexdigest()[:24]
    return digest.hexdigest()[:24]


def _git_common_dir(repo: str | None) -> str | None:
    identity = resolve_git_identity(repo)
    return identity.common_dir if identity else None


def _checkout_root(
    session: LocalSession,
    observed_commands: list[CommandEvidence] | None = None,
) -> str | None:
    """Return the exact checkout observed by the tool when it is trustworthy.

    ``project_path`` intentionally groups linked worktrees under one project in
    parts of the scanner. Fresh Start needs the opposite: the checkout that
    contains the branch, commits, and dirty files the user was actually using.
    ``raw_cwd`` preserves that observation. Only prefer it when Git confirms it
    belongs to the same repository as the grouped project path.
    """
    base_identity = None
    if session.identity_source != "identity_conflict" and session.checkout_path and session.checkout_id:
        cached = resolve_git_identity(session.checkout_path)
        if cached is not None and cached.checkout_id == session.checkout_id:
            base_identity = cached
    if base_identity is None:
        base_identity = identity_for_session(session.project_path, session.raw_cwd)
    if base_identity is not None and base_identity.identity_source == "identity_conflict":
        return None

    grouped_identity = resolve_git_identity(session.project_path)
    expected_common_dir = (
        grouped_identity.common_dir if grouped_identity is not None
        else base_identity.common_dir if base_identity is not None
        else None
    )
    command_identities: dict[str, Any] = {}
    commit_identities: dict[str, Any] = {}
    for observed in observed_commands or []:
        if (
            observed.session_id != session.session_id
            or observed.completion_state != "completed"
            or not observed.cwd
            or not os.path.isabs(observed.cwd)
        ):
            continue
        candidate = resolve_git_identity(observed.cwd)
        if candidate is None:
            continue
        if expected_common_dir is not None and candidate.common_dir != expected_common_dir:
            continue
        command_identities[candidate.checkout_id] = candidate
        if observed.command_kind == "git_commit" and observed.exit_code == 0 and observed.commit_sha:
            commit_identities[candidate.checkout_id] = candidate
    if len(commit_identities) == 1:
        return next(iter(commit_identities.values())).checkout_path
    if not commit_identities and len(command_identities) == 1:
        return next(iter(command_identities.values())).checkout_path
    return base_identity.checkout_path if base_identity else None


def _count_revisions(repo: str, revision_range: str) -> int | None:
    value = _git_text(repo, ["rev-list", "--count", revision_range])
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _checkout_state(repo: str, *, include_fingerprint: bool = True) -> dict[str, Any]:
    branch = _git_text(repo, ["branch", "--show-current"])
    head_result = _run_git(repo, ["rev-parse", "--short=12", "HEAD"])
    head = head_result.stdout.strip() if head_result and head_result.returncode == 0 else None
    upstream = _git_text(repo, ["rev-parse", "--abbrev-ref", "@{upstream}"])
    status_result = _run_git(repo, ["status", "--porcelain=v1", "--untracked-files=all"])
    status = status_result.stdout if status_result and status_result.returncode == 0 else None
    state_observed = head is not None and status is not None
    comparison_ref = upstream
    if not comparison_ref:
        remote_head = _git_text(repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
        current_branch = branch or ""
        candidates = [remote_head, "main", "master"]
        comparison_ref = next(
            (
                candidate for candidate in candidates
                if candidate and candidate != current_branch
                and _git_text(repo, ["rev-parse", "--verify", candidate])
            ),
            None,
        )
    ahead = _count_revisions(repo, f"{comparison_ref}..HEAD") if comparison_ref else None
    behind = _count_revisions(repo, f"HEAD..{comparison_ref}") if comparison_ref else None
    unpushed: list[dict[str, Any]] = []
    if comparison_ref and ahead:
        log = _git_text(repo, ["log", "--format=%h%x1f%s", f"{comparison_ref}..HEAD"])
        for line in (log or "").splitlines()[:10]:
            sha, _, subject = line.partition("\x1f")
            if sha:
                unpushed.append({"sha": sha, "subject": subject.strip()})
    return {
        "branch": branch,
        "head": head,
        "upstream": upstream,
        "comparison_ref": comparison_ref,
        "ahead": ahead,
        "behind": behind,
        "dirty": bool(status.strip()) if state_observed else None,
        "dirty_fingerprint": (
            _working_tree_fingerprint(repo, status)
            if state_observed and include_fingerprint else None
        ),
        "state_observed": state_observed,
        "_status": status,
        "unpushed_commits": unpushed,
    }


def verification_git_fingerprint(path: str) -> dict[str, str | None]:
    """Return the checkout identity used when recording verification receipts."""
    identity = resolve_git_identity(path)
    checkout = identity.checkout_path if identity else None
    if not checkout:
        return {
            "checkout": None,
            "checkout_id": None,
            "repository_id": None,
            "repository_lineage_id": None,
            "head": None,
            "dirty_fingerprint": None,
        }
    head = _git_text(checkout, ["rev-parse", "HEAD"])
    state = _checkout_state(checkout)
    return {
        "checkout": checkout,
        "checkout_id": identity.checkout_id,
        "repository_id": identity.repository_id,
        "repository_lineage_id": identity.repository_lineage_id,
        "head": head,
        "dirty_fingerprint": state.get("dirty_fingerprint"),
    }


def _repository_id(repo: str) -> str | None:
    return repository_identity(repo)


def _same_checkout(left: object, right: object) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.realpath(str(left)) == os.path.realpath(str(right))
    except (OSError, ValueError):
        return False


def _commit_exists(repo: str, value: object) -> bool:
    sha = str(value or "").strip()
    if not sha:
        return False
    result = _run_git(repo, ["cat-file", "-e", f"{sha}^{{commit}}"])
    return bool(result and result.returncode == 0)


def _commit_details(repo: str, value: object) -> dict[str, Any] | None:
    sha = str(value or "").strip()
    if not sha:
        return None
    result = _run_git(
        repo,
        ["show", "-s", "--date=iso-strict", "--format=%H%x1f%cI%x1f%s%x1f%b", f"{sha}^{{commit}}"],
    )
    if not result or result.returncode != 0:
        return None
    parts = result.stdout.rstrip("\r\n").split("\x1f", 3)
    if len(parts) != 4:
        return None
    full_sha, committed_at, subject, body = parts
    return {
        "sha": full_sha[:12],
        "subject": subject.strip(),
        "body": body.strip(),
        "committed_at": committed_at,
        "receipt_observed": True,
        "attribution": "session_bound",
    }


def _ingest_session_command_evidence(
    observed_commands: list[CommandEvidence],
    checkout_id: str | None,
) -> None:
    """Persist bounded facts from structured tool-call/result pairs."""
    for observed in observed_commands:
        cwd = observed.cwd
        if not cwd or not os.path.isabs(cwd):
            continue
        identity = resolve_git_identity(cwd)
        if identity is None or (checkout_id and identity.checkout_id != checkout_id):
            continue
        if observed.command_kind == "verification":
            if not observed.runner:
                continue
            started = observed.started_at or observed.finished_at
            finished = observed.finished_at or observed.started_at
            if not started or not finished:
                continue
            try:
                record_verification_receipt(
                    runner=observed.runner,
                    checkout_path=identity.checkout_path,
                    repository_id=identity.repository_id,
                    repository_lineage_id=identity.repository_lineage_id,
                    checkout_id=identity.checkout_id,
                    head=None,
                    dirty_fingerprint=None,
                    started_at=started,
                    finished_at=finished,
                    exit_code=observed.exit_code,
                    session_id=observed.session_id,
                    session_source="transcript_tool_result",
                    source_id=observed.source_id,
                    completion_state=observed.completion_state,
                    state_binding="historical",
                    truncated=observed.truncated,
                )
            except (OSError, ValueError):
                continue
        elif (
            observed.command_kind == "git_commit"
            and observed.completion_state == "completed"
            and observed.exit_code == 0
            and observed.commit_sha
        ):
            details = _commit_details(identity.checkout_path, observed.commit_sha)
            if details is None:
                continue
            try:
                record_commit_receipt({
                    **details,
                    "sha": _git_text(identity.checkout_path, ["rev-parse", f"{observed.commit_sha}^{{commit}}"]),
                    "repository_id": identity.repository_id,
                    "repository_lineage_id": identity.repository_lineage_id,
                    "checkout_id": identity.checkout_id,
                    "checkout_path": identity.checkout_path,
                    "head": _git_text(identity.checkout_path, ["rev-parse", f"{observed.commit_sha}^{{commit}}"]),
                    "session_id": observed.session_id,
                    "session_source": "transcript_tool_result",
                    "source_id": observed.source_id,
                })
            except (OSError, ValueError):
                continue


def _session_commit_receipts(
    session: LocalSession,
    repository_id: str | None,
    checkout_path: str | None,
    checkout_id: str | None = None,
) -> list[dict[str, Any]]:
    if not checkout_id and not checkout_path:
        return []
    matched: list[dict[str, Any]] = []
    for receipt in recent_commit_receipts(
        repository_id=repository_id,
        checkout_id=checkout_id,
        checkout_path=checkout_path,
        include_legacy=False,
        session_id=session.session_id,
        include_unbound=False,
        limit=100,
    ):
        receipt_checkout_id = receipt.get("checkout_id")
        if checkout_id and receipt_checkout_id:
            if receipt_checkout_id != checkout_id:
                continue
        elif not _same_checkout(receipt.get("checkout_path"), checkout_path):
            continue
        if receipt.get("session_id") != session.session_id:
            continue
        if checkout_path and not _commit_exists(checkout_path, receipt.get("sha")):
            continue
        matched.append(receipt)
    return matched[:20]


def _verification_receipts(
    session: LocalSession,
    repository_id: str | None,
    checkout_path: str | None,
    state: dict[str, Any],
    checkout_id: str | None = None,
) -> list[dict[str, Any]]:
    if not checkout_id and not checkout_path:
        return []
    start, end = _session_window(session)
    if not start:
        return []
    lower = start - timedelta(hours=1)
    upper = (end or start) + timedelta(hours=COMMIT_LOOKAHEAD_HOURS)
    results: list[dict[str, Any]] = []
    state_fingerprint = state.get("dirty_fingerprint")
    fingerprint_checked = state_fingerprint is not None
    for receipt in recent_verification_receipts(
        repository_id=repository_id,
        checkout_id=checkout_id,
        checkout_path=checkout_path,
        include_legacy=True,
        session_id=session.session_id,
        include_unbound=True,
        limit=100,
    ):
        receipt_checkout_id = receipt.get("checkout_id")
        if checkout_id and receipt_checkout_id:
            if receipt_checkout_id != checkout_id:
                continue
        elif not _same_checkout(receipt.get("checkout_path"), checkout_path):
            continue
        if not receipt_checkout_id and checkout_path and not _commit_exists(checkout_path, receipt.get("head")):
            continue
        exact_session = receipt.get("session_id") == session.session_id
        try:
            stamp = datetime.fromisoformat(str(receipt.get("finished_at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if not exact_session and not lower <= stamp <= upper:
            continue
        receipt_head = str(receipt.get("head") or "")
        state_head = str(state.get("head") or "")
        receipt_fingerprint = receipt.get("dirty_fingerprint")
        stable_basis = (
            receipt.get("state_binding") == "git_state"
            and bool(receipt.get("started_checkout_id"))
            and receipt.get("started_checkout_id") == receipt.get("checkout_id")
            and bool(receipt.get("started_head"))
            and receipt.get("started_head") == receipt.get("head")
            and bool(receipt.get("started_dirty_fingerprint"))
            and receipt.get("started_dirty_fingerprint") == receipt_fingerprint
        )
        if not fingerprint_checked:
            status = state.get("_status")
            state_fingerprint = (
                _working_tree_fingerprint(str(checkout_path), status)
                if state.get("state_observed") is True and isinstance(status, str) and checkout_path
                else None
            )
            state["dirty_fingerprint"] = state_fingerprint
            fingerprint_checked = True
        current = (
            stable_basis
            and
            bool(receipt_head and state_head)
            and (receipt_head.startswith(state_head) or state_head.startswith(receipt_head))
            and bool(receipt_fingerprint and state_fingerprint)
            and receipt_fingerprint == state_fingerprint
        )
        results.append({
            "name": receipt.get("runner"),
            "status": receipt.get("status"),
            "finished_at": receipt.get("finished_at"),
            "head": receipt.get("head"),
            "current": current,
            "source": (
                "Session-bound terminal receipt"
                if exact_session else "AIWatcher verification receipt (time-window match)"
            ),
            "attribution": "session_bound" if exact_session else "inferred_time_window",
            "completion_state": receipt.get("completion_state") or "completed",
            "state_binding": receipt.get("state_binding") or "historical",
            "truncated": bool(receipt.get("truncated")),
            "_finished_at_sort": stamp.timestamp(),
        })
    results.sort(key=lambda item: float(item.get("_finished_at_sort") or 0), reverse=True)
    for item in results:
        if item.get("current") is True and item.get("attribution") == "session_bound":
            item["authoritative"] = True
            break
    for item in results:
        item.pop("_finished_at_sort", None)
    return results[:10]


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
    return _checkout_root(session)


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


def repo_state_fingerprint(session: LocalSession) -> str | None:
    """Return HEAD plus the changed-path list, so callers caching handoff
    evidence can tell a commit or new edit apart from an unchanged tree."""
    observed_commands = command_evidence_for_session(session)
    repo = _checkout_root(session, observed_commands)
    if not repo:
        return None
    result = _run_git(repo, ["status", "--porcelain=v2", "--branch", "--untracked-files=no"])
    if not result or result.returncode != 0:
        return None
    return result.stdout


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
                "status": "result unknown",
                "source": "local test artifact",
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
    observed_commands = command_evidence_for_session(session)
    repo = _checkout_root(session, observed_commands)
    evidence = OutcomeEvidence(session_id=session.session_id, project_path=session.project_path, repo_root=repo, survival=survival)
    if not repo:
        evidence.reasons.append("No git repository was detected for this session.")
        return evidence

    state = _checkout_state(repo, include_fingerprint=False)
    identity = resolve_git_identity(repo)
    evidence.checkout_path = repo
    evidence.repository_id = identity.repository_id if identity else _repository_id(repo)
    evidence.repository_lineage_id = identity.repository_lineage_id if identity else None
    evidence.checkout_id = identity.checkout_id if identity else None
    evidence.branch = state["branch"]
    evidence.head = state["head"]
    evidence.upstream = state["upstream"]
    evidence.ahead = state["ahead"]
    evidence.behind = state["behind"]
    evidence.dirty = state["dirty"]
    evidence.unpushed_commits = state["unpushed_commits"]
    evidence.command_evidence_coverage = command_evidence_coverage(session)
    _ingest_session_command_evidence(observed_commands, evidence.checkout_id)
    evidence.commit_receipts = _session_commit_receipts(
        session, evidence.repository_id, repo, evidence.checkout_id,
    )
    if evidence.commit_receipts:
        evidence.commits = [
            detail
            for receipt in evidence.commit_receipts
            if (detail := _commit_details(repo, receipt.get("sha"))) is not None
        ]
        evidence.commit_attribution = "session_bound"
    else:
        evidence.commits = _recent_commits(repo, session)
        for commit in evidence.commits:
            commit["attribution"] = "nearby_time_window"
        evidence.commit_attribution = "nearby_time_window" if evidence.commits else "none"
    evidence.changed_files = _changed_files(repo)
    evidence.files_touched = _files_touched(repo, evidence.commits)
    evidence.tests = _verification_receipts(
        session, evidence.repository_id, repo, state, evidence.checkout_id,
    )
    evidence.tests.extend(_detect_test_artifacts(repo, session))

    current_pass = any(
        item.get("status") == "passed" and item.get("authoritative") is True
        for item in evidence.tests
    )
    if evidence.commits and current_pass:
        evidence.inferred_outcome = "useful"
        evidence.confidence = "medium"
        evidence.reasons.append("A nearby commit and a passing verification for the current Git state were detected.")
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
            "The commit that looked useful is gone from the current branch -- "
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
        key = evidence.repository_id or session.repository_id or session.project_path
        by_project.setdefault(key, []).append((session, evidence))

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
