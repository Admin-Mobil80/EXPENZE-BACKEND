"""Spending in one currency against a budget set in another.

A budget is one number in one currency. Spend is not: somebody buys lunch in
rupees, a conference ticket in dollars and a hotel in dirhams out of the same
monthly allowance. The console used to drop every claim whose currency did not
match the budget's and print a count of them in a footnote, so a team could
pass its limit entirely in dollars and read as comfortably inside it.

Converting is easy to get subtly wrong, and each of these is a way it would be:

* counting an unconvertible claim as zero, which under-reports spend - the one
  direction a budget must never fail in;
* converting at read time, so a figure from last month moves whenever the
  market does and two people disagree about the same budget;
* letting a rate anywhere near what somebody is actually paid.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")

ROOT = os.path.join(os.path.dirname(__file__), "..")

import fx  # noqa: E402


TABLE = {"base": "USD", "fetched_at": 4_000_000_000,
         "rates": {"USD": "1", "INR": "88.5", "EUR": "0.92", "AED": "3.6725"},
         "as_of": "Mon, 14 Sep 2026 00:00:01 +0000"}


class Converting(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(fx, "rates", return_value=TABLE)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_the_same_currency_needs_no_rate(self):
        out = fx.convert("100.00", "INR", "INR")
        self.assertEqual("100.00", out["amount"])
        self.assertEqual("1", out["rate"])

    def test_a_dollar_bill_counts_as_rupees(self):
        out = fx.convert("100", "USD", "INR")
        self.assertEqual("8850.00", out["amount"])

    def test_it_crosses_through_the_base_both_ways(self):
        # A pair neither of which is the base. The two directions have to be
        # each other's inverse, or the same claim counts differently
        # depending on which way somebody happened to ask.
        there = Decimal(fx.convert("100", "EUR", "AED")["amount"])
        back = Decimal(fx.convert(str(there), "AED", "EUR")["amount"])
        self.assertLess(abs(back - Decimal("100")), Decimal("0.01"))

    def test_the_rate_that_did_it_travels_with_the_figure(self):
        out = fx.convert("10", "USD", "INR")
        self.assertEqual("88.50000000", out["rate"])
        self.assertIn("2026", out["as_of"])

    def test_an_unknown_currency_converts_to_nothing_not_to_zero(self):
        # Zero would be counted as spend of nothing, which silently shrinks
        # what a budget is measured against.
        self.assertIsNone(fx.convert("100", "USD", "ZWL"))
        self.assertIsNone(fx.convert("100", "XXX", "INR"))

    def test_an_unreadable_amount_converts_to_nothing(self):
        self.assertIsNone(fx.convert("not a number", "USD", "INR"))

    def test_precision_survives_a_large_amount(self):
        # Rounding the rate first and multiplying by it puts a visible error
        # on a big invoice.
        out = fx.convert("125000", "USD", "INR")
        self.assertEqual("11062500.00", out["amount"])


class WhenTheRatesCannotBeHad(unittest.TestCase):
    def setUp(self):
        fx._memo = None

    def tearDown(self):
        fx._memo = None

    def test_a_stale_table_still_answers(self):
        # A day-old rate answers "roughly how much of the budget has this
        # eaten" well enough to raise a hand. Refusing to convert would drop
        # the claim out of the arithmetic entirely, which is the worse error.
        old = dict(TABLE, fetched_at=1)
        with mock.patch.object(fx, "_stored", return_value=old), \
             mock.patch.object(fx, "_fetch", return_value=None), \
             mock.patch.object(fx, "_store"):
            self.assertEqual(old, fx.rates())

    def test_nothing_at_all_is_an_empty_table_not_an_exception(self):
        # This is called from the read path of a page. It must never be the
        # reason somebody cannot open their expenses.
        with mock.patch.object(fx, "_stored", return_value=None), \
             mock.patch.object(fx, "_fetch", return_value=None), \
             mock.patch.object(fx, "_store"):
            self.assertEqual({}, fx.rates().get("rates"))
            self.assertIsNone(fx.convert("100", "USD", "INR"))

    def test_a_source_that_answers_with_junk_is_skipped(self):
        calls = []

        def fake(request, timeout=0):
            calls.append(request.full_url)
            body = ({"rates": {"INR": 88.5}} if len(calls) == 1
                    else {"rates": {c: 1 for c in ("USD", "INR", "EUR", "AED", "GBP")}})

            class R:
                def read(self_inner):
                    return json.dumps(body).encode()

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False
            return R()

        with mock.patch.object(fx.urllib.request, "urlopen", fake):
            table = fx._fetch()
        self.assertEqual(2, len(calls), "the second source was not tried")
        self.assertIn("GBP", table["rates"])


class ItNeverTouchesWhatSomebodyIsPaid(unittest.TestCase):
    def read(self, path):
        with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
            return handle.read()

    def test_policy_does_not_convert(self):
        # A cap is set in a currency and compared against a bill in that same
        # currency. An exchange rate deciding whether a receipt passes would
        # make the verdict depend on the day it was read.
        policy = self.read("lambda_src/policy.py")
        self.assertNotIn("import fx", policy)
        self.assertNotIn("fx.", policy)

    def test_settlement_does_not_convert(self):
        # A claim is reimbursed in the currency it was incurred in.
        auth = self.read("lambda_src/auth.py")
        outcome = auth.split("def _claim_outcome(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("fx.", outcome)

    def test_the_converted_figure_is_named_for_what_it_is(self):
        self.assertIn("budget", fx.for_budget.__doc__.lower())


class TheFigureIsFrozenWhenTheClaimIsAudited(unittest.TestCase):
    def read(self, path):
        with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
            return handle.read()

    def test_the_auditor_stamps_it_on_the_row(self):
        worker = self.read("lambda_src/auditor_worker.py")
        self.assertIn("budget_value = :bv", worker)
        self.assertIn('outcome.get("budget")', worker)

    def test_the_audit_computes_it_against_the_organisation_s_currency(self):
        handler = self.read("lambda_src/handler.py")
        self.assertIn("fx.for_budget(verdict, org_default)", handler)

    def test_a_stored_figure_is_never_recomputed(self):
        # Re-deriving it at read time would move a month-old figure every time
        # the market did.
        auth = self.read("lambda_src/auth.py")
        fn = auth.split("def _budget_value(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('stored.get("amount")', fn)
        self.assertLess(fn.index('stored.get("amount")'), fn.index("fx.convert"))

    def test_a_claim_audited_before_this_existed_still_counts(self):
        auth = self.read("lambda_src/auth.py")
        fn = auth.split("def _budget_value(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("fx.convert(", fn)
        self.assertIn('"at": "today"', fn)


class TheConsoleCountsConvertedSpend(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_a_foreign_claim_is_no_longer_dropped(self):
        lines = self.app.split("function budgetLines(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("budgetValue(s) !== null", lines)

    def test_an_unconvertible_claim_is_not_counted_as_zero(self):
        fn = self.app.split("function budgetValue(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("return null;", fn)
        # And the caller has to act on that rather than adding it.
        lines = self.app.split("function budgetLines(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("converted === null", lines)

    def test_the_person_is_told_their_claim_was_converted(self):
        # assertIn would print the whole console on a failure.
        for phrase in ("converted to ${b.ccy}", "you are reimbursed in the "):
            self.assertTrue(phrase in self.app, f"the budget note lost {phrase!r}")

    def test_what_could_not_be_converted_is_still_named(self):
        self.assertTrue("not be converted" in self.app,
                        "nothing tells the reader a claim was left out")


if __name__ == "__main__":
    unittest.main()
