from __future__ import annotations

import unittest

from aiwatcher_cli.text_utils import normalize_slug


class NormalizeSlugTests(unittest.TestCase):
    def test_words_are_lowercased_and_hyphenated(self) -> None:
        self.assertEqual(normalize_slug("Delivery Review Ready"), "delivery-review-ready")

    def test_repeated_whitespace_and_separators_collapse(self) -> None:
        self.assertEqual(normalize_slug("  delivery -- review___ready  "), "delivery-review-ready")

    def test_accents_are_transliterated_and_punctuation_is_removed(self) -> None:
        self.assertEqual(normalize_slug("Café déjà vu!"), "cafe-deja-vu")

    def test_empty_or_separator_only_input_returns_empty(self) -> None:
        self.assertEqual(normalize_slug(""), "")
        self.assertEqual(normalize_slug(" -- _ "), "")


if __name__ == "__main__":
    unittest.main()
