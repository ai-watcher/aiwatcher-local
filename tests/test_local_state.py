from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from aiwatcher_cli import local_state


class LocalStateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
