"""Setting an expense type is not approving a payment.

A receipt that arrives with nothing in the policy covering it is blocked on
`no_rule_for_expense_type` and waits in the review queue. A reviewer opens it,
picks the type from the dropdown, and the claim is sent back round the auditor -
which is right, because only the engine may decide what a claim is worth.

It then came back clean, and every reader of "did this release itself" asked
only whether the verdict passed policy. So the claim left the review queue and
arrived in Pending settlement, marked cleared *by the agent*, with no human
decision recorded anywhere - and the reviewer who was looking at it never got to
press Approve. Changing a dropdown paid somebody.

Two things were wrong and they are worth keeping apart.

**Supplying a missing fact is not a decision.** A reviewer must be able to set a
type to see what it does, and be able to change their mind. The correction is an
input to the engine; the approval is a judgment about somebody's money, and one
cannot stand in for the other.

**The submitter was told before it was true.** The re-audit messaged them the
new outcome, so they were told their claim was approved while it was still
sitting in a queue waiting for a person. That is a message we would have had to
take back.

The tests below hold both, and hold the shape of the fix: one predicate that
every reader consults, rather than the condition written out in five places.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class TheCorrectionIsRecorded(unittest.TestCase):

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.retype = self.auth.split("def _claim_retype(", 1)[1].split("\ndef ", 1)[0]

    def test_retyping_stamps_who_corrected_it_and_when(self):
        self.assertIn("corrected_by = :who", self.retype)
        self.assertIn("corrected_at = :cat", self.retype)

    def test_only_a_reviewer_can_do_it(self):
        # The stamp means "a person with authority changed this", so the
        # authority check is part of what makes it meaningful.
        self.assertIn('not runs_the_org(acting)', self.retype)

    def test_it_reaches_the_console(self):
        view = self.auth.split("def _submission_view(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"corrected_by"', view)
        self.assertIn('"corrected_at"', view)

    def test_the_re_audit_does_not_wipe_it(self):
        # The auditor clears the answered_* inputs it consumed. If it cleared
        # this too, the claim would come back looking untouched and release
        # itself - which is the bug, restored one line later.
        worker = read("lambda_src/auditor_worker.py")
        for clause in re.findall(r"REMOVE [^\"]*", worker):
            self.assertNotIn("corrected_", clause)


class OneQuestionAskedInOnePlace(unittest.TestCase):
    """Five copies of a condition is four of them eventually disagreeing."""

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.pred = self.app.split("const agentMayRelease = ", 1)[1].split(";\n", 1)[0]

    def test_it_reads_the_server_not_the_local_preview(self):
        # The one that let a claim vanish mid-review. This read
        # `evaluate(sub)` - the reviewer's what-if - so changing the expense
        # type in the dropdown recomputed the preview as approved, made this
        # true, and the claim left the review queue for Pending settlement
        # while the change was still unsaved. Nothing written, nothing decided,
        # and the claim gone from the screen somebody was working on.
        self.assertIn("AUTO_RELEASED.has(sub.serverVerdict", self.pred)
        # Code only - the comment above it names the call it replaced.
        code = "\n".join(l for l in self.pred.splitlines()
                         if not l.strip().startswith("//"))
        self.assertNotIn("evaluate(sub)", code)

    def test_the_predicate_weighs_all_three(self):
        self.assertIn("!sub.pulledBack", self.pred)
        self.assertIn("!sub.correctedAt", self.pred)
        self.assertIn("AUTO_RELEASED.has", self.pred)

    def test_nothing_else_reads_auto_released_directly(self):
        # One definition, one use. Anything else is a reader that will not
        # learn about the next reason a claim needs a person.
        uses = [line for line in self.app.splitlines()
                if "AUTO_RELEASED" in line]
        self.assertEqual(2, len(uses),
                         "AUTO_RELEASED should be defined once and read once:\n"
                         + "\n".join(uses))

    def test_the_queue_the_claim_page_and_the_payment_run_all_use_it(self):
        for fn, end, marker in (
                ("const isQueued = (sub) => {", "\n};", "!agentMayRelease(sub)"),
                ("function payableClaims(", "\nfunction ", "agentMayRelease(sub)"),
                ("function claimStage(", "\nfunction ", "agentMayRelease(sub)")):
            self.assertIn(fn, self.app, fn)
            body = self.app.split(fn, 1)[1].split(end, 1)[0]
            self.assertIn(marker, body, fn)


class ACorrectedClaimWaitsForAPerson(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_it_stays_in_the_review_queue(self):
        # isQueued is `!agentMayRelease`, and the predicate refuses on
        # correctedAt - so this is the whole of it.
        queued = self.app.split("const isQueued = (sub) => {", 1)[1].split("\n};", 1)[0]
        self.assertIn("!agentMayRelease(sub)", queued)

    def test_it_does_not_appear_in_pending_settlement(self):
        payable = self.app.split("function payableClaims(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("!decision && agentMayRelease(sub)", payable)
        self.assertNotIn("AUTO_RELEASED", payable)

    def test_the_queue_row_says_why_it_is_still_there(self):
        # It no longer has to. The queue named the finding that stopped each
        # claim in a "Waiting on" column, which answered a who-question with a
        # what - and with nothing ever asked of a submitter, the who is always
        # the reviewer reading the row. Being *in* the queue is the statement;
        # the finding is on the claim page under the figures it refers to.
        self.assertNotIn("corrected_awaiting_approval", self.app)
        self.assertNotIn('<th scope="col">Waiting on</th>', self.app)

    def test_the_claim_page_says_approval_is_still_needed(self):
        self.assertIn("still needs your approval", self.app)

    def test_the_old_message_claiming_otherwise_is_gone(self):
        # The product used to say, accurately, that your correction had
        # released the claim without a reviewer's decision.
        self.assertNotIn("without a reviewer's decision", self.app)

    def test_approving_it_still_pays_what_the_engine_worked_out(self):
        # The correction is an input to the engine, not an override of it. Once
        # a human approves, the amount is the engine's.
        payable = self.app.split("function payableClaims(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("const pay = payable(sub);", payable)


class TheSubmitterHearsNothingFromACorrection(unittest.TestCase):
    """A reviewer setting the expense type is not an outcome.

    This used to need arranging. The correction sent the claim back round the
    agent, the agent produced a verdict, and the verdict was messaged to the
    submitter - so somebody was told their claim had been approved before any
    person had approved it, and the message had to be taken back. The re-audit
    grew a `corrected_by_reviewer` gate to suppress it.

    There is no re-audit now. A reviewer's answer is written down and the claim
    stays in front of them; nothing computes a verdict in between, so there is
    nothing to send. What is asserted here is that the endpoint recording the
    answer does not notify anybody - the submitter hears from the decision,
    which is the reviewer's next click.
    """

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.retype = self.auth.split("def _claim_retype(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_recording_an_answer_messages_nobody(self):
        self.assertNotIn("notify.send", self.retype)

    def test_and_does_not_send_the_claim_anywhere_that_would(self):
        # Requeueing it was the mechanism: the worker audited it and the
        # worker tells the sender.
        self.assertNotIn('":queued": "queued"', self.retype)

    def test_the_worker_no_longer_has_a_correction_path_at_all(self):
        worker = read("lambda_src/auditor_worker.py")
        self.assertNotIn("corrected_by_reviewer", worker)
        self.assertNotIn("def _reaudit(", worker)

    def test_those_two_fields_still_have_exactly_one_writer(self):
        # They are how the claim is reported and paid; a second writer would
        # be a second answer to a question only a reviewer may answer.
        # Writes, not mentions: `_claim_review` reads `answered_expense_type`
        # to check the claim is tagged, which is the whole point of it being
        # written, and a test that counted readers would forbid that.
        for field in ("answered_expense_type", "answered_currency"):
            writers = [fn for fn in re.findall(r"def (_[a-z_]+)\(", self.auth)
                       if f"{field} = :" in
                       self.auth.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]]
            self.assertEqual(["_claim_retype"], writers, field)

    def test_the_decision_is_what_reaches_them(self):
        review = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("notify.send", review)


if __name__ == "__main__":
    unittest.main()
