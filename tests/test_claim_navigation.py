"""Working through a queue one claim at a time.

The review queue was a split pane: list on the left, the claim on the right.
That optimises for throughput - work the list without navigating - and a review
decision is not throughput work. It is a discrete act about one person's money,
and the split's failure mode was acting on the wrong claim: decide one and it
leaves the list, the right-hand pane silently swaps to another, and the screen
being read is now a different claim wearing the same furniture. The panels also
had half the width for a receipt photograph and a table of line items.

So the queue is a full-width list and a claim is a page, the same page Pending
settlement and Settled already opened - one place a claim is read and decided
rather than two.

The cost of that is a trip back to the list for every claim, which is why the
page carries its own Previous and Next. The sequence they walk is captured when
the page is opened and held: recomputing it would renumber the list under the
reader as they decide things, which is the disorientation the change was made
to remove.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


class TheQueueIsAListAndAClaimIsAPage(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_the_queue_no_longer_shares_the_page_with_a_claim(self):
        self.assertIn('<div id="split-queue">', self.app)
        self.assertNotIn('<div class="split" id="split-queue">', self.app)

    def test_a_row_opens_the_claim_page_naming_where_it_came_from(self):
        # Through the same helper every other claim table uses, now that the
        # queue is one of them.
        body = self.app.split("function renderQueue()", 1)[1].split("\n}", 1)[0]
        self.assertIn("opensClaim(tr, sub.id)", body)
        self.assertIn("wireClaimRows(box)", body)

    def test_it_is_the_same_table_as_pending_settlement(self):
        # The two lists a reviewer works back to back. A list that changes
        # shape between them makes the reader re-learn where the submitter and
        # the amount live.
        head = self.app.split('<tbody id="queue">', 1)[0].rsplit("<thead>", 1)[1]
        for col in ("Submitted", "Receipt date", "Submitter", "Vendor", "Group", "Claimed"):
            self.assertIn(f">{col}<", head)

    def test_the_selected_row_highlighter_is_gone(self):
        # A list whose rows open a page of their own has no selected row to
        # mark - the marker survived the split pane it belonged to.
        self.assertNotIn(".qrow", self.app)
        self.assertNotIn('aria-current", String(sub.id === selectedId)', self.app)

    def test_the_claim_page_is_the_one_that_already_existed(self):
        # Not a second copy of the panels: two sets of markup this size is two
        # renderers, and two renderers eventually disagree about a claim.
        self.assertEqual(self.app.count('id="detail-panel"'), 1)
        self.assertEqual(self.app.count('id="split-evidence"'), 1)


class MovingBetweenClaimsWithoutGoingBack(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()
        self.fn = self.app.split("function renderClaimNav()", 1)[1].split("\n}", 1)[0]

    def test_the_page_has_previous_and_next(self):
        self.assertIn('id="claim-prev"', self.app)
        self.assertIn('id="claim-next"', self.app)
        self.assertIn("stepClaim(-1)", self.app)
        self.assertIn("stepClaim(1)", self.app)

    def test_the_sequence_is_captured_when_the_page_opens(self):
        # Recomputed per render, deciding a claim would renumber the list under
        # the reader - "3 of 7" becoming "3 of 6" with a different claim third.
        opener = self.app.split("function openClaim(", 1)[1].split("\n}", 1)[0]
        self.assertIn("claimSiblings = siblingsFor(claimFrom);", opener)
        self.assertNotIn("siblingsFor(", self.fn)

    def test_it_says_where_you_are_and_where_back_goes(self):
        self.assertIn("at + 1", self.fn)
        self.assertIn("claimSiblings.length", self.fn)
        # Where the claim *belongs*, not where the reader came from: one opened
        # out of the review queue and approved is in Pending settlement now.
        self.assertIn("claimHome(", self.fn)
        self.assertIn("TAB_LABEL[home]", self.fn)

    def test_the_ends_of_the_list_are_dead_ends_not_wraps(self):
        # Wrapping from the last claim to the first would let somebody circle a
        # queue without noticing they had finished it.
        self.assertIn("$(\"claim-prev\").disabled = at === 0;", self.fn)
        self.assertIn("$(\"claim-next\").disabled = at === claimSiblings.length - 1;",
                      self.fn)

    def test_a_claim_with_no_list_behind_it_loses_the_arrows_not_the_bar(self):
        # Two dead arrows are furniture pretending to be navigation - but the
        # way out is on this bar, so the bar itself has to stay.
        self.assertIn("const stepping = at >= 0 && claimSiblings.length > 1;", self.fn)
        self.assertIn('$("claim-prev").hidden = $("claim-next").hidden = !stepping;',
                      self.fn)
        opener = self.app.split("function openClaim(", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (!claimSiblings.includes(id)) claimSiblings = [];", opener)

    def test_the_bar_is_the_only_way_back(self):
        self.assertIn('$("claim-pos").addEventListener("click",', self.app)
        self.assertEqual(0, self.app.count("claim-back"))

    def test_stepping_stays_on_the_claim_page(self):
        step = self.app.split("function stepClaim(delta)", 1)[1].split("\n}", 1)[0]
        self.assertIn("selectedId = next;", step)
        self.assertNotIn("goTo(", step)
        # Back to the top, or the reader lands halfway down the next claim.
        self.assertIn("window.scrollTo", step)

    def test_pending_settlement_gets_the_same_navigation(self):
        sibs = self.app.split("function siblingsFor(from)", 1)[1].split("\n}", 1)[0]
        self.assertIn('from === "queue"', sibs)
        self.assertIn('from === "payments"', sibs)


class ALabelThatNeverVariesIsNotWorthTheSpace(unittest.TestCase):
    """Every claim in Pending settlement is approved, so the badge said the
    same word on all of them - beside a line already reading "Approved by
    Riyad Rasheed · 22:40" in words, with the person and the time."""

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_the_verdict_badge_is_gone_entirely(self):
        # It survived one round by being hidden on decided claims. The case
        # that killed it is the other one: on a claim in the review queue the
        # engine's verdict is routinely "approved" - the policy has nothing
        # against it and a person still has to release it - so the page read
        # APPROVED in green directly above an Approve button.
        self.assertNotIn('id="verdict-badge"', self.app)
        self.assertNotIn("VERDICT_LABEL", self.app)

    def test_and_what_replaced_it_varies(self):
        # Every claim reached from the review queue is awaiting review and
        # every claim reached from Pending settlement is approved, so any
        # label there is the same word on every claim in its list. Why this
        # one is waiting is not.
        self.assertIn("It is waiting because the ", self.app)
        self.assertIn("releasing it is yours to do", self.app)


if __name__ == "__main__":
    unittest.main()


class TheListsAreScannable(unittest.TestCase):
    """The number of claims visible at once is most of a queue's value.

    Both lists were laid out for a narrow column: the queue row stacked its
    reason and metadata onto rows of their own, and the settlement table
    stacked vendor, channel and reference three high in one cell - which set
    the height of every row in the table. Neither is a narrow column any more.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_the_settlement_vendor_cell_is_one_line(self):
        self.assertIn('td.vend {', self.app)
        self.assertEqual(0, self.app.count("<br>${channelTag("))

    def test_table_rows_lost_their_padding_and_their_chips_their_bulk(self):
        self.assertIn("tbody td { padding:5px 14px;", self.app)
        self.assertIn("tbody td .src, tbody td .state { padding:1px 6px; }", self.app)
