"""Isolate AIWatcher state for `python -m unittest tests.<module>`.

`unittest discover -s tests` -- what CI runs -- never imports this file, because
it makes `tests` the top-level directory rather than a package. That path is
covered by tests/test_000_state_isolation.py, which discovery does import.
Both call into tests/state_isolation.py, which carries the full explanation,
and both are guarded, so whichever runs first wins.
"""

from __future__ import annotations

try:  # package mode: `python -m unittest tests.test_cli`
    from . import state_isolation
except ImportError:  # discovery mode: `tests` is on sys.path, not a package
    import state_isolation  # type: ignore[no-redef]

state_isolation.isolate()
