import json
import unittest

from aiwatcher_cli.delivery import (
    ContributionEdge,
    DeliveryEvent,
    DeliverySnapshot,
    ObjectiveClaim,
    VerificationClaim,
    WorkflowStats,
    WorkReceipt,
)


HEAD = "a" * 40
BASE = "b" * 40


def event(**overrides):
    values = {
        "event_id": "event-1",
        "kind": "push",
        "status": "confirmed",
        "observed_at": "2026-10-09T12:00:00-04:00",
        "repository_id": "repository-v1-test",
        "checkout_id": "checkout-v1-test",
        "head_sha": HEAD,
        "source": "transcript_tool_result",
        "session_id": "session-1",
        "source_id": "source-1",
        "remote": "origin",
        "remote_ref": "feature",
    }
    values.update(overrides)
    return DeliveryEvent(**values)


def snapshot(**overrides):
    values = {
        "repository_id": "repository-v1-test",
        "checkout_id": "checkout-v1-test",
        "branch": "feature",
        "base_sha": BASE,
        "head_sha": HEAD,
        "upstream": "origin/feature",
        "clean": True,
        "commit_shas": (HEAD,),
        "changed_files": ("src/private_name.py",),
        "lines_added": 12,
        "lines_removed": 3,
    }
    values.update(overrides)
    return DeliverySnapshot(**values)


class DeliveryContractTests(unittest.TestCase):
    def test_receipt_has_stable_versioned_shape(self):
        receipt = WorkReceipt(
            receipt_id="receipt-1",
            created_at="2026-10-09T16:01:00Z",
            event=event(),
            snapshot=snapshot(),
            objective=ObjectiveClaim(
                provenance="user_confirmed",
                confidence="high",
                text="Fix the stale status without changing unrelated behavior.",
                confirmed_at="2026-10-09T16:00:00Z",
            ),
            verifications=(VerificationClaim(
                runner="python -m unittest",
                status="passed",
                scope="project_default",
                provenance="observed",
                exact_state=True,
                finished_at="2026-10-09T15:59:00Z",
                source_id="verify-1",
            ),),
            contributions=(ContributionEdge(
                session_id="session-1",
                commit_shas=(HEAD,),
                strength="exact",
                provenance="observed",
                source_id="commit-1",
            ),),
            workflow=WorkflowStats(
                session_count=1,
                user_turns=18,
                model_calls=63,
                tool_calls=141,
                observed_request_seconds=912.4,
                longest_observed_request_seconds=660,
                observed_idle_gap_seconds=120,
                coverage="one exact session",
            ),
            attention=("PR metadata was not checked.",),
        )

        payload = receipt.to_json()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["event"]["observed_at"], "2026-10-09T16:00:00+00:00")
        self.assertEqual(payload["snapshot"]["changed_file_count"], 1)
        self.assertEqual(payload["objective"]["provenance"], "user_confirmed")
        self.assertEqual(payload["verifications"][0]["exact_state"], True)
        self.assertEqual(payload["contributions"][0]["strength"], "exact")
        self.assertEqual(payload["workflow"]["user_turns"], 18)

    def test_persisted_receipt_strips_objective_and_file_paths(self):
        objective = "Ship the secret customer-specific behavior."
        receipt = WorkReceipt(
            receipt_id="receipt-1",
            created_at="2026-10-09T16:01:00Z",
            event=event(),
            snapshot=snapshot(changed_files=("customers/acme/private.py", "secrets.txt")),
            objective=ObjectiveClaim(
                provenance="user_confirmed", confidence="high", text=objective,
            ),
        )

        encoded = json.dumps(receipt.to_persisted_json(), sort_keys=True)
        self.assertNotIn(objective, encoded)
        self.assertNotIn("customers/acme/private.py", encoded)
        self.assertNotIn("secrets.txt", encoded)
        self.assertEqual(receipt.to_persisted_json()["snapshot"]["changed_file_count"], 2)
        self.assertEqual(receipt.to_persisted_json()["snapshot"]["changed_file_hashes"], [])
        self.assertIsNotNone(receipt.to_persisted_json()["objective"]["source_hash"])

    def test_unknown_values_degrade_conservatively(self):
        objective = ObjectiveClaim(provenance="magical", confidence="certain")
        verification = VerificationClaim(
            runner="custom check",
            status="amazing",
            scope="everything",
            provenance="guess",
            exact_state=False,
        )
        self.assertEqual(objective.provenance, "unavailable")
        self.assertEqual(objective.confidence, "none")
        self.assertEqual(verification.status, "result unknown")
        self.assertEqual(verification.scope, "unknown")
        self.assertEqual(verification.provenance, "unavailable")

    def test_user_confirmed_objective_requires_text(self):
        with self.assertRaisesRegex(ValueError, "requires text"):
            ObjectiveClaim(provenance="user_confirmed", confidence="high")

    def test_confirmed_pull_request_requires_url(self):
        with self.assertRaisesRegex(ValueError, "requires a URL"):
            event(kind="pull_request", pull_request_url=None)

    def test_exact_verification_must_be_observed(self):
        with self.assertRaisesRegex(ValueError, "must be observed"):
            VerificationClaim(
                runner="pytest", status="passed", scope="project_default",
                provenance="inferred", exact_state=True,
            )

    def test_exact_contribution_requires_observed_commits(self):
        with self.assertRaisesRegex(ValueError, "requires observed commits"):
            ContributionEdge(
                session_id="session-1", commit_shas=(), strength="exact", provenance="observed",
            )

    def test_receipt_rejects_mismatched_git_identity(self):
        with self.assertRaisesRegex(ValueError, "repository identity differ"):
            WorkReceipt(
                receipt_id="receipt-1",
                created_at="2026-10-09T16:01:00Z",
                event=event(repository_id="other"),
                snapshot=snapshot(),
            )


if __name__ == "__main__":
    unittest.main()
