"""An invoice and its receipt are one claim, not two.

A vendor sends both; the employee forwards the lot; every attachment becomes a
submission. So one Claude subscription charge produced two claims - one
approved, one flagged `possible_duplicate` and put in front of a person to
confirm what the paper already proved - and cost two credits.

`possible_duplicate` is the right answer in general: two people can buy the
same coffee at the same shop for the same price on the same morning, and only
a human can tell that from one bill claimed twice. It is the wrong answer when
both arrived **in one email**, from one sender, carrying one invoice number.
There is no judgment left, so nobody is asked to make one.

The companion is not rejected - nothing about it is wrong, and the document is
evidence somebody may want. It is simply not a second thing to decide or to
pay, and the credit it cost goes back.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def src(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class ArrivingTogetherIsWhatSettlesIt(unittest.TestCase):

    def setUp(self):
        self.worker = src("lambda_src", "auditor_worker.py")

    def test_both_the_delivery_and_the_invoice_number_must_agree(self):
        # Arriving together is necessary and nowhere near sufficient. Ten
        # receipts in one email is ordinary, and two of them can honestly be
        # the same amount at the same shop on the same day.
        self.assertIn('source_ref = str(row.get("source_ref", "") or "")', self.worker)
        self.assertIn("same_message = bool(source_ref) and bool(this_invoice) and held", self.worker)
        self.assertIn("_invoice_of(held) == this_invoice", self.worker)

    def test_a_bill_with_no_invoice_number_is_never_absorbed(self):
        # It proves nothing about another bill that also has none.
        fn = self.worker.split("def taxlike(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("empty never equals empty", fn)
        self.assertIn("bool(this_invoice)", self.worker)

    def test_the_number_is_compared_the_way_a_person_would(self):
        # "4000 - 438350" and "4000-438350" are one number.
        fn = self.worker.split("def taxlike(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("ch.isalnum()", fn)
        self.assertIn(".upper()", fn)

    def test_asking_which_fingerprint_matched_would_answer_the_weak_one(self):
        # `submitter_key` is tried first and is a guess: two colleagues each
        # holding a seat of the same product match it. So the documents are
        # compared directly rather than the keys.
        self.assertNotIn("matched_on", self.worker)

    def test_two_claims_with_no_source_are_not_companions(self):
        # Empty must never compare equal to empty, or every WhatsApp claim
        # becomes a companion of the first one that matches it.
        fn = self.worker.split("def _source_ref_of(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("must never", fn)
        self.assertIn("bool(source_ref) and", self.worker)

    def test_a_match_from_a_different_delivery_is_still_a_question(self):
        # The same bill sent twice on different days, or by two people, is
        # exactly what possible_duplicate is for.
        self.assertIn("elif held:", self.worker)
        self.assertIn('"code": "possible_duplicate"', self.worker)


class TheCompanionCostsNothingAndSaysNothing(unittest.TestCase):

    def setUp(self):
        self.worker = src("lambda_src", "auditor_worker.py")

    def test_the_credit_goes_back(self):
        self.assertIn("_refund_one_credit(org_id, submission_id, companion_of)",
                      self.worker)
        fn = self.worker.split("def _refund_one_credit(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("credits = if_not_exists(credits, :z) + :one", fn)

    def test_it_is_refunded_once_not_once_per_audit(self):
        # A claim can be read again - a reviewer corrects its type, a fault is
        # fixed and it is re-driven - and a refund that fires on every pass
        # mints credits out of a retry.
        self.assertIn('if not str(row.get("companion_of") or ""):', self.worker)

    def test_the_worker_may_actually_write_the_refund(self):
        # It had read-only on the organisations table, so every refund was
        # denied - quietly, as designed, but denied.
        stack = src("expensifyai", "stack.py")
        self.assertIn("orgs_table.grant_read_write_data(auditor_worker)", stack)

    def test_refunding_can_never_fail_the_audit(self):
        fn = self.worker.split("def _refund_one_credit(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("except Exception:", fn)

    def test_the_sender_is_told_once_not_twice(self):
        # One purchase, one outcome message. The original's already went.
        self.assertIn('if verdict.get("companion_of"):', self.worker)
        block = self.worker.split('if verdict.get("companion_of"):', 1)[1].split("\n\n", 1)[0]
        self.assertIn("already sent", block)

    def test_it_is_written_onto_the_claim(self):
        self.assertIn("companion_of = :co", self.worker)
        view = src("lambda_src", "auth.py").split("def _submission_view(", 1)[1] \
                                           .split("\ndef ", 1)[0]
        self.assertIn('"companion_of"', view)


class ItIsNotAThingToDecideOrToPay(unittest.TestCase):

    def setUp(self):
        self.app = src("..", "PORTAL", "app.html")

    def test_the_console_reads_it_back(self):
        self.assertIn("companionOf: s.companion_of", self.app)
        self.assertIn("const isCompanion = (sub) => !!sub.companionOf;", self.app)

    def test_it_is_out_of_the_review_queue(self):
        queued = self.app.split("const isQueued = (sub) => {", 1)[1].split("\n};", 1)[0]
        self.assertIn("if (isCompanion(sub)) return false;", queued)

    def test_and_out_of_the_payment_run(self):
        payable = self.app.split("function payableClaims(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("if (isCompanion(sub)) return;", payable)

    def test_but_not_hidden_and_not_rejected(self):
        # The document is evidence and its reference still resolves. Opening it
        # explains itself rather than showing an approved-looking claim with no
        # controls and no reason.
        self.assertIn("second_document", self.app)
        self.assertIn("the credit for this one was returned", self.app)


class FinanceKeepsBothDocuments(unittest.TestCase):
    """Suppressing the companion as a claim must not suppress it as evidence.

    An invoice and a receipt are both wanted in the record: the invoice is what
    tax is reclaimed against, the receipt is proof the money actually left. A
    claim that shows only one of them leaves finance holding half the paperwork
    for a bill it has paid.
    """

    def setUp(self):
        self.app = src("..", "PORTAL", "app.html")

    def test_the_claim_knows_its_other_documents(self):
        fn = self.app.split("function documentsFor(sub) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("s.companionOf === ref", fn)
        self.assertIn("[sub, ...mine]", fn)

    def test_they_are_named_by_their_filename(self):
        # "the invoice" and "the receipt" are the names on the paper; Document 1
        # and Document 2 are not what anybody is looking for.
        self.assertIn("receiptName: s.receipt_name", self.app)
        view = src("lambda_src", "auth.py").split("def _submission_view(", 1)[1] \
                                           .split("\ndef ", 1)[0]
        self.assertIn('"receipt_name"', view)

    def test_the_strip_is_hidden_when_there_is_only_one(self):
        # A switcher with a single option is furniture.
        fn = self.app.split("function paintDocStrip(sub) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("strip.hidden = docs.length < 2;", fn)

    def test_opening_another_claim_does_not_inherit_the_last_one_s_document(self):
        # The worst way this could be wrong: somebody else's receipt shown
        # under this claim's figures.
        self.assertIn("if (selectedId !== sub.id) openDoc = null;", self.app)

    def test_and_the_submitter_sees_one_row_per_purchase(self):
        # The companion is the same money on the same day from the same
        # vendor; listing it twice reads as having been charged twice.
        self.assertIn("SUBMISSIONS.filter(s => isMine(s) && !isCompanion(s))", self.app)
        # And the row still says there were two, on the line that identifies
        # it, so "one row" is not mistaken for "one document".
        self.assertIn("if (docs > 1) bits.push(`${docs} documents`);", self.app)


if __name__ == "__main__":
    unittest.main()
