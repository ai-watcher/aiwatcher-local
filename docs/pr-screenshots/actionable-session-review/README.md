# Session review decision states

These browser captures use the branch's actual review components and stylesheet
with synthetic session fixtures. They contain no user transcript or repository
contents. The 187.1k / 400k historical example reproduces the reported decision
problem without using a private source log.

- `historical.png`: optional review before resuming, not a live demand.
- `clear.png`: no immediate context action; Keep working is primary.
- `actionable.png`: a current window-limit observation and a continuation preview.
- `unknown.png`: unavailable context; inspect evidence instead of fabricating 0%.
- `waiting.png`: a waiting request with no verified return target; inspect evidence.

Browser checks exercised optional outcome/evidence expansion, checked the
primary action in the cleared state, and verified the drawer did not overflow
at a 390px viewport. Handoff generation and copying retain the existing drawer
lifecycle tests; these captures do not claim that a new AI chat was started.
