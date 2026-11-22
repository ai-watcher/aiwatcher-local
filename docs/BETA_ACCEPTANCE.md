# Focused Developer Beta Acceptance

Use this checklist against the exact commit and wheel proposed for release.
Passing source tests alone is not beta acceptance. Record the commit SHA, wheel
SHA-256, CI run, operating system, Python version, and any unavailable client.

## Automated release gates

The `CI` workflow must pass all of these for the same commit:

- Full Python suite on supported Python versions across Linux, macOS, and
  Windows, plus adapter/browser checks.
- One wheel and source distribution built once and checked with `twine`.
- The same wheel installed outside the checkout on all three operating systems.
- Installed import path is inside the disposable wheel environment, never the
  source checkout.
- CLI entry point, local preflight, dashboard assets, loopback HTTP service,
  and invalid-Host rejection pass from the installed wheel.
- Default `setup` and `status` make no non-loopback network connection.

The release workflow must find that successful `main` CI run for the exact tag
commit, download its artifact, rerun installed-wheel acceptance, and publish
that artifact without rebuilding it.

## Required functional evidence

These flows are release-blocking. Prefer disposable repositories and isolated
`AIWATCHER_HOME`/`AIWATCHER_STATE_FILE` values.

| Area | Required evidence |
| --- | --- |
| First use | Clean wheel install, setup, doctor, start, first useful local result |
| HTTP trust | Invalid/malformed Host rejected for GET/POST/OPTIONS; unrelated origins rejected; supported local clients accepted |
| Repository hooks | Install, invoke, and remove AIWatcher-owned hooks; pre-existing custom hooks remain unchanged |
| Verification | Passing and failing commands produce the correct exact-state evidence |
| Delivery | Successful and failed pushes, review creation, confirmation, decline, and copied bounded summary |
| Invalidation | Changed HEAD, branch, upstream, dirty tree, linked intent, totals, or completeness invalidates approval |
| Large ranges | 199/200/201 commits and 499/500/501 files distinguish total from shown; delivery HEAD is retained |
| Repository identity | Linked worktrees and concurrent sessions do not cross-attribute evidence |
| History failures | Missing, malformed, concurrently truncated, and oversized history remains explicit and bounded |
| Runtime lifecycle | Upgrade, singleton restart, port conflict, Companion/UI ownership, and clean shutdown |
| Removal | Disable tracking, remove owned hooks/autostart, uninstall package, and document separately retained state |
| Optional AI Assist | Off by default; explicit consent, source disclosure, cache behavior, and spending cap tested without a paid call |

## Platform and client evidence

Core CLI and browser Console are claimed for Linux, macOS, and Windows, so the
installed-wheel gate must pass on all three. Native Companion interaction and
AI-tool-specific trials are recorded separately:

| Surface | Beta requirement |
| --- | --- |
| macOS Companion | Swift source typecheck in CI; manual open/review/confirm/decline/restart smoke before release |
| Windows Companion | Automated source/contract checks; manual open/review/confirm/decline/restart smoke before release |
| Linux Companion | Automated Tk/contract checks; label desktop-environment combinations not exercised |
| Claude Code, Codex, Cursor | Run available local trials; label unavailable tool/platform combinations `UNTESTED` |

`UNTESTED` is not a pass. If an untested surface is part of the public beta
claim, either test it or narrow the claim before release.

## Release verdict

Mark **READY FOR BETA** only when:

1. Host/origin protection is verified.
2. Partial delivery evidence is visibly bounded and approval-bound.
3. Public installation and privacy documentation match the published artifact.
4. Exact-artifact CI and publishing gates pass.
5. Required functional evidence passes on every claimed platform.

Otherwise record **NOT READY**, the exact failed or untested gate, and the next
action. Do not publish from this checklist itself.
