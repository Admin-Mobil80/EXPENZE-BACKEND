"""The same receipt twice gets an answer, not silence.

Detection was never the problem. The content fingerprint catches identical
bytes before a credit is spent, discards the upload and returns
`status: "duplicate"` - all correct, and all invisible to the person who sent
it. WhatsApp logged the status and replied with nothing; email fell through to
a branch that said "no receipt attached - attach it and send it again", which
is untrue and an instruction to repeat the thing that caused it.

Somebody who photographs a bill and hears nothing concludes it did not arrive.
That is how one Gamma receipt became six claims, six credits and six
acknowledgements. Catching a duplicate silently and telling the sender to try
again are the two halves of the same loop.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def src(name: str) -> str:
    with open(os.path.join(ROOT, "lambda_src", name), encoding="utf-8") as fh:
        return fh.read()


def sentences(block: str) -> str:
    """Source with comments dropped and adjacent string literals joined.

    Assertions here are about what a person reads, and a sentence in this
    codebase is routinely split across three source lines to stay inside the
    margin. Matching the raw text makes the test fail when somebody rewraps a
    paragraph, which teaches the next reader that these tests are noise.
    """
    code = "\n".join(l for l in block.splitlines() if not l.strip().startswith("#"))
    return re.sub(r'"\s*\n\s*"', "", code)


class NoCreditIsSpentOnOne(unittest.TestCase):

    def test_the_check_runs_before_the_charge(self):
        intake = src("intake.py")
        body = intake.split("file_print = duplicates.file_key", 1)[1]
        self.assertLess(body.index('"status": "duplicate"'),
                        body.index("_charge_and_record"))


class TheSenderIsToldWhichClaimItAlreadyIs(unittest.TestCase):

    def test_the_reply_carries_the_reference(self):
        # "Already submitted" without saying *as what* leaves somebody with no
        # way to check, and the obvious next move is to send it again.
        intake = src("intake.py")
        self.assertIn('"reference": _reference_of(held)', intake)

    def test_reading_it_can_never_lose_the_answer(self):
        fn = src("intake.py").split("def _reference_of(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("except Exception:", fn)
        self.assertIn('return ""', fn)


class WhatsAppAnswersIt(unittest.TestCase):

    def setUp(self):
        self.wa = src("whatsapp.py")

    def test_there_is_a_branch_for_it_at_all(self):
        self.assertIn('elif status == "duplicate":', self.wa)

    def test_it_names_the_claim_and_settles_the_real_worry(self):
        # Which is having claimed the same thing twice - not credits, which
        # are what the organisation is billed in and a word the person who
        # photographed the bill has no use for.
        branch = self.wa.split('elif status == "duplicate":', 1)[1].split("\n    else:", 1)[0]
        code = sentences(branch)
        self.assertIn('outcome.get("reference")', code)
        self.assertIn("No second claim was made.", code)
        self.assertNotIn("credit", code)

    def test_and_says_the_first_one_is_still_coming(self):
        # Otherwise "already received" reads as "and nothing will happen".
        branch = self.wa.split('elif status == "duplicate":', 1)[1].split("\n    else:", 1)[0]
        self.assertIn("The outcome of the first one still follows here.",
                      sentences(branch))


class EmailAnswersItToo(unittest.TestCase):

    def setUp(self):
        self.mail = src("mail.py")

    def test_it_no_longer_claims_nothing_was_attached(self):
        fn = self.mail.split("def _acknowledge(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('again = [o for o in outcomes if o.get("status") == "duplicate"]', fn)
        # Code only. A comment above the branch quotes the sentence it stopped
        # sending, and matching that would compare the fix against its own
        # explanation.
        code = "\n".join(l for l in fn.splitlines() if not l.strip().startswith("#"))
        self.assertLess(code.index("elif again:"), code.index("no receipt attached"))

    def test_it_names_the_claim_it_already_is(self):
        fn = self.mail.split("def _acknowledge(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('o.get("reference")', fn)
        branch = sentences(fn.split("elif again:", 1)[1].split("subject_line", 1)[0])
        self.assertIn("No second claim was created.", branch)
        self.assertNotIn("credit", branch)


if __name__ == "__main__":
    unittest.main()


class ADuplicateOfAClaimNobodyIsPaying(unittest.TestCase):
    """Mobil80-Exp-89 stayed flagged after Exp-86 was withdrawn.

    Nothing was broken in the fingerprint machinery. Withdrawing a claim
    releases every key it held, precisely so the resubmission it was withdrawn
    to make is not turned away - and it did. It released them seven minutes
    too late: Exp-89 arrived at 09:32 and Exp-86 was withdrawn at 09:39, so
    the audit ran while the other claim was still live.

    A verdict is a snapshot of the moment it was computed and nothing re-reads
    it. That is deliberate - re-auditing behind a reviewer is the design this
    product does not have. But a duplicate finding is a statement about
    *another claim*, and that claim's state is live. So it is checked when the
    finding is read, rather than leaving a reviewer to compare a bill against
    something nobody is claiming.

    Settled is not one of the endings that stands it down. A duplicate of a
    bill that has actually been paid is the case this finding exists for.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "..", "PORTAL", "app.html"),
                  encoding="utf-8") as fh:
            self.app = fh.read()
        self.fn = self.app.split("function duplicateSettled(sub) {", 1)[1].split(
            "\n}", 1)[0]

    def test_it_asks_the_other_claim_what_became_of_it(self):
        self.assertIn("SUBMISSIONS.find(s => s.reference === ref || s.id === ref)",
                      self.fn)
        self.assertIn("const stage = claimStage(other).stage;", self.fn)

    def test_withdrawn_and_rejected_stand_it_down(self):
        self.assertIn('(stage === "withdrawn" || stage === "rejected") ? stage : null',
                      self.fn)

    def test_but_a_settled_one_does_not(self):
        # The case the finding exists for: the same bill, paid once already.
        self.assertNotIn('"settled"', self.fn)

    def test_a_claim_that_cannot_be_found_changes_nothing(self):
        # Outside the loaded window, or on another account. Saying "that one
        # was withdrawn" about a claim nobody can see would be a guess.
        self.assertIn("if (!other) return null;", self.fn)

    def test_the_finding_says_what_actually_happened(self):
        # The row stays - the two receipts really were the same bill, and that
        # is worth seeing - it just stops reading as something to weigh up.
        self.assertIn("so nothing ", self.app)
        self.assertIn("This one stands on its own.", self.app)

    def test_and_stops_reading_as_a_warning(self):
        self.assertIn('row.className = "violation " + (wentAway ? "deduct"', self.app)
        self.assertIn('v.code === "possible_duplicate" && !wentAway ? " dupe" : ""',
                      self.app)

    def test_the_queue_chip_goes_with_it(self):
        # A red chip pointing at a claim nobody is paying is a reviewer
        # opening two receipts to find there was never anything to compare.
        self.assertIn("const dupe = sub.duplicateOf && !duplicateSettled(sub)",
                      self.app)

    def test_and_so_does_the_rejection_reason(self):
        # It sits at the top of the list, so it is the reason that would be
        # reached for first - and "the same bill as Exp-86, which has already
        # been claimed" is not true of a claim that was withdrawn.
        self.assertIn("if (sub.duplicateOf && !duplicateSettled(sub)) {", self.app)

    def test_the_release_on_withdrawal_is_still_there(self):
        # It is not the bug, and it is what stops the next resubmission being
        # turned away - this time in the right order.
        review = src("auth.py").split("def _claim_review(", 1)[1].split(
            "\ndef ", 1)[0]
        self.assertIn('if action in ("rejected", "withdrawn"):', review)
        self.assertIn("duplicates.release_all(", review)
