import unittest

from aiwatcher_cli.work_understanding import build_work_understanding


class WorkUnderstandingTests(unittest.TestCase):
    def test_connects_evolving_intent_to_exact_evidence_without_claiming_completion(self) -> None:
        review = build_work_understanding(
            [
                {"turn": 1, "prompt": "Add delivery review cards.", "requests": 2, "tool_calls": 3},
                {"turn": 2, "prompt": "Also make approval stale when HEAD changes.", "requests": 4,
                 "tool_calls": 7, "tokens": 12000, "took_seconds": 185},
            ],
            commits=[{"sha": "a" * 12, "subject": "Bind approval to Git evidence"}],
            files=["aiwatcher_cli/ui.py", "tests/test_delivery_ui.py"],
            checks=[{"status": "passed", "exact_state": True}],
            exact_session_link=True,
        )

        self.assertEqual(review["turn_count"], 2)
        self.assertTrue(review["latest_request_differs"])
        self.assertEqual(review["latest_turn"]["model_calls"], 4)
        self.assertEqual(review["latest_turn"]["duration_label"], "3 min")
        self.assertEqual(review["changed_areas"], ["aiwatcher_cli", "tests"])
        self.assertEqual(review["passed_check_count"], 1)
        self.assertEqual(review["confidence"], "medium")
        self.assertEqual(review["linked_commit_count"], 1)
        self.assertEqual(review["alignment_status"], "confirmation_required")
        self.assertIn("does not prove", review["limitation"])
        self.assertEqual(len(review["intent_source_hash"]), 64)

    def test_compaction_rows_do_not_become_developer_turns(self) -> None:
        review = build_work_understanding([
            {"turn": 1, "prompt": "Implement the card."},
            {"turn": 2, "prompt": "This session is being continued", "compact_summary": True},
        ])

        self.assertEqual(review["turn_count"], 1)
        self.assertFalse(review["latest_request_differs"])

    def test_code_without_linked_intent_stays_low_confidence(self) -> None:
        review = build_work_understanding(
            [], commits=[{"sha": "abc1234", "subject": "Observed change"}], files=["README.md"],
        )

        self.assertIsNone(review["opening_objective"])
        self.assertEqual(review["confidence"], "low")
        self.assertIn("confirm", review["alignment_label"].lower())

    def test_only_receipted_commits_are_session_linked(self) -> None:
        linked = "a" * 40
        unrelated = "b" * 40
        review = build_work_understanding(
            [{"prompt": "Implement the requested feature."}],
            commits=[
                {"sha": linked[:12], "subject": "Implement feature"},
                {"sha": unrelated[:12], "subject": "Unrelated maintenance"},
            ],
            linked_commit_shas=[linked],
        )

        self.assertEqual(review["commit_count"], 2)
        self.assertEqual(review["linked_commit_count"], 1)
        self.assertTrue(review["commits"][0]["session_linked"])
        self.assertFalse(review["commits"][1]["session_linked"])

    def test_all_reviewed_turns_participate_in_intent_identity(self) -> None:
        before = build_work_understanding([
            {"at": "2026-10-09T10:00:00Z", "prompt": "Opening request"},
            {"at": "2026-10-09T10:01:00Z", "prompt": "Require audit logging"},
            {"at": "2026-10-09T10:02:00Z", "prompt": "Final request"},
        ])
        after = build_work_understanding([
            {"at": "2026-10-09T10:00:00Z", "prompt": "Opening request"},
            {"at": "2026-10-09T10:01:00Z", "prompt": "Do not add audit logging"},
            {"at": "2026-10-09T10:02:00Z", "prompt": "Final request"},
        ])

        self.assertNotEqual(before["intent_source_hash"], after["intent_source_hash"])

    def test_turns_from_overlapping_sessions_are_ordered_by_timestamp(self) -> None:
        review = build_work_understanding([
            {"at": "2026-10-09T10:03:00Z", "prompt": "Actually latest"},
            {"at": "2026-10-09T10:01:00Z", "prompt": "Opening"},
            {"at": "2026-10-09T10:02:00Z", "prompt": "Middle"},
        ])

        self.assertEqual(review["opening_objective"], "Opening")
        self.assertEqual(review["latest_request"], "Actually latest")


if __name__ == "__main__":
    unittest.main()
