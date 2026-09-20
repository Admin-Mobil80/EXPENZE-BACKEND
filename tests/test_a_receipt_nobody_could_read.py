"""A claim for INR 0.00, and nothing anywhere saying why.

A blurred photograph of a handwritten bill. The model read it honestly and
said so in its own words - "the handwritten item details are too blurred to
establish any covered expense category reliably" - and returned no line items
and no printed total.

An empty list sums to zero. So the claim arrived as a receipt for INR 0.00
whose only complaint was `no_rule_for_expense_type`, which pointed the
reviewer at the expense type dropdown when the actual problem was that there
were no figures on the claim to type anything about. And the person who sent
it was messaged:

    *D. Velusamy* — INR 0.00
    Sent to your finance team to look at.

Quoting a figure we did not read, as though we had read it, is the one thing a
receipt-reading product must never do. INR 0.00 is not a figure any receipt
carries; it is the arithmetic of an empty list, and the model had already said
it could not read the bill.

So an empty reading is named as what it is. It is listed first, because every
other finding about a claim with no figures is noise on top of it. Approve is
withheld - there is no amount to approve, and setting a type does not create
one. And it is the one blocked case worth telling the claimant about: a cap or
a duplicate is a reviewer's business, but a blurred photograph is a fact about
their own photograph and theirs to fix.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

import policy

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def codes(verdict):
    return [v["code"] for v in verdict.get("violations", [])]


def run(**kw):
    kw.setdefault("rules", policy.DEFAULT_RULES)
    kw.setdefault("currency", "INR")
    return policy.evaluate_policy(**kw)


class AnEmptyReadingIsNamed(unittest.TestCase):

    def test_no_lines_and_no_total_is_a_finding(self):
        v = run(line_items=[], expense_type="meals", stated_total="")
        self.assertIn("nothing_read", codes(v))
        self.assertEqual("needs_review", v["verdict"])

    def test_it_is_listed_first(self):
        # `no_rule_for_expense_type` is appended before this point and is what
        # a reviewer reads at the top. On a claim with no figures it is advice
        # about the wrong control.
        v = run(line_items=[], expense_type="not_covered", stated_total="")
        self.assertEqual("nothing_read", codes(v)[0])
        self.assertIn("no_rule_for_expense_type", codes(v))

    def test_a_legible_printed_total_is_enough(self):
        # The lines may be unreadable while the grand total is perfectly
        # clear - that is an ordinary claim, worth what the bill says.
        v = run(line_items=[], expense_type="meals", stated_total="1400.00")
        self.assertNotIn("nothing_read", codes(v))
        self.assertEqual("1400.00", v["receipt_total"])

    def test_a_bill_that_prints_zero_is_also_nothing_read(self):
        # `_stated` returns None for a total of zero, deliberately: a bill for
        # nothing is not a bill, and it goes to a person either way.
        v = run(line_items=[], expense_type="meals", stated_total="0.00")
        self.assertIn("nothing_read", codes(v))

    def test_one_readable_line_is_enough(self):
        v = run(line_items=[{"description": "Bill", "amount": "1200.00"}],
                expense_type="meals", stated_total="")
        self.assertNotIn("nothing_read", codes(v))

    def test_a_normal_claim_is_untouched(self):
        v = run(line_items=[{"description": "Dinner", "amount": "1200.00"}],
                expense_type="meals", stated_total="1200.00")
        self.assertEqual([], codes(v))
        self.assertEqual("approved", v["verdict"])

    def test_it_blocks_the_agent_from_clearing_it(self):
        v = run(line_items=[], expense_type="meals", stated_total="")
        finding = [x for x in v["violations"] if x["code"] == "nothing_read"][0]
        self.assertTrue(finding["blocks_automatic_decision"])

    def test_the_message_says_what_to_do_about_it(self):
        v = run(line_items=[], expense_type="meals", stated_total="")
        msg = [x for x in v["violations"] if x["code"] == "nothing_read"][0]["message"]
        self.assertIn("blurred", msg)


class TheConsolePortAgrees(unittest.TestCase):
    """Two implementations of one rule, kept in step by hand."""

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split(
            "function evaluatePolicy(currency, lineItems, expenseType, statedTotal) {",
            1)[1].split("\n}", 1)[0]

    def test_the_same_condition(self):
        self.assertIn(
            "if (receiptTotal === 0 && !decided.length && printed === null) {",
            self.fn)

    def test_first_in_the_list_there_too(self):
        self.assertIn('violations.unshift({ code:"nothing_read"', self.fn)

    def test_and_it_blocks(self):
        block = self.fn.split('code:"nothing_read"', 1)[1].split("});", 1)[0]
        self.assertIn("blocking:true", block)


class ApproveIsWithheld(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.detail = self.app.split("function renderDetail() {", 1)[1].split(
            "\nfunction ", 1)[0]

    def test_there_is_no_amount_to_approve(self):
        self.assertIn(
            'const unreadable = res.violations.some(v => v.code === "nothing_read");',
            self.detail)
        self.assertIn(
            "const choices = (!typeOk || unsaved || needsGroup || unreadable)",
            self.detail)

    def test_reject_is_still_offered(self):
        block = self.detail.split(
            "const choices = (!typeOk || unsaved || needsGroup || unreadable)",
            1)[1].split(";", 1)[0]
        self.assertEqual(2, block.count('"Rejected"'))

    def test_it_is_named_before_the_expense_type(self):
        # Setting a type on a claim with no figures does not create an amount,
        # so telling somebody to set one first is sending them the wrong way.
        note = self.detail.split("s.textContent = unsaved", 1)[1]
        self.assertLess(note.index("Nothing was read off this receipt"),
                        note.index("Set an expense type above"))

    def test_the_other_way_out_is_named(self):
        self.assertIn("ask for a clearer photograph of the same bill",
                      self.detail)


class TheSubmitterIsNotQuotedAFigureNobodyRead(unittest.TestCase):

    def setUp(self):
        self.notify = read("lambda_src/notify.py")
        self.fn = self.notify.split("def outcome_notice(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_the_branch_exists_and_omits_the_total(self):
        self.assertIn("elif nothing_read:", self.fn)
        branch = self.fn.split("elif nothing_read:", 1)[1].split("else:", 1)[0]
        self.assertNotIn("{total}", branch)

    def test_it_says_what_happened_and_what_helps(self):
        branch = self.fn.split("elif nothing_read:", 1)[1].split("else:", 1)[0]
        self.assertIn("could not read this one", branch)
        self.assertIn("clearer photo", branch)

    def test_nothing_is_demanded_of_them(self):
        # The claim still goes to a reviewer. A blurred photograph is not a
        # question the submitter has to answer before anything happens.
        branch = self.fn.split("elif nothing_read:", 1)[1].split("else:", 1)[0]
        self.assertIn("you do not need to do anything", branch)

    def test_every_other_finding_stays_the_reviewers_business(self):
        # "Your bill is 205 over the meals cap" invites a claimant to argue a
        # case to the wrong audience. Only this one is about their photograph.
        self.assertIn('str((v or {}).get("code") or "") == "nothing_read"',
                      self.fn)
        for code in ("cap_exceeded", "possible_duplicate", "group_not_set"):
            self.assertNotIn(code, self.fn)

    def test_the_findings_reach_the_notice(self):
        worker = read("lambda_src/auditor_worker.py")
        # To the closing brace of the call, not the first "})" - the dict
        # contains `{}` defaults of its own.
        send = worker.split('notify.send("outcome", member, {', 1)[1].split(
            "\n        })", 1)[0]
        self.assertIn('"violations": verdict.get("violations") or []', send)


class ADecidedClaimIsNotMessagedAgain(unittest.TestCase):
    """The guard asked the wrong question, and nearly caught me out.

    `_tell_sender` refused to send twice by checking whether an *outcome*
    notice had already gone. A claim that was rejected carries a notice of
    kind `rejected`, not `outcome` - so auditing it again would have sent
    "sent to your finance team to look at" to somebody already told their
    claim was refused: a message contradicting the last one they received,
    about a claim that is closed.

    Nothing in the product requeues a decided claim, which is why this never
    fired. It nearly fired by hand. A blurred receipt had been rejected with
    "the receipt is illegible - please send a clearer photo", and re-running
    the agent over it to pick up the new `nothing_read` finding would have
    rewritten a decided record and messaged its submitter a second time.

    A guard that holds only while nobody touches the table is not a guard.
    """

    def setUp(self):
        self.worker = read("lambda_src/auditor_worker.py")
        self.fn = self.worker.split("def _tell_sender(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_a_reviewed_claim_stops_it(self):
        self.assertIn(
            'if str(row.get("review_action") or "") or str(row.get("outcome") or ""):',
            self.fn)

    def test_the_older_guard_is_still_there(self):
        # Two different repeats: the same outcome twice, and an outcome after
        # a decision. Neither implies the other.
        self.assertIn('if str(told.get("kind") or "") == "outcome":', self.fn)

    def test_both_run_before_anything_is_sent(self):
        self.assertLess(self.fn.index('row.get("review_action")'),
                        self.fn.index("notify.send("))

    def test_an_undecided_claim_is_still_told(self):
        # The whole point of the acknowledgement is that the outcome follows.
        self.assertIn("notify.send(", self.fn)
        self.assertIn('"outcome"', self.fn)


if __name__ == "__main__":
    unittest.main()
