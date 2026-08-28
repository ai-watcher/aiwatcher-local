"""Isolate AIWatcher state for `python -m unittest tests.<module>`.

`unittest discover -s tests` -- what CI runs -- never imports this file, because
it makes `tests` the top-level directory rather than a package. That path is
covered by tests/test_000_state_isolation.py, which discovery does import, and
which carries the full explanation. Both are guarded, so whichever runs first
wins.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

PREFIX = "aiwatcher-tests-"

if PREFIX not in os.environ.get("AIWATCHER_HOME", ""):
    _home = tempfile.mkdtemp(prefix=PREFIX)
    os.environ["AIWATCHER_HOME"] = _home
    atexit.register(shutil.rmtree, _home, ignore_errors=True)
