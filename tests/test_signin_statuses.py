"""Who may be sent a sign-in code.

This exists because of a real deadlock that shipped: sign-in required an
`active` membership, but a membership only becomes `active` when its owner
signs in. Everyone who was invited was locked out for ever, and the symptom was
silence - the endpoint answers identically whether or not it sent anything, so
nothing looked wrong from outside.

The rule that resolves it: **signing in is how an invitation is accepted**, so
sign-in is the one place `invited` counts. Everywhere else - receipts by email,
receipts over WhatsApp - still needs an accepted invitation.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
for var in ("USERS_TABLE", "ORGS_TABLE"):
    os.environ.setdefault(var, "t")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")

import identity  # noqa: E402


def rows(*statuses):
    return [{"email": "rana@mobil80.com", "org_id": f"org_{i}", "status": s,
             "added_at": 1700 + i, "email_channel": "pending", "role": "staff"}
            for i, s in enumerate(statuses)]


class WhoMaySignIn(unittest.TestCase):
    def _resolve(self, items):
        with mock.patch.object(identity, "_users") as users:
            users.query.return_value = {"Items": items}
            return identity.resolve_for_signin("rana@mobil80.com")

    def test_an_invited_person_can_sign_in(self):
        # The bug: this returned None, so the invitation could never be
        # accepted and no code was ever sent.
        self.assertIsNotNone(self._resolve(rows("invited")))

    def test_an_active_person_can_sign_in(self):
        self.assertIsNotNone(self._resolve(rows("active")))

    def test_a_pending_email_channel_does_not_block_sign_in(self):
        # email_channel is 'pending' until acceptance - which is the very state
        # this call exists to let somebody out of.
        got = self._resolve(rows("invited"))
        self.assertEqual(got["email_channel"], "pending")

    def test_a_removed_person_cannot(self):
        self.assertIsNone(self._resolve(rows("removed")))

    def test_a_suspended_person_cannot(self):
        self.assertIsNone(self._resolve(rows("suspended")))

    def test_an_unknown_address_cannot(self):
        self.assertIsNone(self._resolve([]))

    def test_a_blank_address_never_queries(self):
        with mock.patch.object(identity, "_users") as users:
            self.assertIsNone(identity.resolve_for_signin(""))
            users.query.assert_not_called()

    def test_the_most_recent_membership_wins(self):
        got = self._resolve(rows("active", "invited"))
        self.assertEqual(got["org_id"], "org_1")

    def test_a_removed_membership_does_not_shadow_a_live_one(self):
        # Removed from one organisation, invited to another: they sign in.
        got = self._resolve(rows("invited", "removed"))
        self.assertEqual(got["status"], "invited")


class SignInIsLooserThanIntakeOnPurpose(unittest.TestCase):
    """An invited person may sign in. Their receipts still wait for acceptance."""

    def test_an_invited_member_cannot_yet_send_receipts_by_email(self):
        with mock.patch.object(identity, "_users") as users:
            users.query.return_value = {"Items": rows("invited")}
            self.assertIsNone(identity.resolve_by_email("rana@mobil80.com", "email"))

    def test_an_invited_member_cannot_send_receipts_over_whatsapp(self):
        with mock.patch.object(identity, "_users") as users:
            users.query.return_value = {"Items": rows("invited")}
            self.assertIsNone(identity.resolve_by_mobile("+919845004028"))

    def test_removed_is_absent_from_the_sign_in_statuses(self):
        for blocked in ("removed", "suspended"):
            self.assertNotIn(blocked, identity.SIGN_IN_STATUSES, blocked)


if __name__ == "__main__":
    unittest.main()
