"""Evidence-backed delivery reviews for individual developers.

The rich objects in this module may contain transient, local-only text used to
compose a review.  ``to_persisted_json`` is the storage boundary: it removes
objective text and file paths so AIWatcher's private state never becomes a
second copy of prompts or source-tree metadata.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


DELIVERY_SCHEMA_VERSION = 1
PROVENANCE_VALUES = {"observed", "user_confirmed", "inferred", "unavailable"}
CONFIDENCE_VALUES = {"high", "medium", "low", "none"}
DELIVERY_KINDS = {"explicit_review", "push", "pull_request"}
DELIVERY_STATUSES = {"confirmed", "candidate", "attempted", "unknown"}
VERIFICATION_STATUSES = {"passed", "failed", "incomplete", "result unknown"}
VERIFICATION_SCOPES = {"project_default", "named_check", "targeted", "unknown"}
CONTRIBUTION_STRENGTHS = {"exact", "candidate", "unknown"}
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _bounded(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.strip().split())
    return clean[:limit] or None


def _enum(value: object, allowed: set[str], fallback: str) -> str:
    clean = str(value or "").strip().lower()
    return clean if clean in allowed else fallback


def _sha(value: object) -> str | None:
    clean = str(value or "").strip()
    return clean.lower() if _SHA.fullmatch(clean) else None


def _stamp(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest()


@dataclass(frozen=True)
class ObjectiveClaim:
    provenance: str = "unavailable"
    confidence: str = "none"
    text: str | None = None
    source_hash: str | None = None
    confirmed_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance", _enum(self.provenance, PROVENANCE_VALUES, "unavailable"))
        object.__setattr__(self, "confidence", _enum(self.confidence, CONFIDENCE_VALUES, "none"))
        text = _bounded(self.text, 2_000)
        object.__setattr__(self, "text", text)
        source_hash = _bounded(self.source_hash, 128)
        if text and not source_hash:
            source_hash = _hash(text)
        object.__setattr__(self, "source_hash", source_hash)
        object.__setattr__(self, "confirmed_at", _stamp(self.confirmed_at))
        if self.provenance == "user_confirmed" and not text:
            raise ValueError("a user-confirmed objective requires text")
        if self.provenance == "unavailable" and text:
            raise ValueError("an unavailable objective cannot carry text")

    def to_json(self, *, include_text: bool = True) -> dict[str, Any]:
        return {
            "provenance": self.provenance,
            "confidence": self.confidence,
            "text": self.text if include_text else None,
            "source_hash": self.source_hash,
            "confirmed_at": self.confirmed_at,
        }


@dataclass(frozen=True)
class DeliveryEvent:
    event_id: str
    kind: str
    status: str
    observed_at: str
    repository_id: str
    checkout_id: str
    head_sha: str
    source: str
    session_id: str | None = None
    source_id: str | None = None
    remote: str | None = None
    remote_ref: str | None = None
    pull_request_url: str | None = None

    def __post_init__(self) -> None:
        for name, limit in (
            ("event_id", 160), ("repository_id", 160), ("checkout_id", 160),
            ("source", 80), ("session_id", 120), ("source_id", 160),
            ("remote", 160), ("remote_ref", 300), ("pull_request_url", 1_000),
        ):
            object.__setattr__(self, name, _bounded(getattr(self, name), limit))
        object.__setattr__(self, "kind", _enum(self.kind, DELIVERY_KINDS, "explicit_review"))
        object.__setattr__(self, "status", _enum(self.status, DELIVERY_STATUSES, "unknown"))
        object.__setattr__(self, "observed_at", _stamp(self.observed_at))
        object.__setattr__(self, "head_sha", _sha(self.head_sha))
        required = (self.event_id, self.observed_at, self.repository_id, self.checkout_id, self.head_sha, self.source)
        if not all(required):
            raise ValueError("delivery event requires stable identity, timestamp, head, and source")
        if self.kind == "pull_request" and self.status == "confirmed" and not self.pull_request_url:
            raise ValueError("a confirmed pull-request event requires a URL")

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class DeliverySnapshot:
    repository_id: str
    checkout_id: str
    branch: str | None
    base_sha: str | None
    head_sha: str
    upstream: str | None
    clean: bool | None
    commit_shas: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    lines_added: int | None = None
    lines_removed: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository_id", _bounded(self.repository_id, 160))
        object.__setattr__(self, "checkout_id", _bounded(self.checkout_id, 160))
        object.__setattr__(self, "branch", _bounded(self.branch, 300))
        object.__setattr__(self, "base_sha", _sha(self.base_sha))
        object.__setattr__(self, "head_sha", _sha(self.head_sha))
        object.__setattr__(self, "upstream", _bounded(self.upstream, 300))
        commits = tuple(item for item in (_sha(value) for value in self.commit_shas) if item)[:200]
        files = tuple(filter(None, (_bounded(value, 1_000) for value in self.changed_files)))[:500]
        object.__setattr__(self, "commit_shas", commits)
        object.__setattr__(self, "changed_files", files)
        for name in ("lines_added", "lines_removed"):
            value = getattr(self, name)
            object.__setattr__(self, name, max(0, int(value)) if isinstance(value, int) else None)
        if not self.repository_id or not self.checkout_id or not self.head_sha:
            raise ValueError("delivery snapshot requires repository, checkout, and head")
        if self.base_sha and self.base_sha == self.head_sha and self.commit_shas:
            raise ValueError("a zero-width delivery cannot contain commits")

    def to_json(self, *, include_paths: bool = True) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "checkout_id": self.checkout_id,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "upstream": self.upstream,
            "clean": self.clean,
            "commit_shas": list(self.commit_shas),
            "changed_files": list(self.changed_files) if include_paths else [],
            "changed_file_hashes": [],
            "changed_file_count": len(self.changed_files),
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
        }


@dataclass(frozen=True)
class VerificationClaim:
    runner: str
    status: str
    scope: str
    provenance: str
    exact_state: bool
    finished_at: str | None = None
    source_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "runner", _bounded(self.runner, 160))
        object.__setattr__(self, "status", _enum(self.status, VERIFICATION_STATUSES, "result unknown"))
        object.__setattr__(self, "scope", _enum(self.scope, VERIFICATION_SCOPES, "unknown"))
        object.__setattr__(self, "provenance", _enum(self.provenance, PROVENANCE_VALUES, "unavailable"))
        object.__setattr__(self, "finished_at", _stamp(self.finished_at))
        object.__setattr__(self, "source_id", _bounded(self.source_id, 160))
        if not self.runner:
            raise ValueError("verification claim requires a runner")
        if self.exact_state and self.provenance != "observed":
            raise ValueError("exact verification must be observed")

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ContributionEdge:
    session_id: str
    commit_shas: tuple[str, ...]
    strength: str
    provenance: str
    source_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _bounded(self.session_id, 120))
        object.__setattr__(self, "commit_shas", tuple(
            item for item in (_sha(value) for value in self.commit_shas) if item
        )[:200])
        object.__setattr__(self, "strength", _enum(self.strength, CONTRIBUTION_STRENGTHS, "unknown"))
        object.__setattr__(self, "provenance", _enum(self.provenance, PROVENANCE_VALUES, "unavailable"))
        object.__setattr__(self, "source_id", _bounded(self.source_id, 160))
        if not self.session_id:
            raise ValueError("contribution edge requires a session")
        if self.strength == "exact" and (not self.commit_shas or self.provenance != "observed"):
            raise ValueError("exact contribution requires observed commits")

    def to_json(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "commit_shas": list(self.commit_shas),
        }


@dataclass(frozen=True)
class WorkflowStats:
    session_count: int = 0
    user_turns: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    observed_request_seconds: float | None = None
    longest_observed_request_seconds: float | None = None
    observed_idle_gap_seconds: float | None = None
    coverage: str = "unavailable"

    def __post_init__(self) -> None:
        for name in ("session_count", "user_turns", "model_calls", "tool_calls"):
            value = getattr(self, name)
            object.__setattr__(self, name, max(0, int(value)) if isinstance(value, int) else 0)
        for name in (
            "observed_request_seconds", "longest_observed_request_seconds", "observed_idle_gap_seconds",
        ):
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                round(max(0.0, float(value)), 3) if isinstance(value, (int, float)) else None,
            )
        object.__setattr__(self, "coverage", _bounded(self.coverage, 160) or "unavailable")

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class WorkReceipt:
    receipt_id: str
    created_at: str
    event: DeliveryEvent
    snapshot: DeliverySnapshot
    objective: ObjectiveClaim = field(default_factory=ObjectiveClaim)
    verifications: tuple[VerificationClaim, ...] = ()
    contributions: tuple[ContributionEdge, ...] = ()
    workflow: WorkflowStats = field(default_factory=WorkflowStats)
    attention: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_id", _bounded(self.receipt_id, 160))
        object.__setattr__(self, "created_at", _stamp(self.created_at))
        object.__setattr__(self, "verifications", tuple(self.verifications)[:100])
        object.__setattr__(self, "contributions", tuple(self.contributions)[:200])
        object.__setattr__(self, "attention", tuple(
            filter(None, (_bounded(value, 500) for value in self.attention))
        )[:20])
        if not self.receipt_id or not self.created_at:
            raise ValueError("work receipt requires identity and timestamp")
        if self.event.repository_id != self.snapshot.repository_id:
            raise ValueError("event and snapshot repository identity differ")
        if self.event.checkout_id != self.snapshot.checkout_id:
            raise ValueError("event and snapshot checkout identity differ")
        if self.event.head_sha != self.snapshot.head_sha:
            raise ValueError("event and snapshot head differ")

    def to_json(self, *, include_objective_text: bool = True, include_paths: bool = True) -> dict[str, Any]:
        return {
            "schema_version": DELIVERY_SCHEMA_VERSION,
            "receipt_id": self.receipt_id,
            "created_at": self.created_at,
            "event": self.event.to_json(),
            "snapshot": self.snapshot.to_json(include_paths=include_paths),
            "objective": self.objective.to_json(include_text=include_objective_text),
            "verifications": [item.to_json() for item in self.verifications],
            "contributions": [item.to_json() for item in self.contributions],
            "workflow": self.workflow.to_json(),
            "attention": list(self.attention),
        }

    def to_persisted_json(self) -> dict[str, Any]:
        """Return the privacy-safe form allowed in ``local-state.json``."""
        return self.to_json(include_objective_text=False, include_paths=False)
