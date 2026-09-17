"""Reconstructing the decisions taken before the log existed.

The log started empty, and the decisions already made lived only as fields on
the claim - which is the very reason the log exists, because those fields are
overwritten by the next decision. So this recovers what is still legible and
writes it once.

The thing worth testing is the honesty of the result rather than the mechanics
of the read. A reconstructed row must never be mistakable for one written at
the time: the production path cannot backdate an entry - `audit.record` stamps
its own clock - and this script can, so every row it writes has to say so. If
that ever stops being true, the log quietly becomes a thing an administrator
can author history into, which is worse than having no log at all.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")

import backfill_audit_log as backfill  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")

# One claim, carrying the shape the live data actually had: sent back for
# review first, then queried - two decisions that survived because they were
# stored in two different sets of fields.
CLAIM = {
    "submission_id": "sub_1789477834875_595007",
    "org_id": "org_5f9e1ddc2b595007",
    "reference": "Mobil80-Exp-4",
    "submitted_by": "rana@mobil80.com",
    "verdict": {"currency": "USD"},
    "pulled_at": 1789478134,
    "pulled_by_name": "Madhusudhan",
    "pulled_reason": "Need INR Conversion",
    "review_at": 1789478340,
    "review_action": "queried",
    "review_by_name": "Madhusudhan",
    "review_reason": "How much was it in INR?",
}


class EveryReconstructedRowSaysSo(unittest.TestCase):

    def setUp(self):
        self.entries = backfill._entries_for(CLAIM, 1_800_000_000_000)

    def test_each_one_is_marked(self):
        for e in self.entries:
            self.assertTrue(e["reconstructed"])
            self.assertEqual(1_800_000_000_000, e["reconstructed_at"])

    def test_the_console_shows_the_mark(self):
        html = open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8").read()
        self.assertIn("e.reconstructed", html)
        self.assertIn("reconstructed from the claim", html)

    def test_the_production_path_cannot_write_one(self):
        # The whole safeguard. `audit.record` takes no timestamp and no
        # `reconstructed` flag it would honour - backdating lives in a script
        # somebody has to run deliberately, not in the request path.
        source = open(os.path.join(ROOT, "lambda_src", "audit.py"), encoding="utf-8").read()
        self.assertNotIn("reconstructed", source)
        signature = source.split("def record(", 1)[1].split(")", 1)[0]
        self.assertNotIn("at:", signature)
        self.assertNotIn("ts:", signature)


class ItRecoversWhatSurvivedAndInventsNothing(unittest.TestCase):

    def setUp(self):
        self.entries = backfill._entries_for(CLAIM, 1_800_000_000_000)

    def test_two_decisions_kept_in_two_field_sets_both_come_back(self):
        # "claim queried" rather than a friendly name: a reviewer can no
        # longer ask a submitter anything, so the live path never writes that
        # action and a reconstruction must not invent a phrase for it.
        self.assertEqual(["sent back for review", "claim queried"],
                         [e["action"] for e in self.entries])

    def test_they_are_in_the_order_they_happened(self):
        self.assertLess(self.entries[0]["at"], self.entries[1]["at"])
        self.assertEqual(1789478134 * 1000, self.entries[0]["at"])

    def test_the_words_people_used_survive(self):
        self.assertEqual("Need INR Conversion", self.entries[0]["reason"])
        self.assertEqual("How much was it in INR?", self.entries[1]["reason"])

    def test_the_role_is_absent_rather_than_guessed(self):
        # It was never stored on the claim, and the role somebody holds today
        # is not the one they held then. Absent is the truthful answer.
        for e in self.entries:
            self.assertNotIn("actor_role", e)

    def test_an_unrecoverable_email_is_left_blank_not_derived_from_the_name(self):
        for e in self.entries:
            self.assertNotIn("actor", e)
            self.assertEqual("Madhusudhan", e["actor_name"])

    def test_a_claim_nobody_decided_yields_nothing(self):
        self.assertEqual([], backfill._entries_for(
            {"submission_id": "sub_1", "org_id": "org_1", "reference": "Exp-1"},
            1_800_000_000_000))

    def test_a_row_with_no_organisation_is_skipped_rather_than_guessed_at(self):
        self.assertEqual([], backfill._entries_for(
            {**CLAIM, "org_id": ""}, 1_800_000_000_000))


class RunningItTwiceIsNotTwoHistories(unittest.TestCase):

    def test_the_key_comes_from_the_decision_not_the_clock(self):
        first = backfill._entries_for(CLAIM, 1_800_000_000_000)
        again = backfill._entries_for(CLAIM, 1_900_000_000_000)
        self.assertEqual([e["ts"] for e in first], [e["ts"] for e in again])

    def test_the_keys_of_two_decisions_on_one_claim_differ(self):
        entries = backfill._entries_for(CLAIM, 1_800_000_000_000)
        self.assertEqual(2, len({e["ts"] for e in entries}))

    def test_it_writes_nothing_without_being_told_to(self):
        source = open(os.path.join(ROOT, "tools", "backfill_audit_log.py"),
                      encoding="utf-8").read()
        self.assertIn("if not args.yes:", source)
        main = source.split("def main(", 1)[1]
        self.assertLess(main.index("if not args.yes:"), main.index("put_item"))


class TheActionsMatchWhatTheLiveCodeWrites(unittest.TestCase):
    """A reconstructed rejection and a real one have to read as one kind of
    thing, or the log has two vocabularies and neither can be filtered on."""

    def test_the_names_are_the_same_strings(self):
        auth = open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8").read()
        for phrase in backfill.ACTIONS.values():
            self.assertIn(f'"{phrase}"', auth,
                          f"{phrase!r} is not a name auth.py ever writes")

    def test_sent_back_for_review_matches_too(self):
        auth = open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8").read()
        self.assertIn('"sent back for review"', auth)
        self.assertIn("sent back for review",
                      [e["action"] for e in backfill._entries_for(CLAIM, 1)])

    def test_an_unexpected_stored_value_is_passed_through_not_dropped(self):
        entries = backfill._entries_for(
            {**CLAIM, "review_action": "something_new"}, 1)
        self.assertIn("claim something_new", [e["action"] for e in entries])


if __name__ == "__main__":
    unittest.main()
