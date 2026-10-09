"""Build deterministic, evidence-backed Local delivery reviews."""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .delivery import (
    ContributionEdge,
    DeliveryEvent,
    DeliverySnapshot,
    ObjectiveClaim,
    VerificationClaim,
    WorkflowStats,
    WorkReceipt,
)
from .git_identity import resolve_git_identity
from .local_state import (
    recent_commit_receipts,
    recent_verification_receipts,
    record_delivery_event,
    record_work_receipt,
)
from .outcome_evidence import verification_git_fingerprint
from .scanner import LocalSession, scan_all, segment_session_by_prompt


GIT_TIMEOUT_SECONDS = 4
MAX_DELIVERY_COMMITS = 200
MAX_DELIVERY_FILES = 500


class DeliveryReviewUnavailable(ValueError):
    """The checkout does not contain enough evidence for a delivery review."""


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


def _git_text(repo: str, args: list[str]) -> str | None:
    result = _run_git(repo, args)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _commit(repo: str, value: str | None) -> str | None:
    if not value:
        return None
    return _git_text(repo, ["rev-parse", "--verify", f"{value}^{{commit}}"]) or None


def _ancestor(repo: str, base: str, head: str) -> bool:
    result = _run_git(repo, ["merge-base", "--is-ancestor", base, head])
    return bool(result is not None and result.returncode == 0)


def _upstream(repo: str) -> str | None:
    return _git_text(repo, ["rev-parse", "--abbrev-ref", "@{upstream}"])


def _base_candidates(repo: str, branch: str | None, upstream: str | None) -> list[str]:
    remote = upstream.split("/", 1)[0] if upstream and "/" in upstream else "origin"
    candidates: list[str] = []
    remote_head = _git_text(repo, ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"])
    if remote_head:
        candidates.append(remote_head)
    candidates.extend((f"{remote}/main", f"{remote}/master", "main", "master"))
    return [value for value in dict.fromkeys(candidates) if value != branch and value != upstream]


def _delivery_base(
    repo: str,
    head: str,
    branch: str | None,
    upstream: str | None,
    requested: str | None,
) -> tuple[str | None, str]:
    requested_sha = _commit(repo, requested)
    if requested_sha and requested_sha != head and _ancestor(repo, requested_sha, head):
        return requested_sha, "observed event range"
    for candidate in _base_candidates(repo, branch, upstream):
        candidate_sha = _commit(repo, candidate)
        if not candidate_sha:
            continue
        merge_base = _git_text(repo, ["merge-base", candidate_sha, head])
        if merge_base and merge_base != head:
            return merge_base, f"merge-base with {candidate}"
    parent = _commit(repo, f"{head}^")
    if parent:
        return parent, "first parent fallback"
    return None, "unavailable"


def _commit_range(repo: str, base: str | None, head: str) -> tuple[str, ...]:
    args = ["rev-list", "--reverse", f"{base}..{head}"] if base else ["rev-list", "--reverse", head]
    text = _git_text(repo, args)
    if not text:
        return ()
    return tuple(line.strip() for line in text.splitlines() if line.strip())[:MAX_DELIVERY_COMMITS]


def _range_files(repo: str, base: str | None, head: str) -> tuple[tuple[str, ...], int, int]:
    range_spec = f"{base}..{head}" if base else head
    names = _run_git(repo, ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", range_spec])
    if base:
        names = _run_git(repo, ["diff", "--name-only", "-z", range_spec])
    files = tuple(
        value for value in ((names.stdout if names and names.returncode == 0 else "").split("\0")) if value
    )[:MAX_DELIVERY_FILES]
    stats = _run_git(repo, ["diff", "--numstat", range_spec] if base else ["show", "--numstat", "--format=", head])
    added = removed = 0
    if stats and stats.returncode == 0:
        for line in stats.stdout.splitlines():
            left, separator, remainder = line.partition("\t")
            right, second, _path = remainder.partition("\t")
            if not separator or not second:
                continue
            if left.isdigit():
                added += int(left)
            if right.isdigit():
                removed += int(right)
    return files, added, removed


def _exact_verifications(identity: Any, head: str) -> tuple[VerificationClaim, ...]:
    current = verification_git_fingerprint(identity.checkout_path)
    fingerprint = current.get("dirty_fingerprint")
    claims: list[VerificationClaim] = []
    seen: set[str] = set()
    for row in recent_verification_receipts(
        repository_id=identity.repository_id,
        checkout_id=identity.checkout_id,
        checkout_path=identity.checkout_path,
        include_legacy=False,
        limit=100,
    ):
        source_id = str(row.get("source_id") or row.get("id") or "")
        if source_id and source_id in seen:
            continue
        if source_id:
            seen.add(source_id)
        stable = (
            row.get("state_binding") == "git_state"
            and row.get("started_checkout_id") == identity.checkout_id
            and row.get("checkout_id") == identity.checkout_id
            and row.get("started_head") == row.get("head") == head
            and bool(row.get("started_dirty_fingerprint"))
            and row.get("started_dirty_fingerprint") == row.get("dirty_fingerprint") == fingerprint
        )
        claims.append(VerificationClaim(
            runner=str(row.get("runner") or "unknown check"),
            status=str(row.get("status") or "result unknown"),
            scope=str(row.get("verification_scope") or "unknown"),
            provenance="observed",
            exact_state=stable,
            finished_at=str(row.get("finished_at") or "") or None,
            source_id=source_id or None,
        ))
    claims.sort(key=lambda item: (not item.exact_state, item.status != "failed", item.finished_at or ""))
    return tuple(claims[:20])


def _contributions(identity: Any, commits: tuple[str, ...]) -> tuple[ContributionEdge, ...]:
    commit_set = set(commits)
    by_session: dict[str, dict[str, Any]] = {}
    for row in recent_commit_receipts(
        repository_id=identity.repository_id,
        checkout_id=identity.checkout_id,
        checkout_path=identity.checkout_path,
        include_legacy=False,
        include_unbound=False,
        limit=500,
    ):
        full_sha = _commit(identity.checkout_path, str(row.get("sha") or ""))
        session_id = str(row.get("session_id") or "")
        if not full_sha or full_sha not in commit_set or not session_id:
            continue
        bucket = by_session.setdefault(session_id, {"commits": [], "source_id": None})
        bucket["commits"].append(full_sha)
        bucket["source_id"] = bucket["source_id"] or row.get("source_id")
    return tuple(
        ContributionEdge(
            session_id=session_id,
            commit_shas=tuple(values["commits"]),
            strength="exact",
            provenance="observed",
            source_id=str(values["source_id"] or "") or None,
        )
        for session_id, values in sorted(by_session.items())
    )


def _workflow_stats(contributions: tuple[ContributionEdge, ...], sessions: Iterable[LocalSession] | None) -> WorkflowStats:
    session_ids = {item.session_id for item in contributions}
    if not session_ids:
        return WorkflowStats()
    rows = list(sessions) if sessions is not None else scan_all()
    selected = [row for row in rows if row.session_id in session_ids]
    if not selected:
        return WorkflowStats(coverage="exact session ids; local transcripts unavailable")
    turns = 0
    request_seconds: list[float] = []
    idle_seconds: list[float] = []
    for row in selected:
        for segment in segment_session_by_prompt(row.source_path, max_chars=1):
            turns += 1
            took = segment.get("took_seconds")
            gap = segment.get("gap_seconds")
            if isinstance(took, (int, float)) and took >= 0:
                request_seconds.append(float(took))
            if isinstance(gap, (int, float)) and gap >= 0:
                idle_seconds.append(float(gap))
    return WorkflowStats(
        session_count=len(selected),
        user_turns=turns,
        model_calls=sum(max(0, int(row.agent_calls)) for row in selected),
        tool_calls=sum(max(0, int(row.tool_calls)) for row in selected),
        observed_request_seconds=sum(request_seconds) if request_seconds else None,
        longest_observed_request_seconds=max(request_seconds) if request_seconds else None,
        observed_idle_gap_seconds=sum(idle_seconds) if idle_seconds else None,
        coverage=f"{len(selected)} session{'s' if len(selected) != 1 else ''} linked by commit receipts",
    )


def build_work_receipt(
    checkout_path: str,
    *,
    objective_text: str | None = None,
    event_kind: str = "explicit_review",
    event_status: str = "confirmed",
    event_source: str = "explicit_local_review",
    event_source_id: str | None = None,
    event_observed_at: str | None = None,
    event_head_sha: str | None = None,
    event_base_sha: str | None = None,
    session_id: str | None = None,
    remote: str | None = None,
    remote_ref: str | None = None,
    pull_request_url: str | None = None,
    sessions: Iterable[LocalSession] | None = None,
    persist: bool = True,
) -> WorkReceipt:
    """Build a review from immutable Git facts and exact local receipts."""
    if event_status not in {"confirmed", "candidate"}:
        raise DeliveryReviewUnavailable("a delivery review requires confirmed evidence or an explicit local candidate")
    if event_status == "candidate" and event_kind != "explicit_review":
        raise DeliveryReviewUnavailable("automatic delivery events must be confirmed")
    identity = resolve_git_identity(checkout_path)
    if identity is None:
        raise DeliveryReviewUnavailable("no Git checkout was detected")
    repo = identity.checkout_path
    head = _commit(repo, event_head_sha or "HEAD")
    if not head:
        raise DeliveryReviewUnavailable("the checkout has no readable HEAD commit")
    branch = _git_text(repo, ["branch", "--show-current"])
    upstream = _upstream(repo)
    base, base_basis = _delivery_base(repo, head, branch, upstream, event_base_sha)
    commits = _commit_range(repo, base, head)
    if not commits:
        raise DeliveryReviewUnavailable("no commits were found in the delivery range")
    files, added, removed = _range_files(repo, base, head)
    status = _run_git(repo, ["status", "--porcelain=v1", "-z"])
    clean = status is not None and status.returncode == 0 and not status.stdout
    observed_at = event_observed_at or datetime.now(timezone.utc).isoformat()
    source_seed = event_source_id or (
        f"explicit_review\0{identity.checkout_id}\0{head}"
        if event_kind == "explicit_review"
        else f"{event_kind}\0{identity.checkout_id}\0{head}\0{observed_at}"
    )
    event_id = "delivery-" + hashlib.sha256(source_seed.encode("utf-8")).hexdigest()[:24]
    objective = ObjectiveClaim(
        provenance="user_confirmed" if objective_text and objective_text.strip() else "unavailable",
        confidence="high" if objective_text and objective_text.strip() else "none",
        text=objective_text if objective_text and objective_text.strip() else None,
        confirmed_at=observed_at if objective_text and objective_text.strip() else None,
    )
    event = DeliveryEvent(
        event_id=event_id,
        kind=event_kind,
        status=event_status,
        observed_at=observed_at,
        repository_id=identity.repository_id,
        checkout_id=identity.checkout_id,
        head_sha=head,
        source=event_source,
        source_id=event_source_id,
        session_id=session_id,
        remote=remote,
        remote_ref=remote_ref,
        pull_request_url=pull_request_url,
    )
    snapshot = DeliverySnapshot(
        repository_id=identity.repository_id,
        checkout_id=identity.checkout_id,
        branch=branch,
        base_sha=base,
        head_sha=head,
        upstream=upstream,
        clean=clean,
        commit_shas=commits,
        changed_files=files,
        lines_added=added,
        lines_removed=removed,
    )
    verifications = _exact_verifications(identity, head)
    contributions = _contributions(identity, commits)
    workflow = _workflow_stats(contributions, sessions)
    attention: list[str] = []
    if objective.provenance == "unavailable":
        attention.append("Objective unavailable. Confirm it before copying a PR summary.")
    if not clean:
        attention.append("The working tree has changes outside the delivered commit range.")
    exact_checks = [item for item in verifications if item.exact_state]
    exact_passes = [item for item in exact_checks if item.status == "passed"]
    if not exact_checks:
        attention.append("No verification was recorded for the exact delivered Git state.")
    elif not exact_passes:
        attention.append("Exact-state verification did not pass.")
    elif not any(item.scope in {"project_default", "named_check"} for item in exact_passes):
        attention.append("Only targeted verification passed for the delivered Git state.")
    if event_kind == "explicit_review":
        attention.append("Local candidate only; remote push and pull-request status were not confirmed.")
    if base_basis != "observed event range":
        attention.append(f"Delivery base selected from {base_basis}.")
    receipt_id = "receipt-" + hashlib.sha256(f"{event_id}\0{head}".encode("utf-8")).hexdigest()[:24]
    receipt = WorkReceipt(
        receipt_id=receipt_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        event=event,
        snapshot=snapshot,
        objective=objective,
        verifications=verifications,
        contributions=contributions,
        workflow=workflow,
        attention=tuple(attention),
    )
    if persist:
        record_delivery_event({
            **event.to_json(),
            "repository_lineage_id": identity.repository_lineage_id,
            "checkout_path": identity.checkout_path,
            "base_sha": base,
        })
        record_work_receipt(receipt.to_persisted_json())
    return receipt


def hydrate_persisted_receipt(receipt: dict[str, Any], event: dict[str, Any] | None) -> dict[str, Any]:
    """Reconstruct transient file paths only while the exact Git range exists."""
    payload = dict(receipt)
    snapshot = dict(payload.get("snapshot") or {})
    repo = str((event or {}).get("checkout_path") or "")
    base = str(snapshot.get("base_sha") or "") or None
    head = str(snapshot.get("head_sha") or "")
    if repo and head and _commit(repo, head) and (not base or _commit(repo, base)):
        files, added, removed = _range_files(repo, base, head)
        snapshot["changed_files"] = list(files)
        snapshot["lines_added"] = added
        snapshot["lines_removed"] = removed
        snapshot["evidence_available"] = True
    else:
        snapshot["changed_files"] = []
        snapshot["evidence_available"] = False
    payload["snapshot"] = snapshot
    return payload


def format_work_receipt(receipt: WorkReceipt | dict[str, Any]) -> str:
    payload = receipt.to_json() if isinstance(receipt, WorkReceipt) else receipt
    objective = payload.get("objective") or {}
    snapshot = payload.get("snapshot") or {}
    event = payload.get("event") or {}
    checks = payload.get("verifications") or []
    workflow = payload.get("workflow") or {}
    lines = ["AIWatcher Delivery Review", ""]
    objective_text = str(objective.get("text") or "Unknown; confirm before sharing.")
    if objective.get("provenance") == "inferred" and objective.get("text"):
        objective_text += "\nInferred from a linked session; confirm before sharing."
    lines.extend(("Objective", objective_text, ""))
    destination = event.get("pull_request_url") or event.get("remote_ref") or snapshot.get("upstream") or "remote status unknown"
    lines.extend((
        "Delivered",
        f"{len(snapshot.get('commit_shas') or [])} commit(s), {snapshot.get('changed_file_count') or 0} file(s), "
        f"+{snapshot.get('lines_added') or 0}/-{snapshot.get('lines_removed') or 0} on "
        f"{snapshot.get('branch') or 'detached HEAD'}; destination: {destination}.",
        "",
        "Verification",
    ))
    exact = [row for row in checks if row.get("exact_state")]
    if exact:
        lines.extend(
            f"- {row.get('runner')}: {row.get('status')} ({row.get('scope')})"
            for row in exact[:5]
        )
    else:
        lines.append("- No exact-state verification recorded.")
    lines.extend(("", "Workflow"))
    if workflow.get("session_count"):
        lines.append(
            f"{workflow.get('session_count')} linked session(s), {workflow.get('user_turns')} user turn(s), "
            f"{workflow.get('model_calls')} model call(s), {workflow.get('tool_calls')} tool call(s)."
        )
        if workflow.get("longest_observed_request_seconds") is not None:
            lines.append(
                f"Longest observed request: {float(workflow['longest_observed_request_seconds']) / 60:.1f} min. "
                "This is request timing, not developer time."
            )
    else:
        lines.append("No session could be linked exactly to the delivered commits.")
    attention = payload.get("attention") or []
    lines.extend(("", "Attention"))
    lines.extend(f"- {item}" for item in attention[:6])
    return "\n".join(lines).strip() + "\n"
