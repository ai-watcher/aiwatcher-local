from __future__ import annotations

import unittest

from aiwatcher_cli.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    MODEL_PRICING,
    estimate_cost,
    is_subscription_model,
    lookup,
)


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


class OpusFivePricingTests(unittest.TestCase):
    # Same gap as the three above: claude-opus-5 was absent from MODEL_PRICING,
    # and lookup()'s prefix match does not rescue it ("claude-opus-5" does not
    # start with "claude-opus-4"), so real sessions on it were costed at $0.00.

    def test_opus_5_is_priced_not_zeroed(self) -> None:
        pricing = lookup("claude-opus-5")
        self.assertIsNotNone(pricing)
        self.assertFalse(pricing["subscription"])
        self.assertEqual(estimate_cost("claude-opus-5", 1_000_000, 1_000_000), 5.00 + 25.00)

    def test_opus_5_has_its_own_entry_rather_than_a_prefix_rescue(self) -> None:
        # Opus 5 happens to be priced the same as Opus 4.x, so comparing the
        # returned dicts proves nothing -- assert the table entry exists.
        # "claude-opus-5".startswith("claude-opus-4") is False, so without its
        # own row lookup() returns None and the session costs $0.00.
        self.assertIn("claude-opus-5", MODEL_PRICING)


class PromptCachePricingTests(unittest.TestCase):
    # Cached tokens are discounted but never free. Before these buckets were
    # priced, a cached session's cost was understated by roughly 11x on real
    # local history, because a long session replays its whole context each turn.

    def test_cache_read_is_a_tenth_of_input(self) -> None:
        self.assertAlmostEqual(
            estimate_cost("claude-sonnet-5", 0, 0, cache_read=1_000_000),
            3.00 * CACHE_READ_MULTIPLIER,
            places=9,
        )

    def test_cache_write_5m_is_a_premium_over_input(self) -> None:
        self.assertAlmostEqual(
            estimate_cost("claude-sonnet-5", 0, 0, cache_write_5m=1_000_000),
            3.00 * CACHE_WRITE_5M_MULTIPLIER,
            places=9,
        )

    def test_cache_write_1h_costs_more_than_5m(self) -> None:
        self.assertAlmostEqual(
            estimate_cost("claude-sonnet-5", 0, 0, cache_write_1h=1_000_000),
            3.00 * CACHE_WRITE_1H_MULTIPLIER,
            places=9,
        )
        self.assertGreater(
            estimate_cost("claude-sonnet-5", 0, 0, cache_write_1h=1_000_000),
            estimate_cost("claude-sonnet-5", 0, 0, cache_write_5m=1_000_000),
        )

    def test_cache_arguments_default_to_zero(self) -> None:
        # Callers that predate the cache buckets must keep their old result,
        # so adding these parameters cannot silently change an existing total.
        self.assertEqual(
            estimate_cost("claude-sonnet-5", 1_000, 500),
            estimate_cost("claude-sonnet-5", 1_000, 500, cache_write_5m=0, cache_write_1h=0, cache_read=0),
        )

    def test_subscription_model_stays_free_even_with_cache_tokens(self) -> None:
        self.assertEqual(
            estimate_cost("gpt-5.2-codex", 1_000_000, 1_000_000, cache_read=1_000_000),
            0.0,
        )

    def test_real_world_turn_is_dominated_by_cache_not_input(self) -> None:
        # Verbatim usage shape from a real local log: two uncached input tokens
        # beside ~47k cached ones. Counting input_tokens alone prices this turn
        # at well under a cent; the true cost is ~18x that.
        counted_only = estimate_cost("claude-sonnet-5", 2, 339)
        with_cache = estimate_cost(
            "claude-sonnet-5", 2, 339, cache_write_1h=13_099, cache_read=33_775
        )
        self.assertAlmostEqual(with_cache, 0.0938175, places=6)
        self.assertGreater(with_cache, counted_only * 15)


if __name__ == "__main__":
    unittest.main()
