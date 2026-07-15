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

    def test_concurrent_decisions_do_not_clobber_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                import threading as threading_module

                def record(index: int) -> None:
                    local_state.record_decision("session-1", f"decision {index}")

                threads = [threading_module.Thread(target=record, args=(i,)) for i in range(20)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                stored = local_state.recent_decisions("session-1", limit=20)

        self.assertEqual(len(stored), 20)

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

    def test_evidence_snapshot_never_persists_commit_text(self) -> None:
        # build_outcome_evidence() now captures real commit subject/body text
        # (see outcome_evidence.py) for use in ephemeral, copy-once handoff
        # briefs. That text must never reach the persistent evidence_snapshot
        # store on disk -- this test simulates the real evidence.to_json()
        # shape and asserts the subject/body text is filtered out before
        # anything is written.
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record = local_state.record_evidence_snapshot("session-1", {
                    "repo_root": "/Users/dev/secret-project",
                    "commits": [{
                        "sha": "abcdef1234567890",
                        "subject": "fix login bug for acme corp",
                        "body": "Session tokens were not being refreshed for the acme account.",
                        "committed_at": "2026-07-13T10:00:00Z",
                    }],
                    "changed_files": ["src/auth/secrets.py"],
                    "tests": [{"artifact": "test-results/auth.xml"}],
                    "inferred_outcome": "useful",
                    "confidence": "low",
                })
                snapshots = local_state.evidence_snapshots_for_sessions({"session-1"})
                with open(state_file, encoding="utf-8") as handle:
                    stored = json.load(handle)

        serialized = json.dumps(stored)
        self.assertEqual(record["commit_shas"], ["abcdef123456"])
        self.assertEqual(snapshots["session-1"]["inferred_outcome"], "useful")
        self.assertNotIn("/Users/dev/secret-project", serialized)
        self.assertNotIn("src/auth/secrets.py", serialized)
        self.assertNotIn("test-results/auth.xml", serialized)
        self.assertNotIn("fix login bug", serialized)
        self.assertNotIn("acme", serialized)

    def test_record_decision_round_trips_and_orders_most_recent_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                local_state.record_decision(
                    "session-1",
                    "Chose real commit subject/body over hashing",
                    reasoning="A commit message explains itself to a future reader.",
                    alternatives_rejected=["hashing the subject"],
                )
                local_state.record_decision(
                    "session-1",
                    "Considered a token-based tiebreaker",
                    reasoning="Still picks turn #1 for unrelated reasons.",
                    alternatives_rejected=["token-based tiebreaker", "git diff --stat only"],
                )
                local_state.record_decision("session-2", "Unrelated decision for a different session")
                decisions = local_state.recent_decisions("session-1")

        self.assertEqual(len(decisions), 2)
        self.assertEqual(decisions[0]["summary"], "Considered a token-based tiebreaker")
        self.assertEqual(decisions[0]["alternatives_rejected"], ["token-based tiebreaker", "git diff --stat only"])
        self.assertEqual(decisions[1]["summary"], "Chose real commit subject/body over hashing")

    def test_record_decision_requires_a_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                with self.assertRaises(ValueError):
                    local_state.record_decision("session-1", "   ")

    def test_recent_decisions_respects_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                for i in range(8):
                    local_state.record_decision("session-1", f"decision {i}")
                decisions = local_state.recent_decisions("session-1", limit=5)

        self.assertEqual(len(decisions), 5)
        self.assertEqual(decisions[0]["summary"], "decision 7")


if __name__ == "__main__":
    unittest.main()
