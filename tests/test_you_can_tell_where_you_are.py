"""Two ways the console stopped saying where the reader was standing.

**The whole tab strip went grey on a claim.** `view` is "claim" there, which
matches no tab, so `aria-current` was false on all eleven of them. Opening a
claim out of the review queue is not leaving the review queue, and for the
whole time somebody spent reading and deciding one - which is most of the time
they spend in this product - the bar answered "where am I" with nothing.

**"Review queue · 1 of 3" over a claim that had just been approved.** Both
halves true: the position counts the list as it was opened, deliberately, so a
claim decided on the way through does not renumber everything after it; and
the claim really is in Pending settlement now. Together they read as the
product being confused about its own state.

The fix for the second is emphatically *not* to move the reader, or to
recount. Deciding a claim moves the claim, not the reader - they keep the
sequence they were working through, Next still steps to the next claim they
were given - so what was missing was one sentence about where the claim went.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class TheTabStripSaysWhereYouAre(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.nav = self.app.split("function renderNav() {", 1)[1].split("\n}", 1)[0]

    def test_a_claim_lights_the_tab_it_was_opened_from(self):
        self.assertIn('const lit = view === "claim"', self.nav)
        self.assertIn("? claimHome(SUBMISSIONS.find(s => s.id === selectedId))",
                      self.nav)

    def test_the_tabs_are_marked_from_it_rather_than_from_the_view(self):
        self.assertIn('b.setAttribute("aria-current", String(lit === id));',
                      self.nav)
        self.assertNotIn('b.setAttribute("aria-current", String(view===id));',
                         self.nav)

    def test_the_selected_tab_is_unmistakable(self):
        rule = self.app.split('.nav button[aria-current="true"] {', 1)[1].split(
            "}", 1)[0]
        self.assertIn("border-bottom-color:var(--ink)", rule)
        self.assertIn("border-bottom-width:3px", rule)
        self.assertIn("font-weight:700", rule)
        self.assertIn("background:var(--surface-2)", rule)

    def test_the_sub_tabs_answer_the_question_the_same_way(self):
        rule = self.app.split('.subtabs button[aria-current="true"] {', 1)[1].split(
            "}", 1)[0]
        self.assertIn("border-bottom-width:3px", rule)
        self.assertIn("font-weight:700", rule)


class ADecidedClaimSaysWhereItWent(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.nav = self.app.split("function renderClaimNav() {", 1)[1].split(
            "\n}", 1)[0]

    def test_the_note_exists_in_the_bar(self):
        self.assertIn('<span class="claimmoved" id="claim-moved" hidden></span>',
                      self.app)

    def test_it_only_speaks_for_a_claim_that_has_left_the_queue(self):
        self.assertIn('sub && claimFrom === "queue" && !isQueued(sub)', self.nav)

    def test_it_names_each_destination(self):
        for phrase in ("Pending settlement", "Settled",
                       "it has left the review queue"):
            self.assertIn(phrase, self.nav)

    def test_the_position_is_still_the_list_as_it_was_opened(self):
        # The counting is not the bug. Recomputing it would renumber the
        # sequence under the reader, which is the disorientation the claim
        # page exists to remove.
        # And it says which set it is counting, so it cannot be read as the
        # tab badge - which counts what is still undecided and therefore
        # disagrees the moment a claim is decided.
        self.assertIn("` · ${at + 1} of ${claimSiblings.length} in this set`",
                      self.nav)
        siblings = self.app.split("function openClaim(id, from) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("claimSiblings = siblingsFor(claimFrom);", siblings)

    def test_deciding_a_claim_does_not_move_the_reader(self):
        # `claimHome` returns where they came from, not where the claim now
        # belongs - so Back leads out of the list they were working.
        home = self.app.split("function claimHome(sub) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (claimFrom) return claimFrom;", home)

    def test_stepping_still_follows_the_original_sequence(self):
        step = self.app.split("function stepClaim(delta) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("const next = claimSiblings[at + delta];", step)


class ApprovingDoesNotHandYouAPaymentRun(unittest.TestCase):
    """The claim page turned into a settlement page under the reviewer.

    Approve a claim out of the review queue and the same page you were
    standing on grew "INR 1,936.00 owed to Reena K R." and a Record payment
    button - with "Review queue - 1 of 3" still above it. One claim of three
    decided, and the product had handed over a payment run.

    They are different jobs, usually different people, certainly different
    days: a reviewer decides what the company owes, finance moves the money.
    Doing the second in the middle of the first is two roles on one screen
    with nothing marking where one ends. Pending settlement is one click away
    and is entirely about this.

    A claim opened *from* Pending settlement keeps the button - that is the
    case it was built for, and the reason the guard is on where the reader
    came from rather than on the claim's state.
    """

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split(
            "function renderSettleOnClaim(sub, mayReview) {", 1)[1].split(
            "\n}", 1)[0]

    def test_no_payment_controls_on_a_claim_opened_from_the_queue(self):
        self.assertIn('if (claimFrom === "queue") return;', self.fn)

    def test_the_guard_is_before_anything_is_drawn(self):
        self.assertLess(self.fn.index('if (claimFrom === "queue") return;'),
                        self.fn.index('owed.textContent'))

    def test_pending_settlement_still_has_it(self):
        # The guard names the queue rather than excluding everything but
        # payments, so a claim reached from Settled or a link is unaffected.
        self.assertNotIn('if (claimFrom !== "payments") return;', self.fn)
        self.assertIn('[["Record payment", "primary", "pay"]]', self.fn)

    def test_the_reviewer_can_still_change_their_mind(self):
        # Reject stays on the actions row: approving and immediately thinking
        # better of it is a review action, and it is the one this screen is
        # for.
        # To the end of the decided-claim branch, not a fixed slice: a branch
        # added above it should not decide whether this passes.
        actions = self.app.split('const abox = $("actions")', 1)[1].split(
            "} else if (agentMayRelease(sub)) {", 1)[0]
        self.assertIn('decision.action !== "Rejected"', actions)
        self.assertIn('no.textContent = "Reject"', actions)


if __name__ == "__main__":
    unittest.main()
