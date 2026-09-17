"""Four roles in one order, and one comparison answers every question.

    owner    one per organisation. Appoints and removes administrators, and is
             the only role that can hand the organisation to somebody else.
    admin    as many as needed. Runs the place day to day, but may not touch
             another admin or the owner - so an administrator cannot quietly
             promote themselves or lock the owner out.
    finance  reviews and settles claims, and manages the people who submit.
    staff    sends receipts.

The rule: you may act on a role strictly beneath your own. Strictly, because
equal ranks managing each other is how two administrators end up able to
remove one another and the last one standing is whoever clicked first.

`owner` is the exception it looks like. Exactly one exists, so appointing
another is not appointing - it is transferring, which is a different act with
a different confirmation and both sides written at once.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
# auth.py reads its table names at import. Nothing here touches a table; they
# only have to be nameable.
for _k, _v in (("AWS_DEFAULT_REGION", "ap-southeast-1"), ("USERS_TABLE", "u"),
               ("ORGS_TABLE", "o"), ("INTAKE_TABLE", "t"), ("AUTH_TABLE", "a"),
               ("ADMINS_TABLE", "d"), ("EXPENSES_TABLE", "e"),
               ("SESSION_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:y")):
    os.environ.setdefault(_k, _v)

ROOT = os.path.join(os.path.dirname(__file__), "..")


def src(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TheOrderIsOneComparison(unittest.TestCase):

    def setUp(self):
        import auth
        self.auth = auth

    def test_the_four_ranks(self):
        self.assertEqual({"staff": 0, "finance": 1, "admin": 2, "owner": 3},
                         self.auth.RANK)

    def test_you_may_act_beneath_you_and_no_higher(self):
        self.assertTrue(self.auth.may_manage("owner", "admin"))
        self.assertTrue(self.auth.may_manage("admin", "finance"))
        self.assertTrue(self.auth.may_manage("finance", "staff"))
        self.assertFalse(self.auth.may_manage("admin", "owner"))
        self.assertFalse(self.auth.may_manage("finance", "admin"))
        self.assertFalse(self.auth.may_manage("staff", "staff"))

    def test_and_never_on_your_own_rank(self):
        # The whole reason it is strict: two administrators removing each
        # other leaves whoever clicked first.
        for role in self.auth.ROLES:
            self.assertFalse(self.auth.may_manage(role, role), role)

    def test_an_unknown_role_carries_no_authority(self):
        self.assertEqual(0, self.auth.rank_of("wizard"))
        self.assertFalse(self.auth.may_manage("wizard", "staff"))

    def test_it_reads_a_membership_or_a_bare_role(self):
        self.assertEqual(3, self.auth.rank_of({"role": "owner"}))
        self.assertEqual(3, self.auth.rank_of("owner"))
        self.assertEqual(0, self.auth.rank_of({}))


class OwnershipMovesRatherThanBeingAssigned(unittest.TestCase):

    def setUp(self):
        self.auth = src("lambda_src", "auth.py")
        self.update = self.auth.split("def _member_update(", 1)[1].split("\ndef ", 1)[0]
        self.invite = self.auth.split("def _invite(", 1)[1].split("\ndef ", 1)[0]
        self.transfer = self.auth.split("def _transfer_ownership(", 1)[1].split("\ndef ", 1)[0]

    def test_the_role_dropdown_refuses_it(self):
        self.assertIn('if role == "owner":', self.update)
        self.assertIn("Use Transfer ownership", self.update)

    def test_an_invitation_refuses_it_too(self):
        # The quieter of the two doors to the same privilege.
        self.assertIn('if role == "owner":', self.invite)

    def test_the_transfer_writes_both_sides(self):
        self.assertIn('":owner": "owner"', self.transfer)
        self.assertIn('":admin": "admin"', self.transfer)

    def test_the_outgoing_owner_lands_as_an_administrator(self):
        # They were running the organisation a moment ago; dropping them to
        # the bottom on the way out is a second decision nobody asked for.
        self.assertIn('":admin": "admin"', self.transfer)

    def test_only_the_owner_may_do_it(self):
        self.assertIn('if org["_role"] != "owner":', self.transfer)

    def test_not_to_somebody_who_has_never_signed_in(self):
        # Handing it to an unaccepted invitation locks everybody out of the
        # things only an owner may do.
        self.assertIn('if str(member.get("status") or "") == "invited":', self.transfer)

    def test_the_root_address_follows_the_owner(self):
        # It is what billing and recovery read.
        self.assertIn("SET root_email = :e", self.transfer)

    def test_and_it_is_written_into_the_audit_log(self):
        self.assertIn('"ownership transferred"', self.transfer)


class TheLastOwnerCannotBeDemoted(unittest.TestCase):

    def test_the_count_is_of_the_organisation_not_the_person(self):
        auth = src("lambda_src", "auth.py")
        fn = auth.split("def owners_of(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("identity.members_of(org_id)", fn)
        self.assertIn('!= "removed"', fn)

    def test_and_the_guard_uses_it(self):
        auth = src("lambda_src", "auth.py")
        update = auth.split("def _member_update(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('if len(owners_of(org["org_id"])) <= 1:', update)


class TheConsoleOffersOnlyWhatItCanGrant(unittest.TestCase):

    def setUp(self):
        self.app = src("..", "PORTAL", "app.html")

    def test_the_ranks_match_the_server(self):
        block = self.app.split("const RANK = {", 1)[1].split("}", 1)[0]
        for role, n in (("staff", 0), ("finance", 1), ("admin", 2), ("owner", 3)):
            self.assertIn(f"{role}: {n}", block)

    def test_owner_is_never_in_an_assignable_list(self):
        fn = self.app.split("const assignableBy = (mine) =>", 1)[1].split(";", 1)[0]
        self.assertNotIn("owner", fn)

    def test_a_role_you_cannot_change_is_dead_rather_than_refused(self):
        # The refusal arriving as a red line after the click, on a control
        # that looked live, is the thing this avoids.
        self.assertIn("roleSel.disabled = person.status === \"removed\" || !mayManage(mine, theirs);",
                      self.app)

    def test_but_their_current_role_still_shows(self):
        # Otherwise the dropdown silently displays a different role from the
        # one they hold.
        self.assertIn("if (!offered.includes(theirs)) offered.push(theirs);", self.app)

    def test_an_administrator_runs_the_place_but_does_not_hold_the_chequebook(self):
        block = self.app.split("const can = {", 1)[1].split("};", 1)[0]
        self.assertIn("editRules:  () => rankOf(currentUser.tier) >= RANK.admin", block)
        self.assertIn('buyCredits: () => currentUser.tier === "owner"', block)
        self.assertIn('transfer:   () => currentUser.tier === "owner"', block)


if __name__ == "__main__":
    unittest.main()
