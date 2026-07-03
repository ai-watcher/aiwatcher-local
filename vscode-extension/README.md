# AIWatcher Local — VS Code Extension

Manually preflight prompt text from an editor selection, the clipboard, or an
input box. AIWatcher shows risk, findings, and a scoped execution brief in a
side panel.

All analysis runs locally via the AIWatcher server (`aiwatcher ui`). Nothing is uploaded.

## Setup

1. Start the local server: `aiwatcher ui`
2. Install this extension (load from VS Code Extensions panel or package with `vsce`)
3. Use any of the commands below

## Commands

| Command | Shortcut (Mac) | What it does |
|---|---|---|
| **AIWatcher: Preflight Selected Text** | `Cmd+Shift+Alt+P` | Preflights selected text in the editor |
| **AIWatcher: Preflight a Prompt…** | `Cmd+Shift+Alt+I` | Opens an input box — type or paste any prompt |
| **AIWatcher: Preflight Clipboard** | — | Preflights whatever is in your clipboard |

## Usage workflow

Ordinary VS Code extension APIs cannot intercept another extension's private
chat composer. Use this adapter as a manual bridge for tools without a native
prompt hook:

1. Copy a draft prompt from the AI chat sidebar.
2. Run **AIWatcher: Preflight Clipboard** from the Command Palette.
3. Review the risk and scoped brief in the side panel.
4. Copy the brief back into the AI tool when useful.

For text in a normal editor, select it and use **AIWatcher: Preflight Selected
Text**. Cursor users should prefer AIWatcher's native `beforeSubmitPrompt` hook.

## Settings

| Setting | Default | Description |
|---|---|---|
| `aiwatcher.serverUrl` | `auto` | Discover ports 8765-8784, or use an explicit local URL |

## Privacy

All preflight analysis runs on your machine via the local server. No prompt text is sent to any external service.
