from __future__ import annotations

import unittest

from aiwatcher_cli.pricing import estimate_cost, is_subscription_model, lookup


class CurrentGenerationModelPricingTests(unittest.TestCase):
    # Regression guard for a real gap: these three models were missing from
    # MODEL_PRICING entirely, so estimate_cost() silently returned $0.00 for
    # any session using them -- indistinguishable from a genuine subscription
    # model, even though none of these are subscription-priced.

    def test_sonnet_5_is_priced_not_zeroed(self) -> None:
        pricing = lookup("claude-sonnet-5")
        self.assertIsNotNone(pricing)
        self.assertFalse(pricing["subscription"])
        self.assertEqual(estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000), 3.00 + 15.00)

    def test_opus_4_8_is_priced_not_zeroed(self) -> None:
        pricing = lookup("claude-opus-4-8")
        self.assertIsNotNone(pricing)
        self.assertFalse(pricing["subscription"])
        self.assertEqual(estimate_cost("claude-opus-4-8", 1_000_000, 1_000_000), 5.00 + 25.00)

    def test_fable_5_is_priced_not_zeroed(self) -> None:
        pricing = lookup("claude-fable-5")
        self.assertIsNotNone(pricing)
        self.assertFalse(pricing["subscription"])
        self.assertEqual(estimate_cost("claude-fable-5", 1_000_000, 1_000_000), 10.00 + 50.00)

    def test_none_of_the_three_are_flagged_as_subscription_only(self) -> None:
        for model in ("claude-sonnet-5", "claude-opus-4-8", "claude-fable-5"):
            self.assertFalse(is_subscription_model(model), model)

    def test_unrecognized_model_still_returns_zero(self) -> None:
        # Sanity check the existing "unknown price -> $0.00" fallback still
        # works -- this fix should not make estimate_cost guess at pricing
        # for models it has never heard of.
        self.assertEqual(estimate_cost("some-future-model-nobody-has-added-yet", 1_000_000, 1_000_000), 0.0)
        self.assertIsNone(lookup("some-future-model-nobody-has-added-yet"))

    def test_dated_snapshot_variants_inherit_the_bare_model_price(self) -> None:
        # Prefix-match fallback: a hypothetical dated snapshot of Sonnet 5
        # should price the same as the bare model, same as existing entries
        # like "claude-opus-4" already do for their dated variants.
        self.assertEqual(
            estimate_cost("claude-sonnet-5-20260615", 1_000_000, 1_000_000),
            estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000),
        )


if __name__ == "__main__":
    unittest.main()
