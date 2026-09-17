"""One call, one charge - even when the caller retries.

An integration that retries a timed-out POST is not misbehaving; it is doing
the correct thing, and until this existed it paid twice for one receipt and
left a duplicate claim for a finance executive to find and reject.

The interesting cases are not the happy replay. They are the two ways a
reservation can exist with nothing behind it - a charge that was refused for
want of credits, and a crash between reserving the key and recording what it
produced. In both, answering "already done" would point the caller at a claim
that does not exist and lose the receipt for good, so the retry must be let
through instead.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
os.environ.setdefault("ORGS_TABLE", "orgs")
os.environ.setdefault("INTAKE_TABLE", "intake")
os.environ.setdefault("USERS_TABLE", "users")

from test_apikeys import FakeTable  # noqa: E402

import intake  # noqa: E402


class Reservation(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable("idem_id")
        patch = mock.patch.object(intake, "_idem", self.table)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_key_seen_for_the_first_time_is_let_through(self):
        self.assertEqual(intake._claim_idempotency("org-1", "erp-88421"), (True, ""))

    def test_the_same_key_again_returns_the_original_submission(self):
        intake._claim_idempotency("org-1", "erp-88421")
        intake._record_idempotency("org-1", "erp-88421", "sub-7")
        self.assertEqual(intake._claim_idempotency("org-1", "erp-88421"), (False, "sub-7"))

    def test_a_reservation_with_nothing_behind_it_is_not_a_duplicate(self):
        # The charge was refused, or the function died before recording. The
        # caller gets no submission id, and lambda_handler lets it retry.
        intake._claim_idempotency("org-1", "erp-88421")
        self.assertEqual(intake._claim_idempotency("org-1", "erp-88421"), (False, ""))

    def test_the_same_key_in_two_organisations_is_two_different_receipts(self):
        # Integrators pick their own keys. An invoice number that happens to
        # collide across customers must not swallow one of the receipts.
        intake._claim_idempotency("org-1", "INV-1001")
        intake._record_idempotency("org-1", "INV-1001", "sub-a")
        self.assertEqual(intake._claim_idempotency("org-2", "INV-1001"), (True, ""))

    def test_no_key_supplied_means_no_reservation(self):
        self.assertEqual(intake._claim_idempotency("org-1", ""), (True, ""))
        self.assertEqual(self.table.rows, {})

    def test_a_reservation_expires_so_the_table_does_not_grow_forever(self):
        intake._claim_idempotency("org-1", "erp-88421")
        row = list(self.table.rows.values())[0]
        window = row["expires_at"] - row["created_at"]
        self.assertEqual(window, intake.IDEMPOTENCY_DAYS * 86400)

    def test_recording_never_throws_into_the_request(self):
        # The receipt has already been accepted and charged at this point;
        # failing the response over a bookkeeping write would be worse than
        # letting one retry through.
        intake._claim_idempotency("org-1", "erp-88421")
        with mock.patch.object(self.table, "update_item", side_effect=RuntimeError("throttled")):
            intake._record_idempotency("org-1", "erp-88421", "sub-7")


class NoTableConfigured(unittest.TestCase):
    def test_receipts_still_flow(self):
        # A build without the table loses deduplication, which is a degraded
        # service. Refusing every API receipt would be an outage.
        with mock.patch.object(intake, "_idem", None):
            self.assertEqual(intake._claim_idempotency("org-1", "k"), (True, ""))
            intake._record_idempotency("org-1", "k", "sub-1")


class ApiKeyRequired(unittest.TestCase):
    """`/intake/api` used to take an unauthenticated POST and spend a credit."""

    def _post(self, headers=None, body="{}"):
        return intake.lambda_handler(
            {"path": "/intake/api", "headers": headers or {}, "body": body}, None)

    def test_no_key_is_refused_before_anything_is_charged(self):
        with mock.patch.object(intake.apikeys, "resolve", return_value=None) as resolve, \
             mock.patch.object(intake, "_charge_and_record") as charge:
            reply = self._post()
        self.assertEqual(reply["statusCode"], 401)
        charge.assert_not_called()
        resolve.assert_called()

    def test_an_unknown_key_is_refused(self):
        with mock.patch.object(intake.apikeys, "resolve", return_value=None), \
             mock.patch.object(intake, "_charge_and_record") as charge:
            reply = self._post({"Authorization": "Bearer exp_test_nope"})
        self.assertEqual(reply["statusCode"], 401)
        charge.assert_not_called()

    def test_whatsapp_and_email_do_not_need_one(self):
        # They arrive through adapters that already proved where they came
        # from; only a bare HTTP POST proves nothing.
        for path in ("/intake/whatsapp", "/intake/email"):
            with mock.patch.object(intake.apikeys, "resolve") as resolve, \
                 mock.patch.object(intake.identity, "resolve_sender", return_value=None):
                intake.lambda_handler({"path": path, "headers": {}, "body": "{}"}, None)
            resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
