"""A reviewer's decision has to be written down somewhere.

It was not. The console's Approve, Reject and Ask-employee buttons wrote to a
variable in the browser and called a re-render. A claim moved to Pending
settlement on screen, sat back in the review queue after a refresh, and the
employee - who is out of pocket - was never told anything either way. Nothing
in the log, nothing on the row, no error: the only way to notice was to approve
something and look again later.

These tests cover the parts that are easy to get wrong now that it is real: who
may decide, that a rejection carries the reason the employee has to act on,
that reopening genuinely clears the decision rather than adding a fourth state,
and that a rejected claim stops holding the receipt's duplicate fingerprint.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")

ROOT = os.path.join(os.path.dirname(__file__), "..")


class TheEndpointExists(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            self.auth = h.read()
        with open(os.path.join(ROOT, "expensifyai", "stack.py"), encoding="utf-8") as h:
            self.stack = h.read()

    def test_every_action_the_console_sends_is_one_the_server_accepts(self):
        with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as h:
            page = h.read()
        block = page.split("const REVIEW_ACTION = {", 1)[1].split("}", 1)[0]
        sent = set(re.findall(r'"?(\w[\w ]*)"?\s*:\s*"(\w+)"', block))
        sent = {server for _, server in sent} | {"reopened"}

        accepted = set(re.findall(r'"(\w+)"',
                                  self.auth.split("REVIEW_ACTIONS = (", 1)[1].split(")", 1)[0]))
        accepted |= set(re.findall(r'"(\w+)"',
                                   self.auth.split("CLAIMANT_ACTIONS = (", 1)[1].split(")", 1)[0]))
        self.assertTrue(sent)
        self.assertEqual(sent - accepted, set(), "the console sends an action the server refuses")

    def test_the_route_is_wired(self):
        self.assertIn('add_resource("review")', self.stack)
        self.assertIn('path.endswith("/claim/review")', self.auth)

    def test_the_route_does_not_require_an_email_in_the_body(self):
        # Every path not in this list is gated on a valid `email` field, which
        # a session-authenticated call does not carry.
        gate = self.auth.split("if not any(path.endswith(p) for p in", 1)[1].split(")):", 1)[0]
        self.assertIn('"/claim/review"', gate)

    def test_a_decision_is_recorded_before_anybody_is_told(self):
        # A message the employee acts on must never describe a state that was
        # never written down.
        body = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]
        self.assertLess(body.index("update_item"), body.index("notify.send"))

    def test_only_an_owner_or_finance_may_decide(self):
        body = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('not runs_the_org(acting)', body)

    def test_an_approval_needs_none(self):
        # Nothing is being asked of the employee, so demanding a sentence from
        # the reviewer would be validation theatre.
        body = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]
        guard = body.split("len(reason) < 4", 1)[0]
        self.assertNotIn('"approved"', guard.split('action in (', 1)[-1])

    def test_reopening_removes_the_decision_rather_than_adding_a_state(self):
        body = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("REMOVE review_action", body)

    def test_a_rejected_claim_releases_its_duplicate_fingerprint(self):
        # Otherwise the employee who fixes the problem and resends is turned
        # away as a duplicate of a claim that went nowhere.
        body = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("duplicates.release", body)

    def test_the_console_can_read_the_decision_back(self):
        view = self.auth.split("def _submission_view(", 1)[1].split("\ndef ", 1)[0]
        for field in ("review_action", "review_reason", "review_by", "review_at"):
            self.assertIn(field, view)


if __name__ == "__main__":
    unittest.main()


class WithdrawingYourOwnClaim(unittest.TestCase):
    """A person can take back what they submitted.

    People send the wrong photograph, or a personal receipt by mistake, or one
    a colleague has already claimed. Until this existed the only way out was to
    ask a finance executive to reject it, which puts a rejection on the record
    for something that was never really a claim.

    The authorisation runs the opposite way to every other action here: a
    Finance Executive may not withdraw an employee's claim on their behalf, and
    an employee may not approve one.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            self.auth = h.read()
        self.body = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]

    def test_withdrawing_is_not_a_reviewer_action(self):
        review = self.auth.split("REVIEW_ACTIONS = (", 1)[1].split(")", 1)[0]
        self.assertNotIn("withdrawn", review)
        self.assertIn("withdrawn", self.auth.split("CLAIMANT_ACTIONS = (", 1)[1].split(")", 1)[0])

    def test_the_reviewer_gate_applies_only_to_reviewer_actions(self):
        # Otherwise an ordinary member of staff cannot withdraw anything.
        self.assertIn('action in REVIEW_ACTIONS and not runs_the_org(acting)',
                      self.body)

    def test_only_the_person_who_submitted_it_may_withdraw_it(self):
        self.assertIn('item.get("submitted_by", "")).lower() != actor.lower()', self.body)

    def test_a_reimbursed_claim_cannot_be_withdrawn(self):
        # It would leave a payment recorded against a claim that says it was
        # never made.
        self.assertIn('item.get("outcome") == "settled"', self.body)
        self.assertIn("409", self.body)

    def test_withdrawing_releases_the_duplicate_fingerprint(self):
        # The corrected receipt the person withdrew in order to send must not
        # be turned away as a duplicate of the one they took back.
        self.assertIn('action in ("rejected", "withdrawn")', self.body)

class ApprovingABlockedClaimReleasesSomething(unittest.TestCase):
    """An approved claim has to arrive somewhere.

    The engine reports nothing reimbursable while a finding blocks a claim -
    that is what blocking means. A reviewer who approves it anyway has decided
    to release the figure the finding was holding up, and until that was
    written down the claim left the review queue with nothing payable in it and
    arrived nowhere: not in the queue, not in Pending settlement, not in any
    list a person would open. Two real claims went missing this way.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            self.auth = h.read()
        with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as h:
            self.app = h.read()
        self.body = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]

    def test_an_approval_records_what_it_released(self):
        self.assertIn("approved_total = :amt", self.body)

    def test_it_falls_back_to_the_provisional_figure(self):
        # Which is precisely "what would be reimbursable if the open question
        # were answered" - the thing the reviewer just decided to waive.
        self.assertIn('verdict.get("provisional_total")', self.body)

    def test_a_claim_that_already_pays_something_keeps_its_own_figure(self):
        self.assertIn('approved_total = str(verdict.get("reimbursable_total") or "0")', self.body)

    def test_the_engine_s_verdict_is_left_alone(self):
        # "The engine computed needs_review and nothing payable" and "a named
        # human released 5,500 anyway" are both true, and an audit needs both.
        self.assertNotIn('verdict["verdict"] =', self.body)
        self.assertNotIn("reimbursable_total\"] =", self.body)

    def test_only_an_approval_sets_it(self):
        self.assertIn('if action == "approved":', self.body)

    def test_a_claim_worth_nothing_is_still_not_payable(self):
        pay = self.app.split("function payableClaims()", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (pay.amount <= 0) return;", pay)
        # And a foreign claim whose rate could not be had is a third case:
        # unknown, not zero, so it is listed rather than dropped.
        self.assertIn("if (pay.amount === null) {", pay)

    def test_the_console_can_read_the_figure_back(self):
        view = self.auth.split("def _submission_view(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"approved_total"', view)


class APaidClaimIsClosedToDecisions(unittest.TestCase):
    """Money has moved, so nothing decided from here on can be true.

    Approving a reimbursed claim changes nothing. Rejecting it says a payment
    should not have been made without unmaking it. Asking the submitter a
    question implies the outcome is still open when they already have the
    money. Only withdrawal was refused; the other four were not, so the console
    hiding the buttons was the only thing stopping any of them - and it was
    still offering "Send back for review" under a claim settled in full.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            self.auth = h.read()
        self.review = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]
        with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_the_server_refuses_every_decision_on_a_settled_claim(self):
        self.assertIn('if item.get("outcome") == "settled":', self.review)
        self.assertIn("409", self.review.split('outcome") == "settled"', 1)[1][:400])

    def test_the_rule_is_stated_once(self):
        # It was in the withdrawal branch and again in the dispute branch, and
        # absent from the three that mattered most.
        self.assertEqual(1, self.review.count('item.get("outcome") == "settled"'))

    def test_the_guard_sits_before_the_branches_it_governs(self):
        settled = self.review.index('outcome") == "settled"')
        self.assertLess(settled, self.review.index("if action in CLAIMANT_ACTIONS"))
        self.assertLess(settled, self.review.index('if action == "disputed"'))

    def test_reopening_is_refused_too(self):
        # It was excepted at first, on the reasoning that undoing a decision
        # decides nothing. True, but it puts the claim back in the queue with
        # the money already gone - a claim awaiting a decision that has also
        # been reimbursed. Reversing the payment has to come first.
        self.assertNotIn('and action != "reopened"', self.review)
        self.assertIn("Reopening included.", self.review)

    def test_the_console_offers_nothing_on_a_paid_claim(self):
        self.assertIn("const isPaid = (sub) => paidFor(sub.id) > 0;", self.app)
        self.assertIn("} else if (isPaid(sub)) {", self.app)
        self.assertIn("closed to further decisions", self.app)

    def test_send_back_and_withdraw_read_the_same_rule(self):
        # Each checking `paidFor` for itself is how one of them came to forget.
        self.assertIn("if (mayReview && !isPaid(sub) && !rejections[sub.id])", self.app)
        self.assertIn("isMine(sub) && !mayReview && !isPaid(sub)", self.app)

    def test_a_part_payment_still_leaves_the_settlement_bar(self):
        # The claim is not finished - it is an amount still to be paid, which
        # is not a decision.
        settle = self.app.split("function renderSettleOnClaim(", 1)[1].split("\n}", 1)[0]
        self.assertIn("c.outstanding <= 0", settle)


class AClaimNothingCoversCannotBeApproved(unittest.TestCase):
    """The model answers `not_covered` when nothing on the bill matches a type.

    That is the honest answer for it to give - a payment to a named individual
    is not a meal, a subscription or a trip. But a claim cannot be approved
    under it: every report groups by expense type, so a payment filed under
    nothing is a line in the accounts nobody can explain. A person supplies
    one, and then decides the claim themselves.

    The gate itself, and the fact that saving an answer no longer re-decides
    anything, are held in test_a_human_decides_it_themselves.py. What is left
    here is the endpoint that records the answer.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            self.auth = h.read()
        self.review = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]
        self.retype = self.auth.split("def _claim_retype(", 1)[1].split("\ndef ", 1)[0]
        with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_approval_is_refused_without_a_configured_type(self):
        self.assertIn("Set an expense type first.", self.review)
        self.assertIn("if chosen not in known:", self.review)

    def test_rejecting_one_is_still_allowed(self):
        # "Nothing covers this" is a perfectly good reason to refuse a claim,
        # and forcing a type on it first would be make-believe.
        guard = self.review.split("known = set(policy.expense_type_ids(",
                                  1)[1].split("origin)", 1)[0]
        self.assertNotIn("rejected", guard)

    def test_a_reviewer_can_set_the_type_and_it_is_recorded(self):
        self.assertIn("answered_expense_type = :t", self.retype)
        # Conditional on it being audited, so a claim mid-read is not clobbered.
        self.assertIn('ConditionExpression="#s = :audited"', self.retype)

    def test_only_a_reviewer_may_set_it(self):
        # It decides how the claim is reported and which budget pays it.
        self.assertIn('not runs_the_org(acting)', self.retype)

    def test_an_invented_type_is_refused(self):
        self.assertIn("Choose one of the configured expense types.", self.retype)

    def test_a_settled_claim_cannot_be_retyped(self):
        self.assertIn('item.get("outcome") == "settled"', self.retype)

    def test_the_console_withholds_approve_and_says_why(self):
        self.assertIn("const typeOk = rules.types.some(t => t.enabled && t.id === w.type);",
                      self.app)
        block = self.app.split(
            "const choices = (!typeOk || unsaved || needsGroup || unreadable)",
            1)[1].split(";", 1)[0]
        self.assertIn("Reject", block)
        self.assertNotIn("Approve as reviewed", block.split("[[", 1)[0] + block.split("]]", 1)[0])

    def test_approve_also_waits_for_an_unsaved_change_to_be_saved(self):
        # Everything above the buttons is a preview the moment a reviewer
        # touches a control; the server decides against what was written down.
        self.assertIn("const unsaved = !isPristine(sub, w);", self.app)
        self.assertIn("Save it before approving.", self.app)

    def test_the_unsaved_message_wins_over_the_requirement(self):
        # A reviewer who has just set the type is told to save it, not told to
        # set it. Being handed back an instruction you have visibly already
        # followed is how a product loses trust in everything else it says.
        note = self.app.split("s.textContent = unsaved", 1)[1]
        self.assertLess(note.index("Save it before approving"),
                        note.index("Set an expense type above"))

    def test_the_note_names_the_change_rather_than_announcing_one(self):
        # "You have unsaved changes" makes a reviewer hunt for what they
        # touched. Naming it removes the hunting.
        self.assertIn("Expense type \u2192 ", self.app)
        self.assertIn("Currency \u2192 ", self.app)
        self.assertIn("not saved yet", self.app)

    def test_one_save_at_a_time_and_the_flag_says_so(self):
        # The state cannot live on the button: the control is redrawn on every
        # render, so the element in flight is not the element that comes back.
        fn = self.app.split("async function saveAnswers()", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("if (!sub || savingOf) return;", fn)
        self.assertIn("savingOf = sub.id;", fn)
        self.assertIn("savingOf = null;", fn)
        self.assertIn("save.disabled = savingOf === sub.id;", self.app)

    def test_the_flag_is_cleared_on_every_path(self):
        # Left set by a network error, it is a button that never works again.
        fn = self.app.split("async function saveAnswers()", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(fn.index("catch"), fn.index("savingOf = null;"))

    def test_both_controls_call_the_same_function_directly(self):
        # The panel's own copy stays hidden; the visible one is in the
        # decision row, where a reviewer is looking once they have used the
        # dropdowns.
        block = self.app.split("if (retype) retype.hidden =", 1)[1].split(";", 1)[0]
        self.assertIn("true", block)
        self.assertIn('save.id = "retype-inline"', self.app)
        self.assertIn('$("retype-go").addEventListener("click", saveAnswers);',
                      self.app)

    def test_the_changed_check_still_decides_whether_saving_is_offered(self):
        self.assertIn("const unsaved = !isPristine(sub, w);", self.app)
        frozen = self.app.split("const frozen =", 1)[1].split(";", 1)[0]
        # Every reason a control is dead belongs in this one expression - a
        # second pass that assigns to the same controls writes `false` back
        # over it, which is how a settled claim came to offer an editable
        # Expense type again.
        self.assertIn("paid || unread || saving || !reviewingHere()", frozen)

    def test_the_route_is_wired(self):
        with open(os.path.join(ROOT, "expensifyai", "stack.py"), encoding="utf-8") as h:
            stack = h.read()
        self.assertIn('claim_res.add_resource("retype").add_method("POST", auth_integration)',
                      stack)
        # And the one beside it kept its method.
        self.assertIn('claim_res.add_resource("answer").add_method("POST", auth_integration)',
                      stack)
        self.assertIn('path.endswith("/claim/retype")', self.auth)
        gate = self.auth.split("if not any(path.endswith(p) for p in", 1)[1].split(")):", 1)[0]
        self.assertIn('"/claim/retype"', gate)


class ASettledClaimSaysSettled(unittest.TestCase):
    """A claim opened from the Settled tab announced itself as Approved.

    Three places said it: the badge, the line beside it, and the stamp in the
    actions row. All three described the reviewer's decision, which on a paid
    claim is a step two moves back - what happened to it is that the money
    went out.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as h:
            self.app = h.read()
        self.css = "\n".join(__import__("re").findall(
            r"<style[^>]*>(.*?)</style>", self.app, __import__("re").S))

    def test_the_line_under_the_heading_says_it(self):
        # The badge that used to carry this is gone - it printed the engine's
        # verdict, which on a queued claim is usually "approved". What states
        # a claim's actual position is the route line, in words, with the
        # person and the time.
        block = self.app.split('$("detail-route").textContent = stage', 1)[1] \
                        .split(";", 1)[0]
        self.assertIn("settledBy(sub)", block)
        self.assertIn("decision.by", block)
        self.assertIn("sent back by", block)
        # The verdict itself moved one element along, into a tag beside this
        # line, so that the one state meaning "nothing more to do here" does
        # not look like every other route line. The sentence no longer repeats
        # the word the tag carries.
        tag = self.app.split("const tagWord = stage", 1)[1].split(";", 1)[0]
        self.assertIn("decidedAs(decision.action)", tag)

    def test_no_label_can_contradict_the_claim_s_position_any_more(self):
        # The badge sat at the top of a claim in Pending settlement with
        # "Approved by Riyad Rasheed" beside it - two labels on one screen
        # saying opposite things, the stale one in the largest type - and at
        # the top of a queued claim reading APPROVED above an Approve button.
        # It was patched twice and then removed: there is one statement of a
        # claim's position now, and it is the route line.
        # The map the badge used to colour itself by. Named exactly, because
        # `DECIDED_BY` is a different thing that does survive.
        self.assertNotIn("const DECIDED = {", self.app)
        self.assertNotIn('id="verdict-badge"', self.app)
        self.assertEqual(1, self.app.count('$("detail-route").textContent'))

    def test_a_decision_attributed_to_a_person_says_reviewed(self):
        # "Approved by Riyad Rasheed" beside a claim in Pending settlement
        # reads as though Riyad paid it. Approval and settlement are two acts
        # by two people on two days, and this line sits directly above
        # "owed to Arjun" and a Record payment button.
        self.assertIn('const DECIDED_BY = { Approved: "Reviewed" };', self.app)
        # The tag says REVIEWED; the line beside it names who and when.
        self.assertIn("decidedAs(decision.action).toUpperCase()", self.app)
        self.assertIn("`by ${personName(decision.by)} \u00b7 ${decision.at}`",
                      self.app)

    def test_but_the_stored_action_keeps_the_server_s_word(self):
        # `decision.action` is the key REVIEW_ACTION maps back to the server's
        # vocabulary. Renaming a value because of how one line displays it is
        # how the console and the server stop agreeing about what happened.
        self.assertIn("const REVIEW_ACTION = { Approved: \"approved\"", self.app)
        self.assertIn('approved: "Approved"', self.app)

    def test_a_part_payment_is_not_called_settled(self):
        fn = self.app.split("const settlementStage =", 1)[1].split("};", 1)[0]
        self.assertIn('"Part settled"', fn)
        self.assertIn("paid < owed", fn)

    def test_the_stamp_leads_with_the_settlement(self):
        block = self.app.split("const stageNow = settlementStage(sub);", 1)[1]
        block = block.split("abox.appendChild(s);", 1)[0]
        self.assertIn("✓ ${stageNow}", block)

    def test_the_approval_is_kept_beside_it_not_replaced(self):
        # Who released the money and who decided it could be released are
        # different people on different days, and an audit wants both.
        block = self.app.split("const stageNow = settlementStage(sub);", 1)[1]
        block = block.split("abox.appendChild(s);", 1)[0]
        self.assertIn("decidedAs(decision.action).toLowerCase()", block)

    def test_a_settled_claim_with_no_human_decision_still_reports(self):
        # An agent-cleared claim that was then paid has no `decision` at all.
        self.assertIn("if (decision || settlementStage(sub)) {", self.app)

    def test_settlement_refuses_rather_than_reopens(self):
        # Reopen was here, and it undid the approval - the claim went back to
        # the review queue. Wrong instrument for what stops a payment at this
        # stage: a bill that never arrived, or paper that does not match, are
        # not judgments a reviewer can remake, because neither is about the
        # claim as submitted.
        self.assertNotIn('decide(sub, "Reopen"', self.app)
        self.assertIn('if (mayReview && !isPaid(sub) && decision && decision.action !== "Rejected") {',
                      self.app)
        self.assertIn('openReasonForm(sub, "settle_rejected", "Rejected")', self.app)


class TheControlsOnAClosedClaimAreInert(unittest.TestCase):
    """Four controls, each disabled on its own terms.

    So a claim the agent cleared and finance then paid still offered an
    editable Covers and Currency: the claim was closed and two of its four
    controls did not know. Currency was worse - interactive on every claim
    while writing nothing anywhere, which is the defect the Source dropdown
    had: a change you can make, watch take effect, and lose.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as h:
            self.app = h.read()
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            self.auth = h.read()
        self.retype = self.auth.split("def _claim_retype(", 1)[1].split("\ndef ", 1)[0]

    def test_one_rule_governs_every_control(self):
        self.assertIn('["headcount", "src", "etype", "ccy", "claim-group"].forEach',
                      self.app)
        self.assertIn("el.disabled = frozen", self.app)

    def test_money_having_moved_freezes_them(self):
        # Not the decision - the payment. A claim approved and waiting to be
        # paid had every control dead, so finance opening it to settle could
        # see a wrong cost centre and not touch it; the only route was Reopen,
        # which undoes a decision somebody deliberately made. Once a transfer
        # is recorded, changing what it was for would leave the payment filed
        # against a claim that no longer describes it, and that stays shut.
        frozen = self.app.split("const frozen =", 1)[1].split(";", 1)[0]
        self.assertIn("paid", frozen)
        self.assertIn("const paid = isPaid(sub) || closed;", self.app)

    def test_somebody_who_is_not_reviewing_cannot_edit_them(self):
        frozen = self.app.split("const frozen =", 1)[1].split(";", 1)[0]
        self.assertIn("!reviewingHere()", frozen)

    def test_a_currency_correction_now_persists(self):
        # A dollar invoice read as rupees is three orders of magnitude out,
        # and the figure a reviewer approves has to be in the currency that
        # was actually spent.
        #
        # It no longer re-runs the engine - nothing does, once a claim is in
        # front of a person - so what is asserted is that the correction is
        # written down and validated, not that a second audit consumes it.
        self.assertIn("answered_currency = :c", self.retype)
        self.assertIn("money.normalise(", self.retype)
        self.assertIn("That is not a currency we support.", self.retype)

    def test_changing_neither_is_refused(self):
        self.assertIn("Nothing to change", self.retype)

    def test_the_type_is_checked_against_the_organisation_s_own_policy(self):
        # Checking `DEFAULT_RULES` would refuse a type the owner added.
        self.assertIn("policy.expense_type_ids(policy.rules_for(org))", self.retype)


class AnApprovalDoesNotMakeTheClaimVanish(unittest.TestCase):
    """Approve a blocked claim and it appeared in neither list.

    It has a decision, so it leaves the review queue. Its value comes from
    `approved_total`, which only the server computes - and the review reply did
    not carry it, so the console fell back to the engine's figure. That figure
    is nothing, precisely because a finding was blocking the claim. Worth zero,
    it was dropped from the settlement list too.

    A reviewer pressed Approve and watched the claim disappear until they
    reloaded the page.
    """

    def setUp(self):
        for name, path in (("app", "../PORTAL/app.html"), ("auth", "lambda_src/auth.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_the_server_says_what_the_approval_released(self):
        review = self.auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"approved_total": approved_total,', review)

