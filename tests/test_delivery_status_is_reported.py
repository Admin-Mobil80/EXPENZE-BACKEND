"""A message AWS accepted is not a message anybody received.

`SendWhatsAppMessage` returns without raising as soon as AWS has taken the
message. Whether WhatsApp then delivered it, refused it, or dropped it arrives
later - as a status webhook on the same SNS topic the receipts come in on.

The adapter used to read `messages` out of that payload and nothing else, so
every status was parsed and discarded. The cost of that was not theoretical:
an acknowledgement nobody received looked, in every log we had, exactly like
one sitting on somebody's phone. `eum.send` returned True, no exception was
logged, and the only way to find out was for the person to tell us.

Meta was reporting the reason the entire time, on a topic we were already
subscribed to.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def source(name: str) -> str:
    with open(os.path.join(ROOT, "lambda_src", name), encoding="utf-8") as h:
        return h.read()


class TheAdapterReadsThem(unittest.TestCase):
    def setUp(self):
        self.src = source("whatsapp.py")
        self.dispatch = self.src.split("def _dispatch(", 1)[1].split("\ndef ", 1)[0]

    def test_statuses_are_walked_alongside_messages(self):
        self.assertIn('for status in value.get("statuses", []):', self.dispatch)
        self.assertIn("_record_status(status)", self.dispatch)

    def test_a_status_is_not_treated_as_an_inbound_receipt(self):
        # It carries no media and no sender to resolve; handing it to
        # `_handle_message` would spend a credit on a delivery receipt.
        self.assertNotIn("_handle_message(status", self.dispatch)

    def test_messages_are_still_handled(self):
        # The status reading is an addition, not a replacement.
        self.assertIn("_handle_message(msg, cfg, from_id)", self.dispatch)


class AFailureIsLoudAndTheRestIsNot(unittest.TestCase):
    def setUp(self):
        self.fn = source("whatsapp.py").split(
            "def _record_status(", 1)[1].split("\ndef ", 1)[0]

    def test_a_refusal_is_logged_at_error(self):
        # This is the line somebody debugging "I sent a receipt and heard
        # nothing" has to be able to grep for.
        self.assertIn("logger.error(", self.fn)
        self.assertIn("did NOT deliver", self.fn)

    def test_with_metas_own_code_and_title(self):
        # "failed" alone says nothing actionable. 131047 is a re-engagement
        # refusal and 131026 is an undeliverable recipient, and the two call
        # for opposite fixes.
        self.assertIn('err.get("code")', self.fn)
        self.assertIn('err.get("title")', self.fn)
        self.assertIn('err.get("error_data")', self.fn)

    def test_errors_count_as_failure_even_when_the_state_does_not(self):
        # A status carrying an `errors` array has not been delivered, whatever
        # the `status` field says.
        self.assertIn('if state == "failed" or errors:', self.fn)

    def test_the_happy_path_stays_quiet(self):
        # A few hundred a week. A heartbeat, not an event.
        self.assertIn("logger.info(", self.fn)

    def test_the_recipient_is_not_written_out_in_full(self):
        # Same rule as the unresolved-sender log: enough for whoever sent it
        # to recognise, useless to anybody else reading CloudWatch.
        self.assertIn("who[-4:]", self.fn)
        self.assertNotIn('logger.error("WhatsApp did NOT deliver to %s"', self.fn)


if __name__ == "__main__":
    unittest.main()
