"""Local handoff capsules for continuing AI work safely.

The capsule is designed for one developer moving work into a fresh Claude,
Codex, Cursor, or other coding-agent session. It summarizes metadata, outcome
evidence, and next-step guardrails without reading source diffs or uploading
anything.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

from .local_state import issue_brief_token, recent_decisions
from .outcome_evidence import build_outcome_evidence
from .pricing import is_subscription_model
from .scanner import LocalEvent, LocalSession, segment_session_by_prompt
from .session_health import analyze_session_health


MAX_HANDOFF_WORDS = 350

_BRIEF_SECTION_PRIORITY = {
    "Task context": 0,
    "Most recent commit message": 2,
    "Open questions and uncertainty": 1,
    "Source of truth to load first": 1,
    "Decisions and constraints": 1,
    "Acceptance criteria and guardrails": 1,
    "Evidence carried forward": 1,
    "Source session identity": 2,
    "Completed work and current state": 2,
    "Completed work": 2,
    "Current state": 2,
    "Objective and context": 4,
    "Objective": 4,
    "Working checkout": 4,
    "Verification and test signals": 4,
    "First action": 4,
}


def bound_handoff_words(text: str, *, max_words: int = MAX_HANDOFF_WORDS) -> str:
    """Fit a handoff to a word budget by dropping lower-value evidence first."""
    lines = text.strip().splitlines()
    if len(text.split()) <= max_words:
        return text.strip()

    section = ""
    entries: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    headings = set(_BRIEF_SECTION_PRIORITY)
    for index, line in enumerate(lines):
        stripped = line.strip()
        heading = next((name for name in headings if stripped == name or stripped.startswith(name + " (")), None)
        if heading:
            section = heading
            entries.append({"index": index, "section": section, "heading": True, "ordinal": -1})
            continue
        ordinal = counts.get(section, 0)
        if stripped:
            counts[section] = ordinal + 1
        entries.append({"index": index, "section": section, "heading": False, "ordinal": ordinal})

    def immutable(entry: dict[str, object]) -> bool:
        return entry["section"] == "Verification and test signals" and entry["ordinal"] == 0

    keep_limits = {0: 0, 1: 1, 2: 6, 4: 7}
    removable: list[tuple[int, int, int]] = []
    for entry in entries:
        index = int(entry["index"])
        if entry["heading"] or not lines[index].strip():
            continue
        if immutable(entry) or (
            entry["section"] == "Most recent commit message" and lines[index].rstrip().endswith("...")
        ):
            continue
        priority = _BRIEF_SECTION_PRIORITY.get(str(entry["section"]), 3)
        if int(entry["ordinal"]) >= keep_limits.get(priority, 2):
            removable.append((priority, -index, index))
    removed: set[int] = set()
    word_count = len(text.split())
    for _, _, index in sorted(removable):
        if word_count <= max_words:
            break
        removed.add(index)
        word_count -= len(lines[index].split())

    if word_count > max_words:
        retained: list[tuple[int, int, int]] = []
        for entry in entries:
            index = int(entry["index"])
            if index in removed or entry["heading"] or not lines[index].strip():
                continue
            if immutable(entry) or (
                entry["section"] == "Most recent commit message" and lines[index].rstrip().endswith("...")
            ):
                continue
            priority = _BRIEF_SECTION_PRIORITY.get(str(entry["section"]), 3)
            retained.append((priority, -len(lines[index].split()), index))
        for _, _, index in sorted(retained):
            if word_count <= max_words:
                break
            words = lines[index].split()
            reducible = max(0, len(words) - 8)
            if not reducible:
                continue
            remove = min(reducible, word_count - max_words)
            lines[index] = " ".join(words[:len(words) - remove]).rstrip(".,;:") + "..."
            word_count -= remove

    if word_count > max_words:
        final_removable: list[tuple[int, int, int]] = []
        for entry in entries:
            index = int(entry["index"])
            if index in removed or entry["heading"] or not lines[index].strip() or immutable(entry):
                continue
            priority = _BRIEF_SECTION_PRIORITY.get(str(entry["section"]), 3)
            final_removable.append((priority, -index, index))
        for _, _, index in sorted(final_removable):
            if word_count <= max_words:
                break
            removed.add(index)
            word_count -= len(lines[index].split())

    if word_count > max_words:
        for entry in entries:
            if not immutable(entry):
                continue
            index = int(entry["index"])
            words = lines[index].split()
            if len(words) > 40:
                remove = min(len(words) - 40, word_count - max_words)
                lines[index] = " ".join(words[:len(words) - remove]).rstrip(".,;:") + "..."
                word_count -= remove

    result = "\n".join(line for index, line in enumerate(lines) if index not in removed).strip()
    if len(result.split()) > max_words:
        # This can only happen if the fixed headings plus one bounded
        # verification line exceed the budget. Never return an oversized brief.
        result = " ".join(result.split()[:max_words])
    return result


def _money(value: float) -> str:
    if value == 0:
        return "$0.00"
    if abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def _compact_int(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _short(value: str | None, limit: int = 900) -> str | None:
    if not value:
        return None
    text = value.strip()
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    # Prefer dropping the last, possibly-partial line entirely (so a bullet
    # list doesn't end on a fragment) rather than just avoiding a mid-word
    # cut. Only fall back to a word boundary when there's no newline close
    # enough to be worth it (e.g. a single long paragraph with no lines).
    newline_break = truncated.rfind("\n")
    if newline_break > limit * 0.5:
        truncated = truncated[:newline_break]
    else:
        space_break = truncated.rfind(" ")
        if space_break > limit * 0.6:
            truncated = truncated[:space_break]
    return truncated.rstrip() + "..."


def _stamp(session: LocalSession) -> str:
    stamp = session.updated_at or session.started_at
    if not stamp:
        return "unknown"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone().isoformat(timespec="minutes")


def _safe_project_path(path: str | None) -> tuple[str, bool]:
    """Return a project label that is safe to put into a handoff prompt.

    A bare filesystem root is a scanner attribution failure, not a project. If
    we paste "/" into a fresh agent prompt, the next agent may inspect the
    whole machine. Use an explicit unknown marker instead and make the brief
    ask for project confirmation before editing.
    """
    if not path:
        return "unknown project", False
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        resolved = Path(path).expanduser()
    if resolved.parent == resolved:
        return "unknown project", False
    return str(resolved).replace("\\", "/"), True


def _display_path(path: str | None, fallback: str) -> str:
    return str(path or fallback).replace("\\", "/")


def _short_session_id(session_id: str | None) -> str:
    if not session_id:
        return "unknown"
    value = str(session_id)
    if len(value) <= 16:
        return value
    return f"{value[:8]}...{value[-4:]}"


def _runtime_identity_lines(
    session: LocalSession,
    runtime_attachment: dict[str, object] | None,
) -> tuple[list[str], str, str]:
    """Return user-facing identity lines for a Fresh Start prompt.

    The Fresh Start brief is pasted into a different chat, so it must lead with
    what AIWatcher can and cannot prove about the source session. Git evidence
    is valuable, but the session identity is the thing the user is trying to
    preserve.
    """
    attachment = runtime_attachment or {}
    identity_label = str(attachment.get("identity_label") or "Historical log only")
    identity_reason = str(
        attachment.get("identity_reason")
        or "AIWatcher found a local session log, but has not verified a live AI chat for this source session."
    )
    exact_return_label = str(attachment.get("exact_return_label") or "Exact chat unavailable")
    exact_return_reason = str(
        attachment.get("exact_return_reason")
        or "No verified app window, terminal pane, or host deep link is available for this exact session."
    )
    confidence = str(attachment.get("confidence") or "low")
    surface = str(attachment.get("surface") or session.surface or "unknown")
    app_name = str(attachment.get("app_name") or "").strip()
    pid = attachment.get("pid")

    lines = [
        f"- Identity confidence: {identity_label} ({confidence})",
        f"- Source session id: {session.session_id} ({_short_session_id(session.session_id)})",
        f"- Source tool/surface: {session.tool} / {surface}",
        f"- Source model: {session.model or 'unknown'}",
        f"- Last observed activity: {_stamp(session)}",
        f"- Identity note: {identity_reason}",
        f"- Return capability: {exact_return_label}",
        f"- Return note: {exact_return_reason}",
    ]
    if app_name:
        lines.append(f"- App/workspace hint: {app_name}")
    if isinstance(pid, int):
        lines.append(f"- Matched process id: {pid}")
    return lines, identity_label, exact_return_label


def _usage_pressure_label(session: LocalSession) -> str:
    tokens = session.tokens_in + session.tokens_out
    value = _money(session.cost_usd)
    if tokens > 0 and session.cost_usd == 0:
        value = f"{value} API-equivalent value (subscription-limited, local, or unavailable pricing)"
    else:
        value = f"{value} API-equivalent value"
    return (
        f"{_compact_int(tokens)} tokens, {session.agent_calls} model calls, "
        f"{session.tool_calls} tool calls, {value}"
    )


HandoffTarget = Literal["generic", "claude", "codex", "cursor", "vscode"]
HandoffType = Literal["coding", "product", "review", "bugbash", "investigation", "general"]


TARGET_LABELS: dict[str, str] = {
    "generic": "Claude/Codex/Cursor",
    "claude": "Claude",
    "codex": "Codex",
    "cursor": "Cursor",
    "vscode": "VS Code",
}


HANDOFF_TYPE_LABELS: dict[str, str] = {
    "coding": "Coding continuation",
    "product": "Product/strategy continuation",
    "review": "Review continuation",
    "bugbash": "Bug bash continuation",
    "investigation": "Investigation continuation",
    "general": "General work continuation",
}


TYPE_PROFILES: dict[str, dict[str, object]] = {
    "coding": {
        "session_label": "AI coding session",
        "purpose": [
            "Preserve momentum from the previous session without replaying its bloated context.",
            "Reconstruct the work from disk, recent commits, changed files, decisions, and the evidence below after confirming the source session identity.",
            "Pick one smallest safe next checkpoint and continue only that checkpoint.",
        ],
        "checkpoint": [
            "Run `git status --short` and inspect only the files listed in workspace evidence first.",
            "Summarize what appears done, what remains uncertain, and propose one smallest next checkpoint.",
            "Continue only after that checkpoint is clear; do not replay broad exploration from the old session.",
        ],
        "finish": "Report changed files, verification run, remaining uncertainty, and whether the result looks useful.",
    },
    "product": {
        "session_label": "product/strategy session",
        "purpose": [
            "Carry forward the product intent, decisions, constraints, and source-of-truth files.",
            "Re-read the source-of-truth materials before proposing scope or implementation.",
            "Produce a clear recommendation, spec, or implementation slice tied to acceptance criteria.",
        ],
        "checkpoint": [
            "Read the source-of-truth files first and restate the product thesis in your own words.",
            "List decisions already made, open questions, and constraints before changing direction.",
            "Propose one smallest useful next artifact or implementation slice.",
        ],
        "finish": "Report the decision/spec changes, evidence used, open questions, and recommended next step.",
    },
    "review": {
        "session_label": "review session",
        "purpose": [
            "Continue the review with the same evidence standard and without re-reading irrelevant history.",
            "Prioritize correctness, regressions, missing tests, product-fit gaps, and privacy/claim risks.",
            "Produce actionable findings before summaries.",
        ],
        "checkpoint": [
            "Identify the exact PR, branch, files, and source-of-truth docs before reviewing.",
            "Compare the change against strategy, tests, and current product behavior.",
            "Return findings ordered by severity with concrete file or scenario references.",
        ],
        "finish": "Report findings, residual risk, tests inspected or run, and whether the change should merge.",
    },
    "bugbash": {
        "session_label": "bug bash session",
        "purpose": [
            "Continue validation against the defined release bar, not a generic QA sweep.",
            "Use scenario IDs and acceptance criteria as the test oracle.",
            "Record bugs with severity, reproduction, expected behavior, and privacy/product impact.",
        ],
        "checkpoint": [
            "Open the bug-bash runbook or test cases before testing.",
            "Pick one phase or workflow and execute it end to end.",
            "File only observed failures; label unverified claims separately.",
        ],
        "finish": "Report pass/fail by scenario, bugs found, severity, and recommended release decision.",
    },
    "investigation": {
        "session_label": "investigation session",
        "purpose": [
            "Continue the investigation from observed facts, not hidden conversation memory.",
            "Separate confirmed evidence, likely causes, rejected hypotheses, and open questions.",
            "Choose the next narrow diagnostic step before changing code.",
        ],
        "checkpoint": [
            "Restate known facts and evidence sources before proposing a cause.",
            "List hypotheses already rejected so the new session does not repeat them.",
            "Run or propose one narrow diagnostic step that can change the conclusion.",
        ],
        "finish": "Report confirmed cause, evidence, fix or recommendation, and unresolved uncertainty.",
    },
    "general": {
        "session_label": "AI work session",
        "purpose": [
            "Preserve the user's goal, constraints, decisions, and useful evidence across a fresh session.",
            "Avoid redoing broad exploration from the previous session.",
            "Choose one smallest productive next step.",
        ],
        "checkpoint": [
            "Restate the objective, known facts, constraints, and uncertainty.",
            "Inspect the listed source-of-truth items before acting.",
            "Propose one smallest next step and continue only after it is clear.",
        ],
        "finish": "Report what changed, what was verified, what remains uncertain, and the next recommended step.",
    },
}


def _handoff_profile(handoff_type: str) -> dict[str, object]:
    return TYPE_PROFILES.get(handoff_type, TYPE_PROFILES["coding"])


def _clean_user_items(items: Sequence[str] | None, *, limit: int = 8, item_limit: int = 220) -> list[str]:
    cleaned: list[str] = []
    for item in items or []:
        text = " ".join(str(item).strip().split())
        if not text:
            continue
        shortened = _short(text, item_limit) or ""
        if shortened and shortened not in cleaned:
            cleaned.append(shortened)
        if len(cleaned) >= limit:
            break
    return cleaned


def _indefinite_article(label: str) -> str:
    """"an AI coding session", not "a AI coding session"."""
    return "an" if label[:1].lower() in "aeiou" else "a"


def _brief_memory_summary(
    *,
    project_label: str,
    project_reliable: bool,
    objective_text: str | None,
    profile: dict[str, object],
    evidence: object,
    decisions: Sequence[dict[str, object]],
    source_identity_label: str,
    exact_return_label: str,
    same_project_session_count: int,
) -> dict[str, list[str]]:
    """Build a human-readable handoff memo before the forensic evidence.

    Claude can summarize its own transcript. AIWatcher often cannot, so this
    section is deliberately framed as local evidence plus inference instead of
    pretending the previous chat text is available.
    """
    project_part = project_label if project_reliable else "an unconfirmed project"
    session_label = str(profile.get("session_label") or "AI work session")
    summary: list[str] = []
    if objective_text:
        summary.append(f"- You were trying to: {objective_text}")
    else:
        summary.append(
            f"- The exact user objective was not captured. This was {_indefinite_article(session_label)} {session_label} in {project_part}; "
            "use the evidence below to reconstruct the state, then ask one focused question to confirm the intended next outcome before editing."
        )
    summary.append(
        f"- Source session match: {source_identity_label}; return capability: {exact_return_label}."
    )
    if same_project_session_count > 1:
        summary.append(
            f"- {same_project_session_count} same-project sessions were observed, so verify this is the intended source before carrying context forward."
        )

    decision_lines: list[str] = []
    for decision in decisions:
        text = str(decision.get("summary") or "").strip()
        if text:
            decision_lines.append(f"- {text}")
        if len(decision_lines) >= 4:
            break
    if not decision_lines:
        decision_lines.append("- No explicit decision notes were found; infer decisions only from commits, changed files, and user confirmation.")

    current_state: list[str] = []
    commits = getattr(evidence, "commits", []) or []
    changed_files = getattr(evidence, "changed_files", []) or []
    tests = getattr(evidence, "tests", []) or []
    if commits:
        latest = commits[0]
        subject = str(latest.get("subject") or "").strip()
        commit_label = (
            "Latest session-bound commit"
            if getattr(evidence, "commit_attribution", "none") == "session_bound"
            else "Latest nearby commit candidate"
        )
        current_state.append(
            f"- {commit_label}: {latest.get('sha')}{(': ' + subject) if subject else ''}."
        )
    if changed_files:
        shown = ", ".join(str(item) for item in changed_files[:4])
        extra = f" and {len(changed_files) - 4} more" if len(changed_files) > 4 else ""
        current_state.append(f"- Working tree has {len(changed_files)} changed file(s): {shown}{extra}.")
    if tests:
        exact_tests = sum(1 for item in tests if item.get("attribution") == "session_bound")
        current_state.append(
            f"- {len(tests)} verification signal(s) were found"
            f" ({exact_tests} session-bound); inspect Git-state binding before claiming done."
        )
    if not current_state:
        current_state.append("- No nearby commits, changed files, or test artifacts were found; reconstruct from the repository state first.")

    checkpoint_items = list(profile.get("checkpoint") or [])
    open_items = [
        "- First confirm the source session identity and project are the work the user meant to continue.",
        f"- Next checkpoint: {checkpoint_items[0] if checkpoint_items else 'inspect the listed evidence and choose one smallest safe step.'}",
    ]
    if not objective_text:
        open_items.append(
            "- After inspecting the evidence, ask: `What outcome should I continue toward in this project?` "
            "Include likely options supported by the evidence instead of asking the user to retell the whole session."
        )

    files: list[str] = []
    for item in changed_files[:8]:
        files.append(f"- {item}")
    if not files:
        files.append("- No changed files detected yet; start with `git status --short` and `git diff --stat`.")

    return {
        "summary": summary,
        "decisions": decision_lines,
        "current_state": current_state,
        "open": open_items,
        "files": files,
    }


def _target_guidance(target: str) -> list[str]:
    if target == "codex":
        return [
            "Treat this as a fresh Codex session with no prior chat context.",
            "Inspect repo state before editing and keep the checkpoint narrow.",
        ]
    if target == "claude":
        return [
            "Treat this as a fresh Claude Code session with no prior chat context.",
            "Summarize current repo state before continuing the work.",
        ]
    if target == "cursor":
        return [
            "Treat this as a fresh Cursor composer thread for this project.",
            "Keep the edit scope to the files Cursor confirms are related.",
        ]
    if target == "vscode":
        return [
            "Treat this as a fresh VS Code assistant thread for this project.",
            "Use AIWatcher preflight again if you edit this brief materially.",
        ]
    return [
        "Treat this as a fresh AI coding session with no prior chat context.",
        "Keep the next session focused on one checkpoint.",
    ]


def build_handoff_capsule(
    session: LocalSession,
    events: Sequence[LocalEvent],
    *,
    outcome: str | None = None,
    include_prompt_excerpt: bool = False,
    target: HandoffTarget = "generic",
    handoff_type: HandoffType = "coding",
    objective: str | None = None,
    source_refs: Sequence[str] | None = None,
    constraints: Sequence[str] | None = None,
    acceptance_criteria: Sequence[str] | None = None,
    extra_warnings: Sequence[str] | None = None,
    related_workspaces: Sequence[str] | None = None,
    runtime_attachment: dict[str, object] | None = None,
    same_project_session_count: int = 1,
) -> dict[str, object]:
    """Build a structured handoff capsule for UI/API rendering.

    extra_warnings (e.g. a loop diagnosis from `watch`) are prepended so they
    lead the "why hand off now" list ahead of the generic health/cost checks.
    """
    evidence = build_outcome_evidence(session)
    health = analyze_session_health(session, events)
    segments = segment_session_by_prompt(session.source_path)
    costliest_prompt = None
    if include_prompt_excerpt and segments:
        by_cost = sorted(segments, key=lambda item: float(item.get("cost_usd") or 0), reverse=True)
        if by_cost:
            costliest_prompt = {
                "turn": by_cost[0].get("turn"),
                "cost_label": _money(float(by_cost[0].get("cost_usd") or 0)),
                "prompt_excerpt": _short(str(by_cost[0].get("prompt") or ""), 900),
            }

    warnings: list[str] = list(extra_warnings or [])
    if evidence.commit_attribution == "nearby_time_window":
        warnings.append(
            "Commit attribution is inferred from checkout timing, not an exact session receipt; confirm authorship before relying on it."
        )
    if evidence.command_evidence_coverage in {"opaque_codex_exec", "partial_opaque_codex_exec"}:
        warnings.append(
            "This Codex transcript has shell calls whose nested results are not structurally visible, so terminal verification may be incomplete."
        )
    if health:
        if health.severity != "healthy":
            detail = (
                f" — {health.bloat_ratio * 100:.0f}% of its spend went on replayed history"
                if health.bloat_measurable else ""
            )
            warnings.append(
                f"Context health is {health.severity}: latest turn used "
                f"{_compact_int(health.latest_turn_tokens)} input tokens{detail}."
            )
        if health.recommendations:
            extras = health.recommendations[:2]
            if health.severity != "healthy":
                # The line just above already gives the per-turn figure and the
                # severity. A recommendation restating them put the same fact in
                # two adjacent bullets -- "latest turn used 823.7k input tokens"
                # followed by "Context is 823,709 tokens/turn (critical)".
                compact = _compact_int(health.latest_turn_tokens)
                exact = str(health.latest_turn_tokens)
                extras = [
                    rec for rec in extras
                    if compact not in rec and exact not in rec.replace(",", "")
                ]
            warnings.extend(extras)
    if session.agent_calls >= 250:
        warnings.append(f"{session.agent_calls} model calls were observed; continue with a smaller checkpoint.")
    if session.tool_calls >= 80:
        warnings.append(f"{session.tool_calls} tool calls were observed; ask the next agent to inspect narrowly.")
    if session.cost_usd >= 5:
        warnings.append(f"{_money(session.cost_usd)} API-equivalent value was observed; avoid repeating broad exploration.")
    if not warnings:
        warnings.append("No urgent context or cost pressure was detected, but start with a concise status check.")

    target = target if target in TARGET_LABELS else "generic"
    handoff_type = handoff_type if handoff_type in HANDOFF_TYPE_LABELS else "coding"
    profile = _handoff_profile(handoff_type)
    target_guidance = _target_guidance(target)
    project_label, project_reliable = _safe_project_path(session.project_path)
    source_identity_lines, source_identity_label, exact_return_label = _runtime_identity_lines(
        session,
        runtime_attachment,
    )
    objective_text = _short(objective, 420) if objective else None
    source_ref_lines = _clean_user_items(source_refs)
    constraint_lines = _clean_user_items(constraints)
    acceptance_lines = _clean_user_items(acceptance_criteria)
    related = [
        item
        for item in dict.fromkeys(str(path) for path in (related_workspaces or []) if path)
        if item not in {project_label, session.project_path}
    ]

    commit_count_label = (
        "Session-bound commits"
        if evidence.commit_attribution == "session_bound"
        else "Nearby commits (time-window candidates)"
    )
    evidence_lines = [
        f"- Active checkout: {_display_path(evidence.checkout_path, project_label)}",
        f"- Branch/HEAD: {evidence.branch or 'unknown'} / {evidence.head or 'unknown'}",
        f"- {commit_count_label}: {len(evidence.commits)}",
        f"- Changed files: {len(evidence.changed_files)}",
        f"- Test artifacts: {len(evidence.tests)}",
    ]
    if evidence.upstream:
        evidence_lines.append(
            f"- Upstream state: {evidence.upstream}; {evidence.ahead or 0} commit(s) ahead, "
            f"{evidence.behind or 0} behind."
        )
    elif evidence.branch:
        evidence_lines.append("- Upstream state: no upstream branch is configured.")
    for commit in evidence.unpushed_commits[:5]:
        evidence_lines.append(f"  - Unpushed commit: {commit.get('sha')}: {commit.get('subject')}")
    shown_commits = evidence.commits[:3]
    for commit in shown_commits:
        subject = str(commit.get("subject") or "").strip()
        label = f"{commit.get('sha')}: {subject}" if subject else str(commit.get("sha"))
        evidence_lines.append(f"  - Commit: {label}")
    if len(evidence.commits) > len(shown_commits):
        evidence_lines.append(f"  - ...and {len(evidence.commits) - len(shown_commits)} more commit(s) (see git log)")
    shown_files = evidence.changed_files[:5]
    for changed_file in shown_files:
        evidence_lines.append(f"  - Changed file: {changed_file}")
    if len(evidence.changed_files) > len(shown_files):
        evidence_lines.append(
            f"  - ...and {len(evidence.changed_files) - len(shown_files)} more changed file(s) (see git status)"
        )
    if evidence.commits:
        evidence_lines.append(f"- Suggested check: git show {evidence.commits[0].get('sha')} --stat")
    evidence_lines.append("- Suggested check: git status --short")
    evidence_lines.append("- Suggested check: git diff --stat")
    if evidence.changed_files:
        evidence_lines.append(
            "- Changed files are workspace evidence, not proof that the source AI session created those edits."
        )
    if not evidence.commits and evidence.changed_files:
        evidence_lines.append(
            "- No nearby commit evidence was found; treat these edits as in-progress work and avoid overwriting local edits or changes."
        )
    if not project_reliable:
        evidence_lines.append(
            "- Project path was not reliable; confirm the intended repository before reading or editing files."
        )

    commit_message_lines: list[str] = []
    if evidence.commits:
        latest_commit = evidence.commits[0]
        body = _short(str(latest_commit.get("body") or ""), 600)
        if body:
            commit_message_lines = [
                "",
                f"Most recent commit message ({latest_commit.get('sha')})",
                body,
            ]

    decisions = recent_decisions(session.session_id, limit=5)
    decision_lines: list[str] = []
    if decisions:
        decision_lines = [
            "",
            "Decisions logged this session (self-reported, not verified against what actually happened)",
        ]
        for decision in decisions:
            summary = str(decision.get("summary") or "").strip()
            if not summary:
                continue
            decision_lines.append(f"- {summary}")
            reasoning = str(decision.get("reasoning") or "").strip()
            if reasoning:
                decision_lines.append(f"  Why: {reasoning}")
            rejected = decision.get("alternatives_rejected") or []
            if rejected:
                decision_lines.append(f"  Rejected: {', '.join(str(item) for item in rejected)}")

    task_context_lines: list[str] = []
    if include_prompt_excerpt and costliest_prompt and costliest_prompt.get("prompt_excerpt"):
        task_context_lines = [
            "",
            f"Task context (your own prompt, turn #{costliest_prompt.get('turn')}, "
            f"{costliest_prompt.get('cost_label')} — review before pasting elsewhere)",
            str(costliest_prompt.get("prompt_excerpt")),
        ]

    warning_lines = [f"- {item}" for item in warnings[:5]]
    done_lines: list[str] = []
    if evidence.changed_files:
        done_lines.append(
            f"- The workspace has {len(evidence.changed_files)} changed file(s) on disk; treat them as possible in-progress context, not proof from this source session."
        )
    if decisions:
        done_lines.append("- Local decision notes exist; review them before changing direction.")
    if not done_lines and not evidence.commits:
        done_lines.append("- No commit, changed-file, or test evidence was found; reconstruct the state carefully.")

    uncertainty_lines: list[str] = []
    if source_identity_label != "Exact active session":
        uncertainty_lines.append(
            "- AIWatcher has not verified the exact active chat. Confirm this source session matches the work the user intended before editing."
        )
    if same_project_session_count > 1:
        uncertainty_lines.append(
            f"- AIWatcher saw {same_project_session_count} recent session(s) for this same project; repository evidence may include work from another chat or manual edits."
        )
    if related:
        uncertainty_lines.append(
            "- Other active AIWatcher sessions are in related workspaces; confirm which repo owns the next checkpoint."
        )
    if evidence.changed_files:
        uncertainty_lines.append(
            "- Git working-tree changes may come from another AI chat or manual edits in the same repository."
        )
    if not project_reliable:
        uncertainty_lines.extend([
            "- AIWatcher could not confidently identify the project path.",
            "- Ask the user to confirm the repository/path before editing.",
        ])
    completed_verification = any(
        item.get("attribution") == "session_bound"
        and item.get("completion_state") == "completed"
        and item.get("status") in {"passed", "failed"}
        for item in evidence.tests
    )

    checkpoint_lines = [
        "- First verify that the source session identity above matches the work the user meant to continue.",
        "- Continue in the same workspace/repository unless the user explicitly asks for a duplicate checkout or new worktree.",
        *[f"- {item}" for item in profile["checkpoint"]],
    ]
    if not project_reliable:
        checkpoint_lines.insert(0, "- Ask the user to confirm the repository/path before editing.")

    source_section: list[str] = []
    if source_ref_lines:
        source_section = [
            "",
            "Source of truth to load first",
            *[f"- {item}" for item in source_ref_lines],
        ]

    memory_summary = _brief_memory_summary(
        project_label=project_label,
        project_reliable=project_reliable,
        objective_text=objective_text,
        profile=profile,
        evidence=evidence,
        decisions=decisions,
        source_identity_label=source_identity_label,
        exact_return_label=exact_return_label,
        same_project_session_count=same_project_session_count,
    )

    next_brief = bound_handoff_words("\n".join([
        "AIWatcher Fresh Start brief",
        "",
        f"Start a fresh {profile['session_label']} from this handoff; the previous chat is unavailable.",
        "Use the exact checkout and evidence below. Do not invent intent or completed work.",
        "",
        "Objective and context",
        *memory_summary["summary"],
        f"- Objective status: {'confirmed from user input' if objective_text else 'not captured; confirmation required before edits'}.",
        "",
        "Working checkout",
        f"- Project: {project_label}",
        f"- Project confidence: {'reliable' if project_reliable else 'unconfirmed'}",
        f"- Path: {_display_path(evidence.checkout_path, project_label)}",
        f"- Branch/HEAD: {evidence.branch or 'unknown'} / {evidence.head or 'unknown'}",
        *(
            [f"- Upstream: {evidence.upstream}; {evidence.ahead or 0} ahead, {evidence.behind or 0} behind."]
            if evidence.upstream else ["- Upstream: not configured or not observed."]
        ),
        f"- Working tree: {'has local changes' if evidence.dirty is True else 'clean' if evidence.dirty is False else 'state unknown'}.",
        *[
            f"- {'Unpushed' if evidence.upstream else 'Local commit ahead of observed base'}: "
            f"{item.get('sha')} {item.get('subject')}"
            for item in evidence.unpushed_commits[:1]
        ],
        *(
            [f"- ...and {len(evidence.unpushed_commits) - 1} more local commit(s) ahead; inspect `git log`."]
            if len(evidence.unpushed_commits) > 1 else []
        ),
        "",
        "Completed work and current state",
        *done_lines,
        *[f"- Commit: {item.get('sha')}: {item.get('subject')}" for item in evidence.commits[:4]],
        *([f"- ...and {len(evidence.commits) - 4} more commit(s); inspect `git log`." ] if len(evidence.commits) > 4 else []),
        *[f"- Changed file: {path}" for path in evidence.changed_files[:8]],
        *([f"- ...and {len(evidence.changed_files) - 8} more changed file(s); inspect `git status --short`." ] if len(evidence.changed_files) > 8 else []),
        *(
            ["- No nearby commit evidence was found; avoid overwriting local edits until their owner and intent are clear."]
            if not evidence.commits and evidence.changed_files else []
        ),
        *commit_message_lines,
        "",
        "Verification and test signals",
        *(
            [
                f"- {item.get('artifact') or item.get('name')}: "
                f"{item.get('status') or item.get('updated_at') or 'observed'}"
                f"{' (current for this Git state)' if item.get('current') is True else ' (stale; Git state changed)' if item.get('current') is False else ''}"
                f"{' [session-bound]' if item.get('attribution') == 'session_bound' else ' [time-window candidate; may belong to another session]' if item.get('attribution') == 'inferred_time_window' else ''}"
                for item in evidence.tests[:6]
            ]
            if evidence.tests else []
        ),
        *([] if completed_verification else ["- No session-bound completed verification was observed; do not claim the prior work is verified."]),
        "",
        "Decisions and constraints",
        *(["- Decisions below are self-reported and not verified against what actually happened."] if decisions else []),
        *(decision_lines[2:] if decisions else memory_summary["decisions"][:5]),
        *([f"- {item}" for item in constraint_lines] if constraint_lines else []),
        "",
        "Open questions and uncertainty",
        *uncertainty_lines[:4],
        "",
        "First action",
        *memory_summary["open"][1:2],
        *(memory_summary["files"][:5] if evidence.changed_files else []),
        *([f"- Inspect `git show {evidence.commits[0].get('sha')} --stat` before changing landed work."] if evidence.commits else []),
        *(["- Ask one focused outcome question before editing because the objective remains unknown."] if not objective_text else []),
        "",
        "Source session identity",
        *source_identity_lines[:4],
        f"- Target: {TARGET_LABELS[target]}",
        f"- Continuation type: {HANDOFF_TYPE_LABELS[handoff_type]}.",
        *([f"- Same-project sessions observed: {same_project_session_count}"] if same_project_session_count > 1 else []),
        *[f"- Related active workspace: {path}" for path in related[:3]],
        "",
        *source_section,
        *task_context_lines,
        "",
        "Acceptance criteria and guardrails",
        *([f"- {item}" for item in acceptance_lines] if acceptance_lines else [f"- {profile['finish']}"]),
        "- Preserve unrelated changes and do not expose secrets.",
        "- Keep the exact checkout above active unless the user explicitly chooses another workspace.",
        "- Stop before destructive changes, force pushes, broad refactors, production writes, or unrelated cleanup.",
    ]))

    return {
        "session_id": session.session_id,
        "project": project_label,
        "project_reliable": project_reliable,
        "tool": session.tool,
        "model": session.model or "unknown",
        "source_path": session.source_path,
        "target": target,
        "target_label": TARGET_LABELS[target],
        "target_guidance": target_guidance,
        "handoff_type": handoff_type,
        "handoff_type_label": HANDOFF_TYPE_LABELS[handoff_type],
        "objective": objective_text,
        "source_refs": source_ref_lines,
        "constraints": constraint_lines,
        "acceptance_criteria": acceptance_lines,
        "updated_at": _stamp(session),
        "usage": {
            "tokens": session.tokens_in + session.tokens_out,
            "tokens_label": _compact_int(session.tokens_in + session.tokens_out),
            "model_calls": session.agent_calls,
            "tool_calls": session.tool_calls,
            "api_value_usd": round(session.cost_usd, 6),
            "api_value_label": _money(session.cost_usd),
            "subscription_limited": is_subscription_model(session.model),
        },
        "outcome": outcome,
        "evidence": evidence.to_json(),
        "warnings": warnings,
        "include_prompt_excerpt": include_prompt_excerpt,
        "costliest_prompt": costliest_prompt,
        "decisions": decisions,
        "related_workspaces": related[:3],
        "runtime_attachment": runtime_attachment or {},
        "source_identity_label": source_identity_label,
        "same_project_session_count": max(1, int(same_project_session_count or 1)),
        "continuation_context": {
            "objective_and_context": memory_summary["summary"],
            "completed_work": done_lines,
            "current_state": memory_summary["current_state"],
            "decisions": memory_summary["decisions"],
            "risks_and_uncertainties": uncertainty_lines,
            "next_steps": [*memory_summary["open"], *checkpoint_lines[1:]],
            "inspect_first": memory_summary["files"],
        },
        "next_brief": next_brief,
    }


def render_handoff_capsule(capsule: dict[str, object]) -> str:
    usage = capsule.get("usage") if isinstance(capsule.get("usage"), dict) else {}
    evidence = capsule.get("evidence") if isinstance(capsule.get("evidence"), dict) else {}
    lines = [
        "AIWatcher Fresh Start capsule",
        "",
        f"Use this when moving work into a fresh {capsule.get('target_label') or 'Claude/Codex/Cursor'} session.",
        "",
        f"Continuation type: {capsule.get('handoff_type_label') or 'Coding continuation'}",
        f"Project: {capsule.get('project')}",
        f"Target: {capsule.get('target_label') or 'generic'}",
        f"Tool/model: {capsule.get('tool')} / {capsule.get('model')}",
        f"Updated: {capsule.get('updated_at')}",
        (
            f"Previous usage: {usage.get('tokens_label')} tokens, "
            f"{usage.get('model_calls')} model calls, {usage.get('tool_calls')} tool calls, "
            f"{usage.get('api_value_label')} API-equivalent"
        ),
        f"Outcome: {capsule.get('outcome') or evidence.get('inferred_outcome') or 'not confirmed'}",
        (
            f"Evidence: {len(evidence.get('commits') or [])} commit(s), "
            f"{len(evidence.get('changed_files') or [])} changed file(s), "
            f"{len(evidence.get('tests') or [])} test artifact(s)"
        ),
        "",
        "Why start fresh now",
    ]
    lines.extend(f"- {item}" for item in capsule.get("warnings", []))
    lines.extend([
        "",
        "Paste this brief into the next AI tool",
        str(capsule.get("next_brief") or ""),
        "",
        f"Capsule-Id: {issue_brief_token('handoff_capsule')}",
    ])
    return "\n".join(lines)
