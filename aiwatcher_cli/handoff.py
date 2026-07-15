"""Local handoff capsules for continuing AI work safely.

The capsule is designed for one developer moving work into a fresh Claude,
Codex, Cursor, or other coding-agent session. It summarizes metadata, outcome
evidence, and next-step guardrails without reading source diffs or uploading
anything.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Sequence

from .local_state import recent_decisions
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


HandoffTarget = Literal["generic", "claude", "codex", "cursor", "vscode"]


TARGET_LABELS: dict[str, str] = {
    "generic": "Claude/Codex/Cursor",
    "claude": "Claude",
    "codex": "Codex",
    "cursor": "Cursor",
    "vscode": "VS Code",
}


def _target_guidance(target: str) -> list[str]:
    if target == "codex":
        return [
            "Paste this as the first prompt in a fresh Codex session.",
            "Ask Codex to inspect before editing and to keep the checkpoint narrow.",
        ]
    if target == "claude":
        return [
            "Paste this as the first prompt in a fresh Claude Code session.",
            "Let Claude summarize current repo state before continuing.",
        ]
    if target == "cursor":
        return [
            "Paste this into Cursor composer for the relevant project.",
            "Keep the edit scope to the files Cursor confirms are related.",
        ]
    if target == "vscode":
        return [
            "Paste this into your selected AI assistant from VS Code.",
            "Use AIWatcher preflight on the brief again if you edit it materially.",
        ]
    return [
        "Paste this as the first prompt in a fresh AI coding session.",
        "Keep the next session focused on one checkpoint.",
    ]


def build_handoff_capsule(
    session: LocalSession,
    events: Sequence[LocalEvent],
    *,
    outcome: str | None = None,
    include_prompt_excerpt: bool = False,
    target: HandoffTarget = "generic",
) -> dict[str, object]:
    """Build a structured handoff capsule for UI/API rendering."""
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

    warnings: list[str] = []
    if health:
        if health.severity != "healthy":
            warnings.append(
                f"Context health is {health.severity}: latest turn used "
                f"{_compact_int(health.latest_turn_tokens)} input tokens with "
                f"{health.efficiency_pct:.0f}% efficiency."
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
    target_guidance = _target_guidance(target)

    evidence_lines = [
        f"- Local evidence: {len(evidence.commits)} nearby commit(s), "
        f"{len(evidence.changed_files)} changed file(s), {len(evidence.tests)} test artifact(s)",
    ]
    shown_commits = evidence.commits[:3]
    for commit in shown_commits:
        subject = str(commit.get("subject") or "").strip()
        label = f"{commit.get('sha')}: {subject}" if subject else str(commit.get("sha"))
        evidence_lines.append(f"  - Commit {label}")
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

    next_brief = "\n".join([
        "You are continuing AI-assisted coding work from a previous local session.",
        f"Target tool: {TARGET_LABELS[target]}.",
        "",
        "Project",
        session.project_path or "unknown",
        "",
        "Current status",
        f"- Previous tool/model: {session.tool} / {session.model or 'unknown'}",
        f"- Previous session usage: {_compact_int(session.tokens_in + session.tokens_out)} tokens, "
        f"{session.agent_calls} model calls, {session.tool_calls} tool calls, "
        f"{_money(session.cost_usd)} API-equivalent value",
        f"- Outcome status: {outcome or evidence.inferred_outcome or 'not confirmed'}",
        *evidence_lines,
        *commit_message_lines,
        *decision_lines,
        *task_context_lines,
        "",
        "Before editing",
        *[f"- {item}" for item in target_guidance],
        "- Inspect the current git status and the smallest relevant files.",
        "- Summarize what appears already done and what remains.",
        "- Continue one checkpoint at a time; do not restart broad exploration.",
        "- Preserve unrelated changes and do not expose secrets.",
        "",
        "When finished",
        "- Report changed files, verification run, remaining uncertainty, and whether the result looks useful.",
    ])

    return {
        "session_id": session.session_id,
        "project": session.project_path or "unknown",
        "tool": session.tool,
        "model": session.model or "unknown",
        "target": target,
        "target_label": TARGET_LABELS[target],
        "target_guidance": target_guidance,
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
        "next_brief": next_brief,
    }


def render_handoff_capsule(capsule: dict[str, object]) -> str:
    usage = capsule.get("usage") if isinstance(capsule.get("usage"), dict) else {}
    evidence = capsule.get("evidence") if isinstance(capsule.get("evidence"), dict) else {}
    lines = [
        "AIWatcher handoff capsule",
        "",
        f"Use this when moving work into a fresh {capsule.get('target_label') or 'Claude/Codex/Cursor'} session.",
        "",
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
        "Why hand off now",
    ]
    lines.extend(f"- {item}" for item in capsule.get("warnings", []))
    lines.extend([
        "",
        "Paste this brief into the next AI tool",
        str(capsule.get("next_brief") or ""),
    ])
    return "\n".join(lines)
