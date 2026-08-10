# AIWatcher Local

Catch expensive AI coding sessions before they run. Find the waste hiding in
the ones that succeeded. Tie every session to whether the work was worth it.

AIWatcher Local is a private control loop for Claude Code, Codex, Cursor, and
other local AI coding tools. It scores prompts before they run, watches sessions
while they do, then attributes what you spent to the commits it produced — so
"was that worth it?" has an answer instead of a token count. No account, no
cloud upload, no LLM calls.

Every other tool in this space reports on sessions that failed. The expensive
ones usually don't. A task phrased loosely, twenty turns of drift and redirect,
and then it works — so nobody looks, at three times the tokens it needed.

![The AIWatcher Local dashboard's Today tab: latest AI work and one thing worth changing up top; useful outcomes, preflight decisions, sessions observed, and API-equivalent value tiles; the latest receipt with proof status; projects driving usage and recent sessions below.](docs/dashboard.svg)

## Contents

- [Privacy](#privacy)
- [Quickstart](#quickstart)
  - [1. Install](#1-install)
  - [2. See where you stand](#2-see-where-you-stand)
  - [3. Install hooks so work is reviewed before it runs](#3-install-hooks-so-work-is-reviewed-before-it-runs)
    - [Prompt preflight hook](#prompt-preflight-hook)
    - [Dangerous-command gate](#dangerous-command-gate)
- [Try the Core Workflows](#try-the-core-workflows)
  - [1. See today's AI work](#1-see-todays-ai-work)
  - [2. Check a prompt by hand](#2-check-a-prompt-by-hand)
  - [3. Use the local dashboard](#3-use-the-local-dashboard)
  - [4. Mark whether work was useful](#4-mark-whether-work-was-useful)
  - [5. See what each commit cost](#5-see-what-each-commit-cost)
  - [6. Resume work without rebuilding context](#6-resume-work-without-rebuilding-context)
  - [7. Log a decision that never became a commit](#7-log-a-decision-that-never-became-a-commit)
  - [8. Check runtime hygiene](#8-check-runtime-hygiene)
  - [9. Export local evidence](#9-export-local-evidence)
- [Commands](#commands) — day-one subset; [full CLI reference](docs/CLI.md)
- [Example Output](#example-output)
- [Why a Loop, Not Another Usage Dashboard](#why-a-loop-not-another-usage-dashboard)
- [The Local Control Loop](#the-local-control-loop)
  - [Hook coverage by tool](#hook-coverage-by-tool)
  - [Prompt Companion for Non-Hook Surfaces](#prompt-companion-for-non-hook-surfaces)
- [What It Reads](#what-it-reads)
- [AIWatcher Local vs AIWatcher Enterprise](#aiwatcher-local-vs-aiwatcher-enterprise)
- [Contributing](#contributing)
- [License](#license)

## Privacy

- Local-only by default
- Read-only
- No LLM calls
- No prompt or source-code upload
- No cloud account required
- Works on macOS, Linux, and Windows

This trust boundary is the product. If AIWatcher Local cannot explain what it
reads and why, it should not read it.

## Quickstart

### 1. Install

Python 3.9+ on macOS, Linux, or Windows:

```sh
pip install aiwatcher-cli
```

Until that release lands on PyPI, run from a clone instead — same commands, no
install step:

```sh
git clone https://github.com/ai-watcher/aiwatcher-local.git
cd aiwatcher-local
python -m pip install -e .
```

On Windows PowerShell the same commands work. If `python` is not on PATH, use
the Python launcher (`py -m pip install -e .`).

Examples below use `python -m aiwatcher_cli`, which works from a clone with no
install at all. Once installed, `aiwatcher <command>` is equivalent everywhere.

### 2. See where you stand

```sh
python -m aiwatcher_cli setup
```

`setup` detects which AI coding tools you have, reports what history it can
already read, and prints the exact next steps for your machine. Start here — it
tells you which of the commands below will actually have data.

Because AIWatcher reads history your tools have already written, these work
immediately, before you install any integration:

```sh
python -m aiwatcher_cli today
python -m aiwatcher_cli last
python -m aiwatcher_cli ui
```

`ui` starts a local-only dashboard on `http://127.0.0.1:8765`. If that port is
busy, AIWatcher Local automatically tries the next available port and prints the
URL it picked.

### 3. Install hooks so work is reviewed before it runs

Everything above is retrospective. Hooks are what make AIWatcher act *before*
execution. There are two, on different lifecycle events. They are independent —
install either, or both:

| Hook | Event | Reviews | Tools |
| --- | --- | --- | --- |
| **Prompt preflight** | `UserPromptSubmit` | Your prompt, before the agent starts | Claude, Codex, Cursor |
| **Dangerous-command gate** | `PreToolUse` | A shell command, before it executes | Claude Code CLI only |

#### Prompt preflight hook

Install the one matching your tool:

```sh
python -m aiwatcher_cli install-claude-hook --write --scope user
python -m aiwatcher_cli install-codex-hook --write --scope user
python -m aiwatcher_cli install-cursor-hook --write --scope user
```

Low-risk prompts pass through untouched. Medium-risk prompts get a scoped
execution brief added alongside them. High-risk prompts pause before execution.

**Add `--gate` for the interactive Prompt Gate.** This does not replace the
behavior above — it adds a review step in front of it. A medium- or high-risk
prompt opens a local decision screen with **Add safer brief**, **Add edited
brief**, **Run original**, and **Cancel run**. If that screen times out or no
display is available, AIWatcher falls back to the same deterministic policy
described above, so nothing is skipped:

```sh
python -m aiwatcher_cli install-claude-hook --write --scope user --gate
```

![AIWatcher Prompt Gate: a local decision screen showing risk score, guardrail chips, findings and suggestions, the original prompt, a proposed execution brief, and the Add safer brief / Add edited brief / Run original / Cancel run actions.](docs/dashboard-prompt-gate.svg)

Prompt text stays transient in that local browser page: AIWatcher persists
hashes, decisions, and predicted impact only.

**Using Codex?** Some Codex builds — including the current Codex Desktop
conversation surface — never invoke `UserPromptSubmit`, so the hook above
installs cleanly and then does nothing. Run `hook-status` after a test prompt to
check. If no event appears, add the shell wrapper instead:

```sh
python -m aiwatcher_cli install-codex-wrapper --write
```

This defines a `codex` shell function that preflights before handing off to the
real binary. It covers prompts you pass when launching Codex from the command
line, not ones typed inside an already-running session. Writes to `~/.zshrc` by
default — pass `--shell-rc ~/.bashrc` for bash.

#### Dangerous-command gate

A separate hook on a separate event. It reviews shell commands the agent is
about to run, independently of how the prompt that produced them was handled:

```sh
python -m aiwatcher_cli install-claude-command-gate --write --scope user
```

**Claude Code CLI only.** Unlike prompt preflight, there is no Codex or Cursor
equivalent — this hook needs the host to expose a lifecycle event *before a tool
call*, and Claude Code's `PreToolUse` is the only one AIWatcher supports today.
On Codex and Cursor, prompt preflight is the whole story.

Where it is available, running both is the normal setup: the prompt hook catches
risky *intent*, the command gate catches a risky *command* that a perfectly
reasonable prompt happened to produce.

#### Verify and undo

Every installer prints the change and writes nothing unless you pass `--write`,
so you can inspect first by dropping that flag. After a test prompt, confirm the
hook actually fired — and back any of them out at any time:

```sh
python -m aiwatcher_cli hook-status
python -m aiwatcher_cli uninstall-claude-hook --scope user
python -m aiwatcher_cli uninstall-claude-command-gate --scope user
```

See [Hook coverage by tool](#hook-coverage-by-tool) for per-tool
setup notes and which surfaces do and do not support hooks.

## Try the Core Workflows

### 1. See today's AI work

```sh
python -m aiwatcher_cli today
```

Shows local sessions, top project, tools, models, API-equivalent value, and
subscription/limited usage notes. API-equivalent value is not always invoice
spend; it is a normalized usage-pressure signal.

### 2. Check a prompt by hand

The same risk analysis is reachable three ways: **automatically**, via the
[hook](#prompt-preflight-hook) from the Quickstart; **by hand**, with the
command below; and **by paste**, in the dashboard's Prompt tab for
[surfaces that expose no hook](#prompt-companion-for-non-hook-surfaces). This is
the by-hand version — useful for sizing up a prompt before pasting it somewhere,
or on a machine where no hook is installed.

macOS/Linux:

```sh
python -m aiwatcher_cli preflight "Refactor the entire codebase and delete old auth secrets" --tool codex --cwd "$(pwd)"
```

Windows PowerShell:

```powershell
py -m aiwatcher_cli preflight "Refactor the entire codebase and delete old auth secrets" --tool codex --cwd (Get-Location)
```

AIWatcher explains risk, produces an intent-preserving execution brief, and only
shows quantified savings after enough comparable local history exists.

### 3. Use the local dashboard

```sh
python -m aiwatcher_cli ui
```

Open the printed URL. The dashboard includes:

The mockups below use synthetic data — like the PR that introduced them, this
README does not embed real dashboard screenshots, since those can expose
private local paths, project names, and AI usage history.

- **Home**: latest work, useful outcomes, preflight decisions, one next
  recommendation, and a Fresh Start companion when context is getting expensive.
- **Control**: local Prompt Companion for surfaces AIWatcher cannot hook yet.
- **Work**: inspect recent sessions, projects, and changes; rank every prompt in
  a session by cost under **Expensive asks** (cost is cumulative — a short
  prompt late in a long session can still be expensive, since it re-sends the
  whole conversation), mark outcomes, review the changes ledger, and create a
  Fresh Start brief to continue in a fresh session.

  ![Work tab: a session list next to a review drawer showing Expensive asks with the costliest step highlighted, outcome buttons, outcome evidence, and a Fresh Start action.](docs/dashboard-sessions.svg)
- **Evidence**: connect each Fresh Start action or preflight decision to what
  happened next — expected replayed context at risk, observed follow-up sessions,
  resulting usage, inferred savings labeled as estimates, risk change, and
  developer outcome.

  ![Receipts tab: a table of intervention receipts with time, tool/project, decision, risk change, result, and a review action per row.](docs/dashboard-receipts.svg)
- **Spend**: local suggestions for waste and risk — concentrated spend,
  large-context sessions, possible iterative loops, subscription/limited
  usage, and unmarked outcome evidence — plus a privacy-safe daily journal
  and weekly report.

  ![Spend tab: a stacked list of flagged suggestions — concentrated spend, a large-context session, a possible iterative loop, subscription/limited usage, and unmarked outcome evidence — next to a daily journal and weekly report, with privacy contract and enterprise path panels below.](docs/dashboard-insights.svg)

### 4. Mark whether work was useful

```sh
python -m aiwatcher_cli outcome useful
```

Or use the **Review outcome** button in the UI. This is how AIWatcher moves from
token counting toward cost per useful change.

For sessions with a clear costliest turn, the review drawer also retroactively
coaches that prompt under **Prompt worth tightening** — the same findings,
suggestions, and risk analysis preflight runs before execution, applied after
the fact, plus a rewritten tighter version of the prompt for next time.

AIWatcher also shows local outcome evidence before you mark a result: nearby
commits, uncommitted files, and recent test artifacts. These signals stay on
your laptop and are labeled as evidence to review, not automatic truth.
When you inspect or confirm a session, AIWatcher also stores a local
privacy-safe evidence snapshot: commit SHAs, hashes of file paths/test
artifacts, confidence, and inferred outcome. It does not store source diffs,
prompt text, commit subjects, or file contents.

This persisted snapshot is separate from the one-time Fresh Start brief you copy
elsewhere (below), which does include the real commit subject and body. A
commit message is written by whoever made the change specifically to explain
it to a future reader, so unlike prompt text it is not treated as private —
just not persisted to disk beyond the hash above.

### 5. See what each commit cost

Marking outcomes by hand answers "was that session useful?" The change ledger
answers the harder question without being asked: what did this commit cost, and
is the code still there?

```sh
python -m aiwatcher_cli changes --days 30
```

A **change** is a commit, and its cost is the AI spend in that repo since the
previous commit. That is a rule rather than a heuristic — there is no matching
step to get wrong, and two sessions running in parallel on one repo both count
toward the commit they preceded. Attribution is per event, not per session, so a
long session spanning several commits splits across them at the turn the spend
actually happened instead of dumping everything on whichever commit came last.

Three numbers are doing the work:

- **$/line** ranks changes by what they cost to produce.
- **Alive** is line-level survival: of the lines a change added, how many does
  `git blame` still attribute to it — and of the lines it deleted, how many have
  stayed gone. Measuring both directions matters, because a change whose whole
  job was deleting dead code adds nothing, and an additions-only measure would
  score it 0%, exactly backwards. Survival is a **floor, not an exact figure**:
  reformatting reattributes lines to the formatting commit. A blank means *not
  measured*, not *did not survive*, and commits are left alone for 7 days before
  it is measured at all.
- **Unbanked** is spend with no commit behind it — exploration that went
  nowhere, or work still sitting uncommitted. It is reported separately rather
  than folded into the next commit, because it is the most direct measure of
  waste here: money spent with nothing to show for it. It cannot tell those two
  cases apart, and says so rather than guessing.

Commits written by someone else are excluded. They arrived by fetch, so no spend
on your machine belongs to them.

For the same thing one commit at a time, printed right after you commit:

```sh
python -m aiwatcher_cli commit-receipt
python -m aiwatcher_cli install-commit-hook --write
```

The receipt names what the change cost, its lines and $/line, and how that rate
compares with your median over the trailing baseline — so a change that ran
several times your usual rate says so while you still remember writing it. Its
shape, with your own numbers in place of these:

```text
AIWatcher receipt  59d7cb1520  fix(prompt-gate): quote JS string escapes
  This change    $19.47  |  +52/-1 lines  |  $0.37/line
                 62 model calls  |  claude-code
  vs baseline    $0.04/line median over 30 days (41 changes) -- this one is 9.3x your usual
  Still unbanked $58.54 spent here in the last 7d has no commit behind it
```

To see the number while it is still moving rather than after the fact, put it in
Claude Code's status line:

```sh
python -m aiwatcher_cli install-statusline --write
```

```text
* $8.01 since commit | $8.01 session | 100K/turn
```

"Since commit" is unbanked spend as it accumulates — the running total of money
that does not yet have a change behind it.

### 6. Resume work without rebuilding context

When a session gets stale, expensive, or you want to move from Claude to Codex,
generate a target-ready continuation brief:

```sh
python -m aiwatcher_cli resume --search <your-project> --target codex --copy
python -m aiwatcher_cli handoff --session-id <session-id> --target cursor
```

Replace `<your-project>` with part of your own project's name — for a repo at
`~/code/payments-api`, `--search payments` finds it. `--search` matches a
project path, tool, model, or session id, and falls back to a rough topic match
over changed file names, so a fragment is usually enough.

The brief opens with why AIWatcher is suggesting a Fresh Start now: degraded
context health or a stale session, 250+ model calls, 80+ tool calls, or
$5+ in API-equivalent value — so you know whether it's worth acting on
before reading further.

The dashboard also surfaces this as a **Fresh Start companion** on Home when active
local work reaches warning or critical context pressure. AIWatcher gives the
signal one primary action, plus **Inspect**, **Snooze**, and **Dismiss**. It
records only local decision metadata and estimated replayed context at risk,
not prompt or source text. When a later same-project local session appears,
AIWatcher links it as a Fresh Start receipt and shows observed next-session
usage/outcome without claiming a guaranteed counterfactual. Recent Fresh Start
decisions appear in Home, Evidence, and `aiwatcher hook-status`. After you act,
snooze, or dismiss, the same signal stays quiet across the native and browser
companions unless severity worsens.

For a lower-friction desktop flow, start the local dashboard. It now starts
Ambient Watch while the dashboard is open, so warnings can appear without a
second command:

```bash
python -m aiwatcher_cli ui
```

When AIWatcher finds an actionable signal, it opens one small local companion
outside Claude, Codex, Cursor, or your editor. It first tries a native
always-on-top bubble and falls back to the browser companion if native UI is
not available. The primary action matches the signal: copy a fresh-session
brief for critical context, inspect a possible loop, copy a focused checkpoint
for unusual velocity, or review a lane switch under runway pressure. You can
also inspect, snooze for 15 minutes, or dismiss the signal for that session.
AIWatcher does not inject UI into third-party apps.

On macOS, AIWatcher deliberately does not fall back to AppleScript
Notification Center alerts. macOS attributes those alerts to Script Editor,
so clicking **Show** opens Script Editor instead of AIWatcher. The actionable
native companion is used instead; `terminal-notifier` remains supported for
standalone `--notify` use because it can open the local dashboard correctly.

You can still run Ambient Watch standalone:

```bash
python -m aiwatcher_cli watch --notify --overlay
```

Use `watch --once --overlay` as a one-shot smoke test, and set
`AIWATCHER_OVERLAY_MODE=browser` if you prefer the browser companion.

Targets: `generic`, `claude`, `codex`, `cursor`, and `vscode`. The brief lists
recent commit subjects/bodies and changed files for context, any decisions
logged for the session (see below), and keeps the next run focused on one
checkpoint. Add `--include-prompt-excerpt` to also include your own
highest-cost prompt from the session — off by default, and labeled as a
privacy opt-in in both the CLI and the dashboard.

**Or do the whole thing in the dashboard.** Run `python -m aiwatcher_cli ui`,
open **Work**, search for the work, click the session, then press
**Copy Fresh Start brief**. The drawer exposes the same controls as the flags
above: the five targets as buttons, the prompt excerpt as a checkbox, and
**Copy Fresh Start brief** in place of `--copy`. Same capability either way — use
whichever suits the moment.

The **Home** tab also surfaces a Fresh Start button directly on sessions under
context pressure, so you can act on one without going looking for it.

### 7. Log a decision that never became a commit

A commit message explains changes that shipped. It cannot explain an approach
you seriously considered and rejected without ever writing code for it — a
fresh session has no way to know that ground was already covered:

```sh
python -m aiwatcher_cli log-decision "Chose X over Y" --reasoning "..." --rejected "Y"
```

Logged decisions for a session are surfaced in its Fresh Start brief, explicitly
labeled self-reported and not verified against what actually happened.
Nothing is logged automatically. To have an AI session call this itself at
real decision points, install a personal convention — this only ever touches
your own machine's `~/.claude/CLAUDE.md`, never a project file shared with
collaborators:

```sh
python -m aiwatcher_cli install-claude-decision-log --write
```

### 8. Check runtime hygiene

```sh
python -m aiwatcher_cli processes --stale-only
```

Lists local AI-related runtime processes such as Codex/Claude/Cursor-ish
wrappers, `node_repl` kernels, and Computer Use clients. AIWatcher highlights
likely stale or orphaned runtimes by local process signals such as PPID=1, old
age, stopped state, missing working directories, or missing temporary kernel
paths.

This is local CPU/RAM/battery and security hygiene, not a billing claim:
AIWatcher does not assume a stale process is still making model/API calls.
It prints a copyable kill command for review, but never kills a process for you.

### 9. Export local evidence

```sh
python -m aiwatcher_cli export --format json --days 30
python -m aiwatcher_cli export --format json --level events --days 30
```

Exports local session summaries or privacy-safe event hashes. Prompt and source
content are not included.

## Commands

Every command is read-only and runs against the history your tools already keep
locally. Run from a clone with `python -m aiwatcher_cli <command>`, or just
`aiwatcher <command>` once installed.

Cost is shown as **API-equivalent value**. AIWatcher Local separates API-priced
tokens from subscription/plan-limited tokens so you can read the numbers
honestly. Subscription plans may not bill this as incremental spend.

These are the commands worth knowing on day one:

| Command | What it does |
| --- | --- |
| `setup` | Detect your tools and print the next steps for your machine |
| `today` | Today's local AI usage, by tool, model, and project |
| `last` | Inspect the most recent session in detail |
| `sessions` | List and search recent sessions |
| `preflight "..."` | Review a prompt for cost, scope, and safety before running it |
| `changes` | What each commit cost, its $/line, and how much of it is still alive |
| `commit-receipt` | What the latest commit cost, against your usual rate |
| `outcome useful` | Mark how the last session turned out |
| `journal` | Daily summary plus one thing to change next time |
| `resume --target codex --copy` | Continue work in a different tool without rebuilding context |
| `watch --once` | Flag expensive or loop-like work |
| `ui` | Local-only browser dashboard |
| `doctor` | Check tool detection and integration status |

**[Full CLI reference →](docs/CLI.md)** — every command with all its flags,
defaults, and examples, including the hook and wrapper installers, `export`,
`mcp`, `handoff`, `log-decision`, `timeline`, `processes`, and `run`.

That reference is generated from the CLI itself and verified by a test, so it
cannot drift out of date. This table is the curated subset; it is not the
complete list and does not try to be.

## Example Output

> The output below is real, captured from a live machine, with local paths
> replaced by `~/code/payments-api`.

```text
$ aiwatcher today
Today - Wednesday, June 24, 2026
2 sessions | 700.6k API-priced tokens | $16.01 API-equivalent value
Projected month: ~$97.34 API-equivalent at current pace
Note: subscription plans may not bill this as incremental spend.

By tool
Tool              API value   Calls    Tokens Sessions
--------------------------------------------------------
claude-code          $16.01     340    700.6k        2

By model
Model                         API value    Tokens   Calls
----------------------------------------------------------
claude-opus-4-8                  $16.01    700.6k     340

Top project: ~/code/payments-api (100% of today's API-equivalent value)

This week: $17.21
This month: $77.87
```

And the ledger behind it — this one captured from AIWatcher's own repo, so the
commit subjects are its real history:

```text
$ aiwatcher changes --days 30
Cost per change - last 30 days, ranked by spend

Commit          Cost        Lines    $/line  Alive    Subject
----------------------------------------------------------------------------------------
ae260330bd    $43.34      +80/-92     $0.25      -   fix(report): render line survival instea
abdf2097a3    $37.45     +338/-48     $0.10      - ~ fix(health): measure context bloat as a
f116b2e56e    $27.53     +425/-25     $0.06      - ~ fix(cost): count and price prompt-cache
a4147f4544    $21.44      +136/-0     $0.16      -   fix(cost): bill each API request once, n
68be623c8a    $21.23     +865/-33     $0.02    78% ~ feat(outcome): commit survival tracking,
1ac0d61a58    $20.45      +814/-9     $0.02    81%   feat(watch): surface outcome-review noti
59d7cb1520    $19.47       +52/-1     $0.37    92%   fix(prompt-gate): quote JS string escape
be6acc01bc    $15.90     +141/-16     $0.10      8%  feat(handoff): enrich brief with commit/
...

2 of 77 commits have no observed AI spend (hand-written, or committed more than 12h after the work).
59 commit(s) written by someone else were excluded: they arrived by fetch, so no spend on this machine belongs to them.
~ marks a commit that was rebased or amended. Cost is attributed by when the work was authored, not when git restamped it.
Blank survival means not measured, not 'did not survive'. It is a floor either way.

Unbanked: $253.32 of the last 30 days (31%) has no commit behind it ($566.66 reached one).
  ~/code/payments-api                                   $141.23
  ~/code/internal-docs                                    $0.14
  Exploration that went nowhere, or work still uncommitted -- this cannot tell them apart.
```

The last row is the one worth staring at. `be6acc01bc` cost $15.90 and 8% of the
lines it wrote are still in the tree. Nothing failed — it committed, it passed,
nobody reverted it. It just didn't last. That session is invisible to every tool
that reports on errors.

## Why a Loop, Not Another Usage Dashboard

**Usage dashboards** — `ccusage`, vendor consoles, OTel exports — tell you what
happened after the money is gone. They are built to investigate failures, so
they have nothing to say about the session that succeeded expensively, which is
where the waste actually lives.

**API gateways** block and route traffic on the one path routed through them.
They never see the coding agent on your laptop, which is where a growing share
of AI work now happens.

**Your own vigilance** is the fallback, and it is the real control plane at most
companies: Claude Code asks permission for every command until the prompts get
turned off. Watching a terminal by hand defeats the point of using agents.

AIWatcher Local replaces none of that and covers the gaps between them. The
loose prompt that would have burned 3x the tokens is caught at preflight and
rewritten into a scoped brief before the first token is spent. The week where
spend crept up becomes a filter rather than an investigation. And "was any of it
worth it?" gets answered by the ledger — cost per surviving change, not cost per
token.

## The Local Control Loop

- **Plan:** Preflight broad, destructive, vague, or potentially expensive work
  and produce an intent-preserving execution brief.
- **Watch:** Detect large contexts, repeated calls, long sessions, and
  subscription or API usage pressure, plus stale local AI runtimes that may be
  wasting CPU/RAM/battery or expanding local attack surface.
- **Control:** Let the developer use the brief, edit it, run the original,
  cancel, or start fresh when context pressure would waste
  turns. High-risk automatic hooks pause before execution.
- **Prove:** Ledger every commit against the AI spend that produced it — $/line,
  how much of the change is still alive, and the unbanked spend that never
  reached a commit. Cost per surviving change, not cost per token. Intervention
  receipts, session timelines, and local git/test evidence back it up, and you
  can still mark a result useful, rework, or abandoned by hand.
- **Improve:** Compare predicted pressure with observed usage and outcomes,
  log a decision that never became a commit, then recommend one better
  behavior or create a Fresh Start brief for the next fresh session.

The [Prompt Gate](#prompt-preflight-hook) is what makes **Control** interactive
instead of just a log: install the prompt hook with `--gate` and you get to
choose per prompt, rather than reading about the decision afterwards. The
[dangerous-command gate](#dangerous-command-gate) applies the same idea one
layer down, at the shell command rather than the prompt.

Prompt content is processed locally. AIWatcher stores hashes, decisions,
predicted impact, and outcomes, not the original or suggested prompt text.

### Hook coverage by tool

Claude Code CLI, the Code tab in Claude Desktop, Codex CLI/TUI builds that
invoke `UserPromptSubmit`, and Cursor support prompt lifecycle hooks. The
[Quickstart](#3-install-hooks-so-work-is-reviewed-before-it-runs) covers
the basic install; this section covers per-tool behavior and the surfaces where
hooks are not available. Every installer flag is documented in the
[CLI reference](docs/CLI.md#hooks-and-wrappers).

Codex requires one additional trust review: open Codex and run `/hooks`.
Reload Cursor after installing its hook and inspect **Output > Hooks** after a
test prompt. Cursor can block a risky submission but cannot replace prompt text,
so it returns a scoped brief for the developer to resubmit.
Low-risk prompts pass unchanged. Medium-risk prompts receive a scoped execution
brief before tools run. High-risk prompts pause before execution. The Prompt
Gate keeps prompt text transient in the local browser page; AIWatcher persists
hashes, decisions, and predicted impact only. MCP remains available for explicit
local usage questions, but hooks provide automatic pre-send coverage.

Claude's `UserPromptSubmit` contract can add context beside the submitted
prompt or block the prompt; it cannot silently replace the user's text. The two
brief actions therefore add controlling execution guidance alongside the
original request. **Cancel run** blocks the original request entirely. Gate
installations set the host timeout above AIWatcher's three-minute decision
window so the browser does not become detached while the user is reviewing it.

Native hooks cover the corresponding coding-agent surfaces, not general vendor
chat pages. Use `aiwatcher hook-status` after a test prompt to verify actual
coverage instead of assuming that a similarly branded chat surface shares the
same lifecycle.

Current verified boundary: Claude Desktop's **Code** tab invokes the Claude
hook. General Claude Desktop chat does not. The current Codex Desktop
conversation surface tested by the project does not invoke the configured
`UserPromptSubmit` hook; use the Prompt Companion, MCP, wrapper, or a Codex
CLI/TUI surface that records a hook event. AIWatcher does not claim silent
interception where a host application provides no lifecycle API.

For that gap, the [shell wrapper](#prompt-preflight-hook) is a shell-level
fallback rather than a native hook: it intercepts `codex` invocations at the
command line and preflights them through AIWatcher before the real binary runs.

If a Codex prompt appears to bypass AIWatcher, run `aiwatcher hook-status`. If
no recent event appears, that Codex surface did not invoke the
`UserPromptSubmit` hook. If an event appears, AIWatcher ran and the event shows
whether prompt text was found and what risk score was computed.

### Prompt Companion for Non-Hook Surfaces

Not every AI surface exposes prompt lifecycle hooks. For general Claude
Desktop chat, the current Codex Desktop conversation surface, web chat, editor
chats, or any tool AIWatcher cannot hook yet, use the local companion:

```sh
python -m aiwatcher_cli ui
```

Open the **Prompt** tab. Draft or paste a prompt, preflight it locally, edit the
execution brief, then copy either the brief or the original prompt into your AI
tool. This is also the foundation for future browser and editor extensions:
they can call the same local `/api/preflight` endpoint without uploading prompt
text. The experimental `browser-extension/` adapter currently supports
`claude.ai`; `vscode-extension/` provides manual editor, clipboard, and input
commands. Neither is described as universal editor-chat interception.

Building your own integration? `POST /api/preflight` is a supported endpoint
for same-machine callers, alongside `POST /api/outcome`. Request and response
shapes, the origin and size limits, and which endpoints are internal and may
change are in the [HTTP API reference](docs/HTTP-API.md).

Optional semantic risk review: by default AIWatcher scores prompts locally with
fast deterministic rules and makes no LLM calls. If you want a local model or
internal policy service to double-check intent, set:

```sh
export AIWATCHER_RISK_REVIEW_CMD='python /path/to/risk_reviewer.py'
```

AIWatcher sends that command JSON on stdin with the prompt, tool, cwd, and
baseline score; the command returns JSON with `risk`, `score`, `findings`, and
`suggestions`. This lets teams plug in Ollama, a private model, or an
Enterprise policy scorer without hardcoding every destructive phrase. External
reviewers can raise risk by default; lowering deterministic safety findings
requires explicitly setting `AIWATCHER_RISK_REVIEW_ALLOW_LOWERING=1`.

## What It Reads

- **Claude Code:** `~/.claude/projects/**/*.jsonl`, normalized to the git
  project root when possible.
- **Codex:** local rollout JSONL with per-turn token events when available,
  plus local SQLite history in read-only mode as a cumulative fallback.
- **Cursor / Cline / Windsurf:** detected where local history is exposed; token
  and cost detail are intentionally marked limited when a vendor does not store
  it locally.
- **Runtime Hygiene:** local process metadata from `ps` on macOS/Linux:
  PID/PPID, state, age, RSS, CPU, command arguments, and explicit
  `--working-dir` / `--session-id` values when present. It does not read prompt
  text, source files, process memory, or send data to the cloud.
- **Dangerous-command gate receipts:** when Claude Code's `PreToolUse` gate
  flags a shell command, AIWatcher stores a command preview, pattern, decision,
  session id, and SHA-256 command hash. Secret-bearing substrings such as
  database URL credentials, token values, and password/API-key flags are
  redacted before anything is persisted or returned to the AI tool.

Tool coverage depends on what each vendor stores on your machine. When a tool is
installed but token/cost history is not exposed, AIWatcher Local says so instead
of guessing.

See [docs/AIWATCHER_LOCAL.md](docs/AIWATCHER_LOCAL.md) for the full product
boundary, privacy contract, and validation checklist.

## AIWatcher Local vs AIWatcher Enterprise

AIWatcher Local is the open-source, developer-controlled loop for one machine.
It is meant to be genuinely useful on its own.

AIWatcher Enterprise applies the same loop across teams and production agents:
managed budgets, model routing, blocking, approvals, measured policy impact,
audit evidence, SSO/RBAC, and production app governance. Learn more at
<https://www.getaiwatcher.com>.

Enterprise features are additive. AIWatcher Local is never gated behind signup.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and our
[Code of Conduct](CODE_OF_CONDUCT.md). To report a security issue, see
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
