# AIWatcher Local — Browser Extension

Intercepts prompts on **claude.ai** before they're sent. Calls the local AIWatcher server, shows a risk score and scoped execution brief for medium/high-risk prompts, and lets you choose to use the brief, edit it, run the original, or cancel.

All analysis runs on your machine. Nothing is uploaded.

## Install (Chrome / Chromium)

1. Start the local server: `aiwatcher ui`
2. Open `chrome://extensions`
3. Enable **Developer mode** (top right)
4. Click **Load unpacked** and select this `browser-extension/` directory
5. Open [claude.ai](https://claude.ai) and submit any prompt — AIWatcher preflights it first

## What happens on each prompt

| Risk level | What you see |
|---|---|
| **Low** (score 0–2) | Prompt goes through unchanged - no interruption |
| **Medium** (score 3–5) | Modal shows findings + scoped brief. Choose: use brief / edit / run original / cancel |
| **High** (score 6+) | Same modal with a stronger warning; the developer remains in control |

## Privacy

- Content script runs only on `claude.ai`
- The extension service worker discovers AIWatcher on ports 8765-8784 and calls it over loopback HTTP
- Web pages cannot call the local preflight endpoint cross-origin
- If the local server is not running, prompts go through unchanged and a visible coverage warning appears
- No prompt text is stored by the extension

## Extension popup

Click the AIWatcher icon in the toolbar to:
- See whether the local server is online
- Toggle interception on/off without uninstalling

## Current support boundary

This is an experimental adapter for `claude.ai`, not Claude Code Desktop. Website
DOM changes can break prompt detection, so validate each supported website with
end-to-end tests before adding it to `manifest.json`. Prefer a vendor's native
prompt hook whenever one exists.
