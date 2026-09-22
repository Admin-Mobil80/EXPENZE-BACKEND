"""When the bill says what the card was charged, that is what we pay.

Mobil80-Exp-16 is a $20.00 Cursor invoice. Printed on it, under Payment
history: "Charged 1,995.62 INR using 1 USD = 99.7812 INR (includes 4%
conversion fee)". It was reimbursed at the market rate for that day, 96.0216,
which came to 1,920.43 - leaving Manoj 75.19 short on a bill that stated in
print exactly what he had paid.

No looked-up rate reproduces 99.7812, because no public rate includes somebody
else's card issuer's margin. Paying at the market rate is not a rounding
difference; it is deciding that the employee carries the conversion fee for
spending the company's money, silently, on every foreign receipt.

So the printed line wins. It is not a fresher rate - it is not a rate at all in
the sense a table means. It is the transaction.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from decimal import Decimal

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lambda_src"))
for _k, _v in (("AWS_DEFAULT_REGION", "ap-southeast-1"),
               ("EXPENSES_TABLE", "e"), ("ORGS_TABLE", "o"),
               ("INTAKE_TABLE", "t"), ("USERS_TABLE", "u")):
    os.environ.setdefault(_k, _v)

import fx  # noqa: E402


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


# The bill, as it actually reads.
EXP_16 = {"currency": "USD", "stated_total": "20.00",
          "charged_total": "1995.62", "charged_currency": "INR",
          "charged_rate": "99.7812"}
VERDICT = {"currency": "USD", "reimbursable_total": "20.00"}


class ThePrintedRateIsPreferred(unittest.TestCase):

    def test_the_payout_is_what_the_bill_says_was_charged(self):
        out = fx.for_payout(VERDICT, "INR", EXP_16)
        self.assertEqual("1995.62", out["amount"])
        self.assertEqual("INR", out["currency"])

    def test_and_it_says_so_rather_than_passing_as_a_looked_up_rate(self):
        # A reader who knows today's rate would otherwise see a figure four per
        # cent out and reasonably conclude the conversion was wrong.
        out = fx.for_payout(VERDICT, "INR", EXP_16)
        self.assertEqual("receipt", out["source"])
        self.assertEqual("as printed on the bill", out["as_of"])

    def test_an_ordinary_bill_is_still_converted_at_the_market(self):
        out = fx.for_payout(VERDICT, "INR", {"currency": "USD",
                                             "stated_total": "20.00"})
        # Either a market conversion or nothing, depending on whether rates
        # were reachable in this environment - but never the receipt branch.
        self.assertNotEqual("receipt", out.get("source"))

    def test_the_rate_is_what_is_stamped_so_a_part_approval_still_works(self):
        # The design this slots into: what is fixed is the rate, not the
        # amount, because what is owed is not settled when a claim is audited.
        # A reviewer approving half of this claim pays half at the bill's rate.
        out = fx.for_payout(VERDICT, "INR", EXP_16)
        half = (Decimal("10.00") * Decimal(out["rate"])).quantize(Decimal("0.01"))
        self.assertEqual(Decimal("997.81"), half)


class WhatCountsAsTheBillSayingIt(unittest.TestCase):

    def test_a_printed_rate_is_taken_as_printed(self):
        self.assertEqual(Decimal("99.7812"), fx.printed_rate(EXP_16, "USD", "INR"))

    def test_a_charge_without_a_rate_is_divided_out(self):
        # Common: the bill names what was taken and not the rate it used.
        rate = fx.printed_rate({**EXP_16, "charged_rate": ""}, "USD", "INR")
        self.assertEqual(Decimal("1995.62") / Decimal("20.00"), rate)

    def test_the_printed_rate_beats_the_division(self):
        # It is the figure the bank quoted, at full precision; the division
        # inherits the rounding of both amounts.
        self.assertNotEqual(fx.printed_rate(EXP_16, "USD", "INR"),
                            fx.printed_rate({**EXP_16, "charged_rate": ""},
                                            "USD", "INR"))

    def test_a_rate_of_zero_is_a_misread_not_a_discount(self):
        # Falls through to the division rather than paying nothing.
        self.assertEqual(Decimal("1995.62") / Decimal("20.00"),
                         fx.printed_rate({**EXP_16, "charged_rate": "0"},
                                         "USD", "INR"))

    def test_a_bill_in_one_currency_states_nothing(self):
        self.assertIsNone(fx.printed_rate({"currency": "USD",
                                           "stated_total": "20.00"}, "USD", "INR"))

    def test_a_charge_in_some_third_currency_is_not_this_conversion(self):
        self.assertIsNone(fx.printed_rate({**EXP_16, "charged_currency": "GBP"},
                                          "USD", "INR"))

    def test_nor_is_one_against_a_price_in_a_different_currency(self):
        # Otherwise it is two unrelated numbers being divided by each other.
        self.assertIsNone(fx.printed_rate({**EXP_16, "currency": "EUR"},
                                          "USD", "INR"))

    def test_junk_is_not_a_rate(self):
        for bad in ("", None, "n/a", "-", "1995.62.1"):
            self.assertIsNone(
                fx.printed_rate({**EXP_16, "charged_rate": bad,
                                 "charged_total": bad}, "USD", "INR"), repr(bad))

    def test_a_missing_receipt_does_not_raise(self):
        for junk in (None, "", [], 7):
            self.assertIsNone(fx.printed_rate(junk, "USD", "INR"), repr(junk))


class TheModelIsAskedForIt(unittest.TestCase):

    def setUp(self):
        self.handler = read("lambda_src/handler.py")

    def test_the_three_parts_are_in_the_schema(self):
        for field in ("charged_total", "charged_currency", "charged_rate"):
            self.assertIn(f'"{field}"', self.handler)

    def test_they_are_required_so_absence_is_stated_rather_than_omitted(self):
        required = self.handler.split('"stated_total",', 1)[1].split("]", 1)[0]
        for field in ("charged_total", "charged_currency", "charged_rate"):
            self.assertIn(field, required)

    def test_they_reach_the_payout_and_not_the_policy_engine(self):
        """The test that used to live here asserted the bug.

        It pinned `charged_total=args.get("charged_total")` as a literal, and
        that line was a keyword argument to `evaluate_policy`, which does not
        take one. Every audit raised TypeError, three times, and parked the
        receipt at needs_human - Mobil80-Exp-63, a handwritten bill for 190.00,
        is the one that hit it. The suite stayed green throughout, because the
        assertion was that the broken line was present.

        A string being in a file says nothing about whether it runs. So this
        calls the function instead, with the shape that crashed.
        """
        import handler, policy
        args = {"line_items": [{"description": "Laptop parts", "amount": "190.00"}],
                "expense_type": "meals", "stated_total": "190.00",
                "charged_total": "1995.62", "charged_currency": "INR",
                "charged_rate": "99.7812"}
        verdict = handler._run_policy(args, "INR", policy.DEFAULT_RULES)
        self.assertEqual("190.00", verdict["receipt_total"])

        # The conversion belongs to the payout, which is handed the receipt.
        self.assertIn('fx.for_payout(verdict, org_default, receipt)', self.handler)
        run = self.handler.split("def _run_policy(", 1)[1].split("\ndef ", 1)[0]
        for field in ("charged_total=", "charged_currency=", "charged_rate="):
            self.assertNotIn(field, run)

    def test_the_engine_signature_is_the_one_being_called(self):
        # The class of mistake, checked as a class: every keyword `_run_policy`
        # passes has to be one `evaluate_policy` accepts.
        import inspect, policy, handler
        accepted = set(inspect.signature(policy.evaluate_policy).parameters)
        run = self.handler.split("def _run_policy(", 1)[1].split("\ndef ", 1)[0]
        body = run.split("policy.evaluate_policy(", 1)[1]
        passed = set(re.findall(r"^\s*(\w+)=", body, re.M))
        self.assertEqual(set(), passed - accepted,
                         "passing a keyword the policy engine does not take")

    def test_the_model_is_told_not_to_invent_one(self):
        # The single thing this field is for is being the figure nobody had to
        # guess. A computed one is worse than none.
        self.assertIn("Never compute a charged amount from a rate you ", self.handler)
        self.assertIn("Copy it, do not derive it.", self.handler)

    def test_and_that_one_currency_is_the_normal_case(self):
        # Otherwise the model reaches for something to put in the field.
        self.assertIn("which is the normal case", self.handler)


class TheConsoleSaysWhereTheRateCameFrom(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_a_bill_rate_is_named_as_one(self):
        self.assertIn('const fromBill = pv.source === "receipt";', self.app)
        self.assertIn("as charged on the bill", self.app)

    def test_and_carries_no_date(self):
        # "as printed on the bill" is the whole provenance; a date beside it
        # would suggest a rate that was current on one day and not another.
        line = self.app.split("const fromBill =", 1)[1].split("payRow.title", 1)[0]
        self.assertIn('fromBill ? " · as charged on the bill"', line)
        self.assertIn("rateDay(pv.as_of)", line)


if __name__ == "__main__":
    unittest.main()
