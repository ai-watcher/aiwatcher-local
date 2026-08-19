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
    return str(resolved), True


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
            warnings.extend(health.recommendations[:2])
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

    evidence_lines = [
        f"- Nearby commits: {len(evidence.commits)}",
        f"- Changed files: {len(evidence.changed_files)}",
        f"- Test artifacts: {len(evidence.tests)}",
    ]
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
    if evidence.commits:
        latest_subject = str(evidence.commits[0].get("subject") or "").strip()
        if latest_subject:
            done_lines.append(f"- Recent commit evidence suggests work landed: {latest_subject}.")
        else:
            done_lines.append("- Recent commit evidence exists; inspect git log before continuing.")
    if evidence.changed_files:
        done_lines.append(
            f"- The workspace has {len(evidence.changed_files)} changed file(s) on disk; treat them as possible in-progress context, not proof from this source session."
        )
    if decisions:
        done_lines.append("- Local decision notes exist; review them before changing direction.")
    if not done_lines:
        done_lines.append("- No commit, changed-file, or test evidence was found; reconstruct the state carefully.")

    uncertainty_lines = [
        "- The previous chat is intentionally not available in this fresh session.",
    ]
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
        uncertainty_lines.append("- AIWatcher could not confidently identify the project path.")
    if not include_prompt_excerpt:
        uncertainty_lines.append("- Prompt text was not included; infer the task from repository state and local evidence.")
    if not evidence.tests:
        uncertainty_lines.append("- No nearby test artifact was detected; choose a narrow verification step after inspection.")

    checkpoint_lines = [
        "- First verify that the source session identity below matches the work the user meant to continue.",
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

    constraint_section: list[str] = []
    if constraint_lines:
        constraint_section = [
            "",
            "Do not lose these constraints",
            *[f"- {item}" for item in constraint_lines],
        ]

    acceptance_section: list[str] = []
    if acceptance_lines:
        acceptance_section = [
            "",
            "Acceptance checks",
            *[f"- {item}" for item in acceptance_lines],
        ]

    next_brief = "\n".join([
        "AIWatcher Fresh Start brief",
        "",
        f"You are starting a fresh {profile['session_label']}. Do not assume access to the previous chat.",
        "Continue from source-session metadata and workspace state, not from hidden conversation history.",
        f"Target tool: {TARGET_LABELS[target]}.",
        f"Continuation type: {HANDOFF_TYPE_LABELS[handoff_type]}.",
        "",
        "Source session identity",
        *source_identity_lines,
        *(
            [
                f"- Same-project sessions observed: {same_project_session_count}",
                "- If this is not the intended source chat, stop and ask the user which session to continue.",
            ]
            if same_project_session_count > 1
            else []
        ),
        "",
        "Goal",
        *([f"- User objective: {objective_text}"] if objective_text else []),
        *[f"- {item}" for item in profile["purpose"]],
        "",
        "How to continue",
        "- If this is a fresh chat: first reconstruct the task from the workspace and evidence below.",
        "- If this is a forked chat: keep the parent chat as source of truth and return only the final summary, files touched, verification, and unresolved questions.",
        "- If this is a subagent task: inspect only the assigned lane, then report evidence and recommendations back to the orchestrator.",
        "- If evidence is insufficient, ask one focused clarification instead of guessing.",
        "",
        "Workspace",
        f"- Project: {project_label}",
        f"- Project confidence: {'reliable' if project_reliable else 'unconfirmed'}",
        *([f"- Related active workspace: {path}" for path in related[:3]] if related else []),
        *source_section,
        *constraint_section,
        *acceptance_section,
        "",
        "What appears done",
        *done_lines,
        "",
        "What remains uncertain",
        *uncertainty_lines,
        "",
        "Recommended next checkpoint",
        *checkpoint_lines,
        "",
        "Source session signals",
        f"- Source tool/model: {session.tool} / {session.model or 'unknown'}",
        f"- Usage pressure: {_usage_pressure_label(session)}",
        f"- Return capability: {exact_return_label}",
        f"- Outcome status: {outcome or evidence.inferred_outcome or 'not confirmed'}",
        "",
        "Why AIWatcher suggested Fresh Start",
        *warning_lines,
        "",
        "Workspace evidence to inspect (not guaranteed source-session evidence)",
        *evidence_lines,
        *commit_message_lines,
        *decision_lines,
        *task_context_lines,
        "",
        "Fresh-session instructions",
        *[f"- {item}" for item in target_guidance],
        "- If the source session identity does not match the user's intended work, stop and ask before editing.",
        "- First reply with what appears done, what remains uncertain, and the smallest next checkpoint.",
        "- State which files or commands you will inspect before editing.",
        "- Implement only the smallest checkpoint after the plan is clear.",
        "- If continuing in Claude/Codex/Cursor, keep the same repository path active before editing.",
        "- Preserve unrelated changes and do not expose secrets.",
        "- Stop before destructive changes, broad refactors, secret exposure, or unrelated cleanup.",
        "",
        "When finished",
        f"- {profile['finish']}",
    ])

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
