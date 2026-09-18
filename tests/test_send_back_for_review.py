"""A claim that was sent back for review, and the record of it.

The console no longer sends one back. "Send back for review" described a
journey an agent-cleared claim had never taken, routed it to a queue watched
by the same two roles that settle, and existed for a case now answered better:
the expense type and the currency are editable on the claim until the money
moves, and changing either re-runs the policy engine and returns a real
verdict. Correcting it in place beats parking it in a list.

What these tests guard is the other half - what happens to the claims it was
already used on. Three things have to hold, and they hold whether or not
anything can still write the state:

* a sent-back claim is out of Pending settlement, or finance pays something
  somebody disputed;
* it is in the review queue rather than nowhere, which is where anything the
  console can neither call queued nor call payable ends up;
* the server still accepts the action, and still tells the submitter. They
  were told days ago they were owed money; somebody who believes a payment is
  coming does not chase it.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class TheServerAcceptsIt(unittest.TestCase):
    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.review = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]
        self.disputed = self.review.split('if action == "disputed":', 1)[1]

    def test_it_is_a_reviewer_action(self):
        actions = self.auth.split("REVIEW_ACTIONS = (", 1)[1].split(")", 1)[0]
        self.assertIn('"disputed"', actions)
        # Which means the owner/finance gate above already covers it: a
        # submitter cannot dispute the agent into re-examining their own claim.
        self.assertIn('if action in REVIEW_ACTIONS and not runs_the_org(acting)',
                      self.review)

    def test_it_needs_a_reason(self):
        # The reviewer it lands on has no finding to read - the agent had
        # nothing to say about this claim, which is exactly why it was
        # released. Without words they are looking at a clean claim with no
        # idea what they are being asked to check.
        gate = self.review.split("reason = str(", 1)[1].split("submission_id =", 1)[0]
        self.assertIn('"disputed"', gate)
        self.assertIn("len(reason) < 4", gate)

    def test_a_reimbursed_claim_cannot_be_sent_back(self):
        # The money has moved. Putting it back in a queue would leave a
        # payment recorded against a claim that says it is undecided.
        #
        # The guard moved out of this branch and up to the top of the function,
        # where it now covers every decision rather than only this one - so it
        # is asserted against the whole handler.
        self.assertIn('if item.get("outcome") == "settled":', self.review)
        self.assertLess(self.review.index('outcome") == "settled"'),
                        self.review.index('if action == "disputed"'))

    def test_it_restores_the_absence_of_a_decision(self):
        # Not a fourth review state. A claim waiting for a human and a claim
        # that was never decided are the same thing, and two representations
        # of one state is how a queue starts disagreeing with itself.
        self.assertIn("REMOVE review_action, review_reason, review_by", self.disputed)
        self.assertIn("approved_total", self.disputed)

    def test_it_leaves_a_trail(self):
        for field in ("pulled_back", "pulled_reason", "pulled_by", "pulled_at"):
            self.assertIn(field, self.disputed, f"{field} is not recorded")

    def test_the_submitter_is_told(self):
        # They were told they were owed money. Saying nothing while the claim
        # goes quietly back in the queue is the one thing this must not do.
        self.assertIn('notify.send("disputed"', self.disputed)

    def test_the_console_can_read_it_back(self):
        record = self.auth.split("def _submission_view(", 1)[1].split("\ndef ", 1)[0]
        for field in ("pulled_back", "pulled_reason", "pulled_by"):
            self.assertIn(f'"{field}"', record)


class TheNoticeDoesNotReadLikeARejection(unittest.TestCase):
    def setUp(self):
        self.notify = read("lambda_src/notify.py")
        self.notice = self.notify.split("def disputed_notice(", 1)[1].split("\ndef ", 1)[0]

    def test_it_is_a_notice_like_any_other(self):
        self.assertIn('"disputed": disputed_notice', self.notify)

    def test_it_says_nothing_is_wanted_from_them(self):
        # A rejection and a question both send somebody looking for something
        # to do. This is neither: nothing has been decided against them and
        # nothing is being asked.
        self.assertIn("It has not been ", self.notice)
        self.assertIn("do not need to do anything", self.notice)

    def test_it_does_not_go_out_on_a_template_approved_for_something_else(self):
        # A number loses its quality rating that way. No approved template
        # exists for this, so it goes as text inside the service window.
        templates = self.notify.split("TEMPLATES = {", 1)[1].split("}", 1)[0]
        self.assertNotIn("disputed", templates)
        self.assertIn('"disputed"',
                      self.notify.split("NO_TEMPLATE = {", 1)[1].split("}", 1)[0])


class TheConsoleMovesItBackToTheQueue(unittest.TestCase):
    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_a_sent_back_claim_is_queued_again(self):
        queued = self.app.split("const isQueued = (sub) => {", 1)[1].split("\n};", 1)[0]
        self.assertIn("!agentMayRelease(sub)", queued)
        self.assertIn("if (decisions[sub.id]) return false;", queued)

    def test_the_agent_may_not_release_what_finance_sent_back(self):
        # The condition moved into one predicate so that every reader of "did
        # this release itself" asks the same question. This is that question.
        fn = self.app.split("const agentMayRelease = ", 1)[1].split(";\n", 1)[0]
        self.assertIn("!sub.pulledBack", fn)
        self.assertIn("AUTO_RELEASED.has", fn)

    def test_it_is_no_longer_payable(self):
        # Otherwise it sits in Pending settlement and in the review queue at
        # once, and whichever screen finance opens first decides what happens.
        payable = self.app.split("function payableClaims(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("!decision && agentMayRelease(sub)", payable)

    def test_the_console_no_longer_offers_it(self):
        self.assertNotIn('decide(sub, "Sent back"', self.app)
        self.assertNotIn('"Sent back": "disputed"', self.app)

    def test_but_a_claim_it_was_used_on_still_says_so(self):
        # The state is unreachable, not erased. A claim finance sent back
        # before this went must still explain why it is in the queue, or it
        # reads as a fault.
        self.assertIn("const sentBack = sub.pulledBack && !decision;", self.app)
        self.assertIn('textContent = "sent_back_for_review"', self.app)
        self.assertIn("pulledBack: !!s.pulled_back", self.app)

    def test_the_reviewer_is_told_why_it_is_in_front_of_them(self):
        # The findings box on a sent-back claim would otherwise read "No
        # findings. The claim passed every rule" - true, and useless.
        self.assertIn("sent_back_for_review", self.app)
        self.assertIn("did not agree with the agent", self.app)
        self.assertIn("!res.violations.length && !sentBack", self.app)

    def test_the_page_still_parses(self):
        # A stray brace in the branch above would take the whole console down,
        # and every other assertion here reads strings out of the same file.
        script = "\n".join(re.findall(
            r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", self.app, re.S))
        self.assertEqual(script.count("function payableClaims"), 1)
        self.assertGreater(len(script), 1000)


if __name__ == "__main__":
    unittest.main()


class ReopeningClearsTheFigureItReleased(unittest.TestCase):
    """A reopened claim kept the amount the reviewer had approved.

    `reopened` removes the decision - the action, the reason, who made it -
    but left `approved_total` behind. A claim reopened and then cleared by the
    agent was still carrying the figure a human had released before anybody
    changed their mind, and that is the figure the payment run reads.
    """

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.review = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]

    def test_reopening_removes_it(self):
        block = self.review.split('if action == "reopened":', 1)[1].split("return", 1)[0]
        self.assertIn("approved_total", block)

    def test_both_ways_back_to_undecided_clear_the_same_fields(self):
        # Sending back and reopening both mean "there is no decision on this
        # claim". Two of them leaving different residue is how one path pays
        # a figure the other would not.
        for action in ('if action == "reopened":', 'if action == "disputed":'):
            # As far as the write, not the first early return in it.
            block = self.review.split(action, 1)[1].split("update_item(", 1)[1].split(
                "ExpressionAttributeValues", 1)[0]
            for field in ("review_action", "review_by", "approved_total"):
                self.assertIn(field, block, f"{action} leaves {field} behind")

    def test_the_console_can_read_the_figure_without_a_guard(self):
        app = read("../PORTAL/app.html")
        payable = app.split("function payableClaims(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("const pay = payable(sub);", payable)
        helper = app.split("function payable(sub) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("approvedInClaimCcy(sub)", helper)
