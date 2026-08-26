"""Point AIWatcher's state at a throwaway directory for the whole suite.

This runs before any test does, and the file name is load-bearing: unittest
discovery imports ``test*.py`` in sorted order, so ``000`` puts this ahead of
every other module. ``tests/__init__.py`` does the same thing for the other
entry point (``python -m unittest tests.test_cli``), which does not import this
module. Both are guarded, so whichever runs first wins and the other is a
no-op.

Why it exists: the suite used to write to the developer's real ``~/.aiwatcher``.
Not hypothetically -- a plain ``python3 -m unittest tests.test_cli`` added
thirteen rows to the live ledger, and the state file on this machine had
accumulated fifty hook events and twenty-seven brief tokens that no feature
ever created.

Two consequences, and the second is the reason this was worth chasing:

1. Running the tests must not edit the data the product reports on. A ledger a
   test run can write to is not evidence of anything.
2. Every test shared that one store -- with the tests before it, with previous
   runs, and with any ``aiwatcher watch`` or dashboard running on the same
   machine, which appends heartbeats and interventions on a timer. Assertions
   about "the most recent decision" or "how many events are recorded" were
   reading a file something else could append to mid-run. That is a flake with
   no fingerprint: it fails once, passes on the rerun, and leaves nothing
   behind to explain itself.

AIWATCHER_HOME redirects the state file, the summary cache beside it, analyst
storage and prompt-signal overrides, so one variable isolates all of them.
Tests that set it themselves still work -- they override this and restore it.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path

PREFIX = "aiwatcher-tests-"


def isolate_state_home() -> str:
    """Redirect AIWatcher state to a temporary home. Safe to call twice."""
    current = os.environ.get("AIWATCHER_HOME", "")
    if PREFIX in current:
        return current
    home = tempfile.mkdtemp(prefix=PREFIX)
    os.environ["AIWATCHER_HOME"] = home
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    return home


isolate_state_home()


class StateIsolationTests(unittest.TestCase):
    """Guards the guard. Without these, losing isolation is silent."""

    def test_state_is_redirected_away_from_the_real_home(self):
        from aiwatcher_cli import local_state

        state = str(local_state.state_path())
        self.assertIn(
            PREFIX, state,
            "AIWatcher state is not isolated, so this run is writing to the real "
            "ledger. If you changed how the tests are invoked, make sure this "
            "module is still imported before the others.",
        )
        self.assertNotIn(str(Path.home() / ".aiwatcher"), state)

    def test_writing_state_does_not_touch_the_real_ledger(self):
        from aiwatcher_cli import local_state

        real = Path.home() / ".aiwatcher" / "local-state.json"
        before = real.read_text(encoding="utf-8") if real.exists() else None
        local_state.record_session_waiting(
            session_id="isolation-probe", tool="claude-code", kind="permission",
        )
        self.assertIn("isolation-probe", local_state.session_waiting_signals())
        after = real.read_text(encoding="utf-8") if real.exists() else None
        self.assertEqual(before, after, "the real AIWatcher ledger was modified")
