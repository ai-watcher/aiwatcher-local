---
name: aiwatcher-ui
description: Rules for changing the AIWatcher local dashboard — the served surfaces (Home, Watch, Plan/Control, Changes, Improve, Prove, Settings, the session drawer) and the numbers on them. Use when editing aiwatcher_cli/ui.py or aiwatcher_cli/web/, adding or relabelling a metric, threshold, badge, pill, or status colour, or judging whether a figure earns its place on screen.
---

# Working on the AIWatcher dashboard

## Who it is for, and what that forbids

The dashboard is **ambient**: a developer leaves it open on a second monitor
while coding and glances at it mid-session. It is not a daily report, not a
post-mortem tool, and not a value-proving artifact for a lead.

Judge every change against: *does this work when glanced at from four feet
away, mid-task, without being read?*

Consequences that are already settled — do not relitigate them without the
owner:

- **Stable layout.** States must not reflow. Home's two ambient states share
  one skeleton at identical slot heights, verified slot-for-slot. If you add a
  state, it fits the existing skeleton.
- **Low motion.** Never animate a value change. Keep "updated Xs ago" visible
  instead — coarse, not a live-ticking second counter.
- **Refresh cadence is fixed**: 10s visible / 60s hidden / immediate on focus.
  `REFRESH_VISIBLE_MS` and `REFRESH_HIDDEN_MS` in `web/index.js`, pinned by
  `tests/test_ui_assets.py::test_cadence_constants`.
- **The tab title and favicon are the real ambient surface.** The page is
  usually not visible. Changes there outrank changes to page content.
- **Narrow windows are the normal case** — it competes with an editor for
  screen space.
- **Every view needs a nav entry.** Asserted as a general rule in the tests,
  not per-view.

## The recurring defect: a true number that answers the wrong question

This is the failure mode this codebase produces over and over. It has been
found and fixed **six** separate times. Every instance was a number that was
arithmetically correct and was printed as the answer to a question it does not
answer.

The six, so you can recognise the seventh:

1. **`tokens >= 500000` judged a session expensive.** A *per-turn* pressure bar
   applied to a *cumulative* session total. It fired for 65% of sessions, so it
   sorted nothing.
2. **"Context pressure 99.5M" in the drawer hero.** Cumulative tokens printed
   under a per-turn label, with a meter comparing them to a 500k per-turn
   threshold. The number was real; the comparison was nonsense.
3. **Survival "unknown" rendered green.** The code tested whether a label
   *existed*, not what it *said*. Absence of evidence was drawn as evidence.
4. **"context at risk" with no number in front of it.** An empty value flowed
   into a template and the UI turned it into a pill reading "review".
5. **Settings "0 of 11 done".** It counted fields that do not exist — the setup
   steps carry no completion state at all, so the denominator was invented.
6. **`replayed_share_pct` read ~98% on every window.** Token-weighted, so cache
   reads dominate the count and it separates nothing. Copy saying "of what they
   cost" needs `replayed_spend_share_pct` (spend-weighted, ~40–70%, which does
   discriminate).

### The rule

**A number earns its label only if it answers the question the label asks.**

Before you put a figure on screen, answer all five in the commit message or a
comment next to it:

1. **What question does the label ask?** Write it as a sentence with a question
   mark.
2. **What is the number's unit and scope?** Per-turn or cumulative? Tokens or
   dollars? This session, this window, or all time? Mismatches here are
   instances 1, 2 and 6.
3. **What is it compared against, and where did that come from?** If a
   threshold is a round number someone chose, say so in the comment (see
   *Thresholds* below).
4. **Does it discriminate?** Run it over real local data. If it fires for most
   rows, or reads ~the same on every row, it sorts nothing and is decoration.
   This killed instances 1 and 6.
5. **What does it do when the input is missing?** "Unknown" must not render as
   a status colour, and must not become a pill by flowing through a template
   (instances 3 and 4). Unmeasurable is a distinct state from measured-and-fine,
   and it must be *shown* as unmeasurable with the reason why.

### Not-measurable is a first-class state

`_session_verdict_inputs` in `ui.py` is the pattern to copy: every block carries
`measurable: bool` plus a `reason` string when false, and the front end renders
the reason rather than a zero, a dash, or a green rail. The three verdict
questions are kept deliberately separate — how much room is left (answerable
now), did it cost more than it needed to (answerable once the session stops),
was it worth it (needs commits to age past `survival.MIN_AGE_DAYS`, 7 days).
Collapsing them into one verdict is what made the old one unable to say
anything.

### Thresholds: compare to the owner, not to a round number

Provider quota is not visible locally, so an honest "are you running hot" has to
be **self-referential**. `metrics.pace_vs_baseline` is the reference
implementation:

- compare this window against the same-length windows before it;
- bucket **events**, not sessions, so spend lands in the window it happened in;
- drop windows with no activity rather than counting them as zero — a fortnight
  away would otherwise halve the baseline and make the return week a spike;
- require a minimum number of comparison windows, and return
  `{"available": False, "reason": ...}` when there are not enough, instead of
  falling back to a picked constant.

A fixed constant is acceptable only when it is externally imposed (a model's
context limit) or explicitly marked as a stopgap with the reason in a comment.
`PRESSURE_TOKENS_PER_TURN` / `CRITICAL_TOKENS_PER_TURN` are the legitimate kind:
they come from a real per-turn limit, and the ambient meter reads its thresholds
from the same `chart.pressure_tokens_n` / `critical_tokens_n` the runway chart
uses so the two cannot disagree.

### Colour is a claim

A status rail asserts "this is good" or "this is bad". Do not attach one to:

- a counterfactual (API-equivalent value: for a subscription user no money
  moved, and spending more is not a failure);
- a raw total. The dashboard judges **ratios** — cost per useful change, cost
  per surviving line — never the raw total;
- anything unmeasured (see instance 3).

Only a running session's context pressure earns a status colour on the ambient
surface. The idle state gets none, and a test pins that.

## Where the code lives

The front end is **not** a Python string any more. It lives in
`aiwatcher_cli/web/` as `index.html` / `index.css` / `index.js` (and the
`overlay.*` trio), spliced back at import by `_load_asset` in `ui.py`, which
resolves `@@INCLUDE:name@@` tokens. `ui.HTML` is still the served document, so
tests that assert on `ui.HTML` keep working.

Rules for that layer:

- Everything must stay **inlined and self-contained** — no external stylesheets,
  scripts, or fonts. `tests/test_ui_assets.py` guards this, along with the
  known-outbound-URL allowlist and the path-traversal rejection in the loader.
- Edit the file in `web/`, never the spliced result.

## Before you claim a surface is fixed

- Read it **on screen at the real data**, not just in the diff. Four of the six
  defects above were invisible in review and obvious in the browser.
- Check the number against real local data for the "does it discriminate" test.
- Beware window artifacts: a column that is empty in every row may just need a
  longer window. The Changes ledger's survival columns looked broken at 7 days
  and were fine at 30 — survival needs commits to age past `MIN_AGE_DAYS`.
- Beware over-stating the problem. The session drawer was reported at "5.3
  screens" by counting *collapsed* `<details>` content; the real problem was
  order, not volume.
- A shared y-scale across small multiples usually fails here: one deep project
  flattens the rest to a few pixels of travel. Use a **meter** for position
  against a shared limit and a **trend line** on its own scale for shape.
