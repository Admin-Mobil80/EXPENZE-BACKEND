"""The number somebody quotes when they ring up about a receipt.

`sub_1789455641368_806298` is an identifier. Nobody reads it down a phone,
nobody types it into a search box, and nobody recognises it on a statement. So
every receipt also gets `Mobil80-Exp-41`.

Two things about that shape matter later, and both are what these cover.

The prefix is pinned to the organisation once, at sign-up. Deriving it live
from the default group's name would have been fewer moving parts until the day
somebody renamed the group - at which point every reference issued after it
has a different shape from every one issued before, and finance cannot search
on a prefix that changed halfway through the year.

The number comes from the same write that spends the credit. One receipt, one
credit, one number, and no second write to fail.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

import reference  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class ThePrefixIsTakenFromTheName(unittest.TestCase):
    def test_the_first_word_is_what_people_call_the_company(self):
        self.assertEqual("Mobil80",
                         reference.prefix_for("Mobil80 Solutions and Services Pvt Ltd"))

    def test_punctuation_and_spaces_are_dropped(self):
        # A reference travels through spreadsheets, bank narration fields and
        # URLs, and at least one of those will mangle an ampersand.
        self.assertEqual("Smith", reference.prefix_for("Smith & Co."))
        self.assertEqual("Acme", reference.prefix_for("  Acme   Holdings  "))

    def test_a_very_long_word_is_cut(self):
        self.assertEqual(reference.MAX_PREFIX,
                         len(reference.prefix_for("Internationalisation Limited")))

    def test_a_wholly_numeric_first_word_takes_the_next_one_with_it(self):
        # "360-Exp-7" reads as though the 360 were part of the number. A word
        # that merely starts with a digit is fine: "3M-Exp-7" is unambiguous.
        self.assertEqual("360Degrees", reference.prefix_for("360 Degrees Ltd"))
        self.assertEqual("3M", reference.prefix_for("3M India Limited"))

    def test_a_nameless_organisation_still_gets_something(self):
        for empty in ("", "   ", "!!!", None):
            self.assertEqual(reference.FALLBACK_PREFIX, reference.prefix_for(empty))

    def test_it_is_deterministic(self):
        # Every organisation that existed before today has its prefix derived
        # rather than stored, so the same name must always give the same tag.
        name = "Mobil80 Solutions and Services Pvt Ltd"
        self.assertEqual(reference.prefix_for(name), reference.prefix_for(name))


class TheReferenceReads(unittest.TestCase):
    def test_it_looks_like_the_thing_that_was_asked_for(self):
        self.assertEqual("Mobil80-Exp-1", reference.build("Mobil80", 1))
        self.assertEqual("Mobil80-Exp-412", reference.build("Mobil80", 412))

    def test_it_counts_from_one(self):
        # A first receipt numbered 0 reads as a bug to the customer.
        self.assertEqual("", reference.build("Mobil80", 0))

    def test_nothing_rather_than_a_malformed_reference(self):
        for bad in (None, "", "abc", -3):
            self.assertEqual("", reference.build("Mobil80", bad))

    def test_a_stored_prefix_wins_over_the_name(self):
        # Pinned at sign-up; renaming the company afterwards must not change
        # the shape of references already issued.
        org = {"ref_prefix": "Mobil80", "name": "Something Else Entirely Ltd"}
        self.assertEqual("Mobil80-Exp-9", reference.of(org, 9))

    def test_an_older_organisation_falls_back_to_its_name(self):
        org = {"name": "Mobil80 Solutions and Services Pvt Ltd"}
        self.assertEqual("Mobil80-Exp-9", reference.of(org, 9))


class ItIsMintedWithTheCredit(unittest.TestCase):
    """One receipt, one credit, one number - in a single conditional write."""

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "intake.py"), encoding="utf-8") as h:
            self.intake = h.read()
        self.charge = self.intake.split("def _charge_and_record(", 1)[1].split("\ndef ", 1)[0]

    def test_the_counter_rides_on_the_credit_decrement(self):
        # A separate write could fail on its own and hand two receipts the
        # same number, or spend a credit and issue none.
        self.assertIn("ADD ref_seq :one", self.charge)
        spend = self.charge.split("UpdateExpression=(", 1)[1].split("),", 1)[0]
        self.assertIn("credits = credits - :one", spend)
        self.assertIn("ADD ref_seq :one", spend)

    def test_the_number_comes_back_from_that_write(self):
        # Reading it separately is a read-then-write, which is exactly the
        # race the atomic ADD exists to avoid.
        self.assertIn('ReturnValues="ALL_NEW"', self.charge)
        self.assertIn('reference.of(updated, updated.get("ref_seq"))', self.charge)

    def test_a_receipt_refused_for_credit_consumes_no_number(self):
        # The conditional fails, so neither the credit nor the counter moves.
        refused = self.charge.split("ConditionalCheckFailedException", 1)[1].split("\n\n", 1)[0]
        self.assertIn("no_credits", refused)

    def test_it_is_stored_on_the_submission(self):
        self.assertIn('"reference": ref,', self.charge)


class ItReachesThePeopleWhoWouldQuoteIt(unittest.TestCase):
    def setUp(self):
        for name, path in (("auth", "lambda_src/auth.py"),
                           ("notify", "lambda_src/notify.py"),
                           ("worker", "lambda_src/auditor_worker.py"),
                           ("app", "../PORTAL/app.html")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_the_api_returns_it(self):
        view = self.auth.split("def _submission_view(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"reference": row.get("reference", "")', view)

    def test_sign_up_pins_the_prefix(self):
        signup = self.auth.split("def _signup(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("reference.prefix_for(org_name)", signup)

    def test_every_notice_carries_it(self):
        # A number nobody is ever told is a number nobody can quote.
        self.assertEqual(3, self.auth.count('"claim_ref": str(item.get("reference", ""))'))
        self.assertIn('"claim_ref": str(row.get("reference", ""))', self.worker)

    def test_the_settlement_notice_does_not_confuse_it_with_the_bank_reference(self):
        # Both are called "reference" and they are not the same thing: one is
        # ours, one is the UTR finance typed in.
        settled = self.notify.split("def settled_notice(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"Bank reference:"', settled)
        self.assertIn('"Claim:", "claim_ref"', settled)

    def test_the_console_shows_it(self):
        self.assertIn("reference: s.reference", self.app)
        self.assertIn('class="refno"', self.app)

    def test_a_receipt_from_before_today_shows_none(self):
        # There is no number that was ever issued for those, and inventing one
        # now would be a reference nobody could look up.
        self.assertIn('sub.reference ? `<span class="refno">', self.app)


if __name__ == "__main__":
    unittest.main()


class ItIsOnEveryScreenAndInEveryMessage(unittest.TestCase):
    """A reference nobody can see is a reference nobody quotes.

    It started on the claim page and under My expenses, which is where the
    person who sent the receipt looks - but finance works from the queue and
    the settlement list, and those are the screens somebody is on when they
    take the call about a claim.
    """

    def setUp(self):
        for name, path in (("app", "../PORTAL/app.html"), ("notify", "lambda_src/notify.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_the_review_queue_row_carries_it(self):
        # A table row now, like every other claim list.
        row = self.app.split("function renderQueue()", 1)[1].split("\n}", 1)[0]
        self.assertIn("sub.reference", row)
        self.assertIn('<span class="refno inline">', row)

    def test_the_settlement_and_settled_rows_carry_it(self):
        # Both build the vendor cell the same way, so both had to change.
        # `inline` since the cell was flattened to one line: stacking vendor,
        # channel and reference set the height of every row in the table.
        self.assertEqual(2, self.app.count(
            "c.sub.reference ? `<span class=\"refno inline\">"))

