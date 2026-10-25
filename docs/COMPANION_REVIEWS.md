# Companion review contract

A warning should lead to a decision, not an inventory of historical usage.
The session review puts identity, the current check, its practical benefit,
and a specific action before expandable evidence.

## Context notification policy

A Companion Fresh Start/context review needs a recent observed session and an
explicit context-limit condition: recorded input reaches the model reference
window, or a prompt as large as one already observed may not fit. Historical
age, cumulative tokens/calls, and historical replay alone are not live alarms.
Old cached summaries without this trigger evidence fail closed until refreshed.
Historical sessions remain accessible for retrospective review and handoffs.
The cached handoff bubble uses the same eligibility rule as grouped reviews.
A bounded, process-local recheck cache prevents a cleared warning from resurfacing
for the unchanged transcript; a later write or newer evidence makes it eligible
for evaluation again. No prompt/source bodies are retained in this cache.

## On opening a session

The detailed review rechecks only the selected transcript through the existing
context cache and refreshes its activity timestamp. It does not scan all
transcripts, make a provider call, execute a cleanup, or change chats.

| State | Decision and action |
| --- | --- |
| Historical | Optional review before resuming; preview a continuation or record an outcome |
| Current context limit | Explain the observed limit/next-prompt condition; preview the continuation brief |
| Waiting | Explain the request; use a verified tool return if available, otherwise inspect evidence |
| Recent advisory | Explain the recorded loop/velocity/runway signal without claiming it still persists; review the next prompt |
| Resolved | Explicit continuation proof and no newer source activity; show the linked follow-up |
| Clear | No imminent context-limit condition established; Keep working |
| Unknown | Explain missing measurements; inspect the evidence or owning tool |
| Checking | Wait for evidence; do not recommend a chat change prematurely |

Context links preserve `review=context` so the review explains its Companion
entry point. The current check takes precedence over the old notification.
Model-table windows are reference values, not the owning tool's live context
allocation. Unavailable measurements are not rendered as zero or all-clear.

## Reward and completion

The visible benefit is the next useful artifact or decision: a continuation
preview that preserves task state, a bounded next prompt, a tool request to
review, or an explicit decision to keep working. Numeric savings are not
promised. Missing handoff objectives and next outcomes still require user
confirmation before editing.

Copying a brief prepares a handoff. Explicit follow-up evidence confirms its
completion; the existing receipt UI distinguishes possible and confirmed links.
A useful-outcome vote is a user judgement and never resolves current pressure.
A completed older handoff also does not hide a new current signal.

## Evidence and disclosure

Indexed usage, charts, receipts, prompt excerpts and verification details remain
expandable. Prompt excerpts are labelled as processed locally, not metadata-only.
Nearby commits are correlation, not session attribution or confirmed delivery.
Prompt-segment subtotals are distinct from indexed session totals. Coaching
requires a specific finding; generic fallback advice is omitted. Historical
prompt analysis does not run savings-estimation scans.
