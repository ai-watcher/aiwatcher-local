"""Point AIWatcher's state at a throwaway directory, and move it per test.

This runs before any test does, and the file name is load-bearing: unittest
discovery imports ``test*.py`` in sorted order, so ``000`` puts this ahead of
every other module. ``tests/__init__.py`` does the same thing for the other
entry point (``python -m unittest tests.test_cli``), which does not import this
module. Both call into ``tests/state_isolation.py``, which carries the full
explanation of what is being isolated and why, and both are guarded, so
whichever runs first wins and the other is a no-op.

The tests below guard the guard. Without them, losing isolation is silent.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

try:  # package mode: `python -m unittest tests.test_000_state_isolation`
    from . import state_isolation
except ImportError:  # discovery mode: `tests` is on sys.path, not a package
    import state_isolation  # type: ignore[no-redef]

PREFIX = state_isolation.PREFIX

state_isolation.isolate()


class StateIsolationTests(unittest.TestCase):
    """The run as a whole must not touch the developer's real store."""

    def test_state_is_redirected_away_from_the_real_home(self):
        from aiwatcher_cli import analyst, local_state, prompt_signals

        real_home = (Path.home() / ".aiwatcher").resolve()
        for label, path in (
            ("local state", local_state.state_path()),
            ("analyst detection cache", analyst._cache_path()),
            ("prompt signal overrides", prompt_signals.config_path()),
        ):
            with self.subTest(label):
                resolved = path.resolve()
                self.assertIn(
                    PREFIX, str(resolved),
                    "AIWatcher state is not isolated, so this run is writing to "
                    "the real ledger. If you changed how the tests are invoked, "
                    "make sure this module is still imported before the others.",
                )
                self.assertFalse(
                    resolved == real_home or real_home in resolved.parents,
                    f"{label} resolves into the real home at {resolved}",
                )

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


class PerTestStateIsolationTests(unittest.TestCase):
    """One test must not inherit the store the test before it wrote.

    These two methods are an ordered pair: unittest sorts test methods by name,
    so ``test_a_...`` runs before ``test_b_...``. That ordering is the only way
    to observe from inside the suite what a second test sees of the first, so
    ``test_b`` refuses to pass if the pair ran out of order or alone.
    """

    SENTINEL = "per-test-isolation-probe"
    first_home: str = ""

    def test_a_probe_records_a_signal_in_its_own_home(self):
        from aiwatcher_cli import local_state

        PerTestStateIsolationTests.first_home = os.environ.get("AIWATCHER_HOME", "")
        self.assertIn(PREFIX, PerTestStateIsolationTests.first_home)
        local_state.record_session_waiting(
            session_id=self.SENTINEL, tool="claude-code", kind="permission",
        )
        self.assertIn(self.SENTINEL, local_state.session_waiting_signals())

    def test_b_the_next_test_starts_from_an_empty_store(self):
        from aiwatcher_cli import local_state

        first_home = PerTestStateIsolationTests.first_home
        self.assertTrue(
            first_home,
            "the probe above did not run, so this proves nothing about "
            "per-test isolation. Run the whole class.",
        )
        home = os.environ.get("AIWATCHER_HOME", "")
        self.assertNotEqual(
            home, first_home,
            "both tests share one AIWatcher home, so each test inherits "
            "whatever the tests before it recorded. See "
            "tests/state_isolation.py:isolate_state_per_test.",
        )
        self.assertFalse(
            Path(local_state.state_path()).exists(),
            f"this test started with a state file already at {local_state.state_path()}",
        )
        self.assertNotIn(self.SENTINEL, local_state.session_waiting_signals())
