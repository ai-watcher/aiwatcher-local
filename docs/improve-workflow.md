# Improve: evidence to follow-up

Improve is a local decision loop, not a savings scoreboard. Its charts remain
available, with explicit actions for reviewing the sessions behind each signal.
It works without a provider key or Enterprise account.

## What changes

1. **Evidence:** replayed history is not automatically waste; missing commits
   are not proof of failed work. Outcome checks disclose their sampled scope.
   Coverage limitations remain in Settings > Trust.
2. **Actions:** each signal opens a bounded, explicitly counted evidence list.
   Full project paths and session IDs distinguish similarly named projects.
   Session review reuses the existing outcome, compaction and Fresh Start controls.
3. **Follow-up:** outcome confirmations immediately leave the pending list,
   including when the underlying summary is cached. Recent results distinguish
   user-confirmed outcomes, prepared Fresh Starts, linked later sessions and
   observed compactions. Copying is never treated as execution or success.
4. **Adaptation and optional AI:** Later, Expected and Not helpful lower the
   same signal/session scope's priority for at most 24 hours. A changed session
   set, day window or snapshot date has its own key.
   Existing Companion decisions and prior unsuccessful context reductions
   direct attention to follow-up rather than another intervention. The ranking
   explanation is visible. This is transparent rules-based feedback, not a
   trained predictive model or a claim of causality.

## Privacy and measurement limits

- Feedback stores only a hashed evidence key, a fixed decision and a timestamp
  in local state, capped at 500 records. No prompt or source text is added.
- Ask about this signal starts in local mode. Optional AI uses the existing
  configuration, confirmation, budget and cache controls. The server resolves
  the selected evidence; the browser cannot substitute an evidence packet.
  The packet contains the finding, scope and next step, not session paths,
  transcripts, charts or source files. The user's typed question is also sent
  when they choose AI Assist.
- Read-only charts and review actions never run compaction, delete files or
  send a prompt to an agent automatically.
- Evidence lists show at most 30 sessions and disclose the total. Session cost
  estimates are whole-session figures, not necessarily the selected day's cost.
- Compaction estimates depend on observable local records and pricing. They
  are API-equivalent ranges, not recovered money or subscription invoice savings.
  Waiting for a request is not displayed as zero context. Fresh Start links are
  correlations, not proof that a recommendation improved the outcome.
- Feedback does not hide charts, permanently dismiss a signal, or suppress
  unrelated sessions. Existing Companion control receipts remain authoritative.

## Manual verification

1. Open Improve. Charts remain visible and recommendations have named actions.
2. Review evidence from two projects with the same basename. Open each session
   and verify its full path and identity match the selected row.
3. Mark a sampled session useful. Return to Improve: the pending count decreases
   without requiring a deep scan. The outcome appears as user feedback in results.
4. Choose Later or Expected. The same evidence moves down with an explanation;
   it remains accessible. Helpful removes that lower-priority feedback.
5. Copy a Fresh Start brief through session review. It must remain prepared until
   a later session is linked; no saved-money claim should appear from the copy.
6. Ask about a signal in local mode. Enable configured AI Assist explicitly and
   verify confirmation and fallback behavior. A stale evidence key must request
   a refresh, never silently answer about another project.
7. Verify the evidence dialog at narrow and desktop widths, keyboard Escape,
   session navigation, feedback failure messages and both themes.
