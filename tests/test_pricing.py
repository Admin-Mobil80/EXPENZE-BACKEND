"""One definition of what credits cost.

This module exists because there were two. The back office merged the stored
pricing against a seeded default before displaying it; the customer console
read the same row raw. BMS therefore showed a full INR column while every slab
in the customer's Credits tab said "no INR price set" - and nothing looked
wrong from either screen, because each was internally consistent.

So these tests are less about arithmetic than about the two readers now
agreeing, and about a currency added later never silently stopping sales.
"""
from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

import pricing  # noqa: E402


class Seed(unittest.TestCase):
    def test_every_slab_is_priced_in_every_currency(self):
        # A slab missing a currency cannot be sold in that market at all.
        for slab in pricing.DEFAULT_PRICING:
            for ccy in pricing.CURRENCIES:
                self.assertIn(ccy, slab, slab["credits"])
                self.assertGreater(slab[ccy], 0, f"{slab['credits']} {ccy}")

    def test_bigger_slabs_cost_less_per_receipt(self):
        rates = [s["usd"] / s["credits"] for s in pricing.DEFAULT_PRICING]
        self.assertEqual(rates, sorted(rates, reverse=True))


class Merge(unittest.TestCase):
    def test_nothing_stored_gives_the_seed(self):
        self.assertEqual(len(pricing.merge(None)), len(pricing.DEFAULT_PRICING))
        self.assertEqual(len(pricing.merge([])), len(pricing.DEFAULT_PRICING))

    def test_stored_prices_win(self):
        got = pricing.merge([{"credits": 500, "usd": 60, "inr": 5000}])
        self.assertEqual(got[0]["usd"], 60)
        self.assertEqual(got[0]["inr"], 5000)

    def test_a_currency_saved_before_it_existed_falls_back_per_slab(self):
        # The actual bug: a row saved when only USD existed. The USD price the
        # customer set must survive, and INR must not come back empty.
        got = pricing.merge([{"credits": 500, "usd": 60}])
        self.assertEqual(got[0]["usd"], 60, "the stored price must survive")
        self.assertEqual(got[0]["inr"], 4200, "the missing one falls back to the seed")

    def test_the_fallback_is_per_slab_not_per_table(self):
        got = pricing.merge([{"credits": 500, "usd": 60},
                             {"credits": 1000, "usd": 95, "inr": 9000}])
        self.assertEqual(got[0]["inr"], 4200)   # seeded
        self.assertEqual(got[1]["inr"], 9000)   # stored

    def test_dynamodb_decimals_come_back_as_numbers(self):
        got = pricing.merge([{"credits": Decimal(500), "usd": Decimal(50),
                              "inr": Decimal(4200)}])
        self.assertEqual(got[0], {"credits": 500, "usd": 50, "inr": 4200})
        for value in got[0].values():
            self.assertNotIsInstance(value, Decimal)

    def test_slabs_come_back_smallest_first(self):
        got = pricing.merge([{"credits": 5000, "usd": 380, "inr": 31900},
                             {"credits": 500, "usd": 50, "inr": 4200}])
        self.assertEqual([s["credits"] for s in got], [500, 5000])

    def test_a_row_with_no_slab_size_is_dropped(self):
        got = pricing.merge([{"usd": 50}, {"credits": 500, "usd": 50, "inr": 4200}])
        self.assertEqual(len(got), 1)

    def test_an_unknown_slab_keeps_what_it_has(self):
        # A size that is not in the seed has nothing to fall back to, so the
        # missing currency stays missing and payments.price_of refuses it -
        # which is the honest outcome rather than an invented price.
        got = pricing.merge([{"credits": 750, "usd": 70}])
        self.assertEqual(got[0]["usd"], 70)
        self.assertIsNone(got[0]["inr"])


class Gst(unittest.TestCase):
    def test_unset_means_the_default(self):
        self.assertEqual(pricing.gst_percent(None), 18)

    def test_zero_is_a_real_choice_not_an_absence(self):
        # Someone turning GST off must not silently get 18% back.
        self.assertEqual(pricing.gst_percent(0), 0)
        self.assertEqual(pricing.gst_percent(Decimal(0)), 0)

    def test_a_stored_rate_is_used(self):
        self.assertEqual(pricing.gst_percent(Decimal("5.5")), 5.5)

    def test_nonsense_falls_back_rather_than_breaking_checkout(self):
        self.assertEqual(pricing.gst_percent("eighteen"), 18)


class BothReadersAgree(unittest.TestCase):
    """The regression that started this: one row, two readers, two answers."""

    STORED = [{"credits": 500, "usd": Decimal(50)},
              {"credits": 1000, "usd": Decimal(90)}]

    def test_the_console_and_the_back_office_see_the_same_prices(self):
        # Both now go through pricing.merge, so there is one answer by
        # construction rather than by two functions happening to match.
        console = pricing.merge(self.STORED)
        back_office = pricing.merge(self.STORED)
        self.assertEqual(console, back_office)
        for slab in console:
            self.assertIsNotNone(slab["inr"], "every slab must be sellable in India")


if __name__ == "__main__":
    unittest.main()
