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

    def test_the_bar_counts_what_is_left_not_where_you_are(self):
        # "1 of 3" counted a position in the queue as it was when the claim
        # was opened, and the tab badge counts what is still undecided, so the
        # moment anything was decided the two disagreed on one screen.
        self.assertIn("` · ${waiting} still waiting`", self.nav)
        self.assertNotIn("of ${claimSiblings.length}", self.nav)

    def test_it_uses_the_same_arithmetic_as_the_badge(self):
        self.assertIn('home === "queue" ? SUBMISSIONS.filter(isQueued).length',
                      self.nav)

    def test_the_sequence_is_still_followed_even_though_it_is_not_reported(self):
        # Stepping walks the list the reader was given. Recomputing that under
        # somebody halfway through it is the disorientation this page exists
        # to remove; not printing it is a different thing from not having it.
        siblings = self.app.split("function openClaim(id, from) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("claimSiblings = siblingsFor(claimFrom);", siblings)
        step = self.app.split("function stepClaim(delta) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("const next = claimSiblings[at + delta];", step)

    def test_deciding_a_claim_does_not_move_the_reader(self):
        # `claimHome` returns where they came from, not where the claim now
        # belongs - so Back leads out of the list they were working.
        home = self.app.split("function claimHome(sub) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (claimFrom) return claimFrom;", home)


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
            "function renderSettleOnClaim(sub, maySettle) {", 1)[1].split(
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

    def test_a_decided_claim_offers_no_way_to_undo_the_decision(self):
        # Reject lived on this row - as Reopen, then as a rejection at
        # settlement, then as "Reject anyway" - and none of those belonged in
        # front of the person who had just decided the claim. The reviewer
        # read the bill and said yes, and the submitter has been told so in
        # writing. Offering to take it back in the same breath makes the
        # approval look provisional, which it is not.
        #
        # To the end of the decided-claim branch, not a fixed slice: a branch
        # added above it should not decide whether this passes.
        actions = self.app.split('const abox = $("actions")', 1)[1].split(
            '} else if (arm === "agent") {', 1)[0]
        self.assertIn('claimFrom !== "queue"', actions)
        # And it is finance's bar, not the reviewer's: a finance executive may
        # not decide a claim and may certainly decline to pay one.
        self.assertIn("settlingHere() && !isPaid(sub)", actions)

    def test_but_it_is_not_the_only_thing_on_offer(self):
        # It was, and that is the whole complaint: a tick, a name, and one red
        # button. The row now says where the claim went and offers the move a
        # reviewer working a queue actually wants next.
        actions = self.app.split('const abox = $("actions")', 1)[1].split(
            '} else if (arm === "agent") {', 1)[0]
        self.assertIn("In Pending settlement now", actions)
        self.assertIn('nx.textContent = "Next claim \u2192"', actions)
        self.assertIn("stepClaim(1)", actions)

    def test_the_forward_move_is_not_offered_at_the_end_of_the_list(self):
        # A dead button is furniture pretending to be navigation, and the same
        # rule already governs the arrows at the top of the page.
        actions = self.app.split('const abox = $("actions")', 1)[1].split(
            '} else if (arm === "agent") {', 1)[0]
        self.assertIn("at >= 0 && claimSiblings[at + 1]", actions)

    def test_and_the_claim_is_not_said_to_be_awaiting_payment_once_it_is_paid(self):
        actions = self.app.split('const abox = $("actions")', 1)[1].split(
            '} else if (arm === "agent") {', 1)[0]
        said = actions.split("In Pending settlement now", 1)[0]
        self.assertIn("!isPaid(sub) && !stageNow", said)


class OneClassNameOneMeaning(unittest.TestCase):
    """A new spinner took a class name the header was already using.

    `.spin` is the refresh glyph beside "just now" - a static U+21BB that the
    class only sizes. Adding `.spin { animation: spin .8s linear infinite }`
    for a busy indicator elsewhere set that arrow turning on every page of the
    console, for ever, while nothing was loading. A spinner that never stops
    is the product telling everybody it is stuck, and it was on the one
    element every screen shows.

    The rule is not "check before naming" - it is that a class which paints
    one specific thing gets a name nothing else would reach for.
    """

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.css = self.app.split("<style>", 1)[1].rsplit("</style>", 1)[0]

    def test_the_header_glyph_is_not_animated(self):
        for rule in self.css.split("}"):
            if ".spin" in rule.split("{", 1)[0] and ".busydot" not in rule:
                self.assertNotIn("animation", rule, rule.strip()[:120])

    def test_the_busy_indicator_has_a_name_of_its_own(self):
        self.assertIn(".busydot {", self.css)
        self.assertIn("@keyframes busyspin", self.css)
        self.assertIn('dot.className = "busydot";', self.app)

    def test_and_its_keyframes_are_its_own_too(self):
        # `@keyframes spin` would be reachable by any future `.spin` rule.
        self.assertNotIn("@keyframes spin ", self.css)

    def test_reduced_motion_still_stops_it(self):
        block = self.css.split("@media (prefers-reduced-motion: reduce) {", 1)[1]
        self.assertIn(".busydot { animation:none;", block[:400])


if __name__ == "__main__":
    unittest.main()
