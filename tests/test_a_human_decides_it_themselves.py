"""Once a claim reaches a person, the person decides it. Nothing else does.

Re-check is gone, and with it the whole idea it stood for: that a reviewer's
correction should send the claim back round the policy engine to be decided
again. It was a plausible design and it was wrong at the seam where the two
meet.

What it produced in practice:

* A courier bill re-checked four times, each time returning "No enabled rule
  covers expense type 'courier'" - because the re-audit path never loaded the
  organisation's policy and fell back to the built-in set, where `courier` does
  not exist. Four model calls, four identical wrong answers, and a reviewer
  with no way to tell the product was arguing with itself.
* Before that, the reverse: the re-audit found nothing wrong, so the claim read
  as cleared-by-the-agent, left the queue and landed in Pending settlement
  without anybody approving it. The correction had become the approval.
* And a write that woke the stream that wrote the row that woke the stream:
  576 attempts in twenty minutes.

Each was patched. The shape kept producing them, because it put a second
decider between a reviewer's answer and their decision.

So: a claim that lands on a human is decided by that human. They read the bill,
the findings and the figures, and they approve or reject. Setting the expense
type, the currency and the group records facts the agent could not supply -
what the claim will be reported under, what it is denominated in, which cost
centre pays it - and changes nothing else.

Which makes the type mandatory, and that is the other half of this file. Every
report groups by expense type, so a payment filed under nothing is a line in
the accounts nobody can explain. `not_covered` is the agent's honest "I cannot
tell", not an expense type, and it cannot be approved.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def code_only(js):
    """The source with its prose removed, so a comment cannot pass a test."""
    js = re.sub(r"/\*[\s\S]*?\*/", " ", js)
    return re.sub(r"(?m)^\s*//[^\n]*", " ", js)


class NothingReDecidesAClaimBehindTheReviewer(unittest.TestCase):

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.worker = read("lambda_src/auditor_worker.py")
        self.handler = read("lambda_src/handler.py")
        self.retype = self.auth.split("def _claim_retype(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_saving_an_answer_does_not_requeue_the_claim(self):
        # The status write is what woke the worker. Without it nothing else
        # has to be stopped.
        self.assertNotIn('"#s = :queued"', self.retype)
        self.assertNotIn('":queued": "queued"', self.retype)

    def test_it_still_writes_what_the_reviewer_answered(self):
        for field in ("answered_expense_type", "answered_currency", "group_id",
                      "group_status", "corrected_by", "corrected_at"):
            self.assertIn(field, self.retype)

    def test_it_still_refuses_a_claim_that_is_being_read(self):
        # The condition stays: an answer written over a claim mid-audit would
        # be overwritten by the audit that lands after it.
        self.assertIn('ConditionExpression="#s = :audited"', self.retype)

    def test_it_no_longer_claims_to_have_requeued_anything(self):
        self.assertIn('"status": "saved"', self.retype)
        self.assertNotIn('"status": "requeued"', self.retype)

    def test_the_worker_has_no_re_audit_branch(self):
        self.assertNotIn("_reaudit(", code_only(self.worker))
        self.assertNotIn("_abandon_reaudit", code_only(self.worker))

    def test_the_engine_has_no_re_audit_entry_point(self):
        self.assertNotIn("def reaudit(", self.handler)

    def test_the_first_audit_is_untouched(self):
        # Removing the second pass must not disturb the one that reads the
        # bill. It still loads the organisation's own policy.
        audit = self.handler.split("\ndef audit(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("rules = policy.rules_for(org)", audit)
        self.assertIn("_run_policy(", audit)


class ApprovingDoesNotWaitForTheFilingFields(unittest.TestCase):
    """The expense type and the cost centre used to withhold Approve.

    They were both required, refusing the approval until they were set, and
    that put the requirement in the wrong place. Approving is a judgment about
    whether the company owes this money. The type and the group are about how
    the payment is *filed*, and filing is what finance does - so a reviewer
    who had read the bill and decided it was legitimate was being stopped by
    two dropdowns that say nothing about that decision.

    It showed. Eighteen of twenty claims in the live queue were missing one of
    them, which made the queue unworkable and bulk approval pointless.

    The requirement moved rather than went. `_unready_to_pay` refuses the
    *settlement* without both, so nothing can be paid - and therefore nothing
    reported - under a missing type or an unnamed cost centre. That is the
    last moment either can be asked and the first moment either matters.
    """

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.review = self.auth.split("def _claim_review(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_an_approval_is_not_refused_for_a_missing_type(self):
        self.assertNotIn("Set an expense type first.", self.review)

    def test_nor_for_a_missing_cost_centre(self):
        self.assertNotIn("Set a group first.", self.review)

    def test_the_requirement_is_asked_where_the_money_moves(self):
        self.assertIn("def _unready_to_pay(", self.auth)
        outcome = self.auth.split("def _claim_outcome(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_unready_to_pay(item,", outcome)

    def test_and_it_still_asks_about_the_claim_as_it_stands(self):
        # A reviewer's answer if they gave one, the agent's reading otherwise,
        # against the types enabled in the policy right now - not against what
        # the verdict happened to block on when it was written.
        fn = self.auth.split("def _unready_to_pay(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('item.get("answered_expense_type")', fn)
        self.assertIn("policy.expense_type_ids(policy.rules_for(org", fn)

    def test_a_rejection_is_asked_neither(self):
        # It files nothing. Demanding a claim be filable before it can be
        # refused would stop finance refusing exactly the claims most likely
        # to be missing something.
        fn = self.auth.split("def _claim_outcome(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('if kind == "settled":', fn.split("_unready_to_pay", 1)[0])


class TheConsoleAsksTheSameQuestion(unittest.TestCase):
    """And the console offers Approve on the same terms the server accepts it.

    What still withholds it: an edit that has not reached the server, and a
    receipt nobody could read. Neither is about filing - one is a change the
    reviewer can see and the record cannot, the other is a claim with no
    amount in it to approve.
    """

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_approve_waits_only_for_those_two(self):
        self.assertIn("const choices = (unsaved || unreadable)", self.app)

    def test_reject_is_always_offered(self):
        choices = self.app.split("const choices = (unsaved || unreadable)", 1)[1] \
                          .split(";", 1)[0]
        self.assertEqual(2, choices.count('"Reject","danger","Rejected"'))

    def test_the_message_says_what_is_outstanding_not_what_is_blocked(self):
        # Finance cannot pay a claim missing either, so saying so here saves
        # them coming back - but a reviewer who knows the bill is good can
        # approve it and move on.
        self.assertIn("You can still approve it; finance cannot pay ", self.app)

    def test_the_findings_no_longer_say_before_approving(self):
        # They said "Set it before approving", which stopped being true.
        worker = read("lambda_src/auditor_worker.py")
        self.assertNotIn("Set it before approving.", worker)
        self.assertIn("it cannot be paid without one", worker)
        self.assertNotIn("before approving it.", self.app)


class TheButtonSavesAndStops(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("async function saveAnswers() {", 1)[1].split(
            "\n}", 1)[0]

    def test_there_is_no_re_check_anywhere(self):
        page = code_only(self.app)
        for gone in ("Re-check", "saveAndRecheck", "watchRecheck", "recheckOf",
                     "RECHECK_EVERY", "RECHECK_GIVE_UP"):
            self.assertNotIn(gone, page, gone)

    def test_the_button_says_what_it_does(self):
        self.assertIn('save.textContent = savingOf === sub.id ? "Saving…" : "Save";',
                      self.app)

    def test_it_appears_only_when_something_has_changed(self):
        # It used to appear on an unchanged claim too, as the way to ask for a
        # re-run. There is nothing to re-run.
        self.assertIn("if (unsaved) {", self.app)

    def test_it_posts_the_answers_the_reader_could_actually_give(self):
        # The type always - it is the one control every role holds. The
        # currency and the group only where they were editable: reading a
        # value off a disabled control and posting it anyway makes the server
        # decide whether to allow a field nobody edited.
        self.assertIn("submission_id: sub.id, expense_type: chosen,", self.fn)
        self.assertIn("...(chosenCcy === null ? {} : { currency: chosenCcy })", self.fn)
        self.assertIn("...(chosenGroup === null ? {} : { group_id: chosenGroup })",
                      self.fn)
        for control, name in (("cs", "ccy"), ("gs", "claim-group")):
            self.assertIn(f'const {control} = $("{name}");', self.fn)
            self.assertIn(f"{control} && !{control}.disabled", self.fn)

    def test_it_reloads_and_says_what_was_saved(self):
        self.assertIn("await loadRecords();", self.fn)
        self.assertIn("Approve or reject it when you are ready.", self.fn)

    def test_the_confirmation_no_longer_promises_an_automatic_release(self):
        # "It is re-checked against the rules for that type, and it may clear
        # for payment without further review" described the behaviour that has
        # just been removed - and was the behaviour nobody wanted.
        self.assertNotIn("may clear for payment without further review",
                         code_only(self.app))
        self.assertIn("the claim stays", self.fn)

    def test_one_save_at_a_time(self):
        self.assertIn("if (!sub || savingOf) return;", self.fn)
        self.assertIn("savingOf = sub.id;", self.fn)
        self.assertIn("savingOf = null;", self.fn)


if __name__ == "__main__":
    unittest.main()
