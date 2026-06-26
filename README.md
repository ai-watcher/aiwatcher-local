# AIWatcher Local

Know what your local AI coding tools are doing before the invoice, incident, or
runaway session surprises you.

AIWatcher Local gives developers private, zero-code-change visibility into
Claude Code, Codex CLI, Cursor, and other local AI coding activity. It reads the
history those tools already keep on your machine and turns it into an honest
view of usage, tokens, and API-equivalent cost — with no account, no upload, and
no changes to how you work.

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

Clone and run directly — no install required (Python 3.9+):

```bash
git clone https://github.com/ai-watcher/aiwatcher-local.git
cd aiwatcher-local
```

From there, run any of the [commands](#commands) below. The local dashboard
(`ui`) serves on `http://127.0.0.1:8765`; if that port is busy, AIWatcher Local
automatically tries the next available port and prints the URL it picked.

### After PyPI release

```bash
pipx install aiwatcher-cli
aiwatcher today
aiwatcher ui
```

## Commands

Every command is read-only and runs against the history your tools already keep
locally. Run from a clone with `python -m aiwatcher_cli <command>`, or just
`aiwatcher <command>` once installed.

Cost is shown as **API-equivalent value** — AIWatcher Local separates API-priced
tokens from subscription/plan-limited tokens so you can read the numbers honestly.
Subscription plans may not bill this as incremental spend.

> The output below is real, captured from a live machine, with local paths
> replaced by `~/code/payments-api`.

### Detect your tools — `start`, `status`

![aiwatcher start: read-only scan detecting installed AI coding tools.](docs/cli-start.svg)

![aiwatcher status: detected tools and local-only mode.](docs/cli-status.svg)

### Today's usage — `today`

![aiwatcher today: sessions, API-equivalent value, and a breakdown by tool and model.](docs/cli-today.svg)

### Rank usage — `tools`, `projects`

![aiwatcher tools: usage ranked by tool over the last 7 days.](docs/cli-tools.svg)

![aiwatcher projects: usage ranked by project over the last 7 days.](docs/cli-projects.svg)

### Weekly report — `report`

![aiwatcher report: weekly totals and top project, tool, and model.](docs/cli-report.svg)

### Recent sessions — `sessions`

![aiwatcher sessions: recent local AI coding sessions.](docs/cli-sessions.svg)

### Export your data — `export`

Session summaries as JSON:

![aiwatcher export sessions: JSON session summaries.](docs/cli-export-sessions.svg)

Privacy-safe event hashes — `content_hash` only, never prompt or source text:

![aiwatcher export events: privacy-safe event hashes as JSON.](docs/cli-export-events.svg)

### Browser dashboard — `ui`

![aiwatcher ui: starts the local-only dashboard server.](docs/cli-ui.svg)

Opens a local-only dashboard at `http://127.0.0.1:8765` (it auto-picks the next
free port if that one is busy). It's the clickable version of everything above —
see the screenshot at the top of this README.

## What it reads

- **Claude Code** — `~/.claude/projects/**/*.jsonl`, normalized to the git
  project root when possible.
- **Codex CLI** — local SQLite history in read-only mode when available.
- **Cursor / Cline / Windsurf** — detected where local history is exposed; token
  and cost detail are intentionally marked limited when a vendor does not store
  it locally.

Tool coverage depends on what each vendor stores on your machine. When a tool is
installed but token/cost history is not exposed, AIWatcher Local says so instead
of guessing.

See [docs/AIWATCHER_LOCAL.md](docs/AIWATCHER_LOCAL.md) for the full product
boundary, privacy contract, and validation checklist.

## AIWatcher Local vs AIWatcher Enterprise

AIWatcher Local is the open-source, local-only visibility tool for individual
developers. It is meant to be genuinely useful on its own.

AIWatcher Enterprise adds what teams need: shared dashboards, hosted retention,
budget guardrails, anomaly alerts, HITL approvals, audit evidence, SSO/RBAC, and
production app governance. Learn more at <https://www.getaiwatcher.com>.

Enterprise features are additive. AIWatcher Local is never gated behind signup.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and our
[Code of Conduct](CODE_OF_CONDUCT.md). To report a security issue, see
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
