# Release Checklist

Use this before publishing AIWatcher Local to a public package registry.

## Package Names

AIWatcher currently has two Python package lanes:

- `ai-watcher`: the existing PyPI SDK package, imported as `aiwatcher`.
- `aiwatcher-cli`: the AIWatcher Local CLI package from this repository,
  imported internally as `aiwatcher_cli` and installed as the `aiwatcher`
  terminal command.

Keep those separate. Do not publish this repository as `ai-watcher`, or it will
collide with the SDK lane and confuse users.

The current repository is not ready to publish as an npm package from the root.
The only JavaScript package manifests are:

- `browser-extension/package.json`, marked `private: true`.
- `vscode-extension/package.json`, a VS Code extension manifest with no npm
  dependencies.

For npm distribution, create a deliberate package first, such as a thin
`aiwatcher-cli` or `@ai-watcher/local` installer wrapper. Do not publish the
repo root to npm until that package boundary exists.

## Preflight

Run these from a clean checkout on the release commit:

```sh
git status -sb
git fetch origin
git rev-parse HEAD
git rev-parse origin/main
python3 scripts/generate_cli_reference.py --check
python3 -m unittest tests.test_ui_assets tests.test_ai_assist tests.test_local_state -q
```

Then scan the tracked source for high-confidence secrets and local/private
paths:

```sh
git grep -n -i -E "(api[_-]?key|apikey|secret|password|passwd|credential|bearer|authorization|private[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|auth[_-]?token|BEGIN [A-Z ]*PRIVATE KEY|ghp_|github_pat_|sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]+|xox[baprs]-|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})"
git grep -n -E "(/Users/|/private/tmp/|/var/folders/|codex://threads|file:///private)"
git ls-files | grep -E '(^|/)(\.env|\.npmrc|\.pypirc|id_rsa|id_ed25519|credentials|secrets|token|key)(\.|$|/)'
```

Expected results are public docs, environment-variable names, product security
copy, and test fixtures with fake values. Investigate anything that looks like
a real credential, customer path, private workspace URL, or personal machine
path.

## Build And Inspect PyPI Artifacts

Use a throwaway environment:

```sh
python3 -m venv /tmp/aiwatcher-release
/tmp/aiwatcher-release/bin/python -m pip install build twine pip-audit setuptools wheel
/tmp/aiwatcher-release/bin/python -m build --no-isolation
/tmp/aiwatcher-release/bin/python -m twine check dist/*
tar -tf dist/aiwatcher_cli-*.tar.gz
unzip -l dist/aiwatcher_cli-*.whl
```

The wheel should contain only `aiwatcher_cli`, `aiwatcher_cli/web`, metadata,
entry points, and the license. If docs, private automation files, local exports,
or hidden tool config appear in the wheel, stop and fix the package boundary
before publishing.

Run a dependency audit against the release environment:

```sh
XDG_CACHE_HOME=/tmp/aiwatcher-pip-audit-cache \
  /tmp/aiwatcher-release/bin/python -m pip_audit --progress-spinner off \
  --path /tmp/aiwatcher-release/lib/python*/site-packages
```

AIWatcher Local currently has no runtime Python dependencies, so findings in
the throwaway environment usually belong to audit/build tools rather than the
package. Still read them before publishing.

## Publish To PyPI

Before publishing, bump `version` in `pyproject.toml` and commit the change.
Prefer PyPI trusted publishing through GitHub Actions when possible, so no PyPI
API token needs to live on a laptop or in repo config.

Manual upload, if trusted publishing is not configured:

```sh
/tmp/aiwatcher-release/bin/python -m twine upload dist/*
```

After upload:

```sh
pipx install aiwatcher-cli
aiwatcher setup
aiwatcher start --open-ui
```

## Publish To npm

Do this only after creating an intentional npm package. The current root repo
has no publishable npm package.

For a future npm package:

```sh
npm login
npm pack --dry-run
npm publish --access public
```

Use npm provenance when available, and keep generated archives out of Git.
