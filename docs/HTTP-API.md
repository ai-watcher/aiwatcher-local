# AIWatcher Local HTTP API

The local dashboard (`aiwatcher ui`) serves a small HTTP API on loopback. Two
endpoints are **supported** for building against. Everything else the server
exposes is internal to the dashboard and may change without notice.

This exists so a local editor plugin, browser extension, or script can reuse
AIWatcher's prompt review without shelling out to the CLI and without uploading
prompt text anywhere.

- [Trust boundary](#trust-boundary)
- [Supported endpoints](#supported-endpoints)
  - [POST /api/preflight](#post-apipreflight)
  - [POST /api/outcome](#post-apioutcome)
- [Internal endpoints](#internal-endpoints)
- [Errors](#errors)

## Trust boundary

The server binds `127.0.0.1` by default and is intended for same-machine
callers only. Do not expose it to a network — there is no authentication, and
adding one is not on the roadmap for AIWatcher Local.

Specifics worth knowing before you build against it:

- **Origins.** Cross-origin responses are allowed only for `chrome-extension://`
  origins and for `http://` origins whose hostname is exactly `127.0.0.1` or
  `localhost`. The check compares the parsed hostname exactly, so lookalike
  hosts such as `http://127.0.0.1.evil.com` are rejected. The server never
  answers with `Access-Control-Allow-Origin: *`.
- **Methods.** `POST` is accepted only on `/api/preflight`, `/api/outcome`, and
  the internal dashboard/companion routes listed below. Any other path returns
  `404` before the request body is read.
- **Content type.** `POST` requires `Content-Type: application/json`, else `415`.
- **Body size.** Requests larger than 64 KiB return `413`.

## Supported endpoints

### `POST /api/preflight`

Review a prompt for cost, scope, and safety risk. Read-only: nothing is
persisted, and the prompt text is analyzed in-process.

**Request**

```jsonc
{
  "prompt": "Refactor the entire codebase and delete old auth secrets",
  "tool": "codex",              // optional, defaults to "agent"
  "cwd": "/path/to/project"     // optional, defaults to the server's directory
}
```

**Response — `200`**

```jsonc
{
  "risk": "low | medium | high",
  "score": 0,
  "tool": "codex",
  "findings": ["..."],
  "suggestions": ["..."],
  "suggested_prompt": "...",   // the scoped execution brief
  "impact_label": "...",       // human-readable estimated pressure, honesty-gated
  "privacy": "..."             // what is and isn't persisted, for display next to the result
}
```

`impact_label` is deliberately conservative. Until enough comparable local
history exists, it says so rather than inventing a savings figure — display it
verbatim instead of parsing numbers out of it.

An empty or whitespace-only `prompt` returns `400` with
`{"error": "prompt is required"}`.

### `POST /api/outcome`

Record how a session turned out. This supported endpoint writes only to the
private local AIWatcher state ledger.

**Request**

```jsonc
{
  "session_id": "4f2c1a9e",
  "outcome": "useful",     // useful | rework | abandoned
  "note": "..."            // optional
}
```

**Response — `200`** — the stored record:

```jsonc
{
  "session_id": "4f2c1a9e",
  "outcome": "useful",
  "note": null,
  "recorded_at": "2026-08-03T12:00:00+00:00"
}
```

Recording an outcome replaces any previous outcome for the same session rather
than appending, so the call is idempotent.

| Condition | Status | Body |
| --- | --- | --- |
| `session_id` missing or empty | `400` | `{"error": "session_id is required"}` |
| `outcome` not one of the three values | `400` | `{"error": "outcome must be useful, rework, or abandoned"}` |
| No local session with that id | `404` | `{"error": "session not found"}` |
| Local state could not be written | `500` | `{"error": "Could not save outcome: ..."}` |

## Internal endpoints

The dashboard also serves the endpoints below. They exist to render the UI,
their shapes track whatever the current dashboard needs, and they are **not
supported for external callers** — treat them as private and expect them to
change without a deprecation period.

`GET` — `/api/health`, `/api/summary`, `/api/companion-state`,
`/api/companion-scan`, `/api/sessions`, `/api/session`,
`/api/session-summary`, `/api/project`, `/api/report`, `/api/journal`,
`/api/handoff-basic`, `/api/handoff`, `/api/handoff-demo`,
`/api/context-health`, `/api/ambient-intervention`, `/api/update-status`,
`/api/ai-assist-status`

`/api/ambient-intervention` returns the content-free local signal metadata
needed to keep the browser fallback consistent with the native companion.
`/api/companion-state` returns the small Plan/Control/Watch state used by the
floating Companion presence control; it is intentionally content-free and does
not expose prompt or source text. `/api/companion-scan` forces the companion to
refresh local watch evidence without waiting for the next polling interval.
`/api/handoff-basic` returns a copyable Fresh Start brief without waiting for
timeline, git, or prompt enrichment; `/api/handoff` returns the enriched drawer
payload; `/api/handoff-demo` returns seeded demo data for the in-dashboard Fresh
Start test flow. `/api/update-status` checks the installed source checkout
against GitHub when the dashboard asks for it and reports whether a clean
fast-forward is available. The top-bar update badge uses this route for
low-frequency status checks and user-triggered refreshes.
`/api/ai-assist-status` returns the optional AI Assist mode, detected local
providers, cloud-key presence by environment-variable name, privacy posture, and
candidate workflows. It never returns secret values and never calls a model.

`POST` — `/api/second-opinion` runs the Plan screen's Stage 2 analysis: it
spawns the user's own agent CLI as a throwaway sibling process in
`<project>/.aiwatcher/analyst/`. Claude Code and Codex can both host it, and
the `tool` field picks which — the vendor you are about to prompt is the one
already installed and authenticated. A vendor with no analyst falls back to
whichever is installed rather than refusing and returns a schema-validated description of
the prompt, or an unavailable state with a reason. It is deliberately a
separate request from `/api/preflight`, which stays fast and complete on its
own: a real analyst run takes about 30 seconds. It costs money on the
caller's own account, so the server re-checks the local blast-radius gate
before spawning and will refuse a prompt that does not reach it, whatever the
request says. Internal, and not a route to build against.

`/api/second-opinion-consent` records whether one project may pay for second
opinions. Asked once per project, before the first spawn, with the cost in the
question. `/api/second-opinion-contents` records whether the analyst may open
files in that project rather than only being given their paths. Off unless set,
and deliberately separate from consent: agreeing to pay for a second opinion is
not agreeing to let it read your source. `/api/ask-aiwatcher` answers
dashboard-only local questions from indexed metadata. `/api/handoff-basic`,
`/api/handoff`, and `/api/handoff-demo` accept the same dashboard-only Fresh
Start options as their `GET` forms. `/api/handoff-ai-assist` runs the optional
Fresh Start brief improvement workflow after the user explicitly asks for it;
it appends an AI Assist refinement to the deterministic brief and records a
privacy-safe run receipt, but does not change source-session evidence or proof
claims. `/api/handoff-decision` records which action you took on a Fresh Start
companion, `/api/handoff-receipts-viewed` marks proof-pending receipts as seen,
`/api/first-run-dismissed` records that the once-only first-run screen has been
seen so it does not return (no body; the timestamp is the server's),
`/api/optimize-decision` records an Improve action, `/api/companion-skip`
snoozes a non-blocking companion reminder, and
`/api/ambient-intervention-action` records the native companion lifecycle
(`displayed`, `acted`, `snoozed`, `dismissed`, or `failed`).
`/api/ai-assist-config` saves the optional AI Assist mode. Supported modes are
`off`, `local`, and `cloud`; source access is `metadata_only`, `prompt_opt_in`,
or `source_opt_in`. For cloud mode, `api_key` may be supplied for the selected
provider (`openai`, `anthropic`, or `openai_compatible`) and is stored only in
local AIWatcher state. Responses never return the raw key; they expose
`stored_keys` booleans instead. `clear_api_key: true` removes the saved key for
the selected provider. Environment keys (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, or `AIWATCHER_AI_API_KEY`) are still supported.
`/api/runtime-return` asks AIWatcher to open the safest available return target
for a local session: exact process attachment when a host exposes enough
metadata, otherwise app/workspace return, otherwise a Fresh Start fallback.
`/api/session-resume` resolves the tool's own resume command for a session
(`claude --resume <id>`, `codex resume <id>`) and, with `"launch": true`, opens
it in a new terminal. Unlike `/api/runtime-return` it does not depend on a live
process, so it still answers for a session whose terminal has since closed; it
reopens the conversation elsewhere rather than focusing the original window.
The command is generated only for verified UUID session ids and is run or copied
from the recorded project directory when that directory still exists. Tools
whose ids AIWatcher synthesises rather than reads (Cursor) return `"available":
false` with the reason.
`/api/update-apply` applies the same conservative source-checkout update as the
CLI: it fast-forwards only a clean, non-diverged Git checkout and reports
package-installer guidance otherwise. When the dashboard posts
`{"restart": true}` after a successful apply, the local server restarts so the
dashboard and Companion use the new code.
These endpoints are called by the dashboard or native companion only.

If you need one of these programmatically, prefer the equivalent CLI command
with `--format json` where available, or
[`aiwatcher export`](CLI.md#aiwatcher-export) — those are stable surfaces.
Better still, open an issue so the endpoint can be promoted deliberately rather
than depended on by accident.

## Errors

All error responses are JSON with a single `error` key and
`Content-Type: application/json; charset=utf-8`.

| Status | Meaning |
| --- | --- |
| `400` | Malformed JSON body, or a required field is missing or invalid |
| `404` | Unknown path, or the referenced session does not exist |
| `413` | Request body exceeds 64 KiB |
| `415` | `Content-Type` was not `application/json` |
| `500` | Local state could not be written |
