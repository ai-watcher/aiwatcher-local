# AIWatcher Local

Improve and control local AI coding work before, during, and after execution.

AIWatcher Local is a private control loop for Claude Code, Codex, Cursor, and
other local AI coding tools. It preflights risky work, watches local sessions,
records outcomes, and turns the history those tools already keep into useful
cost and security guidance with no account or cloud upload.

![The AIWatcher Local dashboard's Today tab: latest AI work and one thing worth changing up top; useful outcomes, preflight decisions, sessions observed, and API-equivalent value tiles; the latest intervention receipt with predicted savings; projects driving usage and recent sessions below.](docs/dashboard.svg)

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
  - [5. Resume work without rebuilding context](#5-resume-work-without-rebuilding-context)
  - [6. Log a decision that never became a commit](#6-log-a-decision-that-never-became-a-commit)
  - [7. Check runtime hygiene](#7-check-runtime-hygiene)
  - [8. Export local evidence](#8-export-local-evidence)
- [Commands](#commands) — day-one subset; [full CLI reference](docs/CLI.md)
- [Example Output](#example-output)
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

Clone and run directly with Python 3.9+:

```sh
git clone https://github.com/ai-watcher/aiwatcher-local.git
cd aiwatcher-local
python -m pip install -e .
```

After the PyPI release, this becomes:

```sh
pipx install aiwatcher-cli
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

- **Today**: latest work, useful outcomes, preflight decisions, one next
  recommendation, and a handoff bubble when context is getting expensive.
- **Prompt**: local Prompt Companion for surfaces AIWatcher cannot hook yet.
- **Projects**: local repos and folders driving usage.
- **Sessions**: inspect recent work, rank every prompt in a session by cost
  under **Expensive asks** (cost is cumulative — a short prompt late in a long
  session can still be expensive, since it re-sends the whole conversation),
  mark outcomes, and create a handoff capsule to continue in a fresh session.

  ![Sessions tab: a session list next to a review drawer showing Expensive asks with the costliest step highlighted, outcome buttons, outcome evidence, and Create handoff capsule.](docs/dashboard-sessions.svg)
- **Receipts**: connect each preflight decision to its resulting session —
  predicted savings before execution, observed usage after, an inferred
  estimate of what was actually avoided (labeled as inferred, not a
  guaranteed counterfactual), risk change, and developer outcome.

  ![Receipts tab: a table of intervention receipts with time, tool/project, decision, risk change, result, and a review action per row.](docs/dashboard-receipts.svg)
- **Insights**: local suggestions for waste and risk — concentrated spend,
  large-context sessions, possible iterative loops, subscription/limited
  usage, and unmarked outcome evidence — plus a privacy-safe daily journal
  and weekly report.

  ![Insights tab: a stacked list of flagged suggestions — concentrated spend, a large-context session, a possible iterative loop, subscription/limited usage, and unmarked outcome evidence — next to a daily journal and weekly report, with privacy contract and enterprise handoff panels below.](docs/dashboard-insights.svg)

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

This persisted snapshot is separate from the one-time handoff brief you copy
elsewhere (below), which does include the real commit subject and body. A
commit message is written by whoever made the change specifically to explain
it to a future reader, so unlike prompt text it is not treated as private —
just not persisted to disk beyond the hash above.

### 5. Resume work without rebuilding context

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

The brief opens with why AIWatcher is suggesting a handoff now: degraded
context health or a stale session, 250+ model calls, 80+ tool calls, or
$5+ in API-equivalent value — so you know whether it's worth acting on
before reading further.

The dashboard also surfaces this as a **handoff bubble** on Today when active
local work reaches warning or critical context pressure: start a fresh chat,
copy a handoff brief, continue here, or inspect the session. AIWatcher records
only the local decision metadata and estimated replayed context avoided, not
prompt or source text. Recent handoff decisions appear in Today, Receipts, and
`aiwatcher hook-status`. After you copy a handoff, the bubble changes into a
next-step confirmation instead of continuing to nag the same session.

For a lower-friction desktop flow, run ambient watch with the local companion
overlay:

```bash
python -m aiwatcher_cli ui
python -m aiwatcher_cli watch --notify --overlay
```

Keep `watch --notify --overlay` running while you work. When context gets
heavy, AIWatcher opens a small local handoff companion outside Claude, Codex,
Cursor, or your editor. It first tries a native always-on-top desktop bubble
and falls back to the browser companion if native UI is not available. From
there you can copy a fresh-session brief, continue here, or inspect the session
without first navigating to the dashboard. AIWatcher does not inject UI into
third-party apps. Use `watch --once --overlay` only as a one-shot smoke test,
and set `AIWATCHER_OVERLAY_MODE=browser` if you prefer the browser companion.

Targets: `generic`, `claude`, `codex`, `cursor`, and `vscode`. The brief lists
recent commit subjects/bodies and changed files for context, any decisions
logged for the session (see below), and keeps the next run focused on one
checkpoint. Add `--include-prompt-excerpt` to also include your own
highest-cost prompt from the session — off by default, and labeled as a
privacy opt-in in both the CLI and the dashboard.

**Or do the whole thing in the dashboard.** Run `python -m aiwatcher_cli ui`,
open the **Sessions** tab, search for the work, click the session, then press
**Create handoff capsule**. The drawer exposes the same controls as the flags
above: the five targets as buttons, the prompt excerpt as a checkbox, and
**Copy handoff brief** in place of `--copy`. Same capability either way — use
whichever suits the moment.

The **Today** tab also surfaces a handoff button directly on sessions under
context pressure, so you can act on one without going looking for it.

### 6. Log a decision that never became a commit

A commit message explains changes that shipped. It cannot explain an approach
you seriously considered and rejected without ever writing code for it — a
fresh session has no way to know that ground was already covered:

```sh
python -m aiwatcher_cli log-decision "Chose X over Y" --reasoning "..." --rejected "Y"
```

Logged decisions for a session are surfaced in its handoff brief, explicitly
labeled self-reported and not verified against what actually happened.
Nothing is logged automatically. To have an AI session call this itself at
real decision points, install a personal convention — this only ever touches
your own machine's `~/.claude/CLAUDE.md`, never a project file shared with
collaborators:

```sh
python -m aiwatcher_cli install-claude-decision-log --write
```

### 7. Check runtime hygiene

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

### 8. Export local evidence

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

## The Local Control Loop

- **Plan:** Preflight broad, destructive, vague, or potentially expensive work
  and produce an intent-preserving execution brief.
- **Watch:** Detect large contexts, repeated calls, long sessions, and
  subscription or API usage pressure, plus stale local AI runtimes that may be
  wasting CPU/RAM/battery or expanding local attack surface.
- **Control:** Let the developer use the brief, edit it, run the original,
  cancel, or start a fresh-session handoff when context pressure would waste
  turns. High-risk automatic hooks pause before execution.
- **Prove:** Inspect a privacy-safe intervention receipt and session timeline,
  review local git/test evidence, then mark the result useful, rework, or
  abandoned.
- **Improve:** Compare predicted pressure with observed usage and outcomes,
  log a decision that never became a commit, then recommend one better
  behavior or create a handoff capsule for the next fresh session.

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
