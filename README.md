# AIWatcher Local

Improve and control local AI coding work before, during, and after execution.

AIWatcher Local is a private control loop for Claude Code, Codex, Cursor, and
other local AI coding tools. It preflights risky work, watches local sessions,
records outcomes, and turns the history those tools already keep into useful
cost and security guidance with no account or cloud upload.

![The AIWatcher Local dashboard: API-equivalent value, projected month, sessions, and tokens up top; top projects, models, recent sessions, and privacy guarantees below.](docs/dashboard.svg)

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

- **Today**: latest work, useful outcomes, preflight decisions, and one next
  recommendation.
- **Prompt**: local Prompt Companion for surfaces AIWatcher cannot hook yet.
- **Projects**: local repos and folders driving usage.
- **Sessions**: inspect recent work and mark outcomes.
- **Insights**: privacy-safe journal and weekly report.

### 4. Mark whether work was useful

```sh
python -m aiwatcher_cli outcome useful
```

Or use the **Review outcome** button in the UI. This is how AIWatcher moves from
token counting toward cost per useful change.

### 5. Export local evidence

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
python -m aiwatcher_cli journal            # one daily improvement recommendation
python -m aiwatcher_cli watch --once       # detect expensive or loop-like work
python -m aiwatcher_cli preflight "..."    # review work before execution
python -m aiwatcher_cli codex "..."        # preflight, choose, and launch Codex
python -m aiwatcher_cli claude "..."       # preflight, choose, and launch Claude
python -m aiwatcher_cli outcome useful     # mark the latest result
python -m aiwatcher_cli hook-status        # debug recent Claude/Codex hook invocations
python -m aiwatcher_cli tools --days 7     # rank usage by tool
python -m aiwatcher_cli projects --days 7  # rank usage by project
python -m aiwatcher_cli report --days 7    # weekly local report
python -m aiwatcher_cli sessions --days 1  # recent local sessions
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
2 sessions · 700.6k API-priced tokens · $16.01 API-equivalent value
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
  subscription or API usage pressure.
- **Control:** Let the developer use the brief, edit it, run the original, or
  cancel. High-risk automatic hooks pause before execution.
- **Prove:** Inspect a privacy-safe session timeline and mark the result useful,
  rework, or abandoned.
- **Improve:** Use the Today view and journal to connect interventions with
  outcomes and recommend one better behavior for the next run.

Prompt content is processed locally. AIWatcher stores hashes, decisions,
predicted impact, and outcomes, not the original or suggested prompt text.

### Automatic Prompt Preflight

Claude Code and Codex CLI/TUI support prompt lifecycle hooks. Install AIWatcher
once:

```sh
python -m aiwatcher_cli install-claude-hook --write --scope user
python -m aiwatcher_cli install-codex-hook --write --scope user
```

For the richer beta workflow, add `--gate`. Risky prompts open a local decision
screen with **Use brief**, **Use edited brief**, **Run original**, and
**Cancel run**:

```sh
python -m aiwatcher_cli install-claude-hook --write --scope user --gate
python -m aiwatcher_cli install-codex-hook --write --scope user --gate
```

Codex requires one additional trust review: open Codex and run `/hooks`.
Low-risk prompts pass unchanged. Medium-risk prompts receive a scoped execution
brief before tools run. High-risk prompts pause before execution. The Prompt
Gate keeps prompt text transient in the local browser page; AIWatcher persists
hashes, decisions, and predicted impact only. MCP remains available for explicit
local usage questions, but hooks provide automatic pre-send coverage.

Current limitation: not every Codex or Claude surface invokes these hooks. Some
desktop/chat/editor surfaces need the Prompt Companion or a future extension
instead of automatic interception.

If a Codex prompt appears to bypass AIWatcher, run:

```sh
python -m aiwatcher_cli hook-status
```

If no recent event appears, that Codex surface did not invoke the
`UserPromptSubmit` hook. If an event appears, AIWatcher ran and the event shows
whether prompt text was found and what risk score was computed.

### Prompt Companion for Non-Hook Surfaces

Not every AI surface exposes prompt lifecycle hooks. For Claude Desktop, web
chat surfaces, editor chats, or any tool AIWatcher cannot hook yet, use the
local companion:

```sh
python -m aiwatcher_cli ui
```

Open the **Prompt** tab. Draft or paste a prompt, preflight it locally, edit the
execution brief, then copy either the brief or the original prompt into your AI
tool. This is also the foundation for future browser and editor extensions:
they can call the same local `/api/preflight` endpoint without uploading prompt
text.

## What It Reads

- **Claude Code:** `~/.claude/projects/**/*.jsonl`, normalized to the git
  project root when possible.
- **Codex CLI:** local SQLite history in read-only mode when available.
- **Cursor / Cline / Windsurf:** detected where local history is exposed; token
  and cost detail are intentionally marked limited when a vendor does not store
  it locally.

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
