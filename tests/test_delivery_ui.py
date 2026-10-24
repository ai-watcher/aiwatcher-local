import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

from aiwatcher_cli import local_state, ui
from aiwatcher_cli.delivery_review import build_work_receipt, work_receipt_matches_current_checkout
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
        with ui._DELEGATED_SESSION_CACHE_LOCK:
            ui._DELEGATED_SESSION_CACHE.update({"expires_at": 0.0, "session_ids": set()})
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

    def test_candidate_approval_is_explicit_privacy_safe_and_bound_to_head(self) -> None:
        candidate = ui.build_explicit_delivery_review({
            "project_path": str(self.repo),
            "objective": "Ship the reviewed delivery evidence.",
        })
        self.assertFalse(candidate["approval_valid"])
        self.assertTrue(candidate["current_state_matches"])
        self.assertEqual(len(candidate["evidence_hash"]), 64)

        rejected = ui.approve_delivery_review({
            "receipt_id": candidate["receipt_id"],
            "evidence_hash": candidate["evidence_hash"],
            "objective": "Ship the reviewed delivery evidence.",
        })
        self.assertEqual(rejected["http_status"], 400)

        approved = ui.approve_delivery_review({
            "receipt_id": candidate["receipt_id"],
            "evidence_hash": candidate["evidence_hash"],
            "objective": "Ship the reviewed delivery evidence.",
            "confirmed": True,
        })
        self.assertTrue(approved["approval_valid"])
        self.assertEqual(approved["objective"]["provenance"], "user_confirmed")
        self.assertNotIn("Objective unavailable", " ".join(approved["attention"]))
        stored = local_state.recent_work_receipts()[0]
        self.assertIsNone(stored["objective"]["text"])
        self.assertEqual(stored["approval"]["evidence_hash"], candidate["evidence_hash"])
        self.assertNotIn("Ship the reviewed delivery evidence.", json.dumps(stored))

        listing = ui.build_delivery_reviews()["reviews"][0]
        self.assertTrue(listing["approval_valid"])
        self.assertIsNone(listing["objective"]["text"])

        (self.repo / "later.txt").write_text("later\n", encoding="utf-8")
        git(self.repo, "add", "later.txt")
        git(self.repo, "commit", "-m", "later change")
        stale = ui.build_delivery_reviews()["reviews"][0]
        self.assertFalse(stale["approval_valid"])
        self.assertFalse(stale["current_state_matches"])
        self.assertIn("Checkout Git evidence changed", " ".join(stale["attention"]))

    def test_approval_becomes_stale_when_dirty_evidence_changes_at_same_head(self) -> None:
        candidate = ui.build_explicit_delivery_review({
            "project_path": str(self.repo),
            "objective": "Ship exact working-tree evidence.",
        })
        approved = ui.approve_delivery_review({
            "receipt_id": candidate["receipt_id"],
            "evidence_hash": candidate["evidence_hash"],
            "objective": "Ship exact working-tree evidence.",
            "confirmed": True,
        })
        self.assertTrue(approved["approval_valid"])

        (self.repo / "uncommitted.txt").write_text("changed after approval\n", encoding="utf-8")
        stale = ui.build_delivery_reviews()["reviews"][0]

        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), candidate["snapshot"]["head_sha"])
        self.assertFalse(stale["current_state_matches"])
        self.assertFalse(stale["approval_valid"])

    def test_unobservable_dirty_state_never_matches_exact_approval(self) -> None:
        candidate = ui.build_explicit_delivery_review({
            "project_path": str(self.repo),
            "objective": "Ship only observable evidence.",
        })
        candidate["snapshot"]["dirty_fingerprint"] = None
        event = local_state.recent_delivery_events()[0]

        self.assertFalse(work_receipt_matches_current_checkout(candidate, event))

    def test_approval_becomes_stale_on_same_head_branch_change(self) -> None:
        candidate = ui.build_explicit_delivery_review({
            "project_path": str(self.repo),
            "objective": "Ship from the reviewed branch.",
        })
        approved = ui.approve_delivery_review({
            "receipt_id": candidate["receipt_id"],
            "evidence_hash": candidate["evidence_hash"],
            "objective": "Ship from the reviewed branch.",
            "confirmed": True,
        })
        self.assertTrue(approved["approval_valid"])

        git(self.repo, "switch", "-c", "same-head-other-branch")
        stale = ui.build_delivery_reviews()["reviews"][0]

        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), candidate["snapshot"]["head_sha"])
        self.assertFalse(stale["current_state_matches"])
        self.assertFalse(stale["approval_valid"])

    def test_approval_becomes_stale_when_tracked_upstream_advances(self) -> None:
        remote = self.root / "remote.git"
        git(self.root, "init", "--bare", str(remote))
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "-u", "origin", "feature")
        candidate = ui.build_explicit_delivery_review({
            "project_path": str(self.repo),
            "objective": "Ship against this upstream state.",
        })
        approved = ui.approve_delivery_review({
            "receipt_id": candidate["receipt_id"],
            "evidence_hash": candidate["evidence_hash"],
            "objective": "Ship against this upstream state.",
            "confirmed": True,
        })
        self.assertTrue(approved["approval_valid"])

        head = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "remote-change.txt").write_text("remote moved\n", encoding="utf-8")
        git(self.repo, "add", "remote-change.txt")
        git(self.repo, "commit", "-m", "advance upstream")
        git(self.repo, "push", "origin", "feature")
        git(self.repo, "reset", "--hard", head)
        stale = ui.build_delivery_reviews()["reviews"][0]

        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertFalse(stale["current_state_matches"])
        self.assertFalse(stale["approval_valid"])

    def test_approval_rejects_a_stale_evidence_hash(self) -> None:
        candidate = ui.build_explicit_delivery_review({
            "project_path": str(self.repo),
            "objective": "Ship exact evidence.",
        })
        result = ui.approve_delivery_review({
            "receipt_id": candidate["receipt_id"],
            "evidence_hash": "0" * 64,
            "objective": "Ship exact evidence.",
            "confirmed": True,
        })
        self.assertEqual(result["http_status"], 409)
        self.assertIn("evidence changed", result["error"])

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

        with patch.object(ui, "_known_delegated_session_ids", return_value=set()), patch.object(
            ui, "_find_session_row", return_value=session,
        ):
            review = ui.build_delivery_reviews()["reviews"][0]

        self.assertEqual(review["objective"]["provenance"], "inferred")
        self.assertEqual(review["objective"]["text"], "Add an evidence-backed delivery review.")
        self.assertIn("confirm before sharing", review["summary_text"])
        self.assertEqual(review["work_understanding"]["turn_count"], 1)
        self.assertTrue(review["work_understanding"]["exact_session_link"])
        self.assertIn("does not prove", review["work_understanding"]["limitation"])
        self.assertIn("Intent and evidence", review["summary_text"])

    def test_delegated_agent_prompt_is_not_presented_as_developer_intent(self) -> None:
        identity = resolve_git_identity(str(self.repo))
        head = git(self.repo, "rev-parse", "HEAD")
        source = self.root / "agent-child.jsonl"
        source.write_text(json.dumps({
            "uuid": "prompt-1", "type": "user", "sessionId": "agent-child",
            "timestamp": "2026-10-09T12:00:00Z",
            "message": {"content": "Review only the authentication files."},
        }) + "\n", encoding="utf-8")
        local_state.record_commit_receipt({
            "sha": head, "checkout_path": identity.checkout_path,
            "repository_id": identity.repository_id,
            "repository_lineage_id": identity.repository_lineage_id,
            "checkout_id": identity.checkout_id, "session_id": "agent-child", "source_id": "commit-child",
        })
        build_work_receipt(
            str(self.repo), event_kind="push", event_status="confirmed",
            event_source="test", event_source_id="push-child", remote="origin", remote_ref="feature",
            sessions=[],
        )
        session = LocalSession(
            session_id="agent-child", tool="codex-cli", source_path=str(source),
            project_path=str(self.repo), raw_cwd=str(self.repo),
        )

        with patch.object(ui, "_known_delegated_session_ids", return_value={"agent-child"}), patch.object(
            ui, "_find_session_row", return_value=session,
        ):
            review = ui.build_delivery_reviews()["reviews"][0]

        self.assertIsNone(review["objective"]["text"])
        self.assertEqual(review["work_understanding"]["turn_count"], 0)
        self.assertEqual(review["work_understanding"]["excluded_delegated_sessions"], 1)
        self.assertEqual(review["work_understanding"]["confidence"], "low")

    def test_only_commits_with_receipts_are_marked_session_linked(self) -> None:
        identity = resolve_git_identity(str(self.repo))
        linked_head = git(self.repo, "rev-parse", "HEAD")
        local_state.record_commit_receipt({
            "sha": linked_head, "checkout_path": identity.checkout_path,
            "repository_id": identity.repository_id,
            "repository_lineage_id": identity.repository_lineage_id,
            "checkout_id": identity.checkout_id, "session_id": "session-1", "source_id": "commit-linked",
        })
        (self.repo / "maintenance.txt").write_text("unrelated\n", encoding="utf-8")
        git(self.repo, "add", "maintenance.txt")
        git(self.repo, "commit", "-m", "unrelated maintenance")
        source = self.root / "session.jsonl"
        source.write_text(json.dumps({
            "uuid": "prompt-1", "type": "user", "sessionId": "session-1",
            "timestamp": "2026-10-09T12:00:00Z", "message": {"content": "Implement feature."},
        }) + "\n", encoding="utf-8")
        build_work_receipt(
            str(self.repo), event_kind="push", event_status="confirmed",
            event_source="test", event_source_id="push-partial", remote="origin", remote_ref="feature",
            sessions=[],
        )
        session = LocalSession(session_id="session-1", tool="claude-code", source_path=str(source))

        with patch.object(ui, "_known_delegated_session_ids", return_value=set()), patch.object(
            ui, "_find_session_row", return_value=session,
        ):
            understanding = ui.build_delivery_reviews()["reviews"][0]["work_understanding"]

        self.assertEqual(understanding["commit_count"], 2)
        self.assertEqual(understanding["linked_commit_count"], 1)
        self.assertEqual([row["session_linked"] for row in understanding["commits"]], [True, False])

    def test_approval_becomes_stale_when_a_middle_intent_turn_changes(self) -> None:
        identity = resolve_git_identity(str(self.repo))
        head = git(self.repo, "rev-parse", "HEAD")
        source = self.root / "session.jsonl"

        def write_turns(middle: str) -> None:
            rows = [
                ("1", "2026-10-09T12:00:00Z", "Implement delivery review."),
                ("2", "2026-10-09T12:01:00Z", middle),
                ("3", "2026-10-09T12:02:00Z", "Keep the final summary concise."),
            ]
            source.write_text("".join(json.dumps({
                "uuid": row_id, "type": "user", "sessionId": "session-1",
                "timestamp": stamp, "message": {"content": prompt},
            }) + "\n" for row_id, stamp, prompt in rows), encoding="utf-8")

        write_turns("Require audit logging.")
        local_state.record_commit_receipt({
            "sha": head, "checkout_path": identity.checkout_path,
            "repository_id": identity.repository_id,
            "repository_lineage_id": identity.repository_lineage_id,
            "checkout_id": identity.checkout_id, "session_id": "session-1", "source_id": "commit-intent",
        })
        build_work_receipt(
            str(self.repo), event_kind="push", event_status="confirmed",
            event_source="test", event_source_id="push-intent", remote="origin", remote_ref="feature",
            sessions=[],
        )
        session = LocalSession(session_id="session-1", tool="claude-code", source_path=str(source))

        with patch.object(ui, "_known_delegated_session_ids", return_value=set()), patch.object(
            ui, "_find_session_row", return_value=session,
        ):
            review = ui.build_delivery_reviews()["reviews"][0]
            approved = ui.approve_delivery_review({
                "receipt_id": review["receipt_id"], "evidence_hash": review["evidence_hash"],
                "objective": review["objective"]["text"], "confirmed": True,
            })
            self.assertTrue(approved["approval_valid"])
            write_turns("Do not add audit logging.")
            stale = ui.build_delivery_reviews()["reviews"][0]

        self.assertFalse(stale["approval_valid"])

    def test_delivery_mutations_reject_cross_origin_pages(self) -> None:
        self.assertIn("/api/delivery-review", ui.SAME_ORIGIN_ONLY_ROUTES)
        self.assertIn("/api/delivery-review-approve", ui.SAME_ORIGIN_ONLY_ROUTES)
        self.assertIn("/api/delivery-review-viewed", ui.SAME_ORIGIN_ONLY_ROUTES)
        self.assertIn("/api/delivery-tracking", ui.SAME_ORIGIN_ONLY_ROUTES)


if __name__ == "__main__":
    unittest.main()
