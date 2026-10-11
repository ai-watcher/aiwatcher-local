# Optimize review contract

Optimize surfaces possible organization work, not proven safe cleanup. The copied prompt is a read-only review request. The owning app remains responsible for any separate archive, removal, or stop action.

## What the reviewer receives

Each session-group packet includes the selection thresholds, exact IDs, known titles and surface, observed role, parent identity and activity, owning and last recorded checkout, latest project activity across all scanned rows, last turn lifecycle and final-response event status, and explicit unavailable checks. Useful outcome labels and completed turns are not completion proof.

The most recent eight older sessions form a bounded review batch. The full older-session count and omitted count are displayed; no classification applies to omitted sessions. Approval/Guardian and other known internal session logs are excluded from ordinary chat candidates. Subagents retain their parent relationship and require a parent check.

Local inspection reads only the first metadata record (at most 64 KiB) and last 64 KiB of an exact observed Codex JSONL file. Results are cached by path, ID, file size and modification time. Only identity and lifecycle metadata leave this reader. Prompt text, final response text, command arguments and source are not included in the packet. A missing event or unreadable/mismatched log remains unknown. Other tools without this Codex metadata stay unknown rather than inheriting a guessed role.

Fresh Start groups preserve exact receipt and source-session IDs. Runtime packets carry bounded PID/parent/tool/working-directory/session/stale-signal observations; the review command must be rerun for current ownership. Workspaces require the reviewer to check current Git state, unpushed work and dependencies. No background network PR lookup is performed by Optimize.

## Review and confirmation

The independent reviewer must match exact owning-app identities, inspect the latest ask and final response, check unresolved work, current parent/project activity, actual checkout and runtime. The reviewer returns a decision table and a confirmation question naming only verified items. If evidence is missing, the next request is verification rather than cleanup approval.

Basic Copy cleanup prompt fetches the current canonical packet through the same refresh endpoint used by review and AI composition; it never copies a prompt string embedded in the page. Copy all also fetches the current queue. During initial loading, cache migration, background refresh or an evidence error, the page shows a visible status instead of copy/review controls. The server rejects those actions until current evidence is ready. Legacy disk snapshots are invalidated and legacy memory candidates trigger a refresh.

The dashboard's Review decision dialog confirms a local review receipt only, never an archive/completion action. It refreshes available local log/runtime metadata before opening and before saving. An evidence mismatch returns HTTP 409 and requires reopening the summary. Receipt suppression is scoped to candidate ID plus evidence hash for 24 hours; changed activity brings the candidate back. Legacy project-scoped receipts remain readable and expire after 24 hours.

AI composition cannot preclassify an item as safe. It preserves the complete local packet, adds optional verification questions, rejects non-array/non-string JSON values and refuses oversized inputs rather than truncating identities or guardrails. Contract versioning invalidates old cached prompts. Historical tokens represent past recorded activity; RAM/disk/context/cost savings are unknown unless separately measured.

## Validation

Python tests cover role/parent identity, privacy, changed turns, unknown identity, bounded groups, recent project activity, schema rejection, receipt expiration, snapshot confirmation and cross-origin protection. Node tests cover stale dialog responses, cancellation and the exact confirmed receipt payload. The actual card and dialog renderer were checked in a browser using synthetic data.
