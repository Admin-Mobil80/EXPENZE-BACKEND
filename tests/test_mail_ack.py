"""Acknowledging an inbound receipt email.

Two things carry risk here. An auto-reply that answers another auto-reply is a
mail loop, and an informative reply to an address we do not recognise tells a
stranger that the mailbox is live and reading their post. Both are pinned.
"""
from __future__ import annotations

import email
import email.policy
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("ORGS_TABLE", "t")
os.environ.setdefault("INTAKE_TABLE", "t")
os.environ.setdefault("USERS_TABLE", "t")
os.environ.setdefault("RECEIPTS_BUCKET", "test-bucket")

import mail  # noqa: E402


def message(**headers) -> email.message.Message:
    msg = email.message.EmailMessage()
    for k, v in headers.items():
        msg[k.replace("_", "-")] = v
    return msg


QUEUED = [{"status": "queued", "submission_id": "sub_1788952460070_0041f6",
           "reference": "Mobil80-Exp-4",
           "org_name": "Mobil80 Technologies", "group_status": "unset",
           "filename": "bill.jpg"}]


class LoopPrevention(unittest.TestCase):
    def test_our_own_acknowledgement_is_not_answered(self):
        self.assertTrue(mail._is_automated(message(Auto_Submitted="auto-replied")))

    def test_a_holiday_responder_is_not_answered(self):
        self.assertTrue(mail._is_automated(message(Auto_Submitted="auto-generated")))

    def test_bulk_and_list_mail_is_not_answered(self):
        self.assertTrue(mail._is_automated(message(Precedence="bulk")))
        self.assertTrue(mail._is_automated(message(List_Id="<staff.mobil80.com>")))

    def test_an_ordinary_message_is_answered(self):
        self.assertFalse(mail._is_automated(message(Subject="Lunch receipt")))
        self.assertFalse(mail._is_automated(message(Auto_Submitted="no")))

    def test_an_automated_sender_gets_no_reply_at_all(self):
        with mock.patch.object(mail, "_ses") as ses:
            mail._acknowledge(message(Auto_Submitted="auto-replied"), "a@x.com", QUEUED)
        ses.send_email.assert_not_called()


class WhatTheSenderIsTold(unittest.TestCase):
    def _sent(self, msg, outcomes):
        with mock.patch.object(mail, "_ses") as ses:
            mail._acknowledge(msg, "riyad@mobil80.com", outcomes)
            self.assertTrue(ses.send_email.called)
            raw = ses.send_email.call_args[1]["Content"]["Raw"]["Data"]
        return email.message_from_bytes(raw, policy=email.policy.default)

    def test_it_carries_a_claim_reference(self):
        out = self._sent(message(Subject="Receipt"), QUEUED)
        self.assertIn("Mobil80-Exp-4", out.get_content())
        self.assertIn("bill.jpg", out.get_content())

    def test_it_is_the_same_reference_every_other_message_uses(self):
        # This printed `EXP-2460070`, the tail of the internal submission id,
        # while the console and every later message called the same claim
        # `Mobil80-Exp-4`. The number the sender saw first was the one that
        # existed nowhere else, so quoting it back got them nothing.
        out = self._sent(message(Subject="Receipt"), QUEUED).get_content()
        self.assertNotIn("EXP-2460070", out)
        self.assertNotIn("2460070", out)

    def test_a_response_with_no_reference_still_reads_properly(self):
        # An older intake response, or one that failed to allocate a number.
        # Better a line naming the file than a line with a gap where the
        # reference should be.
        bare = [{k: v for k, v in QUEUED[0].items() if k != "reference"}]
        out = self._sent(message(Subject="Receipt"), bare).get_content()
        self.assertIn("bill.jpg", out)
        self.assertNotIn("EXP-", out)

    def test_it_threads_into_the_senders_own_conversation(self):
        out = self._sent(message(Subject="Lunch", Message_ID="<abc@mail>"), QUEUED)
        self.assertEqual(out["In-Reply-To"], "<abc@mail>")
        self.assertEqual(out["Subject"], "Re: Lunch")

    def test_it_marks_itself_automatic_so_it_is_not_answered_back(self):
        out = self._sent(message(Subject="Receipt"), QUEUED)
        self.assertEqual(out["Auto-Submitted"], "auto-replied")

    def test_a_reply_lands_back_on_intake(self):
        out = self._sent(message(Subject="Receipt"), QUEUED)
        self.assertEqual(out["Reply-To"], mail.INTAKE_ADDRESS)

    def test_no_attachment_says_how_to_fix_it(self):
        out = self._sent(message(Subject="Here you go"), [])
        body = out.get_content()
        self.assertIn("no receipt attached", body.lower())
        self.assertIn("pasted into the body", body)

    def test_out_of_credits_says_nothing_was_lost(self):
        out = self._sent(message(Subject="Receipt"), [{"status": "no_credits"}])
        self.assertIn("out of", out.get_content().lower())
        self.assertIn("Nothing was charged", out.get_content())

    def test_several_receipts_are_one_message_listing_each(self):
        many = QUEUED + [{"status": "queued", "submission_id": "sub_1788952460099_0041f6",
                          "org_name": "Mobil80 Technologies", "filename": "cab.pdf"}]
        out = self._sent(message(Subject="Two receipts"), many)
        body = out.get_content()
        self.assertIn("Receipts received", body)
        self.assertIn("bill.jpg", body)
        self.assertIn("cab.pdf", body)

    def test_a_missing_subject_still_produces_one(self):
        out = self._sent(message(), QUEUED)
        self.assertTrue(out["Subject"])


if __name__ == "__main__":
    unittest.main()


class SendingPermissions(unittest.TestCase):
    """Every SES action the code actually calls must be granted.

    The acknowledgement builds raw MIME so it can carry In-Reply-To and land on
    the sender's own thread. SESv2 `send_email` with `Content={"Raw": ...}` is
    authorised by IAM as `ses:SendRawEmail`, not `ses:SendEmail` - so while
    sign-in codes and settlement notices went out perfectly, every "you didn't
    attach anything" reply died with AccessDenied in a log nobody was reading.
    The sender just heard nothing back.
    """

    def setUp(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "expensifyai", "stack.py"), encoding="utf-8") as handle:
            self.stack = handle.read()
        self.sources = {}
        for name in ("mail", "auth", "admin", "notify"):
            with open(os.path.join(root, "lambda_src", f"{name}.py"), encoding="utf-8") as handle:
                self.sources[name] = handle.read()

    def test_raw_sends_are_permitted(self):
        senders = [n for n, src in self.sources.items() if '"Raw"' in src]
        self.assertTrue(senders, "nothing sends raw MIME any more; drop this test")
        self.assertIn("ses:SendRawEmail", self.stack,
                      f"{senders} send raw MIME, which IAM gates separately")

    def test_no_ses_statement_grants_only_the_simple_action(self):
        # A statement listing SendEmail alone is the bug coming back.
        lonely = re.findall(r'actions=\["ses:SendEmail"\]', self.stack)
        self.assertEqual(lonely, [], "a policy grants SendEmail without SendRawEmail")
