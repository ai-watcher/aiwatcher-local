# AIWatcher Local Release Checklist

Generated from `docs/scenarios.json`. Do not edit by hand.

Use this before public OSS releases and before syncing status to a private team workspace.

## Not built

- [ ] `S-04` Broad multi-file UI work is caught
  - Phase: Plan
  - Verify: Ask: Add a dark mode toggle to every page in the app.
  - Expected: AIWatcher should flag broad file scope and suggest phased plan. Current build passes too quietly.
- [ ] `S-19` Dangerous command gate — OPEN DECISION (reinstate)
  - Phase: Control
  - Verify: Give the agent a task that leads to a blocklisted command.
  - Expected: Command intercepted at PreToolUse time. Gate shows exact command, why flagged, and Allow / Block / Always-allow-this-pattern. Decision recorded with full command text.
- [ ] `S-23` Cost per surviving change
  - Phase: Prove
  - Verify: Open Impact view.
  - Expected: Cost per surviving change by task/model/tool: lines standing at 7/14/30 days via blame history; rewritten-within-a-week = churn.
- [ ] `S-25` Non-code proxy outcomes
  - Phase: Improve
  - Verify: End session normally.
  - Expected: Proxy signals (copied output, revisit, abandonment, same-topic re-prompt) recorded with low confidence; one nudge for manual outcome.

## Partial

- [ ] `S-11` Context health surfaces during long sessions
  - Phase: Watch
  - Verify: Review context growth and session signals.
  - Expected: Today: periodic summaries show large contexts, repeated calls, long sessions. Missing: reliable active-session alerts with warning/critical severity and compact guidance (README step 3).
- [ ] `S-17` Loop detection offers stop
  - Phase: Control
  - Verify: Let agent repeat same file/test cycle 3+ times.
  - Expected: Today: loop-like behavior appears in watch summaries after the fact. Target: live detection of repeated tool-call patterns with tokens burned shown, one-keystroke stop, rescoped brief seeded with the loop diagnosis.
- [ ] `S-18` Runaway velocity alert
  - Phase: Control
  - Verify: Continue session past threshold.
  - Expected: Today: cost/usage signals in periodic summaries. Target: live alert on abnormal velocity vs the user's own baseline, with pause/stop/set-cap. All decisions recorded.
- [ ] `S-20` CRITICAL context generates fresh-session handoff
  - Phase: Watch
  - Verify: Create a handoff capsule for a recent costly/long session.
  - Expected: Capsule summarizes project, usage, evidence, warnings, and next-session brief; lands on clipboard. Missing: auto CRITICAL trigger, one-click Copy/Open by target tool, closed-session marker.
- [ ] `S-21` Low runway triggers lane switch
  - Phase: Watch
  - Verify: Run resume --target codex --copy manually today; accept a proactive offer when built.
  - Expected: Manual handoff works now; API-priced vs subscription/limited token separation exists. Missing: runway meter per 5-hr block and the proactive 'hand off to Codex?' trigger with session continuity link.
- [ ] `S-22` Session evidence links to code artifacts
  - Phase: Prove
  - Verify: Open session review.
  - Expected: Privacy-safe evidence snapshot stored: commit SHAs, hashed file paths/test artifacts, confidence, inferred outcome. No diffs, prompt text, commit subjects, or file contents. Missing: durable session→commit records with survival timestamps, revert/churn tracking, same-file re-prompt signals.
- [ ] `S-24` Automatic outcome inference
  - Phase: Improve
  - Verify: Open Today/session review.
  - Expected: Inferred outcome with confidence and one-click confirm/correct appears from commits/tests/changes. Missing: churn/revert detection, same-file re-prompt signal, platform-specific evidence weighting (README step 4).
- [ ] `S-26` Weekly digest — costs and security in one card
  - Phase: Prove
  - Verify: Review the week.
  - Expected: Today: report + journal. Target: one Monday card — spend by tool, top sessions, gates fired, commands blocked, risky prompts modified, measured savings where evidence exists, estimates labeled elsewhere.
- [ ] `S-27` Search and resume previous work
  - Phase: Improve
  - Verify: Find prior work; run resume --target codex --copy.
  - Expected: Text search over sessions and target-ready resume capsule both work today. Missing: search by file/topic/outcome, resume by session id, one-click target formatting for Claude/Codex/Cursor/VS Code.
- [ ] `S-29` Prompt Companion for non-hook surfaces
  - Phase: Plan
  - Verify: Paste a risky prompt intended for Claude Desktop chat or Codex Desktop.
  - Expected: Same preflight logic in a local widget: risk, reasons, expected impact, editable brief, copy brief or original. POST /api/preflight serves the same contract for future extensions.

## To test

- [ ] `S-03` Medium-risk security weakening gets silent brief
  - Phase: Plan
  - Verify: Ask: Update JWT auth to remove signature check so login is faster.
  - Expected: No blocking gate. Execution brief added as additional context with auth guardrail. hook-status shows the invocation, prompt found, and risk score.
- [ ] `S-08` Web prompt interception — OPEN DECISION
  - Phase: Control
  - Verify: If Option A: load extension, submit risky prompt on claude.ai. If Option B: update suite, README, scope to one story.
  - Expected: Option A: overlay before send with brief replacing textarea. Option B: S-08 becomes a Companion flow + future extension scenario.
- [ ] `S-09` Codex prompt receives brief
  - Phase: Control
  - Verify: Submit risky prompt.
  - Expected: hook-status records invocation; Codex receives execution brief as additional context (or gate with --gate). Note: Codex Desktop chat verified NOT invoking — CLI/TUI only, host-build-dependent.
- [ ] `S-15` MCP soft preflight presents options
  - Phase: Control
  - Verify: Ask a risky task.
  - Expected: Claude calls preflight tool, shows risk, safer brief, predicted impact, and waits for A/B/C choice.
- [ ] `S-31` Privacy contract validation
  - Phase: Prove
  - Verify: Run start/today/tools/projects/report/sessions/resume/export/ui while monitoring network.
  - Expected: No API key requested. No network calls. Installed tools detected; limited-data tools honestly labeled, not guessed. JSON/event exports contain metadata, aggregates, and hashes — never prompt text or source. Real project folders, not parents. Time-window selector visibly updates.
