# Optimize review contract

Optimize surfaces possible organization work, not proven safe cleanup. The copied prompt starts with a read-only review and requires explicit approval of the final action summary before a separate owning-tool action. AIWatcher itself never executes cleanup.

## What the reviewer receives

Each session-group packet includes the selection thresholds, exact IDs, known titles and surface, observed role, parent identity and activity, owning and last recorded checkout, latest project activity across all scanned rows, last turn lifecycle and final-response event status, and explicit unavailable checks. Useful outcome labels and completed turns are not completion proof.

The most recent eight older sessions form a bounded review batch. The full older-session count and omitted count are displayed; no classification applies to omitted sessions. Approval/Guardian and other known internal session logs are excluded from ordinary chat candidates. Subagents retain their parent relationship and require a parent check.

Local inspection reads only the first metadata record (at most 64 KiB) and last 64 KiB of an exact observed Codex JSONL file. Results are cached by path, ID, file size and modification time. Only identity and lifecycle metadata leave this reader. Prompt text, final response text, command arguments and source are not included in the packet. A missing event or unreadable/mismatched log remains unknown. Other tools without this Codex metadata stay unknown rather than inheriting a guessed role.

Fresh Start groups preserve exact receipt and source-session IDs. Runtime packets carry bounded PID/parent/tool/working-directory/session/stale-signal observations; the review command must be rerun for current ownership. Workspaces require the reviewer to check current Git state, unpushed work and dependencies. No background network PR lookup is performed by Optimize.

Workspace discovery covers registered worktrees of known projects, system temp roots, Codex's worktrees root, and each project's `.worktrees` and `.claude/worktrees`. The filesystem walk is bounded to depth three and 1,200 directories per root. Temporary Git checkouts on `codex/`, `claude/`, or `agent/` branches can be discovered regardless of directory name or whether their owning nested checkout is indexed. Tool-named folders remain inferred candidates; symlinked directories are skipped. Known project paths are not automatically excluded from workspace review, and recent recorded checkout activity preserves them. Arbitrary locations, detached unnamed clones and unrecorded command working directories can be missed. The first eight candidates are returned with total and omitted counts; this is not an exhaustive machine scan.

Workspace prompts carry branch, observed HEAD/common Git directory where available, discovery scope, directory timestamp and linked session IDs. These are refreshed before copying. The independent reviewer must verify current remote main, exact ancestry and squash/rebase equivalents, ignored files and directory-specific live use. An active parent conversation alone does not prevent removing a completed, preserved temporary checkout. A merged feature-branch PR alone does not prove integration into main.

## Review and confirmation

The independent reviewer must match exact owning-app identities, inspect the latest ask and final response, check unresolved work, current parent/project activity, actual checkout and runtime. The reviewer returns a decision table and a confirmation question naming only verified items. If evidence is missing, the next request is verification rather than cleanup approval.

Both basic and AI-assisted prompts require a fresh final action summary immediately before any separately authorized owning-tool action. It names exact targets and actions, verified reasons, outstanding risks, preserved work, recovery or irreversibility, and measured relief (or unknown). Explicit user confirmation applies only to that summary; changed evidence or targets require another confirmation. A general cleanup request or Reviewed receipt is not approval. Reversible archiving is distinguished from permanent deletion. After exact approval the reviewer may execute the supported owning-tool action and verify the result. Registered worktrees use Git removal without force; shared repository and branches are preserved. A refusal must be reported rather than bypassed.

Basic Copy cleanup prompt fetches the current canonical packet through the same refresh endpoint used by review and AI composition; it never copies a prompt string embedded in the page. Copy all also fetches the current queue. During initial loading, cache migration, background refresh or an evidence error, the page shows a visible status instead of copy/review controls. The server rejects those actions until current evidence is ready. Legacy disk snapshots are invalidated and legacy memory candidates trigger a refresh.

The dashboard's Review decision dialog confirms a local review receipt only, never an archive/completion action. It refreshes available local log/runtime metadata before opening and before saving. An evidence mismatch returns HTTP 409 and requires reopening the summary. Receipt suppression is scoped to candidate ID plus evidence hash for 24 hours; changed activity brings the candidate back. Legacy project-scoped receipts remain readable and expire after 24 hours.

AI composition cannot preclassify an item as safe. It preserves the complete local packet, adds optional verification questions, rejects non-array/non-string JSON values and refuses oversized inputs rather than truncating identities or guardrails. Contract versioning invalidates old cached prompts. Historical tokens represent past recorded activity; RAM/disk/context/cost savings are unknown unless separately measured.

AI Assist cache storage preserves up to 32,000 characters intact rather than clipping at 12,000. Optimize cache reads require a content integrity hash; legacy/unverified entries are regenerated and oversized outputs are rejected without partial storage. Confirmation and guardrails must survive both immediate composition and cache replay.

Companion counts review groups separately from session records inside the top group. Three project groups, a Fresh Start receipt group and a runtime group are five groups, not five individual sessions. Stable Codex UUIDs are recovered from rollout filenames when bounded transcript reads omit the metadata header, preventing the same thread being counted under UUID and filename aliases. Database-only rows retain validated rollout paths so approval/subagent metadata can be verified and excluded appropriately. Unknown identity remains unknown rather than being guessed from titles.

For macOS bundled desktop apps and helpers, PPID=1 alone is not treated as an orphan signal: launchd ownership does not establish abandonment. Independent signals such as a suspended state or missing working directory can still surface review candidates. RSS is a changing observation, not guaranteed reclaimed RAM. Organization-only chat groups must not be presented as resource savings.

## Validation

Python tests cover role/parent identity, privacy, changed turns, unknown identity, bounded groups, recent project activity, schema rejection, receipt expiration, snapshot confirmation and cross-origin protection. Node tests cover stale dialog responses, cancellation and the exact confirmed receipt payload. The actual card and dialog renderer were checked in a browser using synthetic data.
