from __future__ import annotations

import unittest
from pathlib import Path

from aiwatcher_cli import local_state


def _real_default_state_path() -> Path:
    return (Path.home() / ".aiwatcher" / "local-state.json").resolve()


class LocalStateIsolationTests(unittest.TestCase):
    """Canary for tests/conftest.py.

    The isolation there is invisible when it works and silent when it breaks --
    a suite run would simply resume writing fixtures into the developer's real
    state file, exactly as it did before. These assertions turn that back into a
    failing test instead of something you find months later while debugging why
    surface coverage trusts a cwd=/repo hook event.

    Every assertion checks the path *before* writing anything. An earlier draft
    wrote first and asserted second, which meant the one test guarding against
    polluting real state polluted it on the way to reporting the failure.
    """

    def assertIsolated(self) -> Path:
        state = local_state.state_path().resolve()
        self.assertNotEqual(
            state, _real_default_state_path(),
            "local state is not isolated -- tests/conftest.py is missing or not applied. "
            "Run the suite with pytest; conftest.py does nothing under `python -m unittest`.",
        )
        return state

    def test_state_path_is_redirected_away_from_the_real_home(self) -> None:
        self.assertIsolated()

    def test_writing_state_lands_in_the_isolated_file(self) -> None:
        # The exact call that was leaking: a hook entrypoint recording an event
        # with no state file of its own. Check isolation first -- if it is gone,
        # this must fail without ever performing the write.
        state = self.assertIsolated()
        local_state.record_hook_event(
            tool="claude", cwd="/repo", event="received", prompt_found=True,
        )
        self.assertTrue(state.exists())
        self.assertEqual(len(local_state.recent_hook_events(limit=10)), 1)


if __name__ == "__main__":
    unittest.main()
