"""Keep AIWatcher's state out of the developer's home -- and out of the next test.

Imported for its side effects by the two entry points that reach the suite
(``tests/__init__.py`` and ``tests/test_000_state_isolation.py``); both are
guarded, so whichever runs first wins and the other is a no-op.

There are two problems here, and they need two different fixes.

**One: the suite must not write to the real store.** Not hypothetically -- a
plain ``python3 -m unittest tests.test_cli`` added thirteen rows to the live
ledger, and the state file on this machine had accumulated fifty hook events and
twenty-seven brief tokens that no feature ever created. Running the tests must
not edit the data the product reports on; a ledger a test run can write to is
not evidence of anything. ``isolate_state_home`` fixes that by pointing
AIWATCHER_HOME at a throwaway directory for the whole run. One variable covers
the state file, the summary cache beside it, analyst storage and prompt-signal
overrides.

**Two: a test must not inherit the store the tests before it wrote.** A
suite-wide temp home solves the first problem and leaves this one untouched:
every test still shares one store with the tests before it, with any thread a
previous test leaked, and -- before the home moved -- with any ``aiwatcher
watch`` or dashboard running on the same machine. 103 tests drive a
``command_*`` entry point without pointing AIWATCHER_STATE_FILE anywhere, so
notification dedupe keys, watcher heartbeats, snooze windows and evidence
snapshots pile up as the module runs. Assertions about "the most recent
decision" or "how many events are recorded" then read a file that something
else may have appended to. That is a flake with no fingerprint: it fails once,
passes on the rerun, and leaves nothing behind to explain itself -- and it cost
a real detour on PR #83, where three failures that had nothing to do with the
change had to be ruled out by running the suite on main to compare.

``isolate_state_per_test`` fixes that by moving AIWATCHER_HOME again around
every single test, so each one starts from an empty store and cannot reach the
one before it. Tests that set AIWATCHER_HOME or AIWATCHER_STATE_FILE themselves
still work -- they run inside this, override it, and restore it.
"""

from __future__ import annotations

import atexit
import functools
import itertools
import os
import shutil
import tempfile
import unittest

PREFIX = "aiwatcher-tests-"

_sequence = itertools.count(1)


def isolate_state_home() -> str:
    """Redirect AIWatcher state to a temporary home. Safe to call twice."""
    current = os.environ.get("AIWATCHER_HOME", "")
    if PREFIX in current:
        return current
    home = tempfile.mkdtemp(prefix=PREFIX)
    os.environ["AIWATCHER_HOME"] = home
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    return home


def isolate_state_per_test() -> None:
    """Give every test its own AIWatcher home. Safe to call twice.

    Wrapping ``TestCase.run`` rather than ``setUp`` is what makes this reach
    the whole suite without every test class having to opt in -- and it covers
    ``setUp``, ``tearDown`` and cleanups too, which run inside it.
    ``setUpClass`` runs outside, so a class that recorded state there would
    lose it; none does, and a new one should set up its state per test.
    """
    if getattr(unittest.TestCase.run, "_aiwatcher_per_test_home", False):
        return

    original = unittest.TestCase.run

    @functools.wraps(original)
    def run(self, result=None):  # type: ignore[no-untyped-def]
        suite_home = isolate_state_home()
        previous = os.environ.get("AIWATCHER_HOME")
        # Deliberately not created: nothing has to exist for a read, and every
        # writer makes its own parents. A test that records nothing leaves no
        # directory behind, which is why this is affordable per test.
        os.environ["AIWATCHER_HOME"] = os.path.join(
            suite_home, "per-test", f"{next(_sequence):05d}"
        )
        try:
            return original(self, result)
        finally:
            if previous is None:
                os.environ.pop("AIWATCHER_HOME", None)
            else:
                os.environ["AIWATCHER_HOME"] = previous

    run._aiwatcher_per_test_home = True  # type: ignore[attr-defined]
    unittest.TestCase.run = run  # type: ignore[method-assign]


def isolate() -> str:
    """Apply both fixes. Returns the suite-wide temporary home."""
    home = isolate_state_home()
    isolate_state_per_test()
    return home
