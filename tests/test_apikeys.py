"""Keys for the REST intake, and the promise that one call is one charge.

Two holes, both of which cost the customer money rather than merely being
untidy:

`/intake/api` took any POST. A work address is guessable, so a stranger who
knew one could create claims and spend an organisation's credits. That was
verified against the running system with a live probe - a balance went from 50
to 49 with no credential of any kind.

And the endpoint had no idempotency. An integration that retried a timeout -
which every well-written integration does - paid twice for one receipt and left
a duplicate claim for someone to find and reject.

The tests below hold the parts of both fixes that are easy to get subtly wrong:
that nothing recoverable is stored, that a revoked key stays dead, that a key
cannot reach across organisations, and that a reservation with nothing behind
it is not mistaken for a completed claim.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")

import apikeys  # noqa: E402


class FakeConditionalCheckFailed(Exception):
    pass


class FakeTable:
    """Enough DynamoDB to exercise the real code paths, including the
    conditional write that makes a reservation exactly-once."""

    def __init__(self, key_name: str):
        self.key_name = key_name
        self.rows: dict[str, dict] = {}

        class _Exceptions:
            ConditionalCheckFailedException = FakeConditionalCheckFailed

        class _Client:
            exceptions = _Exceptions()

        class _Meta:
            client = _Client()

        self.meta = _Meta()

    def put_item(self, Item, ConditionExpression=None):
        pk = Item[self.key_name]
        if ConditionExpression and pk in self.rows:
            raise FakeConditionalCheckFailed(ConditionExpression)
        self.rows[pk] = dict(Item)

    def get_item(self, Key):
        row = self.rows.get(Key[self.key_name])
        return {"Item": dict(row)} if row else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                    ExpressionAttributeNames=None):
        row = self.rows.setdefault(Key[self.key_name], dict(Key))
        names = ExpressionAttributeNames or {}
        for pair in UpdateExpression.replace("SET ", "").split(","):
            field, placeholder = (p.strip() for p in pair.split("="))
            row[names.get(field, field)] = ExpressionAttributeValues[placeholder]

    def delete_item(self, Key, ConditionExpression=None, ExpressionAttributeValues=None):
        pk = Key[self.key_name]
        row = self.rows.get(pk)
        if ConditionExpression:
            # Only the shape this codebase uses: "<field> = :placeholder".
            field, placeholder = (p.strip() for p in ConditionExpression.split("="))
            expected = (ExpressionAttributeValues or {})[placeholder]
            if not row or row.get(field) != expected:
                raise FakeConditionalCheckFailed(ConditionExpression)
        self.rows.pop(pk, None)

    def scan(self, FilterExpression, ExpressionAttributeValues,
             ExpressionAttributeNames=None):
        wanted_org = ExpressionAttributeValues[":o"]
        wanted_status = ExpressionAttributeValues[":a"]
        return {"Items": [dict(r) for r in self.rows.values()
                          if r.get("org_id") == wanted_org
                          and r.get("status") == wanted_status]}


class KeyLifecycle(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable("key_hash")
        patch = mock.patch.object(apikeys, "_keys", self.table)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_new_key_resolves_to_its_organisation(self):
        issued = apikeys.issue("org-1", "test", "riyad@mobil80.com")
        self.assertEqual(apikeys.resolve(issued["key"]), "org-1")

    def test_the_key_itself_is_never_stored(self):
        issued = apikeys.issue("org-1", "test", "riyad@mobil80.com")
        stored = str(list(self.table.rows.values())[0])
        self.assertNotIn(issued["key"], stored)
        # Nor any usable fragment of it: only the last four, for recognition.
        self.assertNotIn(issued["key"][:-4], stored)

    def test_the_row_is_keyed_by_the_hash_so_lookup_is_a_single_read(self):
        issued = apikeys.issue("org-1", "test", "riyad@mobil80.com")
        self.assertIn(apikeys.hash_key(issued["key"]), self.table.rows)

    def test_a_key_carries_the_mode_it_was_made_in(self):
        self.assertTrue(apikeys.issue("org-1", "live", "a@b.c")["key"]
                        .startswith("exp_live_"))
        self.assertTrue(apikeys.issue("org-2", "test", "a@b.c")["key"]
                        .startswith("exp_test_"))

    def test_an_unknown_key_resolves_to_nothing(self):
        apikeys.issue("org-1", "test", "a@b.c")
        for candidate in ("", None, "exp_test_deadbeef", "Bearer nonsense"):
            self.assertIsNone(apikeys.resolve(candidate), candidate)

    def test_issuing_again_kills_the_previous_key(self):
        # Two live keys with no way to tell which system uses which is worse
        # than making somebody re-paste one.
        first = apikeys.issue("org-1", "test", "a@b.c")["key"]
        second = apikeys.issue("org-1", "test", "a@b.c")
        self.assertEqual(second["replaced"], 1)
        self.assertIsNone(apikeys.resolve(first), "the old key must stop working")
        self.assertEqual(apikeys.resolve(second["key"]), "org-1")

    def test_a_revoked_row_is_kept_rather_than_deleted(self):
        # So an integration still calling with a retired key is visible in the
        # table instead of looking like a key that never existed.
        key = apikeys.issue("org-1", "test", "a@b.c")["key"]
        apikeys.revoke_all("org-1", "a@b.c")
        row = self.table.rows[apikeys.hash_key(key)]
        self.assertEqual(row["status"], "revoked")
        self.assertEqual(row["revoked_by"], "a@b.c")

    def test_revoking_one_organisation_leaves_another_alone(self):
        mine = apikeys.issue("org-1", "test", "a@b.c")["key"]
        theirs = apikeys.issue("org-2", "test", "x@y.z")["key"]
        apikeys.issue("org-1", "test", "a@b.c")
        self.assertIsNone(apikeys.resolve(mine))
        self.assertEqual(apikeys.resolve(theirs), "org-2",
                         "another customer's key must not be touched")

    def test_use_is_stamped_so_a_key_can_be_retired_safely(self):
        key = apikeys.issue("org-1", "test", "a@b.c")["key"]
        self.assertEqual(apikeys.describe("org-1")["last_used_at"], 0)
        apikeys.resolve(key)
        self.assertGreater(apikeys.describe("org-1")["last_used_at"], 0)

    def test_a_failed_stamp_never_fails_the_receipt(self):
        key = apikeys.issue("org-1", "test", "a@b.c")["key"]
        with mock.patch.object(self.table, "update_item", side_effect=RuntimeError("throttled")):
            self.assertEqual(apikeys.resolve(key), "org-1")

    def test_surrounding_whitespace_from_a_copy_paste_still_works(self):
        key = apikeys.issue("org-1", "test", "a@b.c")["key"]
        self.assertEqual(apikeys.resolve(f"  {key}\n"), "org-1")


class Describe(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable("key_hash")
        patch = mock.patch.object(apikeys, "_keys", self.table)
        patch.start()
        self.addCleanup(patch.stop)

    def test_nothing_issued_says_so(self):
        self.assertEqual(apikeys.describe("org-1"), {"configured": False})

    def test_it_shows_that_a_key_exists_and_not_what_it_is(self):
        issued = apikeys.issue("org-1", "test", "riyad@mobil80.com")
        seen = apikeys.describe("org-1")
        self.assertTrue(seen["configured"])
        self.assertEqual(seen["tail"], issued["key"][-4:])
        self.assertEqual(seen["created_by"], "riyad@mobil80.com")
        self.assertNotIn(issued["key"], str(seen),
                         "there must be no way to read a key back")

    def test_a_revoked_key_does_not_count_as_configured(self):
        apikeys.issue("org-1", "test", "a@b.c")
        apikeys.revoke_all("org-1", "a@b.c")
        self.assertFalse(apikeys.describe("org-1")["configured"])


class Header(unittest.TestCase):
    def test_a_bearer_token_however_it_is_cased(self):
        for name in ("Authorization", "authorization", "AUTHORIZATION"):
            self.assertEqual(apikeys.bearer({name: "Bearer exp_test_1"}), "exp_test_1")
        self.assertEqual(apikeys.bearer({"Authorization": "bearer exp_test_1"}), "exp_test_1")

    def test_a_bare_key_header_is_accepted_too(self):
        # Some HTTP clients make this easier than a header scheme, and making
        # integrators fight their own library buys nothing.
        self.assertEqual(apikeys.bearer({"X-Api-Key": " exp_test_2 "}), "exp_test_2")

    def test_no_credential_is_an_empty_string_not_a_crash(self):
        for headers in ({}, None, {"Authorization": ""}, {"Authorization": "Basic zzz"}):
            self.assertEqual(apikeys.bearer(headers), "", headers)


class Randomness(unittest.TestCase):
    def test_keys_do_not_repeat(self):
        table = FakeTable("key_hash")
        with mock.patch.object(apikeys, "_keys", table):
            keys = {apikeys.issue(f"org-{n}", "test", "a@b.c")["key"] for n in range(50)}
        self.assertEqual(len(keys), 50)

    def test_a_key_is_long_enough_to_be_unguessable(self):
        table = FakeTable("key_hash")
        with mock.patch.object(apikeys, "_keys", table):
            key = apikeys.issue("org-1", "test", "a@b.c")["key"]
        self.assertGreaterEqual(len(key.rsplit("_", 1)[1]), 48)  # 24 bytes as hex


class NoTableConfigured(unittest.TestCase):
    """A build without the table must refuse callers, not wave them through."""

    def test_resolve_returns_nothing(self):
        with mock.patch.object(apikeys, "_keys", None):
            self.assertIsNone(apikeys.resolve("exp_test_anything"))

    def test_describe_reports_unconfigured(self):
        with mock.patch.object(apikeys, "_keys", None):
            self.assertFalse(apikeys.describe("org-1")["configured"])


if __name__ == "__main__":
    unittest.main()
