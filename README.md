# AIWatcher Local

[![CI](https://github.com/ai-watcher/aiwatcher-local/actions/workflows/ci.yml/badge.svg)](https://github.com/ai-watcher/aiwatcher-local/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/aiwatcher-local.svg)](https://pypi.org/project/aiwatcher-local/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Private guardrails for AI coding work. AIWatcher helps you review risky
prompts before they run, notice expensive or stuck sessions while they are
active, and prove whether the work became useful code afterwards.

It works with local history from tools such as Claude Code, Codex, and Cursor.
No account is required. No cloud upload happens by default. No LLM call happens
unless you explicitly configure optional AI Assist.

![AIWatcher Local home dashboard](docs/readme-home-light.svg)

## Contents

- [What You Get](#what-you-get)
- [First Look](#first-look)
- [Why Developers Use It](#why-developers-use-it)
- [Install and Start](#install-and-start)
- [Already Installed](#already-installed)
- [Connect Your AI Tools](#connect-your-ai-tools)
- [Other Install Methods](#other-install-methods)
- [Troubleshooting](#troubleshooting)
- [What It Reads](#what-it-reads)
- [Common Commands](#common-commands)
- [Project Status](#project-status)
- [AIWatcher Local and Enterprise](#aiwatcher-local-and-enterprise)
- [Contributing](#contributing)
- [License](#license)

## What You Get

In the first few minutes, AIWatcher gives one developer a local control loop for
AI coding work:

- **Before the run:** review risky or over-broad prompts before an agent spends
  context.
- **During the run:** notice loops, context pressure, idle sessions, and work
  waiting on you.
- **After the run:** connect AI sessions to commits, exact-state checks, and a
  confirmed push or pull request without guessing that local work was delivered.

No signup is required, and the default install keeps data on your machine.

## First Look

The Home view shows active AI work, context pressure, update status, and the
small Companion control. Plan helps narrow risky prompts before an agent spends
context:

![AIWatcher Plan prompt gate](docs/readme-plan-light.svg)

## Why Developers Use It

- **Catch expensive prompts early:** preflight broad, vague, destructive, or
  high-context work before an AI agent starts spending tokens.
- **Stay out of runaway sessions:** get local nudges for context pressure,
  loops, long-running work, and sessions waiting on you.
- **Start fresh without losing the plot:** create a compact Fresh Start brief
  for continuing work in a new session.
- **Prove what was worth it:** connect local AI sessions to commits, outcomes,
  receipts, and API-equivalent usage.
- **Review delivered work:** after an observed push or pull request, get a
  concise evidence review of the Git range, exact-state checks, linked sessions,
  and unresolved gaps.
- **Keep trust visible:** label what is automatic, what is inferred, and what
  the current tool surface cannot prove.

## Install and Start

For most users, the best path is [`pipx`](https://pipx.pypa.io/latest/).
It installs AIWatcher from
[PyPI](https://pypi.org/project/aiwatcher-local/) in an isolated environment
and makes the `aiwatcher` command available everywhere.

You need Python 3.10 or newer for current versions of `pipx`. Install AIWatcher
and add pipx's command directory to your `PATH`:

```console
pipx install aiwatcher-local
pipx ensurepath
```

Open a new terminal after `ensurepath`, then run:

```console
aiwatcher setup
aiwatcher doctor
aiwatcher start --open-ui
```

`setup` detects supported local AI tools and prints relevant next steps.
`start --open-ui` starts the private local Console and Companion and opens the
Console in your browser. Your normal command after the first setup is simply:

```console
aiwatcher start --open-ui
```

If `pipx` is not installed yet, expand [Other Install Methods](#other-install-methods)
for short platform-specific instructions. Do not clone the repository just to
use AIWatcher.

## Already Installed

If `pipx install` says AIWatcher is already installed, that is expected. Update
the existing installation instead:

```console
pipx upgrade aiwatcher-local
aiwatcher companion stop
aiwatcher ui --restart --open
```

The restart matters because a running Console or Companion keeps using the code
that was loaded when its process started.

Use the update command that matches how AIWatcher was installed:

| Installation | Update command |
| --- | --- |
| `pipx` from PyPI | `pipx upgrade aiwatcher-local` |
| Windows using `py -m pipx` | `py -m pipx upgrade aiwatcher-local` |
| `pip` virtual environment | Activate it, then run `python -m pip install --upgrade aiwatcher-local` |
| `uv tool` | `uv tool upgrade aiwatcher-local` |
| `pipx` directly from GitHub | `pipx reinstall aiwatcher-local` |
| Source clone on clean `main` | `aiwatcher update --apply` |

After any update, restart both local processes:

```console
aiwatcher companion stop
aiwatcher ui --restart --open
```

Merging code into GitHub `main` does not update PyPI installations. PyPI users
receive the change only after a maintainer publishes a newer package version.
GitHub installs and source clones can receive unreleased commits without a PyPI
release.

A pipx installation is shared across your laptop; it does not belong to the
directory where you ran `pipx install`. The Console reports the running
workspace separately from the package location and update channel.

To repair a broken pipx environment, use `pipx reinstall aiwatcher-local`. To
remove it, use `pipx uninstall aiwatcher-local`.

## Connect Your AI Tools

AIWatcher is useful immediately through the Console, Companion, and manual
`preflight`. Hooks add review before supported AI tools spend context. Install
only the integrations you use:

```console
aiwatcher install-claude-hook --write --scope user --gate
aiwatcher install-codex-hook --write --scope user --gate
aiwatcher install-cursor-hook --write --scope user --gate
```

`--write` applies the configuration; without it the installer only previews the
change. User scope is the simplest choice for most people. For one repository,
replace `--scope user` with `--scope project --project-dir /path/to/repo`.

After installing or changing a hook, reload the tool and send a small test
prompt:

| Tool surface | What to do after installation |
| --- | --- |
| Claude Code CLI | End the active session, start Claude Code again, and send a test prompt |
| Claude Desktop Code tab | Quit Claude Desktop completely, reopen it, and test in the Code tab |
| Codex CLI/TUI | Start a new session, run `/hooks` to review and trust the hook, then test |
| Codex Desktop | Quit and reopen the app, then test; hook invocation depends on the current app build |
| Cursor | Reload the Cursor window, then send a test prompt |

Verify what actually ran:

```console
aiwatcher hook-status
```

Claude Desktop general chat and other surfaces without a verified prompt hook
are not silently protected. Use the Console or Companion **Plan** flow, or run:

```console
aiwatcher preflight "Refactor the checkout flow" --tool codex --cwd "$(pwd)"
```

Claude Code can also gate risky shell commands:

```console
aiwatcher install-claude-command-gate --write --scope user
```

Normal package upgrades do not require reinstalling unchanged hook files. Do
restart the Console and Companion after an upgrade, and reload the AI client if
hook configuration changed or `hook-status` does not show the test invocation.

Hook installers prefer the durable `aiwatcher` executable on your `PATH`.
`aiwatcher doctor` reports a hook as unhealthy when its executable or embedded
source checkout has disappeared; reinstall the hook with the same gated command
to repair it without disabling Prompt Gate.

## Review A Delivery

Capture verification against the exact Git state, then push through AIWatcher:

```console
aiwatcher run -- python -m unittest
aiwatcher push -- --set-upstream origin feature/my-change
```

After a confirmed push, Home and Companion show **Delivery review ready**. Open
**Prove** to inspect the delivered commit range, checks tied to that exact HEAD
and dirty-tree fingerprint, sessions linked by commit receipts, and anything
that still needs confirmation. Enter or confirm the objective there before
copying the bounded PR summary. The objective text stays in that browser preview
only; refresh and AIWatcher asks for it again rather than retaining prompt text.
When an exact commit receipt links a delivery to a local session, Prove may
re-read the first user prompt and show it as **inferred**; it remains explicitly
unconfirmed until the developer accepts or replaces it.

The Prove view can also review a checkout before it is pushed. That result is
labelled **Local candidate** and never triggers a delivery-ready signal. The
objective is used for that preview only; AIWatcher retains its hash, not the
text. Ordinary successful `git push` or `gh pr create` commands observed in a
supported structured terminal transcript can also produce confirmed evidence.
Failed, backgrounded, opaque, or ambiguous commands cannot.

## Other Install Methods

The choices below are ordered from general use to contributor setup.

<details>
<summary>Install pipx on macOS, Linux, or Windows</summary>

Skip this if `pipx --version` already works.

**macOS**

```console
brew install pipx
pipx ensurepath
```

**Ubuntu 23.04+, Debian 12+, or newer**

```console
sudo apt update
sudo apt install pipx
pipx ensurepath
```

**Fedora**

```console
sudo dnf install pipx
pipx ensurepath
```

**Windows PowerShell**

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

Open a new terminal after `ensurepath`. On Windows, `py -m pipx` can replace
`pipx` if the standalone command is not yet available. WSL users should follow
the Linux instructions inside WSL and avoid mixing Windows and WSL Python
installations.

For older operating systems, use the official
[pipx installation guide](https://pipx.pypa.io/latest/how-to/install-pipx.html).

</details>

<details>
<summary>Install with pip in a virtual environment</summary>

Use this when you intentionally manage your own Python environment. AIWatcher
supports Python 3.9+, although Python 3.10+ is recommended.

**macOS or Linux**

```console
python3 -m venv ~/.venvs/aiwatcher-local
source ~/.venvs/aiwatcher-local/bin/activate
python -m pip install --upgrade aiwatcher-local
aiwatcher setup
aiwatcher start --open-ui
```

**Windows PowerShell**

```powershell
py -m venv "$HOME\.venvs\aiwatcher-local"
& "$HOME\.venvs\aiwatcher-local\Scripts\Activate.ps1"
python -m pip install --upgrade aiwatcher-local
aiwatcher setup
aiwatcher start --open-ui
```

Activate the environment again before running `aiwatcher` in a new terminal.
Do not install into the macOS, Homebrew, or Linux system Python, and do not use
`--break-system-packages`.

</details>

<details>
<summary>Install unreleased GitHub main with pipx</summary>

Use this for staging changes that are merged to GitHub but not yet published to
PyPI.

A GitHub installation keeps tracking the branch or revision in its install
spec. The workspace where `aiwatcher` starts does not change that update target.
The command below installs or moves an existing feature-branch installation to
`main`:

```console
pipx install --force 'git+https://github.com/ai-watcher/aiwatcher-local.git@main'
aiwatcher companion stop
aiwatcher ui --restart --open
```

Later, use `pipx reinstall aiwatcher-local` to fetch the current commit from the
same GitHub source, even when the package version has not changed.

</details>

<details>
<summary>Clone the source code for development</summary>

Clone only to contribute, inspect the code, or keep an editable checkout.

**macOS or Linux**

```console
git clone https://github.com/ai-watcher/aiwatcher-local.git
cd aiwatcher-local
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m aiwatcher_cli setup
python -m aiwatcher_cli start --open-ui
```

**Windows PowerShell**

```powershell
git clone https://github.com/ai-watcher/aiwatcher-local.git
cd aiwatcher-local
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m aiwatcher_cli setup
python -m aiwatcher_cli start --open-ui
```

On a clean `main` checkout, `aiwatcher update --apply` fetches and fast-forwards
the clone. On a feature branch or checkout with local changes, use normal Git
commands; AIWatcher refuses to overwrite or move that work automatically.

</details>

Users of the original `aiwatcher-cli` 0.1.0 package should migrate once:

```console
pipx uninstall aiwatcher-cli
pipx install aiwatcher-local
```

## Troubleshooting

<details>
<summary>Installation, PATH, updates, and stale processes</summary>

Start with the first command that fails, then use the matching row.

| Error or symptom | Fix |
| --- | --- |
| `pipx: command not found` | Install pipx using the platform section above, run `pipx ensurepath`, and open a new terminal. On Windows, try `py -m pipx`. |
| `aiwatcher: command not found` | Run `pipx ensurepath`, open a new terminal, and confirm `pipx list` includes `aiwatcher-local`. |
| `aiwatcher-local ... already seems to be installed` | This is normal. Use `pipx upgrade aiwatcher-local`; use `pipx reinstall` only for GitHub installs or to repair the environment. |
| `No matching distribution found for aiwatcher-local` | Check spelling, internet/index configuration, and `python --version`. AIWatcher requires Python 3.9+; current pipx requires Python 3.10+. |
| `externally-managed-environment` | Stop using system pip. Install with pipx or create the virtual environment above. Do not add `--break-system-packages`. |
| `No module named pipx` | Install pipx first. On Linux with PEP 668, use `apt`, `dnf`, or the official pipx guide rather than system pip. |
| `No module named pip3` | Use `python3 -m pip`, not `python3 -m pip3`; the module name is `pip`. |
| `python3` or `py` is missing | Install Python 3.10+ from your package manager or [python.org](https://www.python.org/downloads/), then open a new terminal. |
| Companion says `UI offline` | Run `aiwatcher companion stop`, then `aiwatcher ui --restart --open`. |
| Dashboard opens an older checkout | Stop the old process, change to the intended workspace if using a source clone, and restart. Run `aiwatcher doctor` for install details. |
| Upgrade reports no change | Compare `pipx list` with the [PyPI release page](https://pypi.org/project/aiwatcher-local/). GitHub `main` can be newer than PyPI. |
| Hook does not appear to run | Restart or reload the AI tool, send a test prompt, then run `aiwatcher hook-status`. Use manual `preflight` when the surface does not expose the hook. |

Useful diagnostics:

```console
aiwatcher companion status
aiwatcher doctor
aiwatcher hook-status
aiwatcher update
```

</details>

## What It Reads

AIWatcher reads local evidence that AI tools already store on your machine.

| Area | What AIWatcher uses |
| --- | --- |
| Claude Code | Local JSONL session history under `~/.claude` when present |
| Codex | Local rollout/session history when available |
| Cursor and other tools | Detected local history where the tool exposes it |
| Git repositories | Commit metadata, diffs, kept-line checks, and local working tree state |
| Runtime watch | Process metadata such as age, CPU/RAM, command, and known session flags |

AIWatcher stores local receipts, hashes, decisions, outcomes, and metadata. It
does not persist raw prompt text from Prompt Gate decisions. Optional AI Assist
can send bounded prompt/source context only when you configure it and choose a
workflow that uses it.

See [docs/AIWATCHER_LOCAL.md](docs/AIWATCHER_LOCAL.md) for the full privacy and
coverage boundary.

## Laptop Footprint

AIWatcher is a Python package with static dashboard assets, not a native
background daemon. It does nothing in the background until you run
`aiwatcher start`, `aiwatcher companion start`, or install login autostart.

Measured from this repo on macOS with Python 3.14:

| Area | Observed footprint |
| --- | --- |
| Wheel artifact | 530 KB |
| Installed AIWatcher package | 3.9 MB, excluding the Python/pipx environment |
| Python dependencies | None declared by AIWatcher |
| Idle dashboard process | Usually tens of MB RSS, near 0% CPU when idle |
| Dashboard + Companion | Near 0% CPU between scans; short scan spikes depend on local history size |

On the measured machine, a Companion startup scan over recent local AI history
briefly used more CPU and memory, then settled back near idle. Larger local
Claude/Codex/Cursor histories can make that scan peak higher. The default
Companion interval is 30 seconds, and you can stop it any time:

```sh
aiwatcher companion stop
```

## Common Commands

| Command | Purpose |
| --- | --- |
| `aiwatcher setup` | Detect tools and show recommended setup |
| `aiwatcher start --open-ui` | Start the Console and Companion |
| `aiwatcher doctor` | Check local detection and integration health |
| `aiwatcher hook-status` | Verify hook invocation |
| `aiwatcher preflight "..."` | Review a prompt manually |
| `aiwatcher sessions` | Review recent local AI sessions |
| `aiwatcher changes --days 30` | See AI-attributed commit evidence |
| `aiwatcher delivery-review` | Preview a local checkout without claiming delivery |
| `aiwatcher push -- [git push args]` | Push and create a review only when the remote HEAD is confirmed |
| `aiwatcher outcome useful` | Mark the latest session outcome |
| `aiwatcher update` | Check the running source or package installation for updates |

Full command reference: [docs/CLI.md](docs/CLI.md).

## Project Status

AIWatcher Local is an early open-source release. The local-first workflow is
usable today, but hook coverage depends on what each AI tool exposes on your
machine. The UI is moving quickly, so screenshots and docs may change while the
core privacy boundary stays stable.

Useful next reads:

- [Product and validation notes](docs/AIWATCHER_LOCAL.md)
- [CLI reference](docs/CLI.md)
- [HTTP API reference](docs/HTTP-API.md)
- [Release checklist](docs/RELEASE.md)

## AIWatcher Local and Enterprise

AIWatcher Local is the open-source, developer-controlled loop for one machine.
It should be useful without signup, a cloud account, or a team admin.

AIWatcher Enterprise adds team policy, budgets, approvals, audit evidence,
SSO/RBAC, managed deployment, central retention, org dashboards, and
production-agent governance. Enterprise features are additive; Local is not a
locked demo.

The Apache-2.0 license covers this code. It does not grant rights to the
AIWatcher name, logo, hosted service, or Enterprise control plane. Learn more at
<https://www.getaiwatcher.com>.

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md).

For security reports, use [SECURITY.md](SECURITY.md). Please follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

[Apache License 2.0](LICENSE)
