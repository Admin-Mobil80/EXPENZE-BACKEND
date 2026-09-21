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
        self.assertIn("if action in DECIDING_ACTIONS and not may_review(acting):",
                      self.review)

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

    def test_setting_the_expense_type_is_a_review_act(self):
        retype = self.auth.split("def _claim_retype(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if not may_review(acting):", retype)
        self.assertIn("Only an owner or administrator can set the expense type.",
                      retype)

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

    def test_the_group_control_defers_to_the_payment_form(self):
        # It hides where the settlement form asks the same thing - which is a
        # question about paying, not about reviewing.
        self.assertIn("const payingHere = settlingHere() && !isPaid(sub)",
                      self.app)

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
