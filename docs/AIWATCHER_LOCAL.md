# AIWatcher Local

See what your local AI coding tools are doing before the invoice, incident, or
runaway session surprises you.

AIWatcher Local is the open-source individual tier of AIWatcher. It gives one
developer a private, zero-code-change view of Claude Code, Codex CLI, Cursor,
and other local AI coding activity. The implementation includes a read-only
local scanner, CLI, JSON export, and browser dashboard, but the product promise
is bigger than collection:

> Local visibility for individual developers. Enterprise control for teams.

## What Developers Get

- Prompt preflight with intent-preserving execution briefs.
- Native Claude Code and Codex prompt hooks for automatic coverage.
- Live local watch signals for cost, context growth, and loop-like behavior.
- A local dashboard at `http://127.0.0.1:8765`.
- A CLI for fast terminal checks and agent forwarding.
- Click-through local project and session drill-down.
- Useful, rework, and abandoned outcomes stored only on the laptop.
- Privacy-safe intervention history using prompt hashes rather than prompt text.
- A local weekly report.
- Project, tool, model, session, token, and API-equivalent value breakdowns.
- Separation between API-priced tokens and subscription/limited tokens.
- Local JSON export for personal analysis, including privacy-safe event hashes.
- A privacy contract that does not require an account.

## Privacy Contract

- Local-only by default.
- Read-only.
- No LLM API calls.
- No phone-home telemetry.
- No source-code or prompt-content storage in summaries.
- No cloud upload unless the user explicitly connects AIWatcher Enterprise.

This trust boundary is the product. If AIWatcher Local cannot explain what it
reads and why, it should not read it.

## Platform Support

AIWatcher Local is intended to run on macOS, Linux, and Windows with the same
CLI commands:

```bash
python -m aiwatcher_cli today
python -m aiwatcher_cli ui
```

Cross-platform behavior:

- The CLI and local dashboard use Python standard-library APIs and avoid
  platform-specific date formatting.
- `aiwatcher ui --restart` uses `lsof/kill` on macOS/Linux and
  `netstat/taskkill` on Windows.
- Claude Code and Codex CLI are read from home-directory history when present
  (`~/.claude`, `~/.codex`) and common Windows application-data candidates.
- Cursor, Cline, and Windsurf detection checks both macOS application-support
  paths and common `%APPDATA%` locations.

Tool coverage still depends on what each vendor stores locally on that OS.
When a tool is installed but token/cost history is not exposed, AIWatcher Local
should say so instead of guessing.

## Package Boundary

The Python SDK already owns the `ai-watcher` PyPI package and imports as
`aiwatcher`. Keep that package focused on application instrumentation.

AIWatcher Local ships separately:

- PyPI distribution: `aiwatcher-cli`
- Terminal command: `aiwatcher`
- Internal module: `aiwatcher_cli`

This avoids import-name collisions while still giving developers the clean
command they expect:

```bash
pip install aiwatcher-cli
aiwatcher today
aiwatcher ui
```

## Run From This Repo

Before a PyPI release you can run everything straight from a clone (Python 3.9+):

```bash
python -m aiwatcher_cli start
python -m aiwatcher_cli today
python -m aiwatcher_cli last
python -m aiwatcher_cli timeline
python -m aiwatcher_cli journal
python -m aiwatcher_cli watch --once
python -m aiwatcher_cli preflight "Refactor auth and delete old credentials"
python -m aiwatcher_cli codex --dry-run "Refactor the entire codebase"
python -m aiwatcher_cli install-claude-hook
python -m aiwatcher_cli install-codex-hook
python -m aiwatcher_cli tools --days 7
python -m aiwatcher_cli projects --days 7
python -m aiwatcher_cli report --days 7
python -m aiwatcher_cli sessions --days 1
python -m aiwatcher_cli export --format json --days 30
python -m aiwatcher_cli export --format json --level events --days 7
python -m aiwatcher_cli ui
```

`start` is a one-time local scan, not a long-running daemon.

## Lifecycle Model

AIWatcher Local follows the same lifecycle as AIWatcher Enterprise, while
keeping authority with the individual developer:

1. **Plan** — preflight work and add only the scope, checkpoint, verification,
   and safety controls relevant to the original request.
2. **Watch** — detect large contexts, repeated calls, long sessions, and unusual
   local usage while work is happening.
3. **Control** — use the execution brief, edit it, run the original, cancel, or
   stop work that is becoming wasteful.
4. **Prove** — inspect the local timeline and record whether the result was
   useful, needed rework, or was abandoned.
5. **Improve** — compare interventions and outcomes, then recommend one better
   behavior for the next run.

The local state connects intervention hashes, predicted impact, session IDs,
and outcomes. It does not store original or suggested prompt text.

## Automatic Preflight

Install the native prompt hooks once:

```bash
python -m aiwatcher_cli install-claude-hook --write --scope user
python -m aiwatcher_cli install-codex-hook --write --scope user
```

Use `--gate` when you want the local decision screen before risky work starts:

```bash
python -m aiwatcher_cli install-claude-hook --write --scope user --gate
python -m aiwatcher_cli install-codex-hook --write --scope user --gate
```

After installing the Codex hook, open Codex and run `/hooks` to inspect and
trust the command. Low-risk prompts pass unchanged, medium-risk prompts receive
an execution brief as additional context, and high-risk prompts pause before
execution. With Prompt Gate enabled, medium and high-risk prompts open a
one-shot localhost page with four choices:

- **Use brief** — continue with AIWatcher's scoped execution brief as context.
- **Use edited brief** — tune the brief locally before it reaches the agent.
- **Run original** — proceed unchanged and record that decision locally.
- **Cancel run** — stop the prompt before tools execute.

The Prompt Gate page may display the prompt while you decide, but it does not
persist prompt text. Local state stores hashes, decisions, risk findings, and
predicted impact only.

To verify whether a Codex/Claude hook actually ran, use:

```bash
python -m aiwatcher_cli hook-status
```

No recent event means the current AI surface did not invoke the hook. A recent
event means AIWatcher ran; the event shows whether prompt text was found and
which risk score was computed. This matters because not every Codex or Claude
surface uses the same hook runtime.

## Prompt Companion for Non-Hook Surfaces

Some surfaces do not expose a prompt lifecycle hook. Claude Desktop chat,
browser chat, editor sidebars, and vendor-specific desktop apps cannot be
silently intercepted unless they provide an extension point.

For those, `aiwatcher ui` includes a **Prompt** tab. It gives the same preflight
logic in a local widget:

1. Draft or paste the prompt.
2. Review risk, reasons, and expected impact.
3. Edit the execution brief.
4. Copy either the brief or the original prompt into the AI tool.

This is intentionally useful on its own, and it creates the local API contract
for future extensions. Browser/editor integrations can call
`POST /api/preflight` on the local AIWatcher server and show the same decision
inside the user's current workflow.

## The Wow Moment

`aiwatcher ui` should be the first thing a developer shows a friend. It answers:

- Which local AI coding tools are active?
- Which projects are driving usage?
- Which models are generating the most API-equivalent value?
- Which usage is API-priced versus subscription/limited?
- Which recent sessions are worth inspecting?
- What happened inside a session without showing prompt/source text?
- What can I learn without uploading prompts, source, or telemetry?

`aiwatcher today` is the fast terminal version of that same story.

Example CLI shape:

```text
Today - Saturday, June 20, 2026
5m of measured AI work
6 sessions · 882.5k API-priced tokens · 310.5M plan/limited tokens observed · $12.99 API-equivalent value
Projected month: ~$77.66 API-equivalent at current pace
Note: subscription plans may not bill this as incremental spend.

By tool
Tool              API value   Calls    Tokens Sessions
--------------------------------------------------------
claude-code          $12.99    1519    882.5k        3
codex-cli             $0.00       3    310.5M        3

Top project: ~/code/your-app (62% of today's API-equivalent value)
```

For the browser dashboard:

```bash
python -m aiwatcher_cli ui
```

Then open `http://127.0.0.1:8765`. The dashboard is served locally from the
standard library, uses no external assets, and exposes a read-only JSON summary
at `/api/summary`. Project rows and recent sessions are clickable for local
drill-down.

If `8765` is already in use, AIWatcher Local automatically tries the next
available port and prints the URL it selected. To restart an old local server on
the requested port:

```bash
python -m aiwatcher_cli ui --restart
```

For strict scripts that must fail instead of changing ports:

```bash
python -m aiwatcher_cli ui --no-port-fallback
```

For a weekly terminal report:

```bash
python -m aiwatcher_cli report --days 7
```

For privacy-safe event evidence:

```bash
python -m aiwatcher_cli export --format json --level events --days 7 > aiwatcher-local-events.json
python -m aiwatcher_cli export --format json --since 2026-06-01 > aiwatcher-local-export.json
```

Event exports include timestamps, models, token counts, API-equivalent value,
project paths, event types, and content hashes. They do not include prompt text
or source code. `--since` accepts ISO dates or datetimes and returns a
user-facing error when the date is invalid.

## How To Validate Locally

Use this as the first test script:

```bash
git clone https://github.com/ai-watcher/aiwatcher-local.git
cd aiwatcher-local
python -m aiwatcher_cli start
python -m aiwatcher_cli today
python -m aiwatcher_cli tools --days 7
python -m aiwatcher_cli projects --days 7 --limit 5
python -m aiwatcher_cli report --days 7
python -m aiwatcher_cli sessions --days 1 --limit 5
python -m aiwatcher_cli export --format json --days 7 > aiwatcher-local-export.json
python -m aiwatcher_cli export --format json --level events --days 7 > aiwatcher-local-events.json
python -m aiwatcher_cli ui
```

What to check:

- It should not ask for an API key.
- It should not make any network calls.
- It should detect installed tools.
- It should show real local Claude Code usage if `~/.claude/projects` exists.
- It should show real project folders, not parent folders like your home directory.
- It should honestly mark tools with limited data instead of guessing.
- The JSON export should contain metadata and aggregates, not prompt text or code.
- The event export should contain hashes, not prompt text or code.
- The dashboard time-window selector should visibly update the values.
- Project rows and recent sessions should open useful detail.

## Current Local Sources

- Claude Code: reads `~/.claude/projects/**/*.jsonl`, uses event-level `cwd`,
  and normalizes nested folders to the git project root when possible.
- Codex CLI: reads `~/.codex/state_5.sqlite` in read-only mode when available.
- Cursor: detects local AI log activity where available, but cost/token detail is
  intentionally marked limited because Cursor does not reliably expose it locally.

## Current MVP Limits

- `start` is a one-time local scan, not a long-running daemon.
- Claude Code has the richest support today.
- Codex CLI support depends on local SQLite accessibility and schema shape.
- Cursor support is intentionally conservative until local token/cost data is reliable.
- Event-level export currently has the richest support for Claude Code. Codex
  and Cursor remain session-summary first until their local event shapes are
  reliable enough to expose without guessing.

## OSS vs Enterprise Boundary

AIWatcher Local answers:

> What are my local AI coding tools doing, where is usage growing, and what is
> the API-equivalent value before the invoice arrives?

AIWatcher Enterprise answers:

> What is our company AI work doing, what should we control, and how do we prove
> what happened?

Enterprise adds team and org dashboards, hosted retention, identity/SSO/RBAC,
cost and risk controls, evidence packs, HITL approvals, anomaly detection,
budget guardrails, org policy, and integrations such as Slack, Grafana,
Datadog, and SIEM/SOC workflows. Learn more at <https://www.getaiwatcher.com>.

Enterprise should appear only when the user asks for something local OSS cannot
honestly provide — shared team history, long retention, scheduled evidence
packs, or policy enforcement. AIWatcher Local is never gated behind signup.

## Next Product Steps

The remaining OSS work completes the loop rather than adding more dashboards:

1. Polish Prompt Gate and Prompt Companion after beta feedback: tighter copy,
   command-host quirks, timeout defaults, and copy/paste ergonomics.
2. Link hook interventions to the resulting session automatically and calculate
   measured impact only after comparable evidence exists.
3. Move watch from periodic summaries to reliable active-session loop and
   context-growth alerts, with developer-controlled pause or stop actions.
4. Infer outcomes from tests, commits, and rework while keeping manual outcome
   correction available.
5. Turn Today into the daily control-loop home: active work, interventions,
   useful outcomes, measured impact, and one recommendation.
6. Package the Codex integration as a plugin and add browser/editor companion
   extensions that call the local `/api/preflight` endpoint where true hooks are
   unavailable.
7. Add `pipx` and Homebrew installation only after the workflow is stable.
