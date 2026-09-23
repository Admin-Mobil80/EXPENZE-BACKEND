"""A finance executive decided claims and then paid them.

`runs_the_org` - rank at or above finance - gated both acts, so the person
moving the money was also the person releasing it. In a small company that is
one person doing both halves of the only control the product has, and nothing
downstream would notice: the audit log would show the same name on the
approval and the payment, which is exactly what an auditor looks for and
exactly what the product was arranging.

So they are two bars now:

    may_review    owner, administrator            decide a claim
    runs_the_org  owner, administrator, finance   record the payment

One review action stays on the lower bar, and deliberately. Sending a claim
*back* for review - `disputed` - is a finance executive saying they do not
agree with what the agent cleared, and it decides nothing: it hands the claim
to the people whose job deciding is. Taking it away would leave finance with
pay-it-or-refuse-it on a claim they doubt, which is the exact position that
action was built to end.

In the console the same split is two questions asked of one screen -
`reviewingHere` and `settlingHere` - because two roles act on a claim from it.
Gating the payment controls on the review question would have locked a finance
executive out of the one job that is theirs, which is the mistake this file
exists to keep made once.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
for _k, _v in (("AWS_DEFAULT_REGION", "ap-southeast-1"), ("USERS_TABLE", "u"),
               ("ORGS_TABLE", "o"), ("INTAKE_TABLE", "t"), ("AUTH_TABLE", "a"),
               ("ADMINS_TABLE", "d"), ("EXPENSES_TABLE", "e"),
               ("ADVANCES_TABLE", "adv"),
               ("SESSION_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:y")):
    os.environ.setdefault(_k, _v)

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class TheTwoBars(unittest.TestCase):

    def setUp(self):
        import auth
        self.auth = auth

    def test_deciding_stops_at_administrator(self):
        for role in ("owner", "admin"):
            self.assertTrue(self.auth.may_review(role), role)
        for role in ("finance", "staff"):
            self.assertFalse(self.auth.may_review(role), role)

    def test_paying_goes_one_rank_lower(self):
        for role in ("owner", "admin", "finance"):
            self.assertTrue(self.auth.runs_the_org(role), role)
        self.assertFalse(self.auth.runs_the_org("staff"))

    def test_the_review_bar_is_the_higher_of_the_two(self):
        # Stated as a relation rather than two constants, so moving either one
        # cannot quietly invert them.
        self.assertGreater(self.auth.RANK["admin"], self.auth.RANK["finance"])

    def test_a_role_nobody_recognises_decides_nothing(self):
        for junk in ("administrator", "reviewer", None, {}, ""):
            self.assertFalse(self.auth.may_review(junk), repr(junk))


class WhichActionsSitOnWhich(unittest.TestCase):

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.review = self.auth.split("def _claim_review(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_approve_reject_and_reopen_decide(self):
        self.assertIn('DECIDING_ACTIONS = ("approved", "rejected", "reopened")',
                      self.auth)

    def test_and_are_gated_on_the_higher_bar(self):
        self.assertIn('if action in DECIDING_ACTIONS and action != "rejected" '
                      'and not may_review(acting):', self.review)

    def test_refusing_to_pay_is_not_the_same_act_as_refusing_the_claim(self):
        # One word, two acts, two roles, two moments. At review a rejection is
        # the decision - what the company owes - and stays at owner or
        # administrator. Once the claim has cleared, the judgment is made and
        # the submitter has been told; what is left is whether the money goes,
        # and "the bill never arrived" is finance's reason to have.
        #
        # The same shape as `disputed` on the lower bar: neither lets a finance
        # executive decide what the company owes.
        self.assertIn('if action == "rejected" and not runs_the_org(acting):',
                      self.review)
        self.assertIn('if action == "rejected" and not may_review(acting)',
                      self.review)
        self.assertIn("not policy.awaiting_payment(item)", self.review)

    def test_the_permission_is_read_off_the_claim_not_off_a_screen(self):
        # The console picks which button to draw from where the reader is
        # standing, which is right for a button and no use as a permission: a
        # console is a page somebody can have open from before this shipped.
        self.assertNotIn("claimFrom", self.auth)

    def test_sending_one_back_is_not_deciding(self):
        # It hands the claim to a reviewer rather than resolving it, which is
        # why finance may do it and may not do the others.
        self.assertNotIn("disputed", self.auth.split(
            "DECIDING_ACTIONS = ", 1)[1].split("\n", 1)[0])
        self.assertIn("action not in DECIDING_ACTIONS", self.review)
        self.assertIn("not runs_the_org(acting)", self.review)

    def test_withdrawing_is_still_the_claimant_s(self):
        # Neither bar applies: it is your own claim, and only your own.
        self.assertIn('item.get("submitted_by", "")).lower() != actor.lower()',
                      self.review)

    def test_setting_the_expense_type_is_not_a_review_act(self):
        # It classifies rather than prices. On a claim that has cleared, the
        # amount is fixed at `approved_total`, so re-tagging moves the claim
        # between reports and moves no money - and finance, who is asked what
        # the month was spent on, had to go back to a reviewer for a dropdown.
        retype = self.auth.split("def _claim_retype(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if not runs_the_org(acting):", retype)
        self.assertIn("change a claim's expense type.", retype)

    def test_but_the_currency_still_is(self):
        # Caps are per currency and the payout converts at the rate stamped
        # for that pair, so changing it changes what somebody is paid.
        retype = self.auth.split("def _claim_retype(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('if "currency" in body:', retype)
        self.assertIn("Only an owner or administrator can change the ", retype)

    def test_and_finance_may_only_touch_a_claim_that_has_cleared(self):
        # Before that it is the reviewer's claim, and its type is part of what
        # they are deciding.
        retype = self.auth.split("def _claim_retype(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if not policy.awaiting_payment(item):", retype)

    def test_recording_a_payment_is_not(self):
        outcome = self.auth.split("def _claim_outcome(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("not runs_the_org(acting)", outcome)
        self.assertNotIn("may_review", outcome)

    def test_the_refusals_name_who_can(self):
        self.assertIn('"finance executive can send it back for review."',
                      self.review.replace("\n", " "))


class TheConsoleAsksTheSameTwoQuestions(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_two_predicates_over_one_screen_test(self):
        review = self.app.split("function reviewingHere() {", 1)[1].split(
            "\n}", 1)[0]
        settle = self.app.split("function settlingHere() {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("can.review() && aboutSomebodyElsesClaim()", review)
        self.assertIn("can.seeQueue() && aboutSomebodyElsesClaim()", settle)

    def test_the_payment_controls_hang_on_the_lower_one(self):
        # The mistake this file exists to keep made once: `reviewingHere`
        # gated the Record payment button, so raising the review bar would
        # have locked finance out of settling.
        self.assertIn("renderSettleOnClaim(sub, settlingHere());", self.app)
        fn = self.app.split("function renderSettleOnClaim(sub, maySettle) {",
                            1)[1].split("\n}", 1)[0]
        self.assertIn("if (!c || !maySettle || c.outstanding <= 0) return;", fn)

    def test_the_decision_controls_hang_on_the_higher_one(self):
        self.assertIn("const mayReview = reviewingHere();", self.app)

    def test_finance_holds_the_expense_type_and_nothing_else_on_that_row(self):
        # It classifies; the rest price. On a claim that has cleared the
        # amount is fixed, so re-tagging moves it between reports and moves no
        # money - and finance is who gets asked what the month was spent on.
        self.assertIn("const classFrozen = busy || !(reviewingHere() || settlingHere());",
                      self.app)
        self.assertIn('["etype", "claim-group"].forEach', self.app)
        self.assertIn('["headcount", "src", "ccy"].forEach', self.app)

    def test_and_has_somewhere_to_record_it(self):
        # The Save button lives in the reviewer's branch, which finance never
        # reaches - so without this the control would come alive under them,
        # take a change, and offer nothing to write it down with.
        branch = self.app.split("} else if (!mayReview) {", 1)[1].split(
            "} else if (isPaid(sub)) {", 1)[0]
        self.assertIn("!isPristine(sub, w) && !classFrozen", branch)
        self.assertIn("save.addEventListener(\"click\", saveAnswers);", branch)

    def test_the_hint_no_longer_promises_a_re_check(self):
        # "A change to the type or the currency sends it back through the
        # policy engine" described the re-check, which was removed: a claim in
        # front of a person is decided by that person. The sentence outlived
        # the feature.
        self.assertNotIn("sends it back through the policy engine", self.app)
        self.assertNotIn("the verdict recomputes against the same rules", self.app)
        self.assertIn("not what is paid.", self.app)

    def test_the_group_control_is_the_only_one_and_it_persists(self):
        # The settlement form's copy wrote to the working copy and saved
        # nothing, and the button that opened it stopped being drawn once a
        # settlement could be refused for want of a group.
        self.assertIn("gfield.hidden = !groups.length;", self.app)
        self.assertNotIn('<select id="sf-group">', self.app)

    def test_a_finance_executive_is_told_why_the_queue_is_read_only(self):
        # A list of claims with no buttons and no explanation reads as a
        # fault in the product.
        fn = self.app.split("function renderQueue() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("const watching = can.seeQueue() && !can.review();", fn)
        self.assertIn("Deciding a claim is an ", fn)

    def test_and_on_a_claim_they_cannot_act_on(self):
        self.assertIn("Awaiting review by an owner or administrator.", self.app)

    def test_they_still_land_on_the_screen_that_is_theirs(self):
        self.assertIn('finance: "payments"', self.app)


if __name__ == "__main__":
    unittest.main()


class ASavedCorrectionIsVisibleToTheConsole(unittest.TestCase):
    """A claim nobody could approve, however many times they saved it.

    Mobil80-Exp-63: the agent read a handwritten bill as `not_covered`, a
    reviewer set it to Courier, the server stored it. The row then said
    "Expense type -> Courier - not saved yet. Save it before approving." after
    every save, and Approve never appeared.

    The save was working perfectly. `_submission_view` sent the console the
    *verdict's* expense type and only that, which was true for as long as
    saving a correction re-audited the claim - the engine ran again under the
    new type and wrote it back into the verdict. That re-check was removed
    deliberately, because a claim in front of a person is decided by that
    person, and this was not followed through: the console's working copy said
    Courier, the record it compared against said not_covered, and the two could
    never agree.

    `_claim_review` has always read the answered value when deciding whether a
    claim may be approved. The view is the same question about the same row.
    """

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.view = self.auth.split("def _submission_view(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_the_view_prefers_what_a_reviewer_answered(self):
        self.assertIn('"expense_type": (row.get("answered_expense_type")', self.view)
        self.assertIn('"currency": row.get("answered_currency") or verdict.get("currency", "")',
                      self.view)

    def test_and_still_falls_back_to_what_the_agent_read(self):
        # Every claim decided before a person touched it has no answer on it.
        self.assertIn('or verdict.get("expense_type")', self.view)
        self.assertIn('or receipt.get("expense_type", "")', self.view)

    def test_the_gate_and_the_view_read_the_same_field(self):
        # They disagreed, and the disagreement was invisible: the server would
        # have approved the claim on the type it was storing, while the console
        # refused to offer the button on the type it was being sent.
        # The check moved to settlement, and it reads the same field the view
        # does. They disagreed once, invisibly, and that is the point here.
        pay = self.auth.split("def _unready_to_pay(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('item.get("answered_expense_type")', pay)
        self.assertIn('row.get("answered_expense_type")', self.view)

    def test_nothing_re_audits_a_claim_on_save(self):
        # The reason this mattered. If saving still queued the claim, the
        # verdict would be rewritten and the view would have been right.
        retype = self.auth.split("def _claim_retype(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn(":queued", retype)
