"""Commit receipt: what the change you just made cost.

The point AIWatcher has been building toward. Everything else reports after
the fact, on a dashboard you have to remember to open. A receipt arrives at
the moment the work lands, in the terminal you are already looking at, with
no navigation and nothing to remember.

What it can honestly say at commit time is narrow, and the wording holds to
it. The cost is known: it is the AI spend in this repo since the previous
change, which is a rule rather than a guess. Whether the change *stuck* is
not knowable yet -- nothing has had time to be rewritten -- so the receipt
never claims it. It reports how earlier work has held up instead, which is a
real answer to a slightly different question.

Every figure is compared against something. A dollar amount with no baseline
tells you nothing about whether to care.
"""

from __future__ import annotations

import os
import statistics
import subprocess
from datetime import datetime, timezone
from typing import Any, Sequence

from .ledger import Change, Ledger, build_ledger, repo_identity
from .scanner import LocalEvent

# How far back the comparison baseline reaches. Long enough that a median is
# not two data points, short enough that it reflects how you work now.
BASELINE_DAYS = 30

# The receipt's own window only has to cover the gap since the previous
# commit, and the lookback cap is 12h, so two days is generous. Kept separate
# from BASELINE_DAYS because widening the baseline must not widen this.
RECEIPT_DAYS = 2

# Below this a "$/line vs median" line is noise: a one-line typo fix has a
# spectacular per-line cost and means nothing.
MIN_LINES_FOR_RATE = 10


def _git(repo: str, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            check=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def rewrite_in_progress(repo: str) -> bool:
    """True while a rebase, merge, cherry-pick or revert is running.

    post-commit fires once per commit a rebase replays, so a ten-commit
    rebase would print ten receipts for work that was already reported when
    it was first written. Git leaves these markers in place for exactly this
    kind of check.
    """
    git_dir = _git(repo, ["rev-parse", "--git-dir"])
    if not git_dir:
        return False
    if not os.path.isabs(git_dir):
        git_dir = os.path.join(repo, git_dir)
    markers = (
        "rebase-merge", "rebase-apply",
        "CHERRY_PICK_HEAD", "MERGE_HEAD", "REVERT_HEAD", "BISECT_LOG",
    )
    return any(os.path.exists(os.path.join(git_dir, name)) for name in markers)


def _find_change(ledger: Ledger, sha: str) -> Change | None:
    for change in ledger.changes:
        if change.sha == sha[:12] or sha.startswith(change.sha):
            return change
    return None


def _rates(changes: Sequence[Change], *, exclude_sha: str | None = None) -> dict[str, Any]:
    """Median cost and $/line across changes worth comparing against.

    Tiny changes are excluded: a one-line typo fix has a spectacular per-line
    cost and would drag a median that exists to answer "is this one unusual".
    """
    others = [
        change for change in changes
        if change.sha != exclude_sha
        and change.cost_usd > 0
        and change.lines_changed >= MIN_LINES_FOR_RATE
    ]
    rates = [change.usd_per_line for change in others if change.usd_per_line is not None]
    costs = [change.cost_usd for change in others]
    return {
        "median_usd_per_line": round(statistics.median(rates), 6) if rates else None,
        "median_cost_usd": round(statistics.median(costs), 6) if costs else None,
        "changes": len(others),
    }


def compute_repo_baselines(
    events: Sequence[LocalEvent],
    *,
    days: int = BASELINE_DAYS,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-repo comparison baselines, keyed by repo identity.

    Expensive enough to belong off the hot path: a month of ledger history is
    most of a second, and the receipt runs after every commit. Keyed by repo
    identity rather than path so any clone of a repo finds its own baseline.
    """
    ledger = build_ledger(events, days=days, now=now)
    by_repo: dict[str, list[Change]] = {}
    for change in ledger.changes:
        by_repo.setdefault(repo_identity(change.repo), []).append(change)
    return {
        identity: {**_rates(changes), "window_days": days}
        for identity, changes in by_repo.items()
    }


def build_commit_receipt(
    repo: str,
    sha: str,
    events: Sequence[LocalEvent],
    *,
    now: datetime | None = None,
    baseline_days: int = BASELINE_DAYS,
    baselines: dict[str, Any] | None = None,
    window_days: int = RECEIPT_DAYS,
) -> dict[str, Any]:
    """Everything the receipt for one commit can honestly report.

    Only `window_days` of history is walked, because that is all the commit's
    own cost needs: spend banks against the next commit within 12h, so two days
    is already generous. The comparison baseline comes from `baselines`, which
    the caller reads from cache -- computing it here would put a month of
    ledger history inside a post-commit hook.

    Passing `baselines=None` recomputes it, which is correct but slow; that
    path exists for tests and for the first run before any cache is written.

    Returns `available: False` with a reason rather than an empty receipt, so
    the caller can stay quiet instead of printing a row of dashes.
    """
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ledger = build_ledger(events, days=window_days, now=stamp, only_repo=repo)
    change = _find_change(ledger, sha)
    if change is None:
        return {
            "available": False,
            "reason": (
                f"No commit {sha[:12]} in the last {window_days} days of this repo's "
                "ledger, or it was written by someone else."
            ),
            "sha": sha[:12],
        }

    if baselines is None:
        baselines = compute_repo_baselines(events, days=baseline_days, now=stamp)
    entry = baselines.get(repo_identity(repo)) if isinstance(baselines, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    median_rate = entry.get("median_usd_per_line")
    median_cost = entry.get("median_cost_usd")

    # A commit with no observed spend has no rate to compare. Reporting 0.0
    # would place it infinitely below the median rather than saying nothing.
    rate = (
        change.usd_per_line
        if change.cost_usd > 0 and change.lines_changed >= MIN_LINES_FOR_RATE
        else None
    )
    ratio = None
    if rate:
        ratio = rate / float(median_rate) if median_rate else None

    return {
        "available": True,
        "reason": None,
        "sha": change.sha,
        "subject": change.subject,
        "repo": change.repo,
        "cost_usd": round(change.cost_usd, 6),
        "lines_added": change.lines_added,
        "lines_removed": change.lines_removed,
        "lines_changed": change.lines_changed,
        "files_changed": change.files_changed,
        "event_count": change.event_count,
        "tools": change.tools,
        "models": change.models,
        "was_rewritten": change.was_rewritten,
        "usd_per_line": round(rate, 6) if rate is not None else None,
        "median_usd_per_line": float(median_rate) if median_rate is not None else None,
        "median_cost_usd": float(median_cost) if median_cost is not None else None,
        "rate_ratio": round(ratio, 2) if ratio is not None else None,
        "baseline_changes": entry.get("changes") or 0,
        "baseline_days": entry.get("window_days") or baseline_days,
        "baseline_cached": bool(entry),
        # Spend in this repo still carrying no commit. Named here because the
        # moment after a commit is when it is most actionable: it is either
        # about to bank against the next one, or it never will.
        "unbanked_usd": round(ledger.unbanked_usd, 6),
        "unbanked_window_days": window_days,
    }


def survival_note(receipt: dict[str, Any], survival: dict[str, Any] | None) -> str | None:
    """How earlier work has held up.

    Deliberately not about the commit just made: nothing has had time to be
    rewritten, so any survival figure for it would be a statement about the
    clock. Answering for older work is the honest substitute.
    """
    if not receipt.get("available") or not isinstance(survival, dict):
        return None
    if not survival.get("available"):
        return None
    pct = survival.get("survival_pct")
    measured = survival.get("changes_measured")
    if pct is None or not measured:
        return None
    return (
        f"Earlier work: {pct:.0f}% of the lines in your last {measured} judged "
        f"changes are still standing (a floor -- reformatting moves the blame)."
    )


def _money(value: float) -> str:
    return f"${value:,.2f}"


def format_receipt(receipt: dict[str, Any], *, note: str | None = None) -> str:
    """The terminal form. Narrow enough to sit under a commit without shouting.

    ASCII only, and deliberately. This is printed from a git hook, by a
    short-lived process whose stdout encoding is whatever the host terminal
    reports -- a middle dot or an em dash raises UnicodeEncodeError under
    cp1252 and mangles the receipt in exactly the place it is meant to be read.
    """
    if not receipt.get("available"):
        return ""

    lines: list[str] = []
    lines.append(f"AIWatcher receipt  {receipt['sha']}  {receipt['subject'][:48]}")

    churn = f"+{receipt['lines_added']}/-{receipt['lines_removed']}"
    cost = _money(float(receipt["cost_usd"]))
    if receipt["cost_usd"] <= 0:
        lines.append(f"  This change    no AI spend observed  |  {churn} lines")
    else:
        detail = f"  This change    {cost}  |  {churn} lines"
        if receipt["usd_per_line"] is not None:
            detail += f"  |  {_money(float(receipt['usd_per_line']))}/line"
        lines.append(detail)
        if receipt["event_count"]:
            lines.append(
                f"                 {receipt['event_count']} model calls"
                + (f"  |  {', '.join(receipt['tools'])}" if receipt["tools"] else "")
            )

    ratio = receipt.get("rate_ratio")
    if ratio and receipt.get("median_usd_per_line"):
        median = _money(float(receipt["median_usd_per_line"]))
        if ratio >= 1.15:
            verdict = f"{ratio:.1f}x your usual"
        elif ratio <= 0.87:
            verdict = f"{1 / ratio:.1f}x cheaper than usual"
        else:
            verdict = "about your usual"
        lines.append(
            f"  vs baseline    {median}/line median over {receipt['baseline_days']} days "
            f"({receipt['baseline_changes']} changes) -- this one is {verdict}"
        )
    elif receipt["cost_usd"] > 0 and receipt["lines_changed"] < MIN_LINES_FOR_RATE:
        lines.append(
            f"  vs baseline    too few lines to rate per-line "
            f"(under {MIN_LINES_FOR_RATE})"
        )
    elif receipt["cost_usd"] > 0 and not receipt.get("baseline_cached"):
        lines.append(
            "  vs baseline    not computed yet -- run `aiwatcher today` to build one"
        )

    unbanked = float(receipt.get("unbanked_usd") or 0)
    if unbanked >= 1:
        window = receipt.get("unbanked_window_days")
        lines.append(
            f"  Still unbanked {_money(unbanked)} spent here in the last {window}d "
            "has no commit behind it"
        )
    if note:
        lines.append(f"  {note}")
    return "\n".join(lines)
