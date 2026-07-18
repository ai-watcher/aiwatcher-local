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

The Notion update is intentionally a compact review surface: current status,
lifecycle progress, top open work, and collapsed sections for scope,
requirements, workflows, platform coverage, open decisions, and detailed test
cases. The richer interactive artifact remains
`docs/aiwatcher-scenario-tests.html`.

Required GitHub Actions secrets:

| Secret | Purpose |
| --- | --- |
| `NOTION_TOKEN` | Internal Notion integration token. |
| `NOTION_PAGE_ID` | Destination Notion page or block id where status updates are appended. |

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
