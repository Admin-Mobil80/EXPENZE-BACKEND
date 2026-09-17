"""What a receipt costs to process, and the loop that made it cost 300x.

Two model calls per receipt: one vision extraction, one line of prose. That is
the bill in normal operation, and it is small.

What is not small is a repeatable failure. The worker releases a claim it could
not finish so the next delivery tries again - and the release writes to the
table this worker's stream reads, so the release *is* the next delivery. On a
transient fault that is right and the second attempt succeeds. On a fault in
the code it is a loop with no exit, spending two model calls on every pass,
one of them a vision call over the full receipt image.

A `NameError` in the write that happens *after* the model has been called and
paid for turned 13 real receipts into 6,941 invocations and 3,435 failed audits
in one day. Lambda's own `retry_attempts` cannot help: every cycle is a fresh
event, not a retry of the last one.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def src(name: str) -> str:
    with open(os.path.join(ROOT, "lambda_src", name), encoding="utf-8") as fh:
        return fh.read()


class OneReceiptIsTwoCalls(unittest.TestCase):

    def test_extraction_and_prose_and_nothing_else(self):
        llm = src("llm.py")
        self.assertEqual(2, llm.count("self._client.chat.completions.create("))

    def test_a_multi_page_receipt_is_still_one_call(self):
        # Line items and the total are routinely on different pages, so a call
        # per page would buy two half-answers and pay twice for them.
        llm = src("llm.py")
        fn = llm.split('if kind == "images":', 1)[1].split("return parts", 1)[0]
        self.assertIn("for page in receipt_input[\"pages\"]", fn)

    def test_a_re_audit_does_not_re_read_the_receipt(self):
        # The bill has not changed - a person said what kind of expense it is -
        # so re-extracting would pay for the same answer twice.
        handler = src("handler.py")
        fn = handler.split("def reaudit(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("No second extraction", fn)
        self.assertNotIn("extract_receipt", fn)


class AFailureIsNotRetriedForEver(unittest.TestCase):

    def setUp(self):
        self.worker = src("auditor_worker.py")
        self.fn = self.worker.split("def _release(", 1)[1].split("\ndef ", 1)[0]

    def test_attempts_are_counted_on_the_claim(self):
        self.assertIn("audit_attempts = if_not_exists(audit_attempts, :zero) + :one",
                      self.fn)

    def test_and_there_is_a_limit(self):
        self.assertIn("MAX_AUDIT_ATTEMPTS", self.worker)
        self.assertIn("attempts < MAX_AUDIT_ATTEMPTS", self.fn)

    def test_past_it_the_claim_is_parked_where_nothing_reclaims_it(self):
        # `needs_human` is not a status this worker claims, so the loop ends -
        # and the console already shows it as a receipt the agent could not
        # read, rather than as one that silently stopped.
        self.assertIn('":parked": "needs_human"', self.fn)
        claim = self.worker.split("def _claim(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("queued", claim)

    def test_the_reason_survives_the_parking(self):
        self.assertIn("gave up after", self.fn)


class WhatItSpendsIsRecorded(unittest.TestCase):
    """The question "what does a receipt cost" had no answer but the invoice
    divided by a guess."""

    def setUp(self):
        self.llm = src("llm.py")

    def test_every_call_logs_its_usage(self):
        # Two call sites, plus the definition.
        self.assertEqual(2, self.llm.count("        _log_usage("))
        self.assertIn('_log_usage("extract"', self.llm)
        self.assertIn('_log_usage("explain"', self.llm)

    def test_the_line_is_parseable_by_a_log_filter(self):
        fn = self.llm.split("def _log_usage(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("openai_usage call=%s", fn)
        for field in ("prompt=", "completion=", "total="):
            self.assertIn(field, fn)

    def test_reasoning_tokens_are_counted_too(self):
        # They are billed as output and are invisible in the completion count.
        fn = self.llm.split("def _log_usage(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("reasoning_tokens", fn)

    def test_accounting_can_never_fail_an_audit(self):
        fn = self.llm.split("def _log_usage(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("except Exception:", fn)


if __name__ == "__main__":
    unittest.main()
