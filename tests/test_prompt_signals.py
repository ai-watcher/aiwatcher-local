from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from aiwatcher_cli import cli, prompt_signals as ps

BS = chr(92)

BILLING = "Rewrite the billing module to use the new pricing rules and delete the legacy adapter"
SETTINGS = "Refactor the settings page to use the new design tokens and add tests for the coverage panel"


class RemovalDetectionTests(unittest.TestCase):
    """Spec 4.1. The brief told an agent not to "expand into unrelated cleanup"
    even when the cleanup was the stated goal, so a cautious agent left the
    adapter in place and reported success."""

    def test_a_requested_deletion_is_found(self):
        removals = ps.requested_removals(BILLING)
        self.assertEqual([r["what"] for r in removals], ["legacy adapter"])
        self.assertTrue(removals[0]["requested"])

    def test_a_rewrite_is_not_a_removal(self):
        # Spec 3.3 scopes `removals` to delete/remove/drop/replace. The wider
        # 2.1 list scores destructive intent, which is a different question:
        # listing a rewrite here would tell the agent to delete what the user
        # asked to rewrite.
        self.assertIn("rewrite", ps.scan_prompt(BILLING)["destructive_verbs"])
        self.assertNotIn("rewrite", [r["verb"] for r in ps.requested_removals(BILLING)])

    def test_a_negated_removal_is_not_requested(self):
        for text in (
            "Update the billing module but do not delete the legacy adapter",
            "Refactor this without removing the old adapter",
        ):
            with self.subTest(text=text):
                self.assertEqual(ps.requested_removals(text), [])

    def test_a_prompt_with_no_removal_finds_none(self):
        self.assertEqual(ps.requested_removals(SETTINGS), [])


class WordBoundaryTests(unittest.TestCase):
    def test_terms_match_on_word_boundaries(self):
        # "key" inside "monkey" and "all" inside "install" are exactly the false
        # positives that make a score untrustworthy.
        signals = ps.scan_prompt("Install the monkey patch finally")
        self.assertEqual(signals["sensitive_keywords"], [])
        self.assertEqual(signals["breadth_words"], [])

    def test_multi_word_verbs_match_as_phrases(self):
        self.assertIn("rip out", ps.scan_prompt("Rip out the old adapter")["destructive_verbs"])


class WordListConfigTests(unittest.TestCase):
    """Spec 2.2: both lists live in a config file so tuning does not need a
    release."""

    def test_a_config_file_overrides_the_defaults(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt-signals.json"
            path.write_text(json.dumps({"sensitive_keywords": ["widget"]}), encoding="utf-8")
            with mock.patch.dict("os.environ", {"AIWATCHER_PROMPT_SIGNALS_FILE": str(path)}):
                lists = ps.load_word_lists()
        self.assertEqual(lists["sensitive_keywords"], ("widget",))
        # Unspecified lists keep their defaults.
        self.assertIn("delete", lists["destructive_verbs"])

    def test_a_broken_config_falls_back_rather_than_failing(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt-signals.json"
            path.write_text("{not json", encoding="utf-8")
            with mock.patch.dict("os.environ", {"AIWATCHER_PROMPT_SIGNALS_FILE": str(path)}):
                lists = ps.load_word_lists()
        self.assertIn("billing", lists["sensitive_keywords"])


class ExecutionBriefTests(unittest.TestCase):
    def _brief(self, text):
        return cli.build_execution_brief(
            text, cwd=None, broad_scope=False, needs_checkpoint=True,
            sensitive_or_destructive=True, vague_scope=False, multiple_tasks=False,
            removals=ps.requested_removals(text),
        )

    def test_a_requested_deletion_appears_and_the_contradiction_goes(self):
        brief = self._brief(BILLING)
        self.assertIn("Requested removals", brief)
        self.assertIn("legacy adapter", brief)
        self.assertNotIn("do not expand into unrelated cleanup", brief)
        # Bounded rather than forbidden.
        self.assertIn("Do not remove anything beyond the removals listed above", brief)

    def test_the_removal_is_stated_above_the_guardrails(self):
        brief = self._brief(BILLING)
        self.assertLess(brief.index("Requested removals"), brief.index("Execution approach"))

    def test_a_prompt_with_no_removals_keeps_the_original_bullet(self):
        brief = self._brief(SETTINGS)
        self.assertNotIn("Requested removals", brief)
        self.assertIn("do not expand into unrelated cleanup", brief)


class PreflightPayloadTests(unittest.TestCase):
    """Zone B needs the Stage 1 signals to reach the client. They were computed
    and then dropped by the response builder, which is why the zone rendered
    empty the first time."""

    def test_the_payload_carries_signals_and_removals(self):
        result = cli.analyze_prompt(BILLING, tool="claude", cwd=None)
        self.assertIn("signals", result)
        self.assertIn("removals", result)
        self.assertIn("billing", result["signals"]["sensitive_keywords"])
        self.assertEqual([r["what"] for r in result["removals"]], ["legacy adapter"])

    def test_two_unrelated_prompts_no_longer_produce_the_same_signals(self):
        # The defect that started this: diffing two unrelated prompts produced
        # identical output apart from the tool name.
        billing = cli.analyze_prompt(BILLING, tool="claude", cwd=None)
        settings = cli.analyze_prompt(SETTINGS, tool="claude", cwd=None)
        self.assertNotEqual(billing["signals"], settings["signals"])
        self.assertNotEqual(billing["suggested_prompt"], settings["suggested_prompt"])


BILLING_VARIANTS = (
    "Rewrite the billing module to use the new pricing table and delete the legacy adapter",
    "Rewrite the billing module to use the new pricing rules and delete the legacy adapter",
    "Delete the legacy adapter and rewrite the billing module for the new pricing scheme",
)


class BlastRadiusScoreTests(unittest.TestCase):
    """Plan item 3 / spec M2.

    The acceptance test the plan shipped with -- "the billing prompt reaches the
    medium band and a settings refactor does not" -- passed before any of this
    was written. The billing prompt scored high because "pricing table" sits
    within 35 characters of "delete", and `table` was in the database-target
    list, so the scorer believed a table was being dropped. Swapping one word
    for "rules" dropped it to low. These test the property that was actually
    wanted: the same intent scores the same however it is phrased.
    """

    def _score(self, text):
        with mock.patch.object(cli, "sessions_since", return_value=[]):
            return cli.analyze_prompt(text, tool="claude", cwd=None)

    def test_the_same_intent_scores_the_same_however_it_is_worded(self):
        scores = [self._score(t)["score"] for t in BILLING_VARIANTS]
        self.assertEqual(len(set(scores)), 1, f"paraphrases scored differently: {scores}")
        for text, score in zip(BILLING_VARIANTS, scores):
            with self.subTest(text=text[:40]):
                self.assertGreaterEqual(score, ps.GATE_POINTS)

    def test_ordinary_work_stays_below_the_gate(self):
        for text in (
            SETTINGS,
            "Add a pricing table to the settings page",
            "Show the session id in the drawer header",
            "Explain this function",
        ):
            with self.subTest(text=text[:40]):
                self.assertFalse(ps.score_blast_radius(text)["gate"])

    def test_a_bare_table_is_not_a_database(self):
        # "pricing table", "lookup table", "HTML table". The qualifier is what
        # makes it a database, and `drop table` is matched separately anyway.
        # Compared on the blast contribution, not the total: almost every prompt
        # carries a baseline +2 for naming no plan or checkpoint, which is a
        # statement about prompt shape rather than about what it would touch.
        self.assertEqual(ps.score_blast_radius("Add a pricing table to the settings page")["points"], 2)
        self.assertFalse(ps.score_blast_radius("Add a pricing table to the settings page")["gate"])
        self.assertGreaterEqual(self._score("Drop the users table from the production database")["score"], 6)

    def test_narrow_cleanup_is_not_high_risk(self):
        # A destructive verb aimed at nothing sensitive or broad is maintenance.
        for text in ("delete dead code in one test file", "clear cache",
                     "remove old code from utils.py"):
            with self.subTest(text=text):
                self.assertNotEqual(self._score(text)["risk"], "high")

    def test_documentation_and_fixtures_are_exempt(self):
        for text in (
            "Update the auth docs to remove an obsolete screenshot",
            "Remove the validation error message from the signup form UI",
        ):
            with self.subTest(text=text[:40]):
                blast = ps.score_blast_radius(text)
                self.assertEqual(blast["points"], 0)
                self.assertEqual([r["signal"] for r in blast["reasons"]], ["benign_context"])

    def test_breadth_overrides_the_exemption(self):
        # "delete all the tests" names a fixture context and is still sweeping.
        self.assertGreater(ps.score_blast_radius("delete all the tests across the entire codebase")["points"], 0)

    def test_a_short_question_is_not_a_short_change_request(self):
        # Spec 2 scores a terse *change request*. Terseness alone said a
        # three-word question carried risk.
        self.assertEqual(ps.score_blast_radius("Explain this function")["points"], 0)

    def test_sensitivity_alone_does_not_reach_the_gate(self):
        # Measured over 812 real local prompts, scoring a domain keyword at 3 on
        # its own fires on 19% of them -- "session" alone appears in 11%, being
        # this product's main domain noun. Pairing halves that.
        blast = ps.score_blast_radius("Show the session id in the drawer header")
        self.assertLess(blast["points"], ps.GATE_POINTS)
        self.assertIn("sensitive_alone", [r["signal"] for r in blast["reasons"]])

    def test_a_guarded_prompt_scores_lower_than_the_bare_one(self):
        bare = "Refactor the entire codebase and delete old auth secrets"
        guarded = bare + ". Ask for confirmation before deleting anything."
        self.assertLess(
            ps.score_blast_radius(guarded, guarded=True)["points"],
            ps.score_blast_radius(bare)["points"],
        )

    def test_blast_radius_cannot_break_prompt_analysis(self):
        # It shells out to git. A missing, slow or mocked git must cost the
        # score a signal, not the caller an exception.
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            ps._TREE_CACHE.clear()
            self.assertEqual(ps.repo_paths("/nowhere"), ())
        ps._TREE_CACHE.clear()

    def test_refusal_is_decided_separately_from_the_displayed_score(self):
        # The displayed score includes blast radius, which is the honest figure.
        # Folding it into the block threshold would have taken the hook from
        # blocking 0% of real prompts to 3%, as a side effect of a scoring fix.
        result = self._score(BILLING_VARIANTS[0])
        self.assertGreaterEqual(result["score"], 6)
        self.assertIn("hook_risk", result)
        self.assertEqual(
            cli._risk_for_score(result["score"] - result["blast"]["points"]),
            cli._blocking_risk(result),
        )

    def test_blast_reasons_are_itemised_and_not_mixed_into_findings(self):
        # A finding pairs one-for-one with a guardrail chip; blast reasons are
        # observations, not controls.
        result = self._score(BILLING_VARIANTS[0])
        self.assertTrue(result["blast"]["reasons"])
        for reason in result["blast"]["reasons"]:
            self.assertIn("text", reason)
            self.assertNotIn(reason["text"], result["findings"])


if __name__ == "__main__":
    unittest.main()
