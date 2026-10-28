"""A credential that lives in a config file for a year.

The MCP server runs on a laptop and reads the reports. It needs something
that outlives a sign-in, because a token somebody has to re-paste every
twelve hours is not a setup, it is a chore that gets abandoned - and the
first thing anybody would reach for, a session token with a longer expiry,
is the one thing that must never be done.

**A session token is a credential for the whole console.** Every endpoint
that asks who is calling asks `_identity_from_token`, so a year-long session
token sitting in `claude_desktop_config.json` would approve claims, record
payments and read the settlement history. The file is plaintext, it gets
copied between machines, and it ends up in backups.

So the safety is in reach rather than in lifetime. The token carries a
`kind`, `_identity_from_token` refuses anything carrying one, and exactly
one function in the codebase honours it - `_reader_of`, which serves reads.
That is the whole blast radius.

Revocation has no table behind it: the membership row carries `mcp_from`,
and a token issued before that moment is dead. Minting moves the mark, so
there is never a forgotten second key to the same door.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lambda_src"))
for _k, _v in (("AWS_DEFAULT_REGION", "ap-southeast-1"), ("USERS_TABLE", "u"),
               ("ORGS_TABLE", "o"), ("INTAKE_TABLE", "t"), ("AUTH_TABLE", "a"),
               ("ADMINS_TABLE", "d"), ("EXPENSES_TABLE", "e"),
               ("ADVANCES_TABLE", "adv"), ("OTP_SENDER", "no-reply@expenze.ai"),
               ("INTAKE_ADDRESS", "receipts@expenze.ai"),
               ("SESSION_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:y")):
    os.environ.setdefault(_k, _v)

import auth  # noqa: E402

ME = "riyad@mobil80.com"


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class Signed(unittest.TestCase):
    """Both token kinds, signed with a stubbed key."""

    def setUp(self):
        self.patch = mock.patch.object(auth, "_key", return_value=b"a-test-key")
        self.patch.start()
        self.addCleanup(self.patch.stop)


class AnMcpTokenOpensOneDoor(Signed):

    def test_it_is_not_a_session(self):
        """The property the whole design rests on.

        If this ever starts passing, a config file on a laptop becomes a
        credential that can approve a claim.
        """
        self.assertIsNone(
            auth._identity_from_token(auth._sign_mcp(ME, int(time.time()))))

    def test_and_an_ordinary_session_still_is(self):
        self.assertEqual(ME, auth._identity_from_token(auth._sign_session(ME)))

    def test_any_future_kind_is_refused_by_default(self):
        # Refusing every `kind` rather than listing the ones to refuse means a
        # token type added later is locked out until somebody decides
        # otherwise, instead of being quietly admitted.
        made_up = auth._sign({"email": ME, "kind": "something-new",
                              "exp": int(time.time()) + 60})
        self.assertIsNone(auth._identity_from_token(made_up))

    def test_a_forged_one_is_refused(self):
        token = auth._sign_mcp(ME, int(time.time()))
        body, sig = token.split(".", 1)
        self.assertIsNone(auth._claims_of(f"{body}.{sig[:-2]}xy"))

    def test_an_expired_one_is_refused(self):
        stale = auth._sign({"email": ME, "kind": "mcp", "iat": 1,
                            "exp": int(time.time()) - 1})
        self.assertIsNone(auth._claims_of(stale))

    def test_it_lasts_long_enough_to_be_worth_setting_up(self):
        self.assertEqual(365 * 24 * 60 * 60, auth.MCP_TTL_SECONDS)


class WhoMayReadAReport(Signed):

    def member(self, **over):
        row = {"email": ME, "org_id": "org-1", "role": "owner",
               "status": "active"}
        row.update(over)
        return row

    def reader(self, token, member):
        with mock.patch.object(auth.identity, "resolve_by_email",
                               return_value=member):
            return auth._reader_of(token)

    def test_a_signed_in_person(self):
        self.assertIsNotNone(
            self.reader(auth._sign_session(ME), self.member()))

    def test_and_their_mcp_token(self):
        now = int(time.time())
        self.assertIsNotNone(
            self.reader(auth._sign_mcp(ME, now), self.member(mcp_from=now)))

    def test_a_token_issued_before_the_mark_is_dead(self):
        # Which is what revoking does, and what minting a replacement does to
        # the one it replaces.
        now = int(time.time())
        self.assertIsNone(
            self.reader(auth._sign_mcp(ME, now - 10), self.member(mcp_from=now)))

    def test_a_token_issued_on_the_mark_still_lives(self):
        # The one that was just minted. An off-by-one here kills the token in
        # the same breath as handing it over.
        now = int(time.time())
        self.assertIsNotNone(
            self.reader(auth._sign_mcp(ME, now), self.member(mcp_from=now)))

    def test_somebody_with_no_membership_reads_nothing(self):
        self.assertIsNone(self.reader(auth._sign_session(ME), None))

    def test_nor_does_a_token_of_some_other_kind(self):
        other = auth._sign({"email": ME, "kind": "intake",
                            "exp": int(time.time()) + 60})
        self.assertIsNone(self.reader(other, self.member()))

    def test_nor_does_rubbish(self):
        for junk in ("", "x", "x.y", "....."):
            self.assertIsNone(self.reader(junk, self.member()), junk)


class OnlyTheReportsHonourIt(unittest.TestCase):
    """One function in the codebase serves this token, and it reads."""

    def setUp(self):
        self.app = read("lambda_src/auth.py")

    def test_the_reports_are_the_only_caller(self):
        self.assertEqual(1, self.app.count("_reader_of(token)"),
                         "something other than the reports honours an MCP token")

    def test_every_other_endpoint_goes_through_the_session_check(self):
        # If this count ever drops sharply, something has been quietly moved
        # onto the long-lived credential.
        self.assertGreater(self.app.count("_identity_from_token("), 20)

    def test_the_mint_needs_a_real_session(self):
        """It cannot renew itself.

        Minting from an MCP token would let the credential outlive the moment
        somebody revoked it: revoke, and the holder quietly issues another.
        """
        fn = self.app.split("def _mcp_token(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("actor = _identity_from_token(token)", fn)
        self.assertNotIn("_reader_of", fn)

    def test_and_the_role_bar(self):
        fn = self.app.split("def _mcp_token(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if not runs_the_org(acting):", fn)

    def test_revoking_and_minting_are_the_same_write(self):
        fn = self.app.split("def _mcp_token(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('UpdateExpression="SET mcp_from = :t"', fn)
        # The write happens before the branch, so revoking cannot be skipped
        # by asking for a replacement and vice versa.
        self.assertLess(fn.index("mcp_from = :t"), fn.index('body.get("revoke")'))

    def test_both_are_written_down(self):
        fn = self.app.split("def _mcp_token(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("reports token issued", fn)
        self.assertIn("reports token revoked", fn)

    def test_the_route_exists_and_is_past_the_email_gate(self):
        # Two things a new path needs here, and missing either produces an
        # error about something else entirely.
        self.assertIn('if path.endswith("/mcp-token"):', self.app)
        self.assertIn('"/reports", "/mcp-token",', self.app)
        self.assertIn('auth.add_resource("mcp-token").add_method("POST", auth_integration)',
                      read("expensifyai/stack.py"))


class TheMcpServerCarriesIt(unittest.TestCase):

    def test_it_reads_the_token_from_its_environment(self):
        # Never a literal in the file: the server is in the repository and the
        # token is not.
        source = read("../MCP/expenze_mcp.py")
        self.assertIn('TOKEN = os.environ.get("EXPENZE_TOKEN", "")', source)
        self.assertNotIn("exp_live_", source)
        self.assertNotIn("exp_test_", source)


if __name__ == "__main__":
    unittest.main()
