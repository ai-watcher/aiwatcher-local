from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from aiwatcher_cli.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    INTRO_PRICING,
    MODEL_PRICING,
    cache_read_cost,
    estimate_cost,
    is_subscription_model,
    lookup,
)


class DatedPricingTests(unittest.TestCase):
    """Spend is priced by when it happened, so a promotional rate applies to the
    history billed under it and lapses on its own date without a code change."""

    SONNET_INTRO_ENDS = INTRO_PRICING["claude-sonnet-5"]["until"]

    def test_spend_inside_the_promotion_uses_the_promotional_rate(self) -> None:
        during = self.SONNET_INTRO_ENDS - timedelta(days=30)
        self.assertEqual(
            estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000, when=during),
            2.00 + 10.00,
        )

    def test_spend_after_the_promotion_uses_the_standard_rate(self) -> None:
        after = self.SONNET_INTRO_ENDS + timedelta(days=1)
        self.assertEqual(
            estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000, when=after),
            3.00 + 15.00,
        )

    def test_the_rate_flips_on_the_boundary_without_a_code_change(self) -> None:
        # The whole point of dating the rate: nothing has to run on the day.
        last_moment = self.SONNET_INTRO_ENDS - timedelta(microseconds=1)
        first_moment = self.SONNET_INTRO_ENDS

        self.assertEqual(lookup("claude-sonnet-5", last_moment)["in"], 2.00)
        self.assertEqual(lookup("claude-sonnet-5", first_moment)["in"], 3.00)

    def test_undated_lookups_get_the_standard_rate(self) -> None:
        # A question with no date attached ("is this subscription-only?") must
        # not silently pick up a promotional rate.
        self.assertEqual(lookup("claude-sonnet-5")["in"], 3.00)
        self.assertEqual(estimate_cost("claude-sonnet-5", 1_000_000, 0), 3.00)

    def test_naive_timestamps_are_read_as_utc_rather_than_crashing(self) -> None:
        during = (self.SONNET_INTRO_ENDS - timedelta(days=30)).replace(tzinfo=None)
        self.assertEqual(lookup("claude-sonnet-5", during)["in"], 2.00)

    def test_cache_rates_follow_the_dated_input_price(self) -> None:
        # Cache costs are multiples of the base input price, so they have to move
        # with the promotion or a cached session prices its replay at a rate its
        # own input tokens were never billed at.
        during = self.SONNET_INTRO_ENDS - timedelta(days=30)
        self.assertAlmostEqual(
            cache_read_cost("claude-sonnet-5", 1_000_000, during),
            2.00 * CACHE_READ_MULTIPLIER,
            places=9,
        )
        self.assertAlmostEqual(
            estimate_cost("claude-sonnet-5", 0, 0, cache_read=1_000_000, when=during),
            2.00 * CACHE_READ_MULTIPLIER,
            places=9,
        )

    def test_a_model_with_no_promotion_is_unaffected_by_the_date(self) -> None:
        during = self.SONNET_INTRO_ENDS - timedelta(days=30)
        self.assertEqual(estimate_cost("claude-opus-5", 1_000_000, 0, when=during), 5.00)

    def test_prefix_matched_models_still_resolve_their_promotion(self) -> None:
        # lookup() falls back to prefix matching for dated model ids; the
        # promotion is keyed to the table entry, not the string it was asked for.
        during = self.SONNET_INTRO_ENDS - timedelta(days=30)
        self.assertEqual(lookup("claude-sonnet-5-20260101", during)["in"], 2.00)

    def test_every_promotion_undercuts_the_standard_rate_it_replaces(self) -> None:
        # A promotion that costs more than the standard rate is a typo, and it
        # would quietly overstate a whole window of history.
        for model, intro in INTRO_PRICING.items():
            standard = MODEL_PRICING[model]
            self.assertLess(intro["in"], standard["in"], model)
            self.assertLess(intro["out"], standard["out"], model)
            self.assertEqual(intro["until"].tzinfo, timezone.utc, model)


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
