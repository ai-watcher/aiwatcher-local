import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

from aiwatcher_cli import ui
from aiwatcher_cli.delivery_review import build_work_receipt
from aiwatcher_cli.git_identity import resolve_git_identity
from aiwatcher_cli.scanner import LocalSession


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


class DeliveryReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state.json"
        self.env = patch.dict(os.environ, {"AIWATCHER_STATE_FILE": str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "AIWatcher Test")
        git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "base.txt")
        git(self.repo, "commit", "-m", "base")
        git(self.repo, "switch", "-c", "feature")
        (self.repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        git(self.repo, "add", "feature.txt")
        git(self.repo, "commit", "-m", "feature")

    def test_candidate_preview_is_visible_but_never_ready(self) -> None:
        body = ui.build_explicit_delivery_review({
            "project_path": str(self.repo),
            "objective": "Review this local candidate.",
        })
        self.assertFalse(body["ready"])
        self.assertEqual(body["event"]["status"], "candidate")
        self.assertIn("Review this local candidate", body["summary_text"])

        listing = ui.build_delivery_reviews()
        self.assertEqual(listing["ready_count"], 0)
        self.assertEqual(listing["reviews"][0]["objective"]["text"], None)
        self.assertIn("feature.txt", listing["reviews"][0]["snapshot"]["changed_files"])

    def test_repeated_preview_is_stable_and_confirmed_delivery_replaces_it(self) -> None:
        first = ui.build_explicit_delivery_review({"project_path": str(self.repo)})
        second = ui.build_explicit_delivery_review({"project_path": str(self.repo)})
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(len(ui.build_delivery_reviews()["reviews"]), 1)

        confirmed = build_work_receipt(
            str(self.repo), event_kind="push", event_status="confirmed",
            event_source="test", event_source_id="push-replaces-preview",
            remote="origin", remote_ref="feature", sessions=[],
        )
        listing = ui.build_delivery_reviews()
        self.assertEqual([row["receipt_id"] for row in listing["reviews"]], [confirmed.receipt_id])

    def test_confirmed_review_is_ready_until_viewed(self) -> None:
        receipt = build_work_receipt(
            str(self.repo), event_kind="push", event_status="confirmed",
            event_source="test", event_source_id="push-1", remote="origin", remote_ref="feature",
            sessions=[],
        )
        listing = ui.build_delivery_reviews()
        self.assertEqual(listing["ready_count"], 1)
        self.assertEqual(listing["latest_ready"]["receipt_id"], receipt.receipt_id)

        from aiwatcher_cli.local_state import mark_work_receipt_viewed
        self.assertIsNotNone(mark_work_receipt_viewed(receipt.receipt_id))
        listing = ui.build_delivery_reviews()
        self.assertEqual(listing["ready_count"], 0)
        self.assertIsNotNone(listing["reviews"][0]["viewed_at"])

    def test_objective_can_be_applied_transiently_to_confirmed_review(self) -> None:
        confirmed = build_work_receipt(
            str(self.repo), event_kind="push", event_status="confirmed",
            event_source="test", event_source_id="push-objective", remote="origin", remote_ref="feature",
            sessions=[],
        )

        preview = ui.build_explicit_delivery_review({
            "project_path": str(self.repo),
            "objective": "Ship an evidence-backed delivery review.",
        })

        self.assertEqual(preview["receipt_id"], confirmed.receipt_id)
        self.assertEqual(preview["event"]["status"], "confirmed")
        self.assertTrue(preview["transient_objective"])
        self.assertIn("Ship an evidence-backed delivery review.", preview["summary_text"])
        stored = ui.build_delivery_reviews()["reviews"][0]
        self.assertIsNone(stored["objective"]["text"])
        self.assertNotIn("Ship an evidence-backed delivery review.", stored["summary_text"])
        self.assertIn("aiwatcher push", ui.build_delivery_reviews()["automatic_coverage"])

    def test_objective_is_inferred_on_demand_from_exactly_linked_session(self) -> None:
        identity = resolve_git_identity(str(self.repo))
        head = git(self.repo, "rev-parse", "HEAD")
        source = self.root / "session.jsonl"
        source.write_text(json.dumps({
            "uuid": "prompt-1", "type": "user", "sessionId": "session-1",
            "timestamp": "2026-10-09T12:00:00Z",
            "message": {"content": "Add an evidence-backed delivery review."},
        }) + "\n", encoding="utf-8")
        from aiwatcher_cli import local_state
        local_state.record_commit_receipt({
            "sha": head, "checkout_path": identity.checkout_path,
            "repository_id": identity.repository_id,
            "repository_lineage_id": identity.repository_lineage_id,
            "checkout_id": identity.checkout_id, "session_id": "session-1", "source_id": "commit-1",
        })
        build_work_receipt(
            str(self.repo), event_kind="push", event_status="confirmed",
            event_source="test", event_source_id="push-inferred", remote="origin", remote_ref="feature",
            sessions=[],
        )
        session = LocalSession(
            session_id="session-1", tool="claude-code", project_path=str(self.repo),
            raw_cwd=str(self.repo), source_path=str(source),
            started_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )

        with patch.object(ui, "_find_session_row", return_value=session):
            review = ui.build_delivery_reviews()["reviews"][0]

        self.assertEqual(review["objective"]["provenance"], "inferred")
        self.assertEqual(review["objective"]["text"], "Add an evidence-backed delivery review.")
        self.assertIn("confirm before sharing", review["summary_text"])

    def test_delivery_mutations_reject_cross_origin_pages(self) -> None:
        self.assertIn("/api/delivery-review", ui.SAME_ORIGIN_ONLY_ROUTES)
        self.assertIn("/api/delivery-review-viewed", ui.SAME_ORIGIN_ONLY_ROUTES)


if __name__ == "__main__":
    unittest.main()
