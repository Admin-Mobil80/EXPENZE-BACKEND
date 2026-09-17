"""Which cost centre a bill is made out to, read off the bill.

The submitter is never asked. Most people belong to one group and their
receipts are tagged without anybody thinking about it; the question only arises
for somebody in several, and it used to be answered by messaging them a list of
cost centres to tap - an accounting question put to the person who photographed
a bill. The bill is read instead, and whatever it does not settle goes to the
reviewer who was going to approve the claim anyway.

The thing worth testing is the refusals. Matching a name is easy; refusing to
match one that is merely similar is the whole safety property, because a
wrongly attributed expense is worse than an unattributed one - the second is
visibly unfinished, the first looks like an answer and nobody looks again.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

import grouping  # noqa: E402

GROUPS = [
    {"id": "cocobble", "label": "Cocobble Foods Pvt Ltd", "tax_id": "29AABCU9603R1ZM"},
    {"id": "mobil80", "label": "Mobil80 Solutions & Services Private Limited"},
    {"id": "logistics", "label": "Mobil80 Logistics"},
]


class TheRegistrationAnswersItOutright(unittest.TestCase):

    def test_a_matching_tax_id_names_the_group(self):
        self.assertEqual(("cocobble", "tax_id"),
                         grouping.group_for(GROUPS, {"buyer_tax_id": "29AABCU9603R1ZM"}))

    def test_however_the_vendor_printed_it(self):
        self.assertEqual(("cocobble", "tax_id"),
                         grouping.group_for(GROUPS, {"buyer_tax_id": "GSTIN: 29 aabcu9603r 1zm"}))

    def test_and_it_beats_a_name_that_says_otherwise(self):
        # A registration identifies an entity; a name is how somebody typed
        # one. Where they disagree the number is the one to believe.
        self.assertEqual(
            ("cocobble", "tax_id"),
            grouping.group_for(GROUPS, {"buyer_tax_id": "29AABCU9603R1ZM",
                                        "buyer_name": "Mobil80 Logistics"}))


class TheNameAnswersItWhenTheRegistrationDoesNot(unittest.TestCase):

    def test_an_exact_name_names_the_group(self):
        self.assertEqual(
            ("mobil80", "buyer_name"),
            grouping.group_for(GROUPS, {"buyer_name": "Mobil80 Solutions & Services Private Limited"}))

    def test_however_the_company_form_was_written(self):
        # "Pvt. Ltd." and "Private Limited" are the same company written twice.
        for spelling in ("MOBIL80 SOLUTIONS AND SERVICES PVT LTD",
                         "Mobil80 Solutions & Services Pvt. Ltd.",
                         "mobil80  solutions and services"):
            self.assertEqual(("mobil80", "buyer_name"),
                             grouping.group_for(GROUPS, {"buyer_name": spelling}),
                             f"{spelling!r} is the same company")

    def test_a_suffix_word_inside_a_name_is_not_stripped(self):
        # Only trailing ones are company forms. "Company Kitchens" is a name.
        self.assertEqual("companykitchens", grouping.normalise_name("Company Kitchens"))


class ItRefusesRatherThanGuesses(unittest.TestCase):
    """The safety property. A wrongly attributed expense is paid out of the
    wrong budget and nobody ever looks again."""

    def test_a_name_that_is_merely_similar_matches_nothing(self):
        self.assertEqual(("", ""), grouping.group_for(GROUPS, {"buyer_name": "Mobil80"}))

    def test_a_longer_name_containing_a_group_matches_nothing(self):
        self.assertEqual(("", ""),
                         grouping.group_for(GROUPS, {"buyer_name": "Mobil80 Logistics Europe"}))

    def test_two_groups_reducing_to_one_name_match_nothing(self):
        # Picking either would make the attribution depend on the order
        # somebody happened to add them in.
        twins = [{"id": "a", "label": "Acme Foods Pvt Ltd"},
                 {"id": "b", "label": "Acme Foods Limited"}]
        self.assertEqual(("", ""), grouping.group_for(twins, {"buyer_name": "Acme Foods"}))

    def test_a_bill_naming_nobody_matches_nothing(self):
        for empty in ({}, {"buyer_name": ""}, {"buyer_name": None},
                      {"buyer_tax_id": "", "buyer_name": "   "}):
            self.assertEqual(("", ""), grouping.group_for(GROUPS, empty))

    def test_a_name_too_short_to_identify_anybody_is_ignored(self):
        # Initials and OCR fragments. Matching on two characters attributes a
        # receipt on a coincidence.
        self.assertEqual("", grouping.normalise_name("M80"))
        self.assertEqual("", grouping.normalise_name("Ltd"))

    def test_a_group_with_no_label_is_never_the_answer(self):
        self.assertEqual(("", ""),
                         grouping.group_for([{"id": "x", "label": ""}], {"buyer_name": "  "}))


class TheAnswerSaysWhatSaidSo(unittest.TestCase):
    """An attribution nobody can account for is one nobody corrects with any
    confidence, so the reason travels with the answer onto the claim."""

    def test_each_route_is_named(self):
        self.assertEqual("tax_id",
                         grouping.group_for(GROUPS, {"buyer_tax_id": "29AABCU9603R1ZM"})[1])
        self.assertEqual("buyer_name",
                         grouping.group_for(GROUPS, {"buyer_name": "Mobil80 Logistics"})[1])

    def test_and_the_console_has_a_phrase_for_each(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        app = open(os.path.join(root, "..", "PORTAL", "app.html"), encoding="utf-8").read()
        how = app.split("const GROUP_HOW = {", 1)[1].split("}", 1)[0]
        self.assertIn("assigned_by_tax_id", how)
        self.assertIn("assigned_by_buyer_name", how)
        self.assertIn("the bill is made out to this group's tax ID".lower(),
                      app.lower())
        self.assertIn("the bill is made out to this group by name".lower(),
                      app.lower())


class TheBillHasToNameTheBuyerForAnyOfThisToWork(unittest.TestCase):

    def test_the_extraction_asks_for_it(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        handler = open(os.path.join(root, "lambda_src", "handler.py"), encoding="utf-8").read()
        schema = handler.split("RECEIPT_SCHEMA", 1)[1]
        self.assertIn('"buyer_name"', schema)
        # Required, like every other field: a strict schema with an optional
        # key is a key the model may silently never answer.
        self.assertIn('"buyer_name",', handler.split('"required": [', 1)[1])

    def test_and_is_told_not_to_answer_with_the_seller(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        handler = open(os.path.join(root, "lambda_src", "handler.py"), encoding="utf-8").read()
        field = handler.split('"buyer_name": {', 1)[1].split("},", 1)[0]
        self.assertIn("never repeat the seller", field)
        self.assertIn("empty string", field)


if __name__ == "__main__":
    unittest.main()
