"""Resending a bill that was rejected.

A DTDC bill for INR 1,400 was rejected. The submitter photographed it again and
sent it, and the second claim came back `possible_duplicate` of the rejected
one - "if it is the same bill twice, reject it" - which is precisely what
rejection is supposed to prevent. It is not a duplicate of anything: the first
claim was never paid, and resending after a rejection is the normal, intended
next step.

Rejection did release fingerprints. It released two of the four a claim holds,
and the two it released are the two that cannot match a resubmission:

    #file#   identical bytes - a second photograph is never the same bytes
    #invoice# read off the bill by the model, and it varies between readings.
              Here: `d350596726` against `d3505966726`, one digit dropped

while the two it left held are the two that always match one:

    #sender# same person, same day, same amount, same currency - which is the
             definition of somebody resending their own bill
    #shape#  the same filename and byte count, which WhatsApp gives every
             photograph as `receipt.jpg`

So the release fired on the keys with nothing to give back and skipped the key
that was actually holding the door shut. `release_all` takes the claim and
gives back everything it holds, including the two that were never written down
as such and have to be rebuilt from the row.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

import duplicates

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class Refused(Exception):
    pass


class _Exceptions:
    ConditionalCheckFailedException = Refused


class _Client:
    exceptions = _Exceptions()


class _Meta:
    client = _Client()


class FakeTable:
    """Just enough of the fingerprint table to hold and give back."""

    meta = _Meta()

    def __init__(self):
        self.rows = {}
        self.refused = []

    def put(self, key, owner):
        self.rows[key] = owner

    def put_item(self, Item, ConditionExpression=None):
        key = Item["fingerprint"]
        if key in self.rows:
            raise Refused("already held")
        self.rows[key] = Item["submission_id"]

    def get_item(self, Key):
        key = Key["fingerprint"]
        if key not in self.rows:
            return {}
        return {"Item": {"fingerprint": key, "submission_id": self.rows[key]}}

    def delete_item(self, Key, ConditionExpression=None,
                    ExpressionAttributeValues=None):
        key = Key["fingerprint"]
        want = (ExpressionAttributeValues or {}).get(":s")
        if key not in self.rows or self.rows[key] != want:
            self.refused.append(key)
            raise RuntimeError("condition failed")
        del self.rows[key]


class TheClaimGivesBackEverythingItHolds(unittest.TestCase):

    ORG = "org_5f9e1ddc2b595007"
    FIRST = "sub_1789725271122_595007"
    SECOND = "sub_1789725770464_595007"

    def setUp(self):
        self.table = FakeTable()
        self._real = duplicates._table
        duplicates._table = self.table
        self.addCleanup(setattr, duplicates, "_table", self._real)
        # The claim as it sits in Submissions after being audited, with the
        # real values off the DTDC pair.
        self.item = {
            "submission_id": self.FIRST,
            "org_id": self.ORG,
            "fingerprint": f"{self.ORG}#invoice#dtdc#d350596726",
            "sender_fingerprint":
                f"{self.ORG}#sender#jayakumar@mobil80.com#2026-09-18#INR#1400.00",
            "receipt_sha256": "0af49b9289b563af50a9880965057a5d",
            "receipt_name": "receipt.jpg",
            "receipt_bytes": 91243,
        }
        for key in (self.item["fingerprint"], self.item["sender_fingerprint"],
                    duplicates.file_key(self.ORG, self.item["receipt_sha256"]),
                    duplicates.file_shape_key(self.ORG, "receipt.jpg", 91243)):
            self.table.put(key, self.FIRST)

    def test_all_four_are_released(self):
        self.assertEqual(4, duplicates.release_all(self.item))
        self.assertEqual({}, self.table.rows)

    def test_the_sender_key_is_the_one_that_was_being_left_behind(self):
        # The defect in one assertion: this is the key a resubmission always
        # collides with, and it stayed held through a rejection.
        duplicates.release_all(self.item)
        self.assertNotIn(self.item["sender_fingerprint"], self.table.rows)

    def test_the_shape_key_is_rebuilt_from_the_row(self):
        # It is never stored as a fingerprint, so it has to be recomputed from
        # the filename and byte count or it can never be given back.
        duplicates.release_all(self.item)
        self.assertNotIn(duplicates.file_shape_key(self.ORG, "receipt.jpg", 91243),
                         self.table.rows)

    def test_the_resubmission_is_then_free_to_claim_it(self):
        duplicates.release_all(self.item)
        self.assertIsNone(
            duplicates.claim(self.item["sender_fingerprint"], self.SECOND))

    def test_before_the_fix_it_would_have_been_flagged(self):
        # The old release, spelled out: invoice key and file key only.
        duplicates.release(self.item["fingerprint"], self.FIRST)
        duplicates.release(duplicates.file_key(self.ORG,
                                               self.item["receipt_sha256"]),
                           self.FIRST)
        self.assertEqual(
            self.FIRST,
            duplicates.claim(self.item["sender_fingerprint"], self.SECOND),
            "this is the possible_duplicate the submitter was shown")

    def test_it_will_not_release_a_key_another_claim_now_owns(self):
        # Rejecting an old claim must not unlock a live one's fingerprint.
        self.table.put(self.item["sender_fingerprint"], "sub_someone_else")
        duplicates.release_all(self.item)
        self.assertEqual("sub_someone_else",
                         self.table.rows[self.item["sender_fingerprint"]])

    def test_a_claim_with_no_id_releases_nothing(self):
        self.assertEqual(0, duplicates.release_all(
            {k: v for k, v in self.item.items() if k != "submission_id"}))
        self.assertEqual(4, len(self.table.rows))

    def test_a_claim_audited_before_the_sender_key_existed_still_works(self):
        thin = {"submission_id": self.FIRST, "org_id": self.ORG,
                "fingerprint": self.item["fingerprint"]}
        self.assertEqual(1, duplicates.release_all(thin))

    def test_decimal_byte_counts_survive_dynamodb(self):
        import decimal
        self.item["receipt_bytes"] = decimal.Decimal("91243")
        self.assertEqual(4, duplicates.release_all(self.item))


class EveryPlaceAClaimEndsUnpaidUsesIt(unittest.TestCase):
    """Three routes to an unpaid claim, and all three have to let go."""

    def setUp(self):
        self.auth = read("lambda_src/auth.py")

    def test_rejection_at_review(self):
        block = self.auth.split('if action in ("rejected", "withdrawn"):', 1)[1]
        self.assertIn("duplicates.release_all(", block[:300])

    def test_rejection_at_settlement(self):
        block = self.auth.split('if kind == "rejected":', 1)[1]
        self.assertIn("duplicates.release_all(", block[:300])

    def test_withdrawal_when_the_person_leaves(self):
        block = self.auth.split("def _withdraw_open_claims(", 1)[1].split(
            "\ndef ", 1)[0]
        self.assertIn("duplicates.release_all(", block)

    def test_no_route_releases_only_some_of_them(self):
        # The partial release is what the bug was. It must not come back
        # anywhere a claim ends unpaid.
        self.assertNotIn(
            'duplicates.release(str(item.get("fingerprint", "") or "")',
            self.auth)
        self.assertNotIn(
            'duplicates.release(str(row.get("fingerprint", "") or "")',
            self.auth)


if __name__ == "__main__":
    unittest.main()
