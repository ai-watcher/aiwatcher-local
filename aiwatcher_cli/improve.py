"""Local Improve decisions and conservative, evidence-scoped follow-up views."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

from . import compaction_outcomes, local_state


LABELS = {
    "replayed-context": "Review context options",
    "pace": "Review cost drivers",
    "daily_spend": "Review window sessions",
    "model-mix": "Compare sessions",
    "false-starts": "Review sample",
    "churned": "Review commit evidence",
    "outcome-review": "Review outcomes",
}

NEXT_STEPS = {
    "replayed-context": [
        "Confirm the task, constraints and unfinished work that must survive a context change.",
        "Review this session's existing context options; compact or start fresh only when an appropriate boundary is available.",
        "Compare subsequent request context and verify that the task still succeeds. Earlier spend cannot be recovered.",
    ],
    "pace": [
        "Compare the largest sessions with the work you intended to do; a higher pace is not automatically a problem.",
        "For unexpected work, narrow the next task and define a checkpoint before continuing.",
        "Mark expected workload as Expected; compare the next window rather than treating the difference as savings.",
    ],
    "daily_spend": [
        "Compare unusually large days with your intended workload and the contributing sessions.",
        "Review session history before choosing a smaller next task. A quiet day with no records is not a zero-cost measurement.",
    ],
    "model-mix": [
        "Compare similar tasks and confirmed outcomes, not just average session prices.",
        "Trial a different model on a small reversible task only when its capabilities fit; record useful work or rework afterward.",
    ],
    "false-starts": [
        "Review the sample for useful questions, research and uncommitted work before calling it wasted effort.",
        "Mark useful work accordingly. For an actual abandoned start, define a smaller next task with a clear stopping point.",
    ],
    "churned": [
        "Check whether the change survives under a different commit after an amend or rebase.",
        "Confirm the outcome from the current work. Do not restore or reset a branch based only on commit reachability.",
    ],
    "outcome-review": [
        "Check whether nearby commits and tests actually belong to this session.",
        "Record useful work, rework or abandoned work based on the result; a useful answer need not produce a commit.",
    ],
}


def attach_evidence(cards, rows, all_rows, evidence, *, days, now=None):
    """Resolve identities once; never route using a project basename or search."""
    now = now or datetime.now(timezone.utc)
    index = {row.session_id: row for row in all_rows}
    for card in cards:
        kind = card.get("id")
        if kind not in LABELS:
            continue
        ids = card.pop("evidence_ids", None)
        if kind == "outcome-review":
            ids = [sid for sid, item in evidence.items()
                   if item.inferred_outcome in {"useful", "needs_review"}]
            card["scope_note"] = f"Evidence checked for {len(evidence)} sampled sessions, not all history."
        elif kind == "churned":
            ids = [sid for sid, item in evidence.items() if item.inferred_outcome == "churned"]
        elif kind == "replayed-context":
            ids = [card.get("session_id")]
        elif ids is None:
            source = all_rows if kind == "model-mix" else rows
            ids = [row.session_id for row in sorted(source, key=lambda r: r.cost_usd, reverse=True)]
        ids = list(dict.fromkeys(sid for sid in ids if sid in index))
        card["evidence_total"] = len(ids)
        card["evidence"] = [
            {"session_id": sid, "project": str(index[sid].project_path or "Unknown project"),
             "tool": index[sid].tool, "model": index[sid].model, "cost_usd": index[sid].cost_usd}
            for sid in ids[:30]
        ]
        card["action_label"] = LABELS[kind]
        card["next_steps"] = NEXT_STEPS[kind]
        identity = [kind, days, now.date().isoformat(), sorted(ids)]
        card["evidence_key"] = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        card["scope_note"] = card.get("scope_note") or (
            "All recorded history." if kind in {"model-mix", "false-starts"}
            else f"Selected {days}-day window; session costs are whole-session estimates."
        )
        if kind == "model-mix":
            card["scope_note"] += " Different tasks may need different models; this is not a quality comparison."
    return cards


def current_view(cards, *, now=None, state=None):
    """Overlay feedback and outcomes even when the expensive summary is cached."""
    now = now or datetime.now(timezone.utc)
    decisions = state["decisions"] if state is not None else local_state.recent_improve_decisions()
    outcomes = state["outcomes"] if state is not None else local_state.outcomes_for_sessions()
    result = []
    for original in cards:
        card = dict(original)
        if card.get("id") == "outcome-review" and "evidence" in card:
            card["evidence"] = [row for row in card["evidence"] if row["session_id"] not in outcomes]
            count = len(card["evidence"])
            if not count:
                continue
            card["title"] = f"{count} sampled sessions still need an outcome"
            card["evidence_total"] = count
            card["body"] = "Review the evidence and mark useful work, rework, or abandoned work. Useful research need not produce a commit."
        feedback = next((item for item in decisions if item.get("key") == card.get("evidence_key")
                         and item.get("decision") != "reviewed"), None)
        card["feedback"] = ""
        if feedback:
            try:
                age = now - datetime.fromisoformat(feedback["created_at"])
                if timedelta(0) <= age < timedelta(hours=24):
                    card["feedback"] = feedback["decision"]
            except (KeyError, TypeError, ValueError):
                pass
        card["rank_reason"] = (
            "Lower priority from your feedback on this evidence; resets within 24 hours."
            if card["feedback"] in {"later", "expected", "not_helpful"}
            else "Reviewable session evidence first; historical cost is not promised savings."
        )
        card["follow_up"] = False
        if state is not None and card.get("id") == "replayed-context":
            sid = card.get("session_id")
            receipts = [*state["handoffs"], *state["compact_nudges"]]
            for receipt in receipts:
                if receipt.get("session_id") != sid or receipt.get("decision") not in {"copy_handoff", "new_chat", "copied", "later", "dismissed"}:
                    continue
                try:
                    age = now - datetime.fromisoformat(receipt.get("decided_at") or receipt["created_at"])
                    if timedelta(0) <= age < timedelta(hours=24):
                        card["follow_up"] = True
                        card["action_label"] = "Review context follow-up"
                        card["rank_reason"] = "A recent Companion or Fresh Start decision exists for this session. Review its follow-up before another intervention."
                        break
                except (KeyError, TypeError, ValueError):
                    continue
            for record in reversed(state["compactions"]):
                if record.get("session_id") != sid:
                    continue
                try:
                    age = now - datetime.fromisoformat(record["boundary_at"])
                    if not timedelta(0) <= age < timedelta(hours=24):
                        break
                except (KeyError, TypeError, ValueError):
                    break
                measured = compaction_outcomes.figures(record)
                if record.get("first_after") and measured["context_after"] >= measured["context_before"]:
                    card["rank_reason"] = "The last observed compaction did not reduce this session's next request. Inspect the result before repeating it."
                    card["action_label"] = "Review prior compaction"
                    card["follow_up"] = True
                break
        result.append(card)
    priority = {"outcome-review": 0, "replayed-context": 1, "pace": 2}
    result.sort(key=lambda card: (
        card.get("feedback") in {"later", "expected", "not_helpful"} or card.get("follow_up", False),
        priority.get(card.get("id"), 3),
    ))
    return result


def recent_results(state=None):
    """Observed results and user decisions are distinct; neither proves causality."""
    results = []
    for record in (state["compactions"] if state is not None else local_state.compaction_outcomes()[-20:]):
        measured = compaction_outcomes.figures(record)
        before, after = measured.get("context_before"), measured.get("context_after")
        body = (f"Context before: {before}; after: {after} tokens."
                if record.get("first_after") and before is not None and after is not None else "Waiting for comparable context measurements.")
        if measured.get("measurable"):
            body += (f" Estimated API-equivalent difference: ${measured['saved_usd_low']:.2f}"
                     f" to ${measured['saved_usd_high']:.2f}; not invoice savings.")
        else:
            body += " " + str(measured.get("reason") or "Savings cannot be estimated.")
        body += " Measurement window complete." if measured.get("window_complete") else " Measurement still in progress."
        results.append({"title": "Compaction observed", "body": body,
                        "session_id": record.get("session_id"),
                        "at": record.get("boundary_at", "")})
    for record in (state["handoffs"] if state is not None else local_state.recent_handoff_decisions(limit=20)):
        if record.get("decision") not in {"new_chat", "copy_handoff"}:
            continue
        linked = record.get("next_session_id")
        results.append({"title": "Fresh Start follow-up" if linked else "Fresh Start prepared",
                        "body": ("A later session was linked. This is correlation, not proof of improvement."
                                 if linked else "No later session linked yet. Copying a brief does not prove it was used."),
                        "session_id": record.get("source_session_id") or record.get("session_id"),
                        "at": record.get("created_at", "")})
    if state is not None:
        for sid, record in list(state["outcomes"].items())[-20:]:
            results.append({"title": "Outcome confirmed", "body": f"You marked this session {record.get('outcome')}. User feedback, not automatically verified success.",
                            "session_id": sid, "at": record.get("recorded_at", "")})
    results.sort(key=lambda item: str(item["at"]), reverse=True)
    return results[:10]


def ai_packet(card):
    """Explicit allowlist: no paths, session IDs, transcripts, charts or source."""
    return {"signal": card["id"], "finding": card["title"], "evidence": card["body"],
            "scope": card.get("scope_note", ""), "next_steps": card.get("next_steps", []),
            "limits": "Historical cost is not recoverable savings. No automatic changes. No causal proof."}


def local_answer(card):
    return {"answer": card["body"], "bullets": card.get("next_steps", []) + [card.get("scope_note", "")],
            "confidence": "Selected local evidence", "actions": [],
            "privacy": "Local explanation only. No model call or data upload."}
