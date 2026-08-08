from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aiwatcher_cli import evidence_capture
from aiwatcher_cli.local_state import evidence_snapshots_for_sessions, record_evidence_snapshot
from aiwatcher_cli.scanner import LocalSession


def session(session_id: str, updated_at: datetime) -> LocalSession:
    return LocalSession(
        session_id=session_id,
        tool="claude-code",
        project_path="/repo",
        started_at=updated_at - timedelta(minutes=30),
        updated_at=updated_at,
    )


class Evidence:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def to_json(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "repo_root": "/repo",
            "commits": [{"sha": f"{self.session_id}-commit"}],
            "changed_files": ["app.py"],
            "tests": [],
            "inferred_outcome": "useful",
            "confidence": "low",
        }


class PassiveEvidenceCaptureTests(unittest.TestCase):
    def test_records_capped_old_missing_snapshots_only(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            session("old-1", now - timedelta(hours=4)),
            session("young", now - timedelta(minutes=30)),
            session("old-2", now - timedelta(hours=5)),
            session("old-3", now - timedelta(hours=6)),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}),
                patch.object(
                    evidence_capture,
                    "build_outcome_evidence",
                    side_effect=lambda row: Evidence(row.session_id),
                ),
            ):
                captured = evidence_capture.record_missing_evidence_snapshots(rows, limit=2, now=now)
                snapshots = evidence_snapshots_for_sessions()
                captured_again = evidence_capture.record_missing_evidence_snapshots(rows, limit=5, now=now)

        self.assertEqual(captured, 2)
        self.assertIn("old-1", snapshots)
        self.assertIn("old-2", snapshots)
        self.assertNotIn("young", snapshots)
        self.assertNotIn("old-3", snapshots)
        self.assertEqual(captured_again, 1)

    def test_records_from_precomputed_evidence_without_rebuilding(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [session("old-1", now - timedelta(hours=4)), session("old-2", now - timedelta(hours=5))]
        evidence = {"old-1": Evidence("old-1"), "old-2": Evidence("old-2")}

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record_evidence_snapshot("old-1", Evidence("old-1").to_json())
                captured = evidence_capture.record_missing_evidence_snapshots_from_evidence(
                    rows,
                    evidence,
                    limit=5,
                    now=now,
                )
                snapshots = evidence_snapshots_for_sessions()

        self.assertEqual(captured, 1)
        self.assertIn("old-1", snapshots)
        self.assertIn("old-2", snapshots)


if __name__ == "__main__":
    unittest.main()
