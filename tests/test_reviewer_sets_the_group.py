"""A reviewer can set the cost centre, at review and not only at settlement.

It was settable on the settlement form alone, which is the wrong moment twice
over. A claim can be held *on* the group - somebody in several groups, on a
bill that names none - so the person unblocking it has to be able to answer
it; and a group the agent inferred from the paper is correctable only by
whoever reads the bill, who is the reviewer, not the person paying weeks later.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def src(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TheControlIsOnTheClaimPage(unittest.TestCase):

    def setUp(self):
        self.app = src("..", "PORTAL", "app.html")

    def test_it_sits_with_the_other_two_things_a_reviewer_can_change(self):
        controls = self.app.split('<div class="controls">', 1)[1].split("</div>", 6)[0]
        self.assertIn('id="claim-group"', self.app)
        self.assertIn('id="etype"', controls)

    def test_it_is_hidden_for_an_organisation_with_no_groups(self):
        # An empty dropdown is a question about nothing.
        self.assertIn("gfield.hidden = !groups.length || settlingHere;", self.app)

    def test_and_hidden_again_where_the_payment_form_asks_the_same_thing(self):
        # Two dropdowns over one value six inches apart is a question about
        # which of them wins. At review this is the only one; at settlement it
        # belongs beside the amount and the reference.
        self.assertIn("const settlingHere = reviewingHere() && !isPaid(sub)", self.app)
        self.assertIn('id="sf-group"', self.app)

    def test_it_freezes_with_every_other_control(self):
        # One rule, not its own. A settled claim must not offer an editable
        # anything.
        self.assertIn('["headcount", "src", "etype", "ccy", "claim-group"].forEach',
                      self.app)

    def test_changing_it_counts_as_a_change_worth_saving(self):
        changed = self.app.split("const changed = !frozen", 1)[1].split(";", 1)[0]
        self.assertIn("w.group", changed)

    def test_it_says_what_tagged_the_claim(self):
        # An attribution nobody can account for is one nobody corrects with
        # any confidence.
        self.assertIn("The bill is made out to this group's tax ID.", self.app)
        self.assertIn("The bill is made out to this group by name.", self.app)


class TheServerStoresIt(unittest.TestCase):

    def setUp(self):
        self.retype = src("lambda_src", "auth.py") \
            .split("def _claim_retype(", 1)[1].split("\ndef ", 1)[0]

    def test_clearing_a_group_is_a_change_and_not_a_no_op(self):
        # Absent means the reviewer never touched the control; empty means
        # they cleared one the agent got wrong. Collapsing the two makes a
        # wrong inference uncorrectable.
        self.assertIn('group_given = "group_id" in body', self.retype)
        self.assertIn("if group_given:", self.retype)

    def test_an_unknown_group_is_refused(self):
        self.assertIn("Choose one of your configured groups.", self.retype)

    def test_a_reviewer_s_answer_closes_the_question(self):
        # The auditor only consults the bill while the question is open, so a
        # reviewer's answer must not be overruled on the next re-audit.
        self.assertIn('"set_by_reviewer" if group_id else "unset"', self.retype)

    def test_it_is_written_into_the_audit_log(self):
        self.assertIn("group {item.get('group_id') or 'unset'}", self.retype)

    def test_a_request_changing_only_the_group_is_accepted(self):
        self.assertIn("if not expense_type and not currency and not group_given:",
                      self.retype)


class NobodyIsAskedWhichGroup(unittest.TestCase):

    def test_the_queue_no_longer_says_a_submitter_was_asked(self):
        app = src("..", "PORTAL", "app.html")
        chip = app.split("function groupChip(", 1)[1].split("\n}", 1)[0]
        # What it returns, not the comment above it explaining what it stopped
        # returning - which names the phrase it removed.
        returned = " ".join(re.findall(r"return (.+);", chip))
        self.assertNotIn("awaiting their reply", returned)
        self.assertIn("not set", returned)

    def test_an_unresolved_group_stops_the_claim_for_a_person(self):
        worker = src("lambda_src", "auditor_worker.py")
        self.assertIn('"code": "group_not_set"', worker)
        self.assertIn("Set it before approving.", worker)


if __name__ == "__main__":
    unittest.main()
