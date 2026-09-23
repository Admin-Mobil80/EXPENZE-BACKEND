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
    """Approve is greyed until the claim is finished, and then it does it all.

    The server's own gate is at the settlement - `_unready_to_pay` - and that
    stays where it is: it is the last moment the type and the cost centre can
    be asked and the first moment either matters.

    What the console does in front of it has moved twice. It withheld Approve
    outright, which left eighteen of twenty claims in the live queue
    undecidable. Then it offered Approve regardless and said what was
    outstanding, which moved the work to finance - who cannot pay one without
    both, and who are reading the claim on a screen that does not show them
    the bill it describes.

    A disabled button is the version that costs the reviewer nothing. It
    refuses no decision they have made; it says the claim is not finished,
    with the two controls that finish it directly above it, and it comes alive
    the moment they are answered - read off the dropdowns, not off the record,
    so there is nothing to save first and nothing to come back for.
    """

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_approve_is_greyed_rather_than_withheld(self):
        self.assertIn('[["Approve as reviewed","primary","Approved", blockers.length],',
                      self.app)
        self.assertIn("b.disabled = true;", self.app)

    def test_it_waits_for_the_type_the_cost_centre_and_a_readable_bill(self):
        block = self.app.split("const blockers = [", 1)[1].split("].filter", 1)[0]
        for reason in ("unreadable &&", "!typeOk &&", "!groupOk &&"):
            self.assertIn(reason, block)

    def test_the_cost_centre_is_read_off_the_dropdown_not_the_record(self):
        # `needsGroup` asks what the server holds, which is right for the line
        # that reports what is missing and wrong for a button that has to come
        # alive the moment somebody picks a group - nothing is written down at
        # that point, and that is the whole idea.
        self.assertIn('const chosenGroup = ("group" in w) ? String(w.group || "")',
                      self.app)
        self.assertIn("const groupOk = !groupsExist || !!chosenGroup;", self.app)

    def test_an_organisation_with_no_cost_centres_is_not_missing_one(self):
        self.assertIn("const groupsExist = ((ORG_PROFILE.groups || []).length > 0);",
                      self.app)

    def test_reject_is_never_greyed(self):
        # Refusing a claim charges nothing to a cost centre and files nothing
        # under a type, so it is honest whatever is in the dropdowns - and a
        # reviewer must always be able to refuse a bill.
        self.assertIn('["Reject","danger","Rejected", false]', self.app)

    def test_a_greyed_button_says_why_on_itself(self):
        # A disabled control with its reason three lines away looks broken.
        self.assertIn('b.title = "Not yet — " + blockers.join(", and ") + ".";',
                      self.app)

    def test_the_findings_no_longer_say_before_approving(self):
        # They said "Set it before approving", which stopped being true.
        worker = read("lambda_src/auditor_worker.py")
        self.assertNotIn("Set it before approving.", worker)
        self.assertIn("it cannot be paid without one", worker)


class ApprovingIsTheOnlyButtonOnThatScreen(unittest.TestCase):
    """Save and then Approve is one intention and two presses.

    A reviewer who sets the expense type is on their way to approving the
    claim, not filing a correction - so Save was a step the product needed and
    the person did not, sitting between them and the only thing they came to
    do. Worse, it was the step that decided whether Approve was even offered,
    so the sequence was: change a dropdown, watch Approve disappear, press
    Save, wait, press Approve.

    Now the approval carries the answers in. They are written first, because
    the server decides against what is recorded rather than against what is on
    screen, and then the decision is recorded.
    """

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("async function decide(", 1)[1].split(
            "\nfunction ", 1)[0]

    def test_approving_writes_the_answers_first(self):
        self.assertIn('if (server === "approved" && !isPristine(sub, workingCopy(sub))) {',
                      self.fn)
        self.assertIn("const saved = await saveAnswers({ silent: true });", self.fn)

    def test_nothing_is_decided_against_answers_that_did_not_land(self):
        after = self.fn.split("const saved = await saveAnswers", 1)[1]
        self.assertIn("if (!saved) {", after)
        self.assertIn("return;", after.split("if (!saved) {", 1)[1].split("}", 1)[0])

    def test_rejecting_writes_nothing(self):
        # It charges nothing to a cost centre and files nothing under a type,
        # and a failed write must never be able to stop somebody refusing a
        # bill.
        guard = self.fn.split("const saved = await saveAnswers", 1)[0] \
                       .rsplit("if (", 1)[1]
        self.assertIn('server === "approved"', guard)
        self.assertNotIn("rejected", guard)

    def test_the_row_is_held_across_both_writes(self):
        # `saveAnswers` renders on its way through and reloads the records;
        # without the flag the buttons come back live in between, where a
        # second click is a second decision on the same claim.
        held = self.fn.split("decidingOf = sub.id;", 1)[0]
        self.assertIn('server === "approved"', held)

    def test_but_nothing_is_rendered_before_the_answers_are_read(self):
        # Freezing the row disables the three dropdowns, and `saveAnswers`
        # sends only the controls that are enabled - deliberately, so nobody
        # posts a field they could not edit. A render between the flag and the
        # read turns the reviewer's group and currency into "not sent".
        between = self.fn.split("decidingOf = sub.id;", 1)[1].split(
            "await saveAnswers", 1)[0]
        self.assertNotIn("renderAll()", between)

    def test_the_stale_row_is_not_decided_against(self):
        # `loadRecords` rebuilds the list, so the object this was called with
        # is a copy of something that no longer exists.
        self.assertIn("sub = SUBMISSIONS.find(s => s.id === sub.id) || sub;", self.fn)

    def test_the_silent_save_says_nothing_about_approving_next(self):
        save = self.app.split("async function saveAnswers(", 1)[1].split(
            "\n}\n", 1)[0]
        self.assertIn("const silent = !!(opts && opts.silent);", save)
        self.assertIn("if (!silent) {", save)
        self.assertIn("Approve or reject it when you are ready.", save)

    def test_and_it_reports_whether_the_write_landed(self):
        save = self.app.split("async function saveAnswers(", 1)[1].split(
            "\n}\n", 1)[0]
        self.assertIn("return false;", save)
        self.assertIn("return true;", save)

    def test_there_is_no_save_button_on_the_reviewer_s_arm(self):
        self.assertIn('if (unsaved && arm !== "review"', self.app)


class TheButtonSavesAndStops(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("async function saveAnswers(opts) {", 1)[1].split(
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
        #
        # The other conditions are what moved it out of the reviewer's branch
        # and then off it: `classFrozen` is the same expression that decides
        # whether the dropdowns above are editable, so the button is offered
        # exactly where there is something it could record; the two busy arms
        # own the row while a write or a decision is in flight; and on the
        # reviewer's arm Approve writes the answers itself, so a second button
        # there would be two presses for one intention.
        self.assertIn(
            'if (unsaved && arm !== "review" && !classFrozen && !saving && !deciding) {',
            self.app)

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
        self.assertIn("if (!sub || savingOf) return false;", self.fn)
        self.assertIn("savingOf = sub.id;", self.fn)
        self.assertIn("savingOf = null;", self.fn)


if __name__ == "__main__":
    unittest.main()
