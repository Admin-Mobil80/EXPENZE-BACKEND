"""A submitter's own list: two lines a row, and a status that says the whole thing.

The row was three lines before the status column had said anything - vendor,
then the receipt link, then the claim number, each starting a block of its
own - and the status it finally reached was a badge reading APPROVED with a
name beside it.

That badge leaves both of the reader's questions open. Approved by a person or
by the agent is one of them, and the marker answered it if you knew what a
hollow ring meant. The other it answered wrongly: on a row whose Settled
column is a dash, "approved" reads as finished business, when what it means is
that the claim has cleared review and the money has not moved yet.

So the identifying line collapses to one, and the status says where the claim
stands in the words the person waiting for the money uses, over who put it
there and that they reviewed it first.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def console() -> str:
    with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as fh:
        return fh.read()


class TheRowClosesAtTwoLines(unittest.TestCase):

    def setUp(self):
        self.app = console()
        self.fn = self.app.split("function renderMine() {", 1)[1].split("\n}", 1)[0]

    def test_what_identifies_the_row_shares_one_line(self):
        # Receipt link, document count and claim number, joined rather than
        # stacked. Each was its own block, which is where the third line came
        # from on every row of the table.
        self.assertIn('bits.join(" · ")', self.fn)
        for piece in ("bits.push(`<a class=\"rlink\"",
                      "if (docs > 1) bits.push(`${docs} documents`);",
                      "if (sub.reference) bits.push(esc(sub.reference));"):
            self.assertIn(piece, self.fn)

    def test_and_nothing_is_drawn_when_there_is_nothing_to_say(self):
        # An empty line still costs a line. A claim with no reference, one
        # document and no stored original has nothing under the vendor.
        self.assertIn('bits.length ? `<span class="rowmeta">', self.fn)

    def test_the_line_is_a_block_so_the_row_is_exactly_two(self):
        css = self.app.split(".rowmeta {", 1)[1].split("}", 1)[0]
        self.assertIn("display:block", css)

    def test_the_vendor_cell_no_longer_breaks_by_hand(self):
        # It used to open with a <br>, which is a line whether or not anything
        # follows it.
        cell = self.fn.split("tr.innerHTML = ", 1)[1].split("`</td>", 1)[0]
        self.assertNotIn("<br>", cell)


class TheStatusSaysWhereItStandsAndWhoDecided(unittest.TestCase):

    def setUp(self):
        self.app = console()
        self.fn = self.app.split("function renderMine() {", 1)[1].split("\n}", 1)[0]

    def test_a_claim_that_cleared_review_is_pending_settlement(self):
        # Not "Approved". That is the reviewer's word for the step they took,
        # and this list is read by the person waiting for the money.
        labels = self.app.split("const MINE_STAGE_LABEL = ", 1)[1].split(";", 1)[0]
        self.assertIn('approved: "Pending settlement"', labels)
        # Built from the shared map, so a stage added there cannot go unnamed
        # here - it would print `undefined` in the badge.
        self.assertIn("...STAGE_LABEL", labels)

    def test_the_reviewer_s_own_screens_keep_their_own_word(self):
        # "Approved" is correct where somebody has just approved something.
        shared = self.app.split("const STAGE_LABEL = {", 1)[1].split("};", 1)[0]
        self.assertIn('approved: "Approved"', shared)

    def test_the_row_uses_the_submitter_s_names(self):
        self.assertIn("MINE_STAGE_LABEL[st.stage]", self.fn)

    def test_it_says_the_decision_was_a_review_and_names_who_made_it(self):
        self.assertIn('"Reviewed &amp; approved by"', self.fn)
        self.assertIn("personName(st.by)", self.fn)

    def test_a_refusal_is_not_described_as_an_approval(self):
        self.assertIn('st.stage === "rejected" ? "Reviewed &amp; rejected by"', self.fn)

    def test_and_a_withdrawal_is_neither(self):
        # The submitter took it back; nobody reviewed anything.
        self.assertIn('st.stage === "withdrawn" ? "Withdrawn by"', self.fn)

    def test_nobody_is_named_while_it_is_still_in_review(self):
        # Because nobody has decided it. Naming the reviewer it happens to be
        # sitting with reads as a decision already taken.
        self.assertIn('st.stage === "in_review" ? "With Finance"', self.fn)

    def test_the_agent_and_a_person_stay_told_apart(self):
        # A hollow ring for the agent, a filled one for a person. It is the
        # distinction a submitter looks for first, and it survived the rewrite.
        self.assertIn('st.kind === "agent" ? "&#9673;" : "&#9679;"', self.fn)

    def test_the_byline_is_the_second_line_of_the_cell(self):
        # Same device as the vendor cell, so both columns close at two lines
        # and the row height is set by neither in particular.
        self.assertIn('`<span class="rowmeta">${said}</span>`', self.fn)


if __name__ == "__main__":
    unittest.main()
