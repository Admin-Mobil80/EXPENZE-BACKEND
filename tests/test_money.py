"""Which currency a receipt is read as.

This is a small module with an outsized blast radius: getting it wrong reads a
$180 invoice as ₹180 and clears it three orders of magnitude under the cap, or
reads a ₹3,600 dinner as $3,600 and rejects a legitimate claim. So the rules
are pinned here rather than trusted to hold.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

import money  # noqa: E402


class CountryDefaults(unittest.TestCase):
    def test_every_country_offered_has_a_currency(self):
        # A country a customer can pick with no currency behind it would leave
        # them with no default at all - which is the bug this pairing prevents.
        for code in money.COUNTRIES:
            self.assertIn(code, money.COUNTRY_CURRENCY, code)
            self.assertIn(money.COUNTRY_CURRENCY[code], money.CURRENCIES, code)

    def test_the_obvious_ones(self):
        self.assertEqual(money.for_country("IN"), "INR")
        self.assertEqual(money.for_country("SG"), "SGD")
        self.assertEqual(money.for_country("DE"), "EUR")
        self.assertEqual(money.for_country("JP"), "JPY")

    def test_an_unknown_country_yields_nothing_rather_than_a_guess(self):
        for value in ("ZZ", "", None):
            self.assertEqual(money.for_country(value), "", repr(value))

    def test_yen_has_no_minor_unit(self):
        self.assertEqual(money.CURRENCIES["JPY"]["decimals"], 0)


class OrgDefault(unittest.TestCase):
    def test_follows_the_country_when_nothing_is_pinned(self):
        self.assertEqual(money.default_for_org({"address": {"country": "IN"}}), "INR")

    def test_an_explicit_choice_beats_the_country(self):
        org = {"address": {"country": "SG"}, "default_currency": "USD"}
        self.assertEqual(money.default_for_org(org), "USD")

    def test_a_nonsense_choice_falls_back_to_the_country(self):
        org = {"address": {"country": "IN"}, "default_currency": "BITCOIN"}
        self.assertEqual(money.default_for_org(org), "INR")

    def test_an_org_with_nothing_set_gets_no_currency_at_all(self):
        # Inventing one here is an 80x error waiting to happen. Nothing means
        # the receipt goes to a human, which is the honest outcome.
        self.assertEqual(money.default_for_org({}), "")
        self.assertEqual(money.default_for_org({"address": {}}), "")


class PrintedCurrencyWins(unittest.TestCase):
    """The organisation default must never override the document."""

    def test_printed_code(self):
        got = money.resolve("SGD", "printed_code", "SGD", org_default="INR")
        self.assertEqual(got["currency"], "SGD")
        self.assertFalse(got["assumed"])
        self.assertEqual(got["note"], "")

    def test_unambiguous_symbol(self):
        got = money.resolve("GBP", "unambiguous_symbol", "£", org_default="INR")
        self.assertEqual(got["currency"], "GBP")
        self.assertFalse(got["assumed"])

    def test_rupee_sign_is_not_ambiguous(self):
        got = money.resolve("INR", "unambiguous_symbol", "₹", org_default="USD")
        self.assertEqual(got["currency"], "INR")
        self.assertFalse(got["assumed"])


class NothingPrinted(unittest.TestCase):
    """The handwritten-bill case: a pad of paper with figures and no currency."""

    def test_uses_the_organisation_default(self):
        got = money.resolve(None, "absent", "", org_default="INR")
        self.assertEqual(got["currency"], "INR")
        self.assertTrue(got["assumed"])
        self.assertIn("INR", got["note"])

    def test_a_singapore_org_gets_sgd_not_inr(self):
        self.assertEqual(money.resolve(None, "absent", "", org_default="SGD")["currency"], "SGD")

    def test_a_guess_the_model_slipped_in_anyway_is_ignored(self):
        # Reporting 'absent' and a code at once is contradictory; the absence
        # is the honest half, so the default applies.
        got = money.resolve("USD", "absent", "", org_default="INR")
        self.assertEqual(got["currency"], "INR")
        self.assertTrue(got["assumed"])

    def test_it_is_always_labelled(self):
        self.assertTrue(money.resolve(None, "absent", "", org_default="INR")["note"])


class AmbiguousSymbols(unittest.TestCase):
    def test_rs_in_an_indian_org_is_inr(self):
        got = money.resolve("INR", "ambiguous_symbol", "Rs.", org_default="INR")
        self.assertEqual(got["currency"], "INR")
        self.assertTrue(got["assumed"], "shared symbols are never certain")

    def test_rs_in_a_sri_lankan_org_is_lkr(self):
        got = money.resolve("INR", "ambiguous_symbol", "Rs.", org_default="LKR")
        self.assertEqual(got["currency"], "LKR")

    def test_dollar_in_an_indian_org_is_not_inr(self):
        # Someone travelled. Forcing the org default here would be worse than
        # admitting the doubt: the rupee does not use a dollar sign.
        got = money.resolve("USD", "ambiguous_symbol", "$", org_default="INR")
        self.assertEqual(got["currency"], "USD")
        self.assertTrue(got["assumed"])

    def test_dollar_in_a_singapore_org_is_sgd(self):
        got = money.resolve("USD", "ambiguous_symbol", "$", org_default="SGD")
        self.assertEqual(got["currency"], "SGD")

    def test_local_script_rupee_marks_resolve(self):
        for mark in ("रु", "ரூ", "రూ", "ರೂ", "രൂ", "રૂ"):
            got = money.resolve(None, "ambiguous_symbol", mark, org_default="INR")
            self.assertEqual(got["currency"], "INR", mark)

    def test_a_mark_only_one_currency_uses_is_not_an_assumption(self):
        # The model calls "S$" ambiguous because it contains a dollar sign;
        # it is not, and saying so would cry wolf on the flag that matters.
        got = money.resolve("SGD", "ambiguous_symbol", "S$", org_default="INR")
        self.assertEqual(got["currency"], "SGD")
        self.assertFalse(got["assumed"])
        self.assertEqual(got["note"], "")

    def test_an_unrecognised_mark_falls_back_to_the_default(self):
        got = money.resolve(None, "ambiguous_symbol", "¤¤", org_default="INR")
        self.assertEqual(got["currency"], "INR")
        self.assertTrue(got["assumed"])


class NoOrganisationCurrency(unittest.TestCase):
    """A customer who has set neither a country nor a currency. Guessing for
    them reads a 10,000 rupee bill as 10,000 dollars."""

    def test_an_unmarked_receipt_gets_no_currency(self):
        got = money.resolve(None, "absent", "", org_default="")
        self.assertEqual(got["currency"], "")
        self.assertTrue(got["assumed"])

    def test_the_note_says_what_to_do_about_it(self):
        note = money.resolve(None, "absent", "", org_default="")["note"]
        self.assertIn("has not set one", note)
        self.assertIn("Organisation", note)

    def test_a_printed_currency_still_wins(self):
        # Nothing set on the organisation does not stop a receipt that says INR.
        got = money.resolve("INR", "printed_code", "INR", org_default="")
        self.assertEqual(got["currency"], "INR")
        self.assertFalse(got["assumed"])


class Robustness(unittest.TestCase):
    def test_a_missing_source_is_inferred(self):
        self.assertFalse(money.resolve("INR", "", "₹", org_default="USD")["assumed"])
        self.assertTrue(money.resolve(None, "", "", org_default="USD")["assumed"])

    def test_an_invalid_org_default_never_propagates(self):
        got = money.resolve(None, "absent", "", org_default="RUPEES")
        self.assertEqual(got["currency"], "")
        self.assertTrue(got["assumed"])

    def test_case_and_padding_do_not_matter(self):
        self.assertEqual(money.resolve(" inr ", "printed_code", "", "USD")["currency"], "INR")

    def test_a_bogus_printed_code_is_not_trusted(self):
        got = money.resolve("XYZ", "printed_code", "XYZ", org_default="INR")
        self.assertEqual(got["currency"], "INR")
        self.assertTrue(got["assumed"])


if __name__ == "__main__":
    unittest.main()
