import os
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from aiwatcher_cli import cli, local_state
from aiwatcher_cli.delivery_review import (
    DeliveryReviewUnavailable,
    build_work_receipt,
    format_work_receipt,
    hydrate_persisted_receipt,
)
from aiwatcher_cli.git_identity import resolve_git_identity
from aiwatcher_cli.outcome_evidence import verification_git_fingerprint


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


class DeliveryReviewBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "AIWatcher Test")
        git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-c", "feature")
        (self.repo / "feature.py").write_text("print('delivery')\n", encoding="utf-8")
        git(self.repo, "add", "feature.py")
        git(self.repo, "commit", "-m", "add delivery")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.state = self.root / "state.json"
        self.env = patch.dict(os.environ, {"AIWATCHER_STATE_FILE": str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _record_exact_verification(self) -> None:
        identity = resolve_git_identity(str(self.repo))
        self.assertIsNotNone(identity)
        before = verification_git_fingerprint(str(self.repo))
        local_state.record_verification_receipt(
            runner="python -m unittest",
            checkout_path=identity.checkout_path,
            repository_id=identity.repository_id,
            repository_lineage_id=identity.repository_lineage_id,
            started_checkout_id=before["checkout_id"],
            started_head=before["head"],
            started_dirty_fingerprint=before["dirty_fingerprint"],
            checkout_id=before["checkout_id"],
            head=before["head"],
            dirty_fingerprint=before["dirty_fingerprint"],
            started_at="2026-10-09T15:00:00+00:00",
            finished_at="2026-10-09T15:01:00+00:00",
            exit_code=0,
            state_binding="git_state",
            verification_scope="project_default",
            session_id="session-1",
            source_id="verification-1",
        )

    def test_explicit_review_builds_honest_local_candidate(self) -> None:
        self._record_exact_verification()
        identity = resolve_git_identity(str(self.repo))
        local_state.record_commit_receipt({
            "sha": self.head,
            "checkout_path": identity.checkout_path,
            "repository_id": identity.repository_id,
            "repository_lineage_id": identity.repository_lineage_id,
            "checkout_id": identity.checkout_id,
            "session_id": "session-1",
            "source_id": "commit-1",
        })

        receipt = build_work_receipt(
            str(self.repo),
            objective_text="Add a deterministic delivery review.",
            event_status="candidate",
            sessions=[],
        )

        self.assertEqual(receipt.event.status, "candidate")
        self.assertEqual(receipt.snapshot.base_sha, self.base)
        self.assertEqual(receipt.snapshot.commit_shas, (self.head,))
        self.assertEqual(receipt.snapshot.changed_files, ("feature.py",))
        self.assertTrue(receipt.snapshot.clean)
        self.assertTrue(receipt.verifications[0].exact_state)
        self.assertEqual(receipt.contributions[0].session_id, "session-1")
        self.assertIn("Local candidate only", " ".join(receipt.attention))
        self.assertEqual(len(local_state.recent_work_receipts()), 1)

        stored = local_state.recent_work_receipts()[0]
        self.assertIsNone(stored["objective"]["text"])
        self.assertEqual(stored["snapshot"]["changed_files"], [])
        event = local_state.recent_delivery_events()[0]
        hydrated = hydrate_persisted_receipt(stored, event)
        self.assertEqual(hydrated["snapshot"]["changed_files"], ["feature.py"])

    def test_root_commit_review_includes_its_changed_paths(self) -> None:
        root_repo = self.root / "root-only"
        root_repo.mkdir()
        git(root_repo, "init", "-b", "main")
        git(root_repo, "config", "user.name", "AIWatcher Test")
        git(root_repo, "config", "user.email", "test@example.com")
        (root_repo / "first.txt").write_text("first\n", encoding="utf-8")
        git(root_repo, "add", "first.txt")
        git(root_repo, "commit", "-m", "first")

        receipt = build_work_receipt(
            str(root_repo), event_kind="explicit_review", event_status="candidate", sessions=[],
        )

        self.assertEqual(receipt.snapshot.changed_files, ("first.txt",))
        self.assertEqual(receipt.snapshot.lines_added, 1)

    def test_confirmed_push_is_ready_evidence(self) -> None:
        receipt = build_work_receipt(
            str(self.repo),
            event_kind="push",
            event_status="confirmed",
            event_source="wrapped_git_push",
            event_source_id="push-1",
            event_base_sha=self.base,
            remote="origin",
            remote_ref="feature",
            sessions=[],
        )
        self.assertEqual(receipt.event.status, "confirmed")
        self.assertNotIn("Local candidate only", " ".join(receipt.attention))

    def test_unconfirmed_automatic_event_cannot_create_review(self) -> None:
        with self.assertRaisesRegex(DeliveryReviewUnavailable, "must be confirmed"):
            build_work_receipt(
                str(self.repo), event_kind="push", event_status="candidate", persist=False,
            )

    def test_dirty_tree_is_separate_from_delivered_range(self) -> None:
        (self.repo / "scratch.txt").write_text("not delivered\n", encoding="utf-8")
        receipt = build_work_receipt(
            str(self.repo), event_status="candidate", sessions=[], persist=False,
        )
        self.assertFalse(receipt.snapshot.clean)
        self.assertNotIn("scratch.txt", receipt.snapshot.changed_files)
        self.assertIn("working tree has changes", " ".join(receipt.attention))

    def test_text_summary_names_observed_timing_as_non_developer_time(self) -> None:
        receipt = build_work_receipt(
            str(self.repo), event_status="candidate", sessions=[], persist=False,
        )
        text = format_work_receipt(receipt)
        self.assertIn("AIWatcher Delivery Review", text)
        self.assertIn("No exact-state verification", text)
        self.assertNotIn("developer productivity", text.lower())

    def test_push_wrapper_creates_review_only_after_confirmed_current_head_push(self) -> None:
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        git(self.repo, "remote", "add", "origin", str(remote))
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = cli.main([
                "push", "--repo", str(self.repo), "--objective", "Deliver the feature.",
                "--", "--set-upstream", "origin", "feature",
            ])
        self.assertEqual(code, 0)
        events = local_state.recent_delivery_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "push")
        self.assertEqual(events[0]["status"], "confirmed")
        self.assertEqual(events[0]["head_sha"], self.head)
        self.assertIn("AIWatcher Delivery Review", output.getvalue())

    def test_failed_push_creates_no_delivery_event(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = cli.main([
                "push", "--repo", str(self.repo), "--", "missing-remote", "feature",
            ])
        self.assertNotEqual(code, 0)
        self.assertEqual(local_state.recent_delivery_events(), [])
        self.assertIn("did not create a delivery review", output.getvalue())

    def test_tag_only_push_cannot_take_credit_for_current_upstream(self) -> None:
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "--set-upstream", "origin", "feature")
        git(self.repo, "tag", "v1")

        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = cli.main(["push", "--repo", str(self.repo), "--", "origin", "v1"])

        self.assertEqual(code, 0)
        self.assertEqual(local_state.recent_delivery_events(), [])
        self.assertIn("did not prove that the current branch was delivered", output.getvalue())


if __name__ == "__main__":
    unittest.main()
