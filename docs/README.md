# AIWatcher Local Docs

This folder keeps deeper product, release, and lifecycle material out of the
root README. The root README should stay focused on the developer's first run.

## Public Docs

| File | Purpose | Source |
| --- | --- | --- |
| `AIWATCHER_LOCAL.md` | Product boundary, privacy contract, platform support, OSS vs Enterprise split, and validation checklist. | Edited by hand |
| `scenarios.json` | Single source of truth for lifecycle requirements, workflows, platform coverage, and scenario status. | Edited by hand |
| `aiwatcher-scenario-tests.html` | Interactive lifecycle scenario suite. | Generated from `scenarios.json` |
| `scenario-status.md` | Compact status table for README/PR/status updates. | Generated from `scenarios.json` |
| `release-checklist.md` | Release checklist grouped by unfinished scenario status. | Generated from `scenarios.json` |
| `dashboard.svg` | Public README dashboard preview. | Edited by hand or replaced as needed |

Regenerate derived docs after changing `scenarios.json`:

```sh
python3 scripts/generate_scenario_docs.py
```

Install the local git hook once if you want commits to regenerate these files
automatically:

```sh
python3 scripts/install_git_hooks.py
```

## Private Team Status / Notion

Do not put private team execution status, customer notes, or internal roadmap
commitments into this public repository. Use `docs/scenarios.json` as the clean
public source and sync it privately when needed.

The workflow at `.github/workflows/scenario-docs.yml` runs on every push. In
practice, this means every commit pushed to GitHub gets a fresh generated-docs
check and, when secrets are configured, appends a private status update to the
team Notion page. Local-only commits do not sync until they are pushed.

The Notion update is intentionally a navigable team workspace, not a historical
append-only log. On each sync it cleans the configured parent page's generated
top-level content, keeps managed child pages/databases, and updates:

- `AIWatcher Review Home` — current status, lifecycle progress, top open work,
  and links to the rest of the workspace.
- `Scenario Tracker` — a Notion database that can be filtered by `Status`
  (`Done`, `In progress`, `To verify`, `Blocker`), `Phase`, `Priority`, and
  platform.
- `Scope` — product position, OSS boundary, strategic filter, and non-scope.
- `Requirements` — lifecycle requirements mapped to user value and scenario
  coverage.
- `UX Workflows` — daily developer workflows and concrete examples.
- `Gaps` — blockers, in-progress work, to-verify items, and open decisions.
- `Test Cases` — full scenario checklist for manual verification.

The richer interactive artifact remains `docs/aiwatcher-scenario-tests.html`.

Required GitHub Actions secrets:

| Secret | Purpose |
| --- | --- |
| `NOTION_TOKEN` | Internal Notion integration token. |
| `NOTION_PAGE_ID` | Destination Notion page or block id where status updates are appended. |

The Notion integration should have read, insert, and update content access for
the destination page if you want the sync to clean old generated content and
refresh child pages in place. If update/delete access is missing, the workflow
will warn and continue. It will still update what the token is allowed to
update, but it intentionally skips appending duplicate generated content to an
existing child page that could not be cleaned first. Old generated blocks may
remain visible until the page is cleaned manually or the integration permissions
are expanded.

Recommended flow:

1. Keep this OSS repo public and free of Notion tokens.
2. Configure `NOTION_TOKEN` and `NOTION_PAGE_ID` as GitHub Actions secrets in
   this repo, or mirror the same workflow in a private automation repo.
3. Read `docs/scenarios.json`, `docs/scenario-status.md`, and
   `docs/release-checklist.md`.
4. Update a private Notion database/page using a Notion integration token stored
   as a private secret.
5. The workflow at `.github/workflows/scenario-docs.yml` runs on every push and
   by manual `workflow_dispatch`. If the Notion secrets are missing, it skips
   Notion sync successfully so public contributors are not blocked.

The sync intentionally rewrites generated content in the configured Notion
parent page. Keep manual notes in a separate child page if they should not be
managed by automation.

Manual dry run without secrets:

```sh
python3 scripts/generate_scenario_docs.py
python3 scripts/sync_notion_status.py
```

The second command should print that Notion sync was skipped. To test against a
real private Notion page, export the two secrets in your shell and run the same
script locally. Do not commit those values.

This keeps the public OSS docs honest while giving the team a current private
status page.
