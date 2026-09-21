"""A payout figure is printed in the currency the money left the account in.

A claim is converted once - at the rate stamped on it when it was audited, so
the report and the payment run cannot drift - and every payment against it is
recorded in the organisation's own currency. The figures that come out of
`payableClaims` are therefore payout figures, and formatting one with the
*receipt's* currency prints a real rupee amount behind a dollar sign.

That is what the Settled list did: a USD 20.00 Cursor invoice, settled for
INR 1,913.49, displayed as "$1,913.49". Not the claimed amount, not the paid
amount, and wrong by the exchange rate - the one class of display bug on this
screen that a finance team could act on before noticing.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def console() -> str:
    with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as fh:
        return fh.read()


class TheSettledListPrintsWhatWasPaid(unittest.TestCase):

    def setUp(self):
        self.app = console()
        self.fn = self.app.split("function renderSettled(", 1)[1].split("\nfunction ", 1)[0]

    def test_the_row_takes_the_payout_currency(self):
        self.assertIn("const ccy = c.payCcy || orgCurrency();", self.fn)
        self.assertNotIn("const ccy = c.res.currency;", self.fn)

    def test_and_says_what_the_bill_itself_said(self):
        # A rupee figure against a dollar invoice reads as a misread receipt
        # unless the conversion is shown beside it.
        self.assertIn("c.from && c.from !== ccy", self.fn)
        self.assertIn("fmtPlain(c.original, c.from)", self.fn)


class MyExpensesPrintsWhatWasPaid(unittest.TestCase):

    def setUp(self):
        self.app = console()
        self.fn = self.app.split("function renderMine(", 1)[1].split("\nfunction ", 1)[0]

    def test_the_settled_column_is_in_the_organisation_s_currency(self):
        self.assertIn("fmt(paid, orgCurrency())", self.fn)

    def test_but_the_claimed_column_is_in_the_receipt_s(self):
        # What the bill came to is a fact about the bill, and it is printed in
        # the currency the bill printed - now through `claimedCell`, which
        # adds the converted figure beneath it.
        self.assertIn("claimedCell(sub, res)", self.fn)
        cell = self.app.split("function claimedCell(sub, res) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("const billed = esc(fmt(res.receiptTotal, res.currency));",
                      cell)

    def test_and_says_what_that_comes_to_here(self):
        # Claimed was in the receipt's currency and Settled in the
        # organisation's, so a dollar invoice put $100.00 in one column and
        # INR 8,819.23 in the next with nothing on the row to say they were
        # the same money.
        cell = self.app.split("function claimedCell(sub, res) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn('<span class="inhome">', cell)
        self.assertIn("res.receiptTotal * rate", cell)

    def test_a_rupee_claim_keeps_one_line(self):
        # A second line saying the identical thing is noise on every row of a
        # rupee-only account.
        cell = self.app.split("function claimedCell(sub, res) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("if (!ccy || ccy === home) return billed;", cell)


class ThePendingListAlreadyHadThisRight(unittest.TestCase):
    """Kept as a test because it is the pattern the other two now follow."""

    def test_the_settlement_form_and_the_payable_list_use_payCcy(self):
        app = console()
        self.assertEqual(2, len(re.findall(r"const ccy = c\.payCcy;", app)))


if __name__ == "__main__":
    unittest.main()
