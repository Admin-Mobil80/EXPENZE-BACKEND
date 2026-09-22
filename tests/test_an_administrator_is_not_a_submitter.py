"""Rana Ghosh signed in as an Administrator and the console said SUBMITTER.

His membership row said `admin`, and had since the day he was appointed. The
console demoted him on sight, drew no Administration tab, and left him with his
own receipts - and the server, asked, would have agreed with the console.

One omission in two places. Administrators were added as a *rank*: RANK,
ROLE_LABEL, `may_manage`, a role picker in the People tab, a rule about who may
appoint whom. Everything that decides what somebody may actually do was written
earlier, when owner and finance were the only roles above a submitter, and was
written as a literal list of those two:

    const tier = ["owner", "finance", "staff"].includes(data.role) ? ... : "staff"
    if org["_role"] not in ("owner", "finance"):

`admin` matches neither. So the role existed, could be granted, showed on the
roll, and conferred nothing: an administrator outranked a finance executive and
could reach less of the product than they could, and no more than a submitter.

The fix is the same in both places - ask the rank, not a list - so that the
next role to be added is placed by where it ranks instead of by remembering
every gate it belongs in.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
# auth.py reads its table names at import; nothing here touches a table.
for _k, _v in (("AWS_DEFAULT_REGION", "ap-southeast-1"), ("USERS_TABLE", "u"),
               ("ORGS_TABLE", "o"), ("INTAKE_TABLE", "t"), ("AUTH_TABLE", "a"),
               ("ADMINS_TABLE", "d"), ("EXPENSES_TABLE", "e"),
               ("SESSION_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:y")):
    os.environ.setdefault(_k, _v)

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class TheConsoleBelievesTheRoleItIsGiven(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_the_tier_comes_from_the_ranks_not_a_list(self):
        self.assertIn(
            'const tier = Object.prototype.hasOwnProperty.call(RANK, data.role)',
            self.app)

    def test_the_list_that_omitted_administrators_is_gone(self):
        self.assertNotIn('["owner", "finance", "staff"].includes(data.role)',
                         self.app)

    def test_every_role_the_server_can_send_is_known_to_it(self):
        # The guard is only as good as RANK being the full set of roles.
        self.assertIn("const RANK = { staff: 0, finance: 1, admin: 2, owner: 3 };",
                      self.app)

    def test_an_administrator_gets_the_administration_surface(self):
        self.assertIn('admin: "Administration"', self.app)

    def test_and_lands_on_a_tab_that_exists_for_them(self):
        self.assertIn('admin: "reports"', self.app)


class TheServerAsksTheRankToo(unittest.TestCase):

    def setUp(self):
        self.auth = read("lambda_src/auth.py")

    def test_the_helper_exists_and_puts_the_floor_at_finance(self):
        self.assertIn('return rank_of(who) >= RANK["finance"]', self.auth)

    def test_no_gate_is_still_written_as_a_pair_of_role_names(self):
        # One survivor, deliberately: buying credits. Everything else is
        # ranked. Counting rather than asserting absence, so the exception
        # cannot quietly grow back into a habit.
        lines = [l for l in self.auth.splitlines()
                 if '"owner", "finance"' in l and not l.strip().startswith("#")
                 and "`" not in l]
        self.assertEqual(1, len(lines), lines)

    def test_an_administrator_may_invite_people(self):
        # The user's rule: administrators invite submitters. They could not
        # invite anybody, while the finance executive below them could.
        block = self.auth.split("def _invite(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('rank_of(membership) < RANK["finance"]', block)

    def test_the_surfaces_an_administrator_was_shut_out_of(self):
        for name in ("_budgets_put", "_org_put", "_member_groups",
                     "_member_update", "_people", "_audit_log",
                     "_claim_outcome"):
            block = self.auth.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]
            self.assertIn("runs_the_org(", block, name)

    def test_deciding_a_claim_is_ranked(self):
        # Deciding one is ranked higher still - an owner's or an
        # administrator's, never a finance executive's. See `may_review`.
        self.assertIn('if action in DECIDING_ACTIONS and action != "rejected" '
                      'and not may_review(acting):', self.auth)
        self.assertIn("and not runs_the_org(acting):", self.auth)

    def test_the_refusals_name_the_role_that_is_now_allowed(self):
        # A message that lists two roles when three are allowed is a support
        # ticket from the administrator who reads it and believes it.
        self.assertNotIn("Only an owner or finance executive can set", self.auth)
        self.assertIn("Only an owner, administrator or finance executive can",
                      self.auth)

    def test_buying_credits_was_not_widened_with_the_rest(self):
        # Spending the company's money is not part of running its settings,
        # and nobody asked for administrators to be given the card.
        block = self.auth.split("def _credits_order(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('org["_role"] not in ("owner", "finance")', block)
        self.assertNotIn("runs_the_org(", block)


class TheRankHelper(unittest.TestCase):
    """`runs_the_org` takes whatever the caller happens to be holding."""

    def test_roles_above_a_submitter_run_the_organisation(self):
        import auth
        for role in ("finance", "admin", "owner"):
            self.assertTrue(auth.runs_the_org(role), role)
            self.assertTrue(auth.runs_the_org({"role": role}), role)

    def test_a_submitter_does_not(self):
        import auth
        self.assertFalse(auth.runs_the_org("staff"))
        self.assertFalse(auth.runs_the_org({"role": "staff"}))

    def test_nor_does_a_role_nobody_recognises(self):
        import auth
        self.assertFalse(auth.runs_the_org("administrator"))
        self.assertFalse(auth.runs_the_org(None))
        self.assertFalse(auth.runs_the_org({}))

    def test_an_administrator_outranks_a_finance_executive(self):
        # The inversion the defect produced: the higher rank could do less.
        import auth
        self.assertGreater(auth.rank_of("admin"), auth.rank_of("finance"))


if __name__ == "__main__":
    unittest.main()
