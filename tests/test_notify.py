"""What the employee is told when a claim is settled or rejected.

The wording is the product here. A settlement notice without the payment
reference cannot be reconciled against a bank statement; a rejection without
the reason leaves someone out of pocket with nothing to act on. Both are
checked, along with the rule that a claim outcome only reaches a WhatsApp
number its owner verified.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import notify  # noqa: E402


SETTLED = {
    "vendor": "The Bombay Canteen", "currency": "INR",
    "approved": "3631.00", "paid": "3631.00", "outstanding": "0",
    "mode": "bank transfer", "reference": "UTR9912837465",
    "paid_on": "2026-09-09", "settled_by": "Meera Iyer",
    "group": "Bengaluru office", "note": "September payment run",
}

REJECTED = {
    "vendor": "Toit Brewpub", "currency": "INR", "approved": "2114.00",
    "reason": "The cover count was four but only two people were on the trip.",
    "rejected_by": "Meera Iyer", "rejected_on": "09 Sep 2026",
}


class Money(unittest.TestCase):
    def test_codes_not_symbols(self):
        # A rupee sign that renders as a box is useless on a figure someone is
        # meant to reconcile against a bank statement.
        self.assertEqual(notify.money("3631", "INR"), "INR 3,631.00")

    def test_thousands_are_grouped(self):
        self.assertEqual(notify.money("1234567.5", "USD"), "USD 1,234,567.50")

    def test_a_bad_amount_does_not_blow_up_the_notice(self):
        self.assertIn("INR", notify.money("n/a", "INR"))


class SettledNotice(unittest.TestCase):
    def setUp(self):
        self.n = notify.settled_notice(SETTLED)

    def test_the_subject_carries_the_amount_and_the_vendor(self):
        self.assertIn("INR 3,631.00", self.n["subject"])
        self.assertIn("The Bombay Canteen", self.n["subject"])

    def test_the_reference_is_there_to_reconcile_against(self):
        self.assertIn("UTR9912837465", self.n["text"])
        self.assertIn("UTR9912837465", self.n["whatsapp"])

    def test_mode_date_and_who_paid_are_all_stated(self):
        for expected in ("bank transfer", "2026-09-09", "Meera Iyer", "Bengaluru office"):
            self.assertIn(expected, self.n["text"])

    def test_a_full_settlement_does_not_mention_a_balance(self):
        self.assertNotIn("Still owed", self.n["text"])

    def test_a_part_settlement_says_what_is_left(self):
        part = notify.settled_notice({**SETTLED, "paid": "1000.00", "outstanding": "2631.00"})
        self.assertIn("part payment", part["subject"])
        self.assertIn("Still owed", part["text"])
        self.assertIn("INR 2,631.00", part["text"])
        self.assertIn("still owed", part["whatsapp"])

    def test_optional_fields_are_simply_absent(self):
        bare = notify.settled_notice({"vendor": "Chai Point", "currency": "INR",
                                      "paid": "157.50", "approved": "157.50"})
        self.assertIn("INR 157.50", bare["text"])
        self.assertNotIn("Reference:", bare["text"])
        self.assertNotIn("None", bare["text"])


class RejectedNotice(unittest.TestCase):
    def setUp(self):
        self.n = notify.rejected_notice(REJECTED)

    def test_the_reason_is_carried_verbatim(self):
        self.assertIn(REJECTED["reason"], self.n["text"])
        self.assertIn(REJECTED["reason"], self.n["whatsapp"])

    def test_it_names_who_decided(self):
        self.assertIn("Meera Iyer", self.n["text"])
        self.assertIn("Meera Iyer", self.n["whatsapp"])

    def test_the_subject_says_not_reimbursed(self):
        self.assertIn("not reimbursed", self.n["subject"].lower())
        self.assertIn("Toit Brewpub", self.n["subject"])

    def test_it_says_the_decision_can_be_revisited(self):
        # Someone out of pocket needs to know there is a route back.
        self.assertIn("finance team", self.n["text"])

    def test_a_long_reason_is_truncated_not_dropped(self):
        long = notify.rejected_notice({**REJECTED, "reason": "x" * 5000})
        self.assertIn("x" * 100, long["text"])
        self.assertLess(len(long["text"]), 5000)


class Delivery(unittest.TestCase):
    MEMBER = {"email": "priya@mobil80.com", "mobile": "+919845011237",
              "whatsapp_channel": "active"}

    def test_both_channels_when_the_number_is_verified(self):
        with mock.patch.object(notify, "_send_email", return_value=True) as em, \
             mock.patch.object(notify, "_send_whatsapp", return_value=True) as wa:
            got = notify.send("settled", self.MEMBER, SETTLED)
        self.assertEqual(got["sent"], {"email": True, "whatsapp": True})
        self.assertEqual(em.call_args[0][0], "priya@mobil80.com")
        self.assertEqual(wa.call_args[0][0], "+919845011237")
        self.assertEqual(wa.call_args[0][1], "settled")

    def test_an_unverified_number_is_never_messaged(self):
        member = {**self.MEMBER, "whatsapp_channel": "pending"}
        with mock.patch.object(notify, "_send_email", return_value=True), \
             mock.patch.object(notify, "_send_whatsapp") as wa:
            got = notify.send("rejected", member, REJECTED)
        wa.assert_not_called()
        self.assertFalse(got["sent"]["whatsapp"])

    def test_email_still_goes_when_whatsapp_fails(self):
        with mock.patch.object(notify, "_send_email", return_value=True), \
             mock.patch.object(notify, "_send_whatsapp", return_value=False):
            got = notify.send("settled", self.MEMBER, SETTLED)
        self.assertTrue(got["sent"]["email"])
        self.assertFalse(got["sent"]["whatsapp"])

    def test_an_unknown_notice_is_refused_rather_than_sent_empty(self):
        with self.assertRaises(ValueError):
            notify.send("maybe", self.MEMBER, SETTLED)



class TemplateParameters(unittest.TestCase):
    """WhatsApp rejects a template variable that is empty or contains a newline,
    and a claim outcome routinely has both - an unrecorded reference, and a
    rejection reason someone typed across two lines."""

    def test_settled_fills_every_slot(self):
        got = notify._template_parameters("settled", SETTLED)
        self.assertEqual(len(got), 5)
        self.assertEqual(got[0], "The Bombay Canteen")
        self.assertEqual(got[1], "INR 3,631.00")
        self.assertEqual(got[2], "UTR9912837465")

    def test_a_missing_reference_becomes_words_not_a_blank(self):
        got = notify._template_parameters("settled", {**SETTLED, "reference": ""})
        self.assertEqual(got[2], "not recorded")
        self.assertTrue(all(p.strip() for p in got))

    def test_newlines_are_flattened(self):
        got = notify._template_parameters(
            "rejected", {**REJECTED, "reason": "Line one.\nLine two.\tTabbed."})
        self.assertNotIn("\n", got[2])
        self.assertNotIn("\t", got[2])
        self.assertEqual(got[2], "Line one. Line two. Tabbed.")

    def test_rejected_fills_every_slot(self):
        got = notify._template_parameters("rejected", REJECTED)
        self.assertEqual(len(got), 4)
        self.assertEqual(got[3], "Meera Iyer")

    def test_nothing_at_all_still_yields_usable_parameters(self):
        for kind in ("settled", "rejected"):
            got = notify._template_parameters(kind, {})
            self.assertTrue(all(p.strip() for p in got), kind)

if __name__ == "__main__":
    unittest.main()
