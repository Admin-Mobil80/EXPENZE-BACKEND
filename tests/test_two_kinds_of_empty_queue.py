"""An empty review queue means two different things, and showed one screen.

An organisation on its first day has received nothing. The three ways to send
a receipt in - WhatsApp, email, the portal - are the whole answer for them,
and the empty queue is the right place to say it.

A reviewer who has just decided the last claim is a different person with a
different question. They know how receipts arrive; they have been reading them
all morning. Handing them a WhatsApp number and "photograph the receipt and
send it" as the reward for finishing their work is a non-sequitur, and it is
what the screen did. Those channels belong on My expenses, where somebody is
actually about to send something, and that is where they already are.

What a cleared queue owes its reader is where the work went: what is waiting to
be paid and what was refused - as somewhere to go, not as a sentence leaving
them to go and count it.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class TheTwoStatesAreToldapart(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function renderQueue() {", 1)[1].split(
            "\n}", 1)[0]

    def test_the_question_is_whether_anything_ever_arrived(self):
        self.assertIn("const everReceived = SUBMISSIONS.length > 0;", self.fn)

    def test_a_new_organisation_is_shown_the_channels(self):
        self.assertIn("if (ways) ways.hidden = everReceived;", self.fn)
        self.assertIn("No receipts have been submitted yet.", self.fn)

    def test_a_cleared_queue_is_not(self):
        # The same flag, the other way round, on the other panel.
        self.assertIn("next.hidden = !everReceived;", self.fn)

    def test_the_headings_differ(self):
        self.assertIn('"Nothing left to review" : "Nothing to review yet"',
                      self.fn)

    def test_the_channels_still_exist_where_they_belong(self):
        # My expenses is the screen for somebody about to send a receipt.
        self.assertIn('SEND ON WHATSAPP TO', self.app.upper())


class ACleardQueueSaysWhereTheWorkWent(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function renderQueue() {", 1)[1].split(
            "\n}", 1)[0]

    def test_it_counts_what_is_waiting_to_be_paid(self):
        self.assertIn("const owed = payableClaims().filter(c => c.outstanding > 0);",
                      self.fn)
        self.assertIn("owed to submitters, waiting to be paid.", self.fn)

    def test_and_what_was_refused(self):
        self.assertIn("const rejected = rejectedClaims().length;", self.fn)

    def test_a_card_for_nothing_is_not_drawn(self):
        # "0 rejected" is a fact about nothing, and a card nobody can act on.
        self.assertIn("if (rejected) {", self.fn)

    def test_pending_settlement_is_shown_even_at_zero(self):
        # It is the one place the reader goes next, and "nothing waiting to be
        # paid" is the good news rather than an empty tile.
        self.assertIn('"Nothing is waiting to be paid."', self.fn)

    def test_the_cards_go_somewhere(self):
        self.assertIn('if (to === "queue-rejected") { queueTab = "rejected"; renderQueue(); }',
                      self.fn)
        self.assertIn("else goTo(to);", self.fn)

    def test_they_are_reachable_from_a_keyboard(self):
        # They are buttons in everything but the tag name.
        self.assertIn('card.setAttribute("role", "button");', self.fn)
        self.assertIn("card.tabIndex = 0;", self.fn)
        self.assertIn('if (e.key === "Enter" || e.key === " ")', self.fn)

    def test_and_look_pressable(self):
        css = self.app.split("<style>", 1)[1].rsplit("</style>", 1)[0]
        self.assertIn(".blankways .goes { cursor:pointer; }", css)
        self.assertIn(".blankways .goes:focus-visible", css)

    def test_the_count_is_written_for_one_as_well_as_many(self):
        self.assertIn('owed.length === 1 ? "" : "s"', self.fn)


if __name__ == "__main__":
    unittest.main()
