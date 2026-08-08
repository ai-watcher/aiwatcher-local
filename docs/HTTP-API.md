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
  the internal `/api/handoff-decision`. Any other path returns `404` before the
  request body is read.
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

Record how a session turned out. **This is the only endpoint that writes.**

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

`GET` — `/api/health`, `/api/summary`, `/api/sessions`, `/api/session`,
`/api/project`, `/api/report`, `/api/journal`, `/api/handoff`,
`/api/context-health`, `/api/runtime-return`

`/api/runtime-return` is dashboard-only. It asks AIWatcher to open the safest
available return target for a local session: exact process attachment when a
host exposes enough metadata, otherwise app/workspace return, otherwise a
handoff fallback. It must not be treated as a stable deep-link API.

`POST` — `/api/handoff-decision`, which records which action you took on a
handoff bubble. It is called by the dashboard and the native overlay only.

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
