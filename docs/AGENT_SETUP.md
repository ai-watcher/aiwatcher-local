# AIWatcher Local Agent Setup Guide

Use this page when a developer asks an AI coding agent to install or repair
AIWatcher Local. The goal is a working private local Console and Companion,
with clear verification and no unnecessary changes to the developer's tools.

## Success Criteria

Setup is complete when:

- one AIWatcher Console and one logical Companion service are running;
- `aiwatcher doctor` completes and its warnings are explained;
- delivery tracking is recorded as enabled, declined, unavailable in the
  installed release, or deferred;
- the developer knows the local Console URL and normal start command; and
- optional AI-tool hooks are installed only with the developer's approval.

Do not configure AI Assist, cloud upload, login autostart, or tool hooks unless
the developer explicitly requests them or approves the change. Do not use
system Python, `sudo pip`, or `--break-system-packages`.

## 1. Inspect Before Changing Anything

First determine whether the agent is operating on the developer's intended
local desktop host. Detect SSH, WSL, containers, remote development hosts, and
cloud or ephemeral workspaces. Stop and ask before installing when the target
is ambiguous. Installing inside a container or remote shell will not place the
native Companion on the developer's desktop.

Identify the operating system and shell, then inspect the current setup:

```console
python3 --version
pipx --version
command -v aiwatcher
aiwatcher doctor
```

On Windows PowerShell, use these equivalents:

```powershell
py --version
py -m pipx --version
Get-Command aiwatcher -ErrorAction SilentlyContinue
aiwatcher doctor
```

Missing-command failures are expected during a first install. If `aiwatcher`
already exists, use `doctor` and the install location to identify whether it is
a pipx package, virtual environment, or source checkout. Preserve an editable
source checkout; do not silently replace it with a package installation.

Read the plan back once before changing the machine. Include the target host,
install or update method, whether a native Companion is supported, and which
optional repository or tool configuration changes are proposed.

## 2. Install Or Update The Core

For a new macOS or Linux installation, prefer pipx:

```console
pipx install aiwatcher-local
pipx ensurepath
```

For a new Windows installation:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
py -m pipx install aiwatcher-local
```

If pipx itself is missing on macOS or Linux, use the platform package manager
described in the root [README](../README.md#other-install-methods). Do not
install into a system-managed Python environment.

For an existing pipx installation from PyPI:

```console
pipx upgrade aiwatcher-local
```

For an existing GitHub-based pipx installation, use
`pipx reinstall aiwatcher-local` so pipx fetches the configured revision even
when the package version is unchanged. For a virtual environment, activate it
and run `python -m pip install --upgrade aiwatcher-local`. For a clean source
checkout on `main`, use `aiwatcher update --apply`; leave feature branches and
dirty checkouts to the developer's normal Git workflow.

If `pipx ensurepath` changes `PATH`, open a new terminal before continuing.

Before relying on release-sensitive commands from this page, confirm the
installed version supports them:

```console
aiwatcher --help
aiwatcher delivery-tracking --help
```

The guide tracks GitHub `main`, which can be newer than the current PyPI
release. In particular, automatic delivery tracking may be absent while manual
`delivery-review` and per-repository `install-commit-hook` remain available. If
a documented command is missing, mark that step unavailable and explain the
mismatch. Do not silently switch update channels to obtain an unreleased
feature.

## 3. Start The Private Local Services

Run the one-time setup and health check. On a macOS or Windows desktop host,
start the active installation with native presence:

```console
aiwatcher setup
aiwatcher doctor
aiwatcher start --open-ui
```

On Linux, WSL, a container, or another headless environment, do not promise a
native floating Companion. Start the background Companion and loopback Console
without native presence:

```console
aiwatcher setup
aiwatcher doctor
aiwatcher start --no-presence
```

Report the printed loopback Console URL. Use `--open-ui` as well only when the
environment has a usable local browser.

AIWatcher owns one local Console and one Companion. Starting it from another
valid installation transfers singleton ownership instead of intentionally
leaving duplicate background services. The normal desktop command after setup
is:

```console
aiwatcher start --open-ui
```

After an update on a desktop host, use:

```console
aiwatcher restart --open-ui
```

The normal command for Linux, WSL, containers, and headless hosts is:

```console
aiwatcher start --no-presence
```

After an update or when replacing an offline Console there, use:

```console
aiwatcher restart --no-presence
```

The Console normally opens on a loopback URL such as
`http://127.0.0.1:8765`. Do not expose it on a public network interface.

## 4. Offer Delivery Tracking

Explain before enabling delivery tracking: AIWatcher discovers repositories
from recent local AI sessions and adds or maintains its marked `post-commit`
hook block where it is safe to do so. It leaves custom or pre-existing hooks
unchanged and reports those repositories for review. Ask for approval, then
run only when approved:

```console
aiwatcher delivery-tracking enable
aiwatcher delivery-tracking status
```

If declined, leave it disabled. If the command is unavailable in the installed
release, mark it deferred instead of claiming it is enabled.

## 5. Offer Relevant AI-Tool Integrations

Explain that hooks let AIWatcher review supported prompts before an agent
spends context, and that they change the selected tool's local configuration.
Ask the developer which detected tools they want to connect before running any
`--write` command.

Install only the approved integrations:

```console
aiwatcher install-claude-hook --write --scope user --gate
aiwatcher install-codex-hook --write --scope user --gate
aiwatcher install-cursor-hook --write --scope user --gate
```

Use `--scope project --project-dir /path/to/repo` instead of user scope when
the developer requests a repository-only setup. Do not claim that a hook works
on every surface: Claude Desktop general chat and some Codex Desktop builds may
not expose prompt hooks. The Console, Companion, and manual `preflight` remain
available where no verified hook exists.

After a hook change, tell the developer to reload the affected tool. Codex
CLI/TUI users should start a new session and inspect `/hooks`; desktop apps
should be fully quit and reopened.

## 6. Verify With Evidence

Run:

```console
aiwatcher doctor
aiwatcher companion status
aiwatcher hook-status
```

Verify that the Console opens and the Companion service is healthy. On macOS
or Windows, also verify that the native Companion appears. Record delivery
tracking as enabled, declined, deferred, or needing repository review. If hooks
were installed, ask the developer to send a small test prompt in each connected
tool, then run `aiwatcher hook-status` again. Report unsupported surfaces and
warnings as limitations rather than marking them successful.

If the Companion reports **UI offline** on a desktop host, expand it and use
**Restart**, or run `aiwatcher restart --open-ui`. On a headless host, run
`aiwatcher restart --no-presence`. If the wrong source checkout appears, use
`aiwatcher doctor` to identify the active install before restarting from the
intended one.

## 7. Give The Developer A Short Handoff

Finish with:

- installation source and installed version;
- Console URL and whether Companion is healthy;
- delivery-tracking status;
- hooks installed, skipped, or unsupported;
- any remaining `doctor` warnings; and
- the platform-appropriate normal command: `aiwatcher start --open-ui` on a
  desktop host or `aiwatcher start --no-presence` on a headless host.

Never report setup as complete solely because an install command exited zero.
The health checks and local user-visible surfaces are the completion evidence.

For deeper installation choices and troubleshooting, use the root
[README](../README.md). For privacy and evidence boundaries, use
[AIWATCHER_LOCAL.md](AIWATCHER_LOCAL.md).
