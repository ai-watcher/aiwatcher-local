"""Gate acceptance: of the times Prompt Gate stopped and asked, how often the brief was taken."""
from __future__ import annotations

import unittest

from aiwatcher_cli import ui


class GateAcceptanceTests(unittest.TestCase):
    def test_only_asks_make_the_ratio(self) -> None:
        decisions = (
            ["allowed_original"] * 19 + ["brief_accepted"] + ["context_added"] * 22 + ["blocked"] * 3 + ["cancelled"]
        )
        gate = ui._gate_acceptance(decisions)
        self.assertTrue(gate["measurable"])
        self.assertEqual((gate["asks"], gate["taken"]), (21, 1))
        self.assertEqual(gate["ran_original"], 19)
        self.assertEqual(gate["cancelled"], 1)
        # Silent briefs and blocks are reported beside the ratio, never inside it.
        self.assertEqual((gate["silent_briefs"], gate["blocked"]), (22, 3))
        self.assertIsNone(gate["reason"])

    def test_no_asks_is_not_measurable_rather_than_zero_of_zero(self) -> None:
        gate = ui._gate_acceptance(["context_added"] * 5 + ["blocked"])
        self.assertFalse(gate["measurable"])
        self.assertEqual((gate["asks"], gate["taken"]), (0, 0))
        self.assertIn("did not ask", gate["reason"])
        self.assertEqual(gate["silent_briefs"], 5)

    def test_edited_briefs_count_as_taken(self) -> None:
        gate = ui._gate_acceptance(["brief_edited", "allowed_original"])
        self.assertEqual((gate["asks"], gate["taken"]), (2, 1))


if __name__ == "__main__":
    unittest.main()
