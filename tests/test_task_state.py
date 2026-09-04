from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from aiwatcher_cli import local_state


class TaskStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(self.temp.name, "local-state.json")})
        env.start()
        self.addCleanup(env.stop)

    def test_boundary_records_upsert_per_session_and_turn(self) -> None:
        local_state.record_task_boundary("sess-1", 4, False)
        local_state.record_task_boundary("sess-1", 7, True)
        local_state.record_task_boundary("sess-1", 4, True)  # changed their mind
        local_state.record_task_boundary("sess-2", 4, False)
        self.assertEqual(local_state.task_boundary_overrides(), {"sess-1": {4: True, 7: True}, "sess-2": {4: False}})

    def test_first_turn_and_bad_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            local_state.record_task_boundary("sess-1", 1, True)
        with self.assertRaises(ValueError):
            local_state.record_task_boundary("", 3, True)
        with self.assertRaises(ValueError):
            local_state.record_task_boundary("sess-1", True, True)  # a bool is not a turn number

    def test_verdicts_upsert_per_task(self) -> None:
        local_state.record_task_verdict("abc", "not_done", "sess-1")
        local_state.record_task_verdict("abc", "done", "sess-1")
        local_state.record_task_verdict("def", "done")
        self.assertEqual(local_state.task_verdicts(), {"abc": "done", "def": "done"})
        with self.assertRaises(ValueError):
            local_state.record_task_verdict("abc", "maybe")

    def test_fresh_state_carries_the_new_keys(self) -> None:
        data = local_state._empty_state()
        self.assertEqual(data["task_boundaries"], [])
        self.assertEqual(data["task_verdicts"], [])


if __name__ == "__main__":
    unittest.main()
