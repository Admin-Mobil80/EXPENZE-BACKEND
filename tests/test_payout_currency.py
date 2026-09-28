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


def strip_comments(js: str) -> str:
    """Code only. A comment explaining a rule is not a breach of it."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(^|\s)//[^\n]*", " ", js)


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
        self.assertIn('<span class="amtsub">', cell)
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


class TheNoticeAndTheLogCountInTheSameCurrency(unittest.TestCase):
    """"Paid: USD 1,920.43" on a claim for $20.00.

    Mobil80-Exp-16: a $20.00 Cursor invoice, reimbursed by bank transfer for
    INR 1,920.43. The settlement notice told Rehaan he had been paid USD
    1,920.43 and the audit log recorded $1,920.43 - a rupee figure wearing a
    dollar sign, ninety-six times too large, in the one message a person
    checks against their bank statement.

    Both came from one line. `tellClaimant` posted `claim.res.currency`, which
    is the currency the bill was written in, while every figure beside it -
    approved, paid, outstanding - is a payout figure that `payableClaims` has
    already converted to the currency the money leaves in. The Settled list
    and the payment form were fixed to read `payCcy`; this call was missed,
    and it is the one that writes to a person and to the log.
    """

    def setUp(self):
        self.app = console()
        self.fn = self.app.split("async function tellClaimant(", 1)[1].split(
            "\n}", 1)[0]

    def test_the_settlement_post_sends_the_payout_currency(self):
        self.assertIn("currency: claim.payCcy || orgCurrency(),", self.fn)
        self.assertNotIn("currency: claim.res.currency", self.fn)

    def test_and_the_figures_beside_it_are_payout_figures(self):
        # The point of the pairing: these three are minor units of the payout
        # currency, so the code beside them has to name that currency.
        for field in ("approved: (claim.approved / 100).toFixed(2)",
                      "paid: ((detail.paid ?? 0) / 100).toFixed(2)",
                      "outstanding: ((detail.outstanding ?? 0) / 100).toFixed(2)"):
            self.assertIn(field, self.fn)

    def test_nothing_else_still_reaches_for_the_receipt_s_currency(self):
        # `res.currency` is right where a receipt is being shown as written.
        # It is wrong anywhere a payout is being described, and this is the
        # list of places that describe one.
        #
        # Comments stripped first: the code that gets this right says so in
        # prose beside itself ("Not `res.currency`, which is what the receipt
        # was in"), and a test that reads the explanation as the thing it
        # warns against fails on the fix.
        for caller in ("async function tellClaimant(",
                       "function settleForm(",
                       "function renderSettleOnClaim("):
            block = self.app.split(caller, 1)[1].split("\n}", 1)[0]
            self.assertNotIn("res.currency", strip_comments(block), caller)

    def test_the_server_writes_the_log_from_what_it_was_sent(self):
        # So the fix has to be at the source. Nothing downstream re-derives
        # the currency, and nothing should: the console did the conversion.
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            auth = h.read()
        outcome = auth.split("def _claim_outcome(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"currency": str(body.get("currency", ""))[:3].upper()', outcome)


if __name__ == "__main__":
    unittest.main()


class TheSettlementLogHadTheSameBugOneLayerDown(unittest.TestCase):
    """The log printed rupee figures under a dollar sign.

    Anthropic at "$14,778.17" when the bill was $153.88 and the transfer was
    ₹14,778.17 - neither number, and out by a factor of ninety. doola at
    "$57,332.19". Every row whose receipt was written in something other than
    the organisation's currency.

    It was not the renderer. `toClaim` rebuilds a settlement record from the
    claim row for anything read back off the server, and it paired `s.paid` -
    what left the bank - with `s.currency` - what the bill was written in.
    Those agree only on a rupee receipt, which is why most rows looked right.

    There is nothing to put in that field instead. Every payout leaves in the
    organisation's own currency: `payable` hard-codes it, the settle form
    posts it, and the record written for a payment made in this session takes
    `claim.payCcy`. So the field goes and the one line that reads it falls
    back to the organisation's.
    """

    def setUp(self):
        self.app = console()

    def test_the_rebuilt_record_no_longer_carries_the_receipt_s_currency(self):
        record = self.app.split("settlement: s.outcome === \"settled\" && s.paid", 1)[1] \
                         .split(": null,", 1)[0]
        self.assertIn('ccy: ""', record)
        self.assertNotIn("ccy: s.currency", record)

    def test_the_log_falls_back_to_the_organisation_s(self):
        self.assertIn("fmt(e.amount, e.ccy || orgCurrency())", self.app)

    def test_the_record_written_in_this_session_was_already_right(self):
        # It takes the payout currency off the claim, and is the shape the
        # rebuilt one should have had.
        fn = self.app.split("async function pay(claim, minor, label, detail) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertIn("ccy: claim.payCcy || orgCurrency(),", fn)

    def test_only_one_line_reads_a_settlement_s_currency(self):
        # A second reader is a second chance to pair an amount with the wrong
        # symbol, and the first one went unnoticed for months.
        #
        # Comments stripped: the line that gets this right names the field in
        # the prose beside it, and counting that as a reader fails on the fix.
        self.assertEqual(1, strip_comments(self.app).count("e.ccy"))
