# Contributing to AIWatcher Local

Thanks for your interest in improving AIWatcher Local. This project is the
open-source, local-only visibility tool for AI coding agents. Contributions that
keep it private, honest, and genuinely useful for individual developers are
very welcome.

## Ground rules

AIWatcher Local has a privacy contract that is part of the product. Any change
must preserve it:

- **Local-only by default.** No new network calls, telemetry, or phone-home
  behavior.
- **Read-only.** Never write to or mutate the history of the tools we observe.
- **No prompt or source-code capture.** Summaries and exports contain metadata,
  aggregates, and content hashes — never prompt text or source code.
- **Honest over impressive.** When local data is missing or unreliable, say so
  instead of guessing.

Pull requests that weaken these guarantees will not be merged.

## Development setup

Requires Python 3.9+.

```bash
git clone https://github.com/ai-watcher/aiwatcher-local.git
cd aiwatcher-local
pip install -e .

# Run from source
python -m aiwatcher_cli --help
python -m aiwatcher_cli today
python -m aiwatcher_cli ui
```

## Before you open a pull request

```bash
python -m compileall aiwatcher_cli
python -m aiwatcher_cli --help
python -m aiwatcher_cli status
python -m aiwatcher_cli export --format json --since 2099-01-01
```

CI runs these on Linux, macOS, and Windows across Python 3.9–3.13. Please make
sure they pass locally first.

## What makes a good contribution

- New local tool support that reads only what the vendor already stores locally.
- More accurate token / cost attribution, clearly separating API-priced from
  subscription/limited usage.
- Cross-platform fixes (macOS, Linux, Windows path and process handling).
- Clearer output, better dashboard drill-down, documentation improvements.

If you are planning a larger change, please open an issue first to discuss the
approach.

## Style

- Standard library only where practical; keep runtime dependencies minimal.
- Match the existing code style (type hints, small focused functions).
- Keep user-facing output honest and free of private paths or personal data.

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
