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
- [Try the Core Workflows](#try-the-core-workflows)
  - [1. See today's AI work](#1-see-todays-ai-work)
  - [2. Preflight a risky prompt](#2-preflight-a-risky-prompt)
  - [3. Use the local dashboard](#3-use-the-local-dashboard)
  - [4. Mark whether work was useful](#4-mark-whether-work-was-useful)
  - [5. Resume work without rebuilding context](#5-resume-work-without-rebuilding-context)
  - [6. Log a decision that never became a commit](#6-log-a-decision-that-never-became-a-commit)
  - [7. Check runtime hygiene](#7-check-runtime-hygiene)
  - [8. Export local evidence](#8-export-local-evidence)
- [Commands](#commands)
- [Example Output](#example-output)
- [The Local Control Loop](#the-local-control-loop)
  - [Automatic Prompt Preflight](#automatic-prompt-preflight)
  - [Prompt Companion for Non-Hook Surfaces](#prompt-companion-for-non-hook-surfaces)
- [What It Reads](#what-it-reads)
- [AIWatcher Local vs AIWatcher Enterprise](#aiwatcher-local-vs-aiwatcher-enterprise)
- [After PyPI Release](#after-pypi-release)
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

Clone and run directly with Python 3.10+:

```sh
git clone https://github.com/ai-watcher/aiwatcher-local.git
cd aiwatcher-local
python -m aiwatcher_cli today
python -m aiwatcher_cli preflight "Refactor auth and delete old credentials"
python -m aiwatcher_cli last
python -m aiwatcher_cli ui
```

`ui` starts a local-only dashboard on `http://127.0.0.1:8765`. If that port is
busy, AIWatcher Local automatically tries the next available port and prints the
URL it picked.

For local development, install the package entrypoint:

```sh
python -m pip install -e .
aiwatcher today
aiwatcher ui
```

On Windows PowerShell, the same commands work. If `python` is not on PATH, use
the Python launcher:

```powershell
py -m aiwatcher_cli today
py -m aiwatcher_cli ui
py -m pip install -e .
aiwatcher today
```

The examples below use `python -m aiwatcher_cli`; Windows users can replace
`python` with `py` when needed.

## Try the Core Workflows

### 1. See today's AI work

```sh
python -m aiwatcher_cli today
```

Shows local sessions, top project, tools, models, API-equivalent value, and
subscription/limited usage notes. API-equivalent value is not always invoice
spend; it is a normalized usage-pressure signal.

### 2. Preflight a risky prompt

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
python -m aiwatcher_cli resume --search orcha --target codex --copy
python -m aiwatcher_cli handoff --session-id <session-id> --target cursor
```

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

When context gets heavy, AIWatcher opens a small local handoff companion outside
Claude, Codex, Cursor, or your editor. From there you can copy a fresh-session
brief, continue here, or inspect the session without first navigating to the
dashboard. AIWatcher does not inject UI into third-party apps.

Targets: `generic`, `claude`, `codex`, `cursor`, and `vscode`. The brief lists
recent commit subjects/bodies and changed files for context, any decisions
logged for the session (see below), and keeps the next run focused on one
checkpoint. Add `--include-prompt-excerpt` to also include your own
highest-cost prompt from the session — off by default, and labeled as a
privacy opt-in in both the CLI and the dashboard.

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

```sh
python -m aiwatcher_cli start              # detect tools and run a one-time local scan
python -m aiwatcher_cli status             # show detected tools and local status
python -m aiwatcher_cli today              # today's local AI usage
python -m aiwatcher_cli last               # inspect the latest local AI session
python -m aiwatcher_cli timeline           # privacy-safe event timeline
python -m aiwatcher_cli handoff            # create a fresh-session handoff capsule
python -m aiwatcher_cli resume --target codex --copy  # find a session and continue it elsewhere
python -m aiwatcher_cli log-decision "..." --reasoning "..." --rejected "..."  # note a rejected approach
python -m aiwatcher_cli install-claude-decision-log --write  # personal convention to log decisions automatically
python -m aiwatcher_cli journal            # daily usage summary plus one thing to change next time
python -m aiwatcher_cli watch --once       # detect expensive or loop-like work
python -m aiwatcher_cli run -- npm test    # run any command, then summarize the AI session alongside it
python -m aiwatcher_cli doctor             # check tool detection and hook/wrapper install status
python -m aiwatcher_cli processes          # local AI runtime hygiene; suggests manual cleanup only
python -m aiwatcher_cli processes --stale-only
python -m aiwatcher_cli preflight "..."    # review work before execution
python -m aiwatcher_cli codex "..."        # preflight, choose, and launch Codex
python -m aiwatcher_cli claude "..."       # preflight, choose, and launch Claude
python -m aiwatcher_cli outcome useful     # mark the latest result
python -m aiwatcher_cli hook-status        # inspect hook invocations and recent decisions
python -m aiwatcher_cli tools --days 7     # rank usage by tool
python -m aiwatcher_cli projects --days 7  # rank usage by project
python -m aiwatcher_cli report --days 7    # weekly local report
python -m aiwatcher_cli sessions --days 1  # recent local sessions
python -m aiwatcher_cli sessions --search orcha --days 30
python -m aiwatcher_cli export --format json --days 30      # export session summaries
python -m aiwatcher_cli export --format json --level events # privacy-safe event hashes
python -m aiwatcher_cli ui                 # local-only browser dashboard
python -m aiwatcher_cli mcp                # local stdio MCP server
```

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

**Prompt Gate** is what makes **Control** interactive instead of just a log:
install a native hook with `--gate` and a risky prompt opens a local decision
screen with **Add safer brief**, **Add edited brief**, **Run original**, and
**Cancel run** before anything executes. Prompt text stays transient in that
local browser page — AIWatcher persists hashes, decisions, and predicted
impact only. See [Automatic Prompt Preflight](#automatic-prompt-preflight)
below for install commands and per-tool setup notes.

![AIWatcher Prompt Gate: a local decision screen showing risk score, guardrail chips, findings and suggestions, the original prompt, a proposed execution brief, and the Add safer brief / Add edited brief / Run original / Cancel run actions.](docs/dashboard-prompt-gate.svg)

Prompt content is processed locally. AIWatcher stores hashes, decisions,
predicted impact, and outcomes, not the original or suggested prompt text.

### Automatic Prompt Preflight

Claude Code CLI, the Code tab in Claude Desktop, Codex CLI/TUI builds that
invoke `UserPromptSubmit`, and Cursor support prompt lifecycle hooks. Install
AIWatcher once:

```sh
python -m aiwatcher_cli install-claude-hook --write --scope user
python -m aiwatcher_cli install-codex-hook --write --scope user
python -m aiwatcher_cli install-cursor-hook --write --scope user
```

For the richer beta workflow, add `--gate`. Risky prompts open a local decision
screen with **Add safer brief**, **Add edited brief**, **Run original**, and
**Cancel run**:

```sh
python -m aiwatcher_cli install-claude-hook --write --scope user --gate
python -m aiwatcher_cli install-codex-hook --write --scope user --gate
python -m aiwatcher_cli install-cursor-hook --write --scope user --gate
```

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

For that gap, the wrapper is a shell-level fallback rather than a native hook:
it installs a shell function (in your shell rc file) that intercepts `codex`
invocations at the command line and preflights them through AIWatcher before
the real binary runs:

```sh
python -m aiwatcher_cli install-codex-wrapper --write
```

If a Codex prompt appears to bypass AIWatcher, run:

```sh
python -m aiwatcher_cli hook-status
```

If no recent event appears, that Codex surface did not invoke the
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

`POST /api/preflight` contract (loopback-only, same-machine callers):

```jsonc
// Request
{"prompt": "...", "tool": "agent", "cwd": "/path/to/project"}  // tool/cwd optional

// Response
{
  "risk": "low|medium|high", "score": 0,
  "findings": ["..."], "suggestions": ["..."],
  "suggested_prompt": "...",       // the scoped execution brief
  "impact_label": "...",           // human-readable estimated pressure, honesty-gated
  "privacy": "..."                 // what is/isn't persisted, for display next to the result
}
```

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

## After PyPI Release

```sh
pipx install aiwatcher-cli
aiwatcher today
aiwatcher ui
```

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and our
[Code of Conduct](CODE_OF_CONDUCT.md). To report a security issue, see
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
