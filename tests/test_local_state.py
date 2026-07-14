from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from aiwatcher_cli import local_state


class LocalStateTests(unittest.TestCase):
    def test_state_lock_is_only_ever_used_inside_locked_state(self) -> None:
        # _STATE_LOCK guards only in-process threads; a bare `with _STATE_LOCK:`
        # in a new record_*/recent_*/get_* function would compile and pass
        # every other test while silently reintroducing the cross-process
        # race _locked_state() exists to close. This asserts the only
        # reference to the raw lock left after removing _locked_state()'s own
        # body is its single definition line -- i.e. nothing else touches it
        # directly. If this fails, whatever new function added a second
        # reference should be using `with _locked_state():` instead.
        source = inspect.getsource(local_state)
        locked_state_source = inspect.getsource(local_state._locked_state)
        remainder = source.replace(locked_state_source, "", 1)
        self.assertEqual(
            remainder.count("_STATE_LOCK"), 1,
            "_STATE_LOCK must only be referenced in its own definition and inside "
            "_locked_state(); use `with _locked_state():` in any new function instead.",
        )
    def test_intervention_stores_hashes_not_prompt_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                intervention_id = local_state.record_intervention(
                    tool="claude",
                    cwd="/repo",
                    risk="high",
                    score=8,
                    findings=["Broad scope"],
                    original_prompt="delete the secret file",
                    suggested_prompt="inspect the smallest relevant file first",
                    decision="suggested",
                    selected_prompt="inspect the smallest relevant file first",
                    estimated_impact={
                        "available": True,
                        "confidence": "medium",
                        "savings": {"tokens": [1000, 2000], "api_value_usd": [0.1, 0.2]},
                    },
                    selected_risk="low",
                    selected_score=1,
                )
                local_state.link_intervention_session(intervention_id, "session-1")
                with open(state_file, encoding="utf-8") as handle:
                    stored = json.load(handle)

        serialized = json.dumps(stored)
        self.assertNotIn("delete the secret file", serialized)
        self.assertNotIn("inspect the smallest relevant file first", serialized)
        record = stored["interventions"][0]
        self.assertEqual(record["session_id"], "session-1")
        self.assertEqual(record["phase"], "plan")
        self.assertEqual(record["intervention_type"], "prompt_preflight")
        self.assertEqual(record["predicted_impact"]["savings"]["tokens"], [1000, 2000])
        self.assertEqual(record["selected_risk"], "low")
        self.assertEqual(record["risk_points_reduced"], 7)

    def test_outcome_replaces_previous_value_for_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_outcome("session-1", "rework")
                local_state.record_outcome("session-1", "useful", "tests passed")
                outcome = local_state.get_outcome("session-1")
                counts = local_state.outcome_counts()

        self.assertEqual(outcome["outcome"], "useful")
        self.assertEqual(outcome["note"], "tests passed")
        self.assertEqual(counts["useful"], 1)
        self.assertEqual(counts["rework"], 0)

    def test_rejects_unknown_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                with self.assertRaises(ValueError):
                    local_state.record_outcome("session-1", "maybe")

    def test_existing_shared_parent_permissions_are_not_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(local_state.os, "chmod") as chmod,
            ):
                local_state.record_outcome("session-1", "useful")

        if os.name == "nt":
            chmod.assert_not_called()
        else:
            chmod.assert_called_once_with(local_state.Path(state_file), 0o600)

    def test_cross_process_lock_blocks_a_second_holder_until_released(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                lock_path = local_state._lock_path()
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with open(lock_path, "a+b") as first:
                    if first.seek(0, os.SEEK_END) == 0:
                        first.write(b"\0")
                        first.flush()
                    local_state._acquire_file_lock(first)
                    try:
                        with open(lock_path, "a+b") as second:
                            with (
                                patch.object(local_state, "LOCK_TIMEOUT_SECONDS", 0.2),
                                patch.object(local_state, "LOCK_POLL_SECONDS", 0.02),
                            ):
                                with self.assertRaises(local_state.StateLockTimeout):
                                    local_state._acquire_file_lock(second)
                    finally:
                        local_state._release_file_lock(first)

                # Once released, a fresh acquire succeeds immediately.
                with open(lock_path, "a+b") as third:
                    local_state._acquire_file_lock(third)
                    local_state._release_file_lock(third)

    def test_concurrent_interventions_do_not_clobber_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                import threading as threading_module

                def record(index: int) -> None:
                    local_state.record_intervention(
                        tool="claude",
                        cwd="/repo",
                        risk="low",
                        score=0,
                        findings=[],
                        original_prompt=f"prompt {index}",
                        suggested_prompt=f"prompt {index}",
                        decision="allowed_original",
                        selected_prompt=None,
                    )

                threads = [threading_module.Thread(target=record, args=(i,)) for i in range(20)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                with open(state_file, encoding="utf-8") as handle:
                    stored = json.load(handle)

        self.assertEqual(len(stored["interventions"]), 20)

    def test_hook_events_are_privacy_safe_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                for index in range(55):
                    local_state.record_hook_event(
                        tool="codex",
                        cwd="/repo",
                        event="received",
                        prompt_found=True,
                        risk="high",
                        score=8,
                        error="bind failed" if index == 54 else None,
                    )
                events = local_state.recent_hook_events(limit=60)
                with open(state_file, encoding="utf-8") as handle:
                    stored = json.load(handle)

        self.assertEqual(len(events), 50)
        self.assertEqual(events[0]["error"], "bind failed")
        self.assertNotIn("Refactor the entire codebase", json.dumps(stored))


if __name__ == "__main__":
    unittest.main()
