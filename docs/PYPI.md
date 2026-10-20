# AIWatcher Local

Keep track of AI coding work on your own laptop. AIWatcher brings local session
activity, context pressure, prompt checks, and Git evidence into a browser
dashboard and a small Companion window.

Use it to decide when to intervene, what to carry into a fresh chat, and which
completed or stale work deserves a cleanup review.

**No account required. Local by default. No Python runtime dependencies.**
Optional AI Assist requires configuration and an explicit workflow action.

![Illustrated AIWatcher Home preview with sample session data](https://raw.githubusercontent.com/ai-watcher/aiwatcher-local/main/docs/readme-home-light.png)

*Illustrated product preview with sample data. The installed UI may differ.*

## Install and start

For most users, install with [pipx](https://pipx.pypa.io/stable/installation/)
using Python 3.10 or newer. AIWatcher itself supports Python 3.9+.

```console
pipx install aiwatcher-local
pipx ensurepath
```

Open a new terminal if this is your first pipx installation, then run:

```console
aiwatcher setup
aiwatcher doctor
aiwatcher start --open-ui
```

This starts the local browser dashboard and Companion. Install once per laptop;
you do not need a separate installation in each project. The first scan can take
longer when you have substantial local AI history.

Prefer a Python virtual environment? Activate it and run
`python -m pip install aiwatcher-local`, then the same setup and start commands.
See the [installation guide](https://github.com/ai-watcher/aiwatcher-local#other-install-methods)
for platform instructions and source installs.

## What to try first

| Your situation | Use AIWatcher to | What to expect |
| --- | --- | --- |
| A broad or risky coding request | Review it in **Plan**, or run `aiwatcher preflight "your request"` | Local checks that help narrow scope before the agent runs |
| Several AI sessions running | Open **Home** and keep the Companion visible | Session activity and supported context-pressure signals |
| A long chat needs a fresh start | Generate a **Fresh Start** brief | Checkout and session evidence for a new chat; confirm the objective if it is missing |
| You want to evaluate completed work | Review **Prove** or run `aiwatcher delivery-review` in the checkout | Git and verification evidence, with gaps labelled rather than assumed complete |
| Old chats or temporary workspaces are accumulating | Open **Optimize** and inspect a candidate | Evidence and a review prompt; cleanup requires identity checks and a separate user decision |

An inactive session count is not a count of safe deletions. Optimize can group
many sessions into fewer project review items. Historical tokens describe past
activity; they are not reclaimable RAM, disk space, or guaranteed cost savings.
Archiving a chat does not establish that a running process has stopped.

## Connect the tools you actually use

AIWatcher reads available local history from Claude Code, Codex, and Cursor.
Coverage depends on the history and integration surface each tool exposes.
Session visibility does not mean every prompt is intercepted.

For supported prompt-gate integrations, install only the hooks you need:

```console
aiwatcher install-claude-hook --write --scope user --gate
aiwatcher install-codex-hook --write --scope user --gate
aiwatcher install-cursor-hook --write --scope user --gate
aiwatcher hook-status
```

Reload the AI tool and check hook status after a test prompt. Desktop surfaces
may not invoke CLI hooks; use manual Plan/preflight where interception is
unavailable. See the [tool coverage guide](https://github.com/ai-watcher/aiwatcher-local#connect-your-ai-tools).

## Privacy and evidence boundaries

- Local scanning uses tool history, Git state, and process metadata. No cloud
  upload happens by default.
- Prompt Gate decision receipts do not persist raw prompt text. Optional AI
  Assist can send bounded context to the configured provider when invoked;
  review the workflow's source-access disclosure first.
- Usage and API-equivalent cost estimates are not provider billing. Missing
  objectives, uncertain ownership, and unverified checks remain explicit gaps.
- Optimize recommends review. It does not automatically delete files, remove
  worktrees, archive chats, or stop processes.

Read the [privacy and coverage details](https://github.com/ai-watcher/aiwatcher-local/blob/main/docs/AIWATCHER_LOCAL.md)
before enabling optional integrations.

## Update, restart, or diagnose

For an installation from PyPI:

```console
pipx upgrade aiwatcher-local
aiwatcher restart --open-ui
```

Restart after upgrading so the running dashboard and Companion load the new
code. PyPI installs use the published release; GitHub `main` may contain newer
unreleased changes. GitHub-source pipx installs use `pipx reinstall aiwatcher-local`
to refresh their configured source instead.

```console
aiwatcher doctor
aiwatcher companion status
aiwatcher hook-status
```

If the command is missing, run `pipx ensurepath` and reopen the terminal.
If the Companion reports an offline UI, run `aiwatcher restart --open-ui`.
The Companion can be stopped with `aiwatcher companion stop`.

## Documentation and support

AIWatcher Local is an early open-source release for macOS, Linux, and Windows.
Integration coverage varies by platform and AI tool. Scans use laptop resources;
large histories take longer, especially on the first read.

- [Full user guide and troubleshooting](https://github.com/ai-watcher/aiwatcher-local/blob/main/README.md)
- [CLI reference](https://github.com/ai-watcher/aiwatcher-local/blob/main/docs/CLI.md)
- [Release notes](https://github.com/ai-watcher/aiwatcher-local/releases)
- [Report an issue](https://github.com/ai-watcher/aiwatcher-local/issues)
- [Source code and contributions](https://github.com/ai-watcher/aiwatcher-local)
- [Apache-2.0 license](https://github.com/ai-watcher/aiwatcher-local/blob/main/LICENSE)
