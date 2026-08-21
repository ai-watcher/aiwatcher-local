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


if __name__ == "__main__":
    unittest.main()
