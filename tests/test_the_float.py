"""Cash advances: the float a handful of people hold and spend from.

Housekeeping and the canteen are not bought on anybody's own card. A few
people are handed cash, they spend it through the month, and they submit the
receipts here like everybody else. What was missing was the other half of that
arrangement: how much of the company's money each of them is holding.

A float holder's claim reaches Pending settlement like anybody's. What differs
is the choice finance makes there, between two genuinely different acts that
the single word "settle" hides:

**From the float.** No money moves. They already spent the company's cash and
this is the company accounting for it, so the advance is drawn down. Finance
replenishes by advancing more.

**A separate payout.** An ordinary reimbursement - a bill too large for the
float, or one they paid personally. It must leave the float alone, or the next
replenishment is calculated against a balance that never moved.

    still out = advanced - returned - settled from the float
    in hand   = still out - claims spent but not yet settled

The second is a worst case while claims are undecided, because none of them
has been assigned to the float yet. That is why the red flag hangs on the
first: `still out` moves only when a claim has actually been settled against
the advance, so negative there means somebody really has spent more of the
company's money than they were given. A negative `in hand` may be nothing but
a laptop they paid for personally, and it resolves the moment finance chooses.

The ledger is append-only. A stored balance drifts from the events that
produced it, and the events are what somebody asks about when the money does
not add up - so a correction is another movement, never an edit.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from decimal import Decimal

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


def out_and_hand(advanced=0, returned=0, from_float=0, pending=0, paid_out=0):
    """The two figures the server computes, spelled out.

    `paid_out` is accepted and then ignored, on purpose: a separate payout
    must make no difference to either figure, and a signature that could not
    express one could not test that.
    """
    d = (lambda x: Decimal(str(x)))
    out = d(advanced) - d(returned) - d(from_float)
    return out, out - d(pending)


class TheFloatIsDrawnDownByWhatIsSettledAgainstIt(unittest.TestCase):
    """Kasthuri, the canteen float, in order."""

    def test_an_advance_is_what_is_out_with_them(self):
        self.assertEqual((Decimal("20000"), Decimal("20000")),
                         out_and_hand(advanced=20000))

    def test_spending_it_does_not_move_the_advance(self):
        # The cash has left her hands, but the company has not accounted for
        # it yet - so what is out is unchanged and what she carries is less.
        self.assertEqual((Decimal("20000"), Decimal("15000")),
                         out_and_hand(advanced=20000, pending=5000))

    def test_settling_from_the_float_draws_it_down(self):
        self.assertEqual((Decimal("15000"), Decimal("15000")),
                         out_and_hand(advanced=20000, from_float=5000))

    def test_replenishing_is_simply_another_advance(self):
        self.assertEqual((Decimal("20000"), Decimal("20000")),
                         out_and_hand(advanced=31000, from_float=11000))

    def test_handing_cash_back_reduces_it(self):
        self.assertEqual((Decimal("10000"), Decimal("10000")),
                         out_and_hand(advanced=20000, returned=10000))

    def test_a_withdrawn_claim_never_counts(self):
        view = read("lambda_src/auth.py").split("def _advances_view(", 1)[1].split(
            "\ndef ", 1)[0]
        self.assertIn('if str(claim.get("review_action") or "") == "withdrawn":',
                      view)


class ABillTooLargeForTheFloat(unittest.TestCase):
    """The case that made this a choice rather than a rule."""

    def test_a_separate_payout_leaves_the_float_alone(self):
        # 50,000 she paid personally, against a 20,000 float. Reimbursed on
        # top; the float is still 20,000, which is the whole point.
        self.assertEqual((Decimal("20000"), Decimal("20000")),
                         out_and_hand(advanced=20000, paid_out=50000))

    def test_while_undecided_it_only_dents_the_in_hand_figure(self):
        self.assertEqual((Decimal("20000"), Decimal("-30000")),
                         out_and_hand(advanced=20000, pending=50000))

    def test_so_the_red_flag_hangs_on_the_other_one(self):
        app = read("../PORTAL/app.html")
        fn = app.split("function renderAdvances() {", 1)[1].split("\n}", 1)[0]
        self.assertIn('const tone = stillOut < 0 ? "var(--bad)"', fn)
        self.assertIn(': inHand < 0 ? "var(--warn)"', fn)

    def test_a_real_overdraw_is_still_red(self):
        # 8,000 settled against a 5,000 advance: genuinely more of the
        # company's money spent than was given.
        self.assertEqual((Decimal("-3000"), Decimal("-3000")),
                         out_and_hand(advanced=5000, from_float=8000))


class TheChoiceIsRecordedOnTheClaim(unittest.TestCase):

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.fn = self.auth.split("def _claim_outcome(", 1)[1].split(
            "\ndef ", 1)[0]
        self.view = self.auth.split("def _advances_view(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_the_settlement_stores_where_the_money_came_from(self):
        self.assertIn('"source": "float" if str(body.get("source", "")) == "float"',
                      self.fn)
        self.assertIn("outcome_source = :src", self.fn)

    def test_anything_unsaid_is_a_payout(self):
        # Every claim settled before this shipped, and every claim by somebody
        # holding no float. Defaulting the other way would draw down floats
        # that were never touched.
        self.assertIn('else "payout"', self.fn)
        self.assertIn(':src": claim.get("source") or "payout"',
                      self.fn.replace('"', '"'))

    def test_only_a_float_settlement_moves_the_balance(self):
        self.assertIn('if settled and str(claim.get("outcome_source") or "") == "float":',
                      self.view)
        self.assertIn('who["from_float"] += _as_decimal(claim.get("outcome_paid"))',
                      self.view)
        self.assertIn('out = who["advanced"] - who["returned"] - who["from_float"]',
                      self.view)

    def test_a_separate_payout_is_reported_but_not_subtracted(self):
        # Finance will ask why a holder's spend and their float do not line
        # up, and this is the answer - but it is not in the arithmetic.
        self.assertIn('who["paid_out"] +=', self.view)
        formula = self.view.split('out = who["advanced"]', 1)[1].split("\n", 1)[0]
        self.assertNotIn("paid_out", formula)

    def test_an_unsettled_claim_is_in_flight_not_drawn_down(self):
        self.assertIn('who["pending"] += _as_decimal(', self.view)
        self.assertIn('"in_hand": str(out - who["pending"])', self.view)


class TheLedgerIsAppendOnly(unittest.TestCase):

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.fn = self.auth.split("def _advance_record(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_a_movement_is_written_never_updated(self):
        self.assertIn("_advances.put_item(Item=row)", self.fn)
        self.assertNotIn("update_item", self.fn)

    def test_the_sort_key_is_milliseconds(self):
        # Two advances recorded in one sitting must not collide on the key and
        # silently overwrite each other.
        self.assertIn('"ts": int(time.time() * 1000),', self.fn)

    def test_both_directions_are_recorded(self):
        self.assertIn('ADVANCE_KINDS = ("advance", "return")', self.auth)
        self.assertIn("if kind not in ADVANCE_KINDS:", self.fn)

    def test_who_recorded_it_is_kept(self):
        for field in ('"by": actor', '"by_name"', '"at": now'):
            self.assertIn(field, self.fn)

    def test_it_reaches_the_audit_log(self):
        self.assertIn('"advance paid" if kind == "advance" else "advance returned"',
                      self.fn)


class OnlyTheRightPeopleAndTheRightAmounts(unittest.TestCase):

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.fn = self.auth.split("def _advance_record(", 1)[1].split(
            "\ndef ", 1)[0]
        self.view = self.auth.split("def _advances_view(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_recording_is_gated_like_a_settlement(self):
        # It is money leaving the company, and it is the same people who
        # record money leaving the company.
        self.assertIn("if not runs_the_org(acting):", self.fn)

    def test_so_is_reading_it(self):
        # A float balance is a statement about a colleague's cash.
        self.assertIn("if not runs_the_org(acting):", self.view)

    def test_the_holder_must_be_in_this_organisation(self):
        self.assertIn('str(holder.get("org_id") or "") != org_id', self.fn)

    def test_no_more_cash_to_somebody_taken_off_the_roll(self):
        self.assertIn('if str(holder.get("status") or "active") == "removed":',
                      self.fn)
        # What they hand back is still recordable - the only way to close out
        # a float when somebody leaves.
        self.assertIn("Record what they hand back", self.fn)

    def test_zero_and_negative_amounts_are_refused(self):
        # A negative advance is a return, and it has its own kind. Two
        # spellings of one movement would make the ledger ambiguous.
        self.assertIn("if amount <= 0:", self.fn)

    def test_the_routes_are_wired(self):
        stack = read("expensifyai/stack.py")
        self.assertIn('advances_res = auth.add_resource("advances")', stack)
        self.assertIn('advances_res.add_resource("record").add_method("POST", auth_integration)',
                      stack)
        self.assertIn('if path.endswith("/advances/record"):', self.auth)
        gate = self.auth.split("if not any(path.endswith(p) for p in", 1)[1].split(
            ")):", 1)[0]
        self.assertIn('"/advances"', gate)

    def test_the_table_is_retained(self):
        stack = read("expensifyai/stack.py")
        block = stack.split("advances_table = dynamodb.Table(", 1)[1].split(
            ")\n", 1)[0]
        self.assertIn('table_name="Expenze-Advances"', block)
        self.assertIn("removal_policy=RemovalPolicy.RETAIN", block)


class HoldingAFloatIsDerivedNotDeclared(unittest.TestCase):
    """No flag to set, none to forget, none that can disagree with the money."""

    def test_a_holder_is_somebody_who_was_handed_cash(self):
        view = read("lambda_src/auth.py").split("def _advances_view(", 1)[1].split(
            "\ndef ", 1)[0]
        self.assertIn("holders.setdefault(email, {", view)

    def test_there_is_no_membership_flag_for_it(self):
        auth = read("lambda_src/auth.py")
        for flag in ("holds_float", "float_holder", "is_float"):
            self.assertNotIn(flag, auth)


class TheConsoleOffersTheChoiceWhereTheMoneyIsPaid(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function settleForm(c, ccy) {", 1)[1].split(
            "\n  return td;", 1)[0]

    def test_the_control_exists(self):
        self.assertIn('<select id="sf-source">', self.app)
        self.assertIn('<option value="float">Their float</option>', self.app)

    def test_it_is_hidden_for_somebody_with_no_float(self):
        # A dropdown with one real option is furniture.
        self.assertIn('<div class="sf" id="sf-source-wrap" hidden>', self.app)
        self.assertIn("if (float0 && srcWrap) {", self.fn)

    def test_it_defaults_to_whichever_is_true(self):
        # Inside the float is the ordinary case; a bill beyond it cannot come
        # out of money they do not have.
        self.assertIn("const fits = c.outstanding <= inHand;", self.fn)
        self.assertIn('sel.value = fits ? "float" : "payout";', self.fn)

    def test_it_says_what_each_choice_does_to_the_balance(self):
        self.assertIn("No money moves.", self.fn)
        self.assertIn("this draws it down to", self.fn)
        self.assertIn("Paid to them on top of the float", self.fn)

    def test_the_choice_reaches_the_server(self):
        self.assertIn('source: (td.querySelector("#sf-source") || {}).value || "payout"',
                      self.app)
        self.assertIn('source: detail.source || "payout",', self.app)


class TheFloatTabShowsItsWorking(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function renderAdvances() {", 1)[1].split(
            "\n}", 1)[0]

    def test_the_tab_exists_and_is_a_place_to_work(self):
        self.assertIn('advances: "Advances"', self.app)
        self.assertIn('"settled", "advances", "reports"', self.app)

    def test_every_figure_the_balance_is_made_of_is_a_column(self):
        # A balance somebody cannot reconstruct is one they will not act on.
        head = self.app.split('<tbody id="adv-list">', 1)[0].rsplit("<thead>", 1)[1]
        for col in ("Advanced", "Settled from it", "Still out", "In flight",
                    "In hand"):
            self.assertIn(f">{col}<", head)

    def test_the_formula_is_printed_on_the_page(self):
        self.assertIn("Still out = advanced", self.fn)

    def test_the_two_acts_are_explained_where_they_are_chosen(self):
        self.assertIn("draws it down and moves no ", self.fn)

    def test_the_list_is_worst_first(self):
        view = read("lambda_src/auth.py").split("def _advances_view(", 1)[1].split(
            "\ndef ", 1)[0]
        self.assertIn('people.sort(key=lambda p: _as_decimal(p["held"]))', view)

    def test_recording_one_is_confirmed_first(self):
        fn = self.app.split("async function recordAdvance() {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("if (!confirm(", fn)
        self.assertIn("correction is another movement, and both stay on the "
                      "ledger.", fn)

    def test_a_removed_person_is_not_offered(self):
        form = self.app.split("function paintAdvanceForm() {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn('PEOPLE.filter(p => p.status !== "removed")', form)

    def test_read_only_for_everybody_else(self):
        self.assertIn("The float is a Finance Executive's to record.", self.fn)


if __name__ == "__main__":
    unittest.main()
