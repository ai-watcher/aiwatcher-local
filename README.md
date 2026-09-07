# AIWatcher Local

[![CI](https://github.com/ai-watcher/aiwatcher-local/actions/workflows/ci.yml/badge.svg)](https://github.com/ai-watcher/aiwatcher-local/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Private guardrails for AI coding work. AIWatcher helps you review risky
prompts before they run, notice expensive or stuck sessions while they are
active, and prove whether the work became useful code afterwards.

It works with local history from tools such as Claude Code, Codex, and Cursor.
No account is required. No cloud upload happens by default. No LLM call happens
unless you explicitly configure optional AI Assist.

![AIWatcher Local Console overview](docs/dashboard.svg)

## Why Developers Use It

- **Catch expensive prompts early:** preflight broad, vague, destructive, or
  high-context work before an AI agent starts spending tokens.
- **Stay out of runaway sessions:** get local nudges for context pressure,
  loops, long-running work, and sessions waiting on you.
- **Start fresh without losing the plot:** create a compact Fresh Start brief
  for continuing work in a new session.
- **Prove what was worth it:** connect local AI sessions to commits, outcomes,
  receipts, and API-equivalent usage.
- **Keep trust visible:** label what is automatic, what is inferred, and what
  the current tool surface cannot prove.

## Install

Recommended for early users: install from GitHub without cloning the repo.
Use Python 3.10+ for this `pipx` install path. AIWatcher itself supports
Python 3.9+ when installed from source. Python 2 is not supported.

Pick one path and ignore the rest.

### One-Line Install

Use this when Python 3.10+, Git, and pipx are already installed.

macOS or Linux:

```sh
pipx install git+https://github.com/ai-watcher/aiwatcher-local.git && pipx ensurepath && ~/.local/bin/aiwatcher setup && ~/.local/bin/aiwatcher start --open-ui
```

Windows PowerShell:

```powershell
pipx install git+https://github.com/ai-watcher/aiwatcher-local.git; pipx ensurepath; & "$env:USERPROFILE\.local\bin\aiwatcher.exe" setup; & "$env:USERPROFILE\.local\bin\aiwatcher.exe" start --open-ui
```

### Missing Prerequisites

Use this if you are not sure what is already installed. These commands check
first and only install missing prerequisites.

macOS:

```sh
command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
command -v python3 >/dev/null || brew install python
command -v git >/dev/null || brew install git
command -v pipx >/dev/null || brew install pipx
if [ -x ~/.local/bin/aiwatcher ]; then
  pipx upgrade aiwatcher-cli
else
  pipx install git+https://github.com/ai-watcher/aiwatcher-local.git
fi
pipx ensurepath
~/.local/bin/aiwatcher setup
~/.local/bin/aiwatcher start --open-ui
```

Ubuntu or Debian:

```sh
if ! command -v python3 >/dev/null || ! command -v git >/dev/null || ! command -v pipx >/dev/null; then
  sudo apt update
fi
command -v python3 >/dev/null || sudo apt install -y python3 python3-pip
command -v git >/dev/null || sudo apt install -y git
command -v pipx >/dev/null || sudo apt install -y pipx
if [ -x ~/.local/bin/aiwatcher ]; then
  pipx upgrade aiwatcher-cli
else
  pipx install git+https://github.com/ai-watcher/aiwatcher-local.git
fi
pipx ensurepath
~/.local/bin/aiwatcher setup
~/.local/bin/aiwatcher start --open-ui
```

For other Linux distributions, install Python 3.10+, Git, and pipx with your
package manager, then use the one-line install.

Windows PowerShell:

```powershell
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
  winget install Python.Python.3.12
  Write-Host "Open a new PowerShell after Python installs, then rerun these commands."
  exit
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  winget install Git.Git
  Write-Host "Open a new PowerShell after Git installs, then rerun these commands."
  exit
}
py -3 --version
py -3 -m pipx --version *> $null
if ($LASTEXITCODE -ne 0) { py -3 -m pip install --user pipx }
$aiwatcher = "$env:USERPROFILE\.local\bin\aiwatcher.exe"
if (Test-Path $aiwatcher) {
  py -3 -m pipx upgrade aiwatcher-cli
} else {
  py -3 -m pipx install git+https://github.com/ai-watcher/aiwatcher-local.git
}
py -3 -m pipx ensurepath
& $aiwatcher setup
& $aiwatcher start --open-ui
```

After opening a new terminal, the shorter command should work:

```sh
aiwatcher setup
aiwatcher start --open-ui
```

`setup` detects local AI tools and prints the next steps for your machine.
`start --open-ui` starts the browser Console, the background Companion, and the
small floating control on macOS and Windows.

## If Install Fails

Use the row matching the error you saw.

| Error | Fix |
| --- | --- |
| Python reports `2.x` or below `3.10` | Install Python 3.10+ for the recommended `pipx` path. AIWatcher does not support Python 2. |
| `externally-managed-environment` | On macOS Homebrew Python, run `brew install pipx`, then use `pipx install ...`. Do not add `--break-system-packages`. |
| `brew: command not found` | Install Homebrew from [brew.sh](https://brew.sh), then rerun the macOS commands. |
| `pipx: command not found` | macOS: `brew install pipx`. Ubuntu/Debian: `sudo apt install pipx`. Windows: use `py -3 -m pipx ...` after installing pipx. |
| `python: command not found` | Use `python3` on macOS/Linux or `py -3` on Windows. |
| `python3: command not found` | Install Python 3.10+. macOS: `brew install python` or use python.org. Windows: use python.org or `winget install Python.Python.3.12`. |
| `py: command not found` | Install Python 3 from python.org or run `winget install Python.Python.3.12`, then open a new PowerShell. |
| `git: command not found` | Install Git. macOS: `xcode-select --install` or `brew install git`. Windows: install Git for Windows or run `winget install Git.Git`. |
| `No module named pip` | Run `python3 -m ensurepip --upgrade` on macOS/Linux or `py -3 -m ensurepip --upgrade` on Windows. |
| `No module named pip3` | Use `python3 -m pip install ...`, not `python3 -m pip3 install ...`. The module name is `pip`. |
| `aiwatcher: command not found` | Open a new terminal after `ensurepath`, or use `~/.local/bin/aiwatcher` / `& "$env:USERPROFILE\.local\bin\aiwatcher.exe"`. |

## First Useful Checks

```sh
aiwatcher doctor
aiwatcher hook-status
aiwatcher preflight "Refactor the checkout flow and delete old auth secrets" --tool codex --cwd "$(pwd)"
```

- `doctor` shows which local tools AIWatcher can read.
- `hook-status` proves whether a tool actually invoked AIWatcher.
- `preflight` gives value immediately, even before hooks are installed.

## Optional Hooks

Hooks let AIWatcher act before the AI tool spends context. Install only the
ones you use:

```sh
aiwatcher install-claude-hook --write --scope user --gate
aiwatcher install-codex-hook --write --scope user --gate
aiwatcher install-cursor-hook --write --scope user --gate
```

For Claude Code CLI, AIWatcher can also review risky shell commands before
they run:

```sh
aiwatcher install-claude-command-gate --write --scope user
```

Then send a small test prompt in your AI tool and verify:

```sh
aiwatcher hook-status
```

If a surface does not invoke hooks, use the Console or Companion **Plan** flow
to preflight prompts manually. AIWatcher does not claim silent protection on
tool surfaces that do not expose a verified lifecycle hook.

## Clone The Codebase

Clone only if you want to contribute, inspect code locally, or use the
dashboard's source-update flow. Most users should use the `pipx` path above.

The source clone path creates a project-local virtual environment, so it does
not modify your Homebrew, system, or Windows Python packages.

macOS or Linux:

```sh
git clone https://github.com/ai-watcher/aiwatcher-local.git
cd aiwatcher-local
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m aiwatcher_cli setup
python -m aiwatcher_cli start --open-ui
```

Windows PowerShell:

```powershell
git clone https://github.com/ai-watcher/aiwatcher-local.git
cd aiwatcher-local
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m aiwatcher_cli setup
python -m aiwatcher_cli start --open-ui
```

The key detail is `python -m pip` inside the virtual environment. Do not use
`python -m pip3`.

## Keep AIWatcher Updated

| Install type | Update command |
| --- | --- |
| GitHub `pipx` install | macOS/Linux: `pipx upgrade aiwatcher-cli`; Windows: `py -3 -m pipx upgrade aiwatcher-cli` |
| GitHub `pip` install | macOS/Linux: `python3 -m pip install --upgrade git+https://github.com/ai-watcher/aiwatcher-local.git`; Windows: `py -3 -m pip install --upgrade git+https://github.com/ai-watcher/aiwatcher-local.git` |
| Source clone | `aiwatcher update --apply`, then `aiwatcher start --open-ui` |
| `uv` tool install | `uv tool upgrade aiwatcher-cli` |

For source clones, the top-bar update badge checks GitHub only when clicked
unless you turn on automatic checks in Settings. Applying an update is a second
explicit step from Settings.

After the first PyPI release, the recommended install/update path becomes:

```sh
pipx install aiwatcher-cli
pipx upgrade aiwatcher-cli
```

Maintainers should use [docs/RELEASE.md](docs/RELEASE.md) before publishing.

## What It Reads

AIWatcher reads local evidence that AI tools already store on your machine.

| Area | What AIWatcher uses |
| --- | --- |
| Claude Code | Local JSONL session history under `~/.claude` when present |
| Codex | Local rollout/session history when available |
| Cursor and other tools | Detected local history where the tool exposes it |
| Git repositories | Commit metadata, diffs, survival checks, and local working tree state |
| Runtime watch | Process metadata such as age, CPU/RAM, command, and known session flags |

AIWatcher stores local receipts, hashes, decisions, outcomes, and metadata. It
does not persist raw prompt text from Prompt Gate decisions. Optional AI Assist
can send bounded prompt/source context only when you configure it and choose a
workflow that uses it.

See [docs/AIWATCHER_LOCAL.md](docs/AIWATCHER_LOCAL.md) for the full privacy and
coverage boundary.

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
| `aiwatcher outcome useful` | Mark the latest session outcome |
| `aiwatcher update` | Check whether a source clone is behind GitHub |

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
It should be useful without signup.

AIWatcher Enterprise adds team policy, budgets, approvals, audit evidence,
SSO/RBAC, and production-agent governance. Enterprise features are additive;
Local is not a locked demo. Learn more at <https://www.getaiwatcher.com>.

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md).

For security reports, use [SECURITY.md](SECURITY.md). Please follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE)
