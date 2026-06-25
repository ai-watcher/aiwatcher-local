# AIWatcher Local

Know what your local AI coding tools are doing before the invoice, incident, or
runaway session surprises you.

AIWatcher Local gives developers private, zero-code-change visibility into
Claude Code, Codex CLI, Cursor, and other local AI coding activity. It reads the
history those tools already keep on your machine and turns it into an honest
view of usage, tokens, and API-equivalent cost — with no account, no upload, and
no changes to how you work.

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

```bash
python -m aiwatcher_cli start              # detect tools and run a one-time local scan
python -m aiwatcher_cli status             # show detected tools and local status
python -m aiwatcher_cli today              # today's local AI usage
python -m aiwatcher_cli tools --days 7     # rank usage by tool
python -m aiwatcher_cli projects --days 7  # rank usage by project
python -m aiwatcher_cli report --days 7    # weekly local report
python -m aiwatcher_cli sessions --days 1  # recent local sessions
python -m aiwatcher_cli export --format json --days 30      # export session summaries
python -m aiwatcher_cli export --format json --level events # privacy-safe event hashes
python -m aiwatcher_cli ui                 # local-only browser dashboard
```

Cost is shown as **API-equivalent value**. Subscription plans may not bill this
as incremental spend — AIWatcher Local separates API-priced tokens from
plan/limited tokens so you can interpret the numbers honestly.

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
