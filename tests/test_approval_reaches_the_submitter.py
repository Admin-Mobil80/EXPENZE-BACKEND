"""A person approving a claim is told to the person who sent it.

The gap this closes: a claim that reaches review is told "sent to your finance
team, nothing is needed from you" and then told nothing at all until it is
settled, which can be days. That silence is the one stretch where the claimant
has been promised an answer and given no sign that anything moved - and the
approval is the moment the outcome stops being in doubt.

What has to stay true is the same thing that is true of an automatic approval:
it must not read as a payment. Somebody who believes the money is on its way
does not chase it.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")

import notify  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")

CLAIM = {
    "vendor": "The Copper Table",
    "claim_ref": "Mobil80-Exp-41",
    "currency": "INR",
    "approved": "4820.00",
    "approved_by": "Madhusudhan",
}


class ItSaysWhoDecidedAndWhatHappensNext(unittest.TestCase):

    def setUp(self):
        self.n = notify.approved_notice(CLAIM)

    def test_the_person_is_named(self):
        # Who decided is part of what happened, and a claimant chasing the
        # money should know whose desk it left.
        self.assertIn("Madhusudhan", self.n["text"])
        self.assertIn("Madhusudhan", self.n["whatsapp"])

    def test_the_amount_and_vendor_are_there(self):
        for field in ("text", "whatsapp"):
            self.assertIn("The Copper Table", self.n[field])
            self.assertIn("4,820.00", self.n[field])

    def test_the_reference_is_the_one_every_other_message_uses(self):
        for field in ("text", "whatsapp"):
            self.assertIn("Mobil80-Exp-41", self.n[field])

    def test_it_does_not_read_as_a_payment(self):
        # The same sentence an automatic approval carries, because it is the
        # same fact about the same next step.
        for field in ("text", "whatsapp"):
            self.assertIn(notify.PENDING, self.n[field])

    def test_nothing_is_asked_of_them(self):
        self.assertNotIn("?", self.n["text"])
        self.assertNotIn("reply", self.n["text"].lower())

    def test_a_missing_reviewer_name_does_not_leave_a_blank(self):
        n = notify.approved_notice({**CLAIM, "approved_by": ""})
        self.assertIn("your finance team", n["text"])


class TheReviewPathSendsIt(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as fh:
            self.auth = fh.read()

    def test_it_is_registered_as_a_notice_kind(self):
        with open(os.path.join(ROOT, "lambda_src", "notify.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('"approved": approved_notice', src.split("NOTICES = {", 1)[1])

    def test_both_halves_of_a_human_decision_are_sent(self):
        self.assertIn('if action in ("approved", "rejected"):', self.auth)

    def test_but_not_a_withdrawal_or_a_reopen(self):
        # They did the first themselves; the second decides nothing.
        block = self.auth.split('if action in ("approved", "rejected"):', 1)[1] \
                         .split("return _reply(", 1)[0]
        self.assertNotIn("withdrawn", block)
        self.assertNotIn("reopened", block)

    def test_the_figure_sent_is_what_the_reviewer_released(self):
        # `approved_total` is a string, and "0" is truthy - a plain `or` would
        # announce a claim as approved for 0.00.
        block = self.auth.split('if action in ("approved", "rejected"):', 1)[1] \
                         .split("return _reply(", 1)[0]
        self.assertIn("released = approved_total", block)
        self.assertIn("if _as_decimal(released) <= 0:", block)


class WhatsAppDeliveryIsHonestAboutItsWindow(unittest.TestCase):
    """An approval lands hours or days after the receipt, so the 24-hour
    customer-service window is usually shut and free text will not send.
    Email always carries it; this is recorded rather than pretended about."""

    def test_it_is_not_sent_under_another_notice_s_template(self):
        # Borrowing an approved template for a different message is how a
        # number loses its quality rating.
        with open(os.path.join(ROOT, "lambda_src", "notify.py"), encoding="utf-8") as fh:
            src = fh.read()
        templates = src.split("TEMPLATES = {", 1)[1].split("}", 1)[0]
        self.assertNotIn("approved", templates)
        self.assertIn("approved", src.split("NO_TEMPLATE = {", 1)[1].split("}", 1)[0])


if __name__ == "__main__":
    unittest.main()
