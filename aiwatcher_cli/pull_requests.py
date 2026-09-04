"""Pull requests as task milestones, read through the GitHub CLI.

A commit says work landed locally; a PR says it was put in front of someone.
Nothing local records PRs, so this asks `gh` -- only for the user's own PRs,
only per repository, and only when `gh` is installed and signed in. When it is
not, the answer is "not measurable" with the reason, never an empty list
pretending nothing was opened.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

GH_TIMEOUT_SECONDS = 8
GH_LIST_LIMIT = 100
CACHE_TTL_SECONDS = 300


@dataclass
class PullRequestLookup:
    available: bool
    reason: str | None = None
    pull_requests: list[dict[str, Any]] = field(default_factory=list)


# {repo_root: (fetched_at_monotonic, lookup)}
_CACHE: dict[str, tuple[float, PullRequestLookup]] = {}


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def list_pull_requests(repo_root: str, *, now: float | None = None) -> PullRequestLookup:
    """The user's own PRs on this repository, newest first, cached for a few minutes."""
    stamp = time.monotonic() if now is None else now
    cached = _CACHE.get(repo_root)
    if cached and stamp - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    lookup = _fetch(repo_root)
    _CACHE[repo_root] = (stamp, lookup)
    return lookup


def _fetch(repo_root: str) -> PullRequestLookup:
    if not shutil.which("gh"):
        return PullRequestLookup(False, "GitHub CLI (gh) is not installed, so pull requests cannot be linked.")
    try:
        completed = subprocess.run(
            [
                "gh", "pr", "list", "--state", "all", "--author", "@me", "--limit", str(GH_LIST_LIMIT),
                "--json", "number,title,url,headRefName,createdAt,mergedAt,closedAt,state",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PullRequestLookup(False, f"gh could not be run here: {exc}")
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip().splitlines()
        reason = message[0] if message else f"gh exited with {completed.returncode}"
        return PullRequestLookup(False, f"gh could not list pull requests: {reason[:160]}")
    try:
        rows = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        return PullRequestLookup(False, "gh returned something that was not JSON.")
    pull_requests: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("number"), int):
            continue
        pull_requests.append(
            {
                "number": row["number"],
                "title": str(row.get("title") or "")[:120],
                "url": row.get("url"),
                "branch": row.get("headRefName"),
                "opened_at": row.get("createdAt"),
                "merged_at": row.get("mergedAt"),
                "closed_at": row.get("closedAt"),
                "state": str(row.get("state") or "").lower(),
                "repo_root": repo_root,
            }
        )
    return PullRequestLookup(True, None, pull_requests)


def pull_request_opened_at(pull_request: dict[str, Any]) -> datetime | None:
    return _parse(pull_request.get("opened_at"))
