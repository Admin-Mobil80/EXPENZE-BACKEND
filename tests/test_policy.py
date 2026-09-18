"""Tests for the deterministic policy engine.

Pure Python, no AWS and no model - these run in under a second and are the
regression net for the only code that decides anyone's money.

The engine answers one question: can the agent clear this, or does a person
have to look at it? It used to answer four - approved, partially approved,
rejected, needs review - and compute how much of a claim was allowed, from a
per-head cap multiplied by a headcount it asked the claimant for. Every part of
that was defensible and together they made a product nobody could hold in their
head, including the person whose money it was.

So what these tests mostly hold is the *absence* of arithmetic: a claim is
worth what the receipt prints, the cap decides who says yes rather than how
much, and no rule anywhere depends on a fact a receipt does not carry.
"""
import os
import sys
import json
import re
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

import policy  # noqa: E402


def item(desc, amount):
    return {"description": desc, "amount": amount}


def decide(total=None, expense_type="meals", currency="INR", items=None,
           rules=None, stated=None):
    """One receipt through the engine. `total` is shorthand for a single line."""
    if items is None:
        items = [item("Dinner", total)] if total is not None else []
    return policy.evaluate_policy(currency=currency, line_items=items,
                                  expense_type=expense_type, rules=rules,
                                  stated_total=stated)


def blocking(verdict):
    return [v["code"] for v in verdict["violations"] if v["blocks_automatic_decision"]]


class ThereAreExactlyTwoOutcomes(unittest.TestCase):
    """Cleared, or with a person. Nothing in between, and nothing partial."""

    def test_a_claim_inside_the_cap_clears(self):
        v = decide("830.00")
        self.assertEqual("approved", v["verdict"])
        self.assertEqual([], v["violations"])

    def test_a_claim_over_the_cap_goes_to_a_person_whole(self):
        # Not part paid. A bill over the limit is a judgment - there may be a
        # good reason for it - and quietly paying the cap makes that judgment
        # on the reviewer's behalf while telling the claimant they were docked.
        v = decide("3705.00")
        self.assertEqual("needs_review", v["verdict"])
        self.assertIn("cap_exceeded", blocking(v))
        self.assertEqual("3705.00", v["reimbursable_total"])

    def test_reimbursable_always_equals_the_receipt_total(self):
        for total in ("830.00", "3000.00", "3705.00", "99999.00"):
            v = decide(total)
            self.assertEqual(v["receipt_total"], v["reimbursable_total"], total)

    def test_no_verdict_but_those_two_is_reachable(self):
        source = open(os.path.join(os.path.dirname(__file__), "..",
                                   "lambda_src", "policy.py"), encoding="utf-8").read()
        body = source.split("def evaluate_policy(", 1)[1]
        # Everything after the docstring, which names what it replaced.
        code = body.split('"""')[2]
        code = "\n".join(l for l in code.splitlines()
                         if not l.strip().startswith("#"))
        for gone in ("partially_approved", '"rejected"', "disallowed", "provisional",
                     "assured"):
            self.assertNotIn(gone, code, f"{gone} survives in the engine")

    def test_the_engine_reports_no_figure_it_did_not_decide(self):
        v = decide("830.00")
        for gone in ("disallowed_total", "provisional_total", "assured_total",
                     "attendee_count", "cap_per_head"):
            self.assertNotIn(gone, v)


class TheCapDecidesWhoSaysYesNotHowMuch(unittest.TestCase):

    def test_exactly_on_the_cap_clears(self):
        v = decide("3000.00")
        self.assertEqual("approved", v["verdict"])

    def test_one_paisa_over_does_not(self):
        v = decide("3000.01")
        self.assertEqual("needs_review", v["verdict"])

    def test_the_cap_is_measured_against_the_whole_bill(self):
        v = decide(items=[item("Food", "2000.00"),
                          item("GST", "1500.00")])
        self.assertEqual("needs_review", v["verdict"])
        self.assertEqual("3500.00", v["receipt_total"])

    def test_the_excess_is_named_but_nothing_is_deducted(self):
        v = decide("3705.00")
        cap = [x for x in v["violations"] if x["code"] == "cap_exceeded"][0]
        self.assertEqual("705.00", cap["amount"])
        self.assertEqual("3705.00", v["reimbursable_total"])

    def test_a_type_with_no_cap_always_clears(self):
        rules = policy.normalise_rules({
            "version": 1,
            "expense_types": [{"id": "meals", "label": "Meals", "enabled": True,
                               "caps": {}}]})
        v = decide("999999.00", rules=rules)
        self.assertEqual("approved", v["verdict"])

    def test_each_currency_has_its_own_cap(self):
        self.assertEqual("approved", decide("30.00", currency="USD")["verdict"])
        self.assertEqual("needs_review", decide("40.00", currency="USD")["verdict"])

    def test_a_currency_the_cap_is_not_set_in_goes_to_a_person(self):
        # Never an invented exchange rate.
        v = decide("100.00", currency="EUR")
        self.assertIn("unsupported_currency", blocking(v))


class NothingDependsOnWhatAReceiptCannotSay(unittest.TestCase):
    """The rule that keeps the product simple: no rule may need an answer.

    Every question the old engine asked a claimant came from a rule whose
    input the bill did not print - a headcount, above all. A cap per
    transaction is decidable from the receipt alone, every time.
    """

    def test_the_engine_takes_no_headcount(self):
        args = policy.evaluate_policy.__code__.co_varnames[
            :policy.evaluate_policy.__code__.co_argcount]
        self.assertNotIn("attendee_count", args)
        self.assertNotIn("attendee_count_source", args)

    def test_no_rule_can_require_one(self):
        rule = policy.normalise_rules(policy.DEFAULT_RULES)["expense_types"][0]
        self.assertNotIn("requires_headcount", rule)
        self.assertNotIn("requires_itemisation", rule)

    def test_an_unitemised_bill_is_not_a_finding(self):
        # This used to stop a grocery claim with no breakdown.
        v = decide(items=[item("Payment to Mythri Bazaar", "150.00")],
                   expense_type="canteen_groceries")
        self.assertEqual("approved", v["verdict"])

    def test_only_one_kind_of_cap_exists(self):
        self.assertEqual("per_transaction", policy.CAP_KIND)
        for t in policy.normalise_rules(policy.DEFAULT_RULES)["expense_types"]:
            self.assertNotIn("per_head", t["caps"])


class AnExpenseTheP0licyDoesNotCover(unittest.TestCase):
    """Nothing is defaulted to the nearest rule."""

    def test_an_unknown_type_goes_to_a_human(self):
        v = decide("100.00", expense_type="hot_air_balloon")
        self.assertIn("no_rule_for_expense_type", blocking(v))
        self.assertEqual("needs_review", v["verdict"])

    def test_a_disabled_type_counts_as_having_no_rule(self):
        rules = policy.normalise_rules({
            "version": 1,
            "expense_types": [
                {"id": "meals", "label": "Meals", "enabled": False,
                 "caps": {"INR": "1500.00"}},
                {"id": "travel", "label": "Travel", "enabled": True,
                 "caps": {"INR": "1000.00"}}]})
        v = decide("100.00", rules=rules)
        self.assertIn("no_rule_for_expense_type", blocking(v))

    def test_the_claim_is_still_worth_its_total(self):
        v = decide("100.00", expense_type="hot_air_balloon")
        self.assertEqual("100.00", v["reimbursable_total"])


class TheBillsOwnTotalIsWhatIsClaimed(unittest.TestCase):
    """Two witnesses to what a receipt comes to; the printed one wins.

    It is what the vendor charged and what the card was debited. The line items
    are our reading of it, and a reading that comes up short is a reason to
    doubt the reading, not the bill - paying the lower of the two would short
    the employee by exactly our own extraction error.
    """

    def test_the_printed_total_wins_over_the_lines(self):
        v = decide(items=[item("Groceries", "7000.00")], stated="7010.00")
        self.assertEqual("7010.00", v["receipt_total"])
        self.assertEqual("7010.00", v["reimbursable_total"])

    def test_it_holds_when_the_lines_come_to_more(self):
        v = decide(items=[item("Groceries", "7500.00")], stated="7010.00")
        self.assertEqual("7010.00", v["receipt_total"])

    def test_a_bill_with_no_lines_is_worth_what_it_prints(self):
        v = decide(items=[], stated="500.00")
        self.assertEqual("500.00", v["receipt_total"])

    def test_no_total_falls_back_to_the_lines(self):
        v = decide(items=[item("A", "100.00"), item("B", "50.00")])
        self.assertEqual("150.00", v["receipt_total"])

    def test_the_gap_is_no_longer_a_finding(self):
        # It mattered while lines could be struck out individually. Now that a
        # claim is worth its printed total, a difference between the two is a
        # note about our own extraction, not something to put to a reviewer.
        v = decide(items=[item("Groceries", "7000.00")], stated="7010.00",
                   expense_type="canteen_groceries")
        self.assertEqual([], [x["code"] for x in v["violations"]])

    def test_the_cap_is_measured_against_the_printed_total(self):
        v = decide(items=[item("Dinner", "100.00")], stated="9000.00")
        self.assertIn("cap_exceeded", blocking(v))


class ALineIsADescriptionAndAnAmount(unittest.TestCase):
    """Nothing on a line decides money, so nothing else is kept on one."""

    def test_every_line_survives_whatever_it_is_called(self):
        v = decide(items=[item("Beer", "400.00"), item("Food", "600.00")])
        self.assertEqual("1000.00", v["reimbursable_total"])
        self.assertEqual(2, len(v["line_items"]))

    def test_a_line_carries_nothing_else(self):
        v = decide(items=[item("Dinner", "830.00")])
        self.assertEqual({"description", "amount"}, set(v["line_items"][0]))

    def test_a_category_arriving_from_an_old_client_is_dropped(self):
        # A claim resubmitted from a page loaded before the deploy, or a model
        # reply from a cached schema. The key is not carried forward: keeping
        # it on some claims and not others gives reports two vocabularies.
        v = decide(items=[{"description": "Cab", "amount": "300.00",
                           "category": "ride_hire"}])
        self.assertNotIn("category", v["line_items"][0])

    def test_a_line_carries_no_disposition_any_more(self):
        # There is nothing for one to say: every line is claimable.
        v = decide("100.00")
        self.assertNotIn("disposition", v["line_items"][0])


class MoneyIsHandledExactly(unittest.TestCase):

    def test_floats_do_not_drift(self):
        v = decide(items=[item("A", 0.1), item("B", 0.2)])
        self.assertEqual("0.30", v["receipt_total"])

    def test_a_negative_line_is_ordinary(self):
        # Invoices carry discounts, credits, proration and returned items as
        # negative lines, and they are part of what the bill comes to.
        # Refusing them threw out the whole claim: a Claude subscription
        # invoice with one discount line failed three audits and was parked
        # for a human, for printing something entirely unremarkable.
        v = decide(items=[item("Pro plan", "100.00"), item("Discount", "-10.00")])
        self.assertEqual("90.00", v["receipt_total"])
        self.assertEqual("-10.00", v["line_items"][1]["amount"])

    def test_but_a_negative_cap_is_still_refused(self):
        # "You may spend minus fifty" is not a rule anybody meant to write, and
        # taking it at face value would clear every claim of that type.
        with self.assertRaises(policy.PolicyInputError):
            policy.normalise_rules({"version": 1, "expense_types": [
                {"id": "meals", "label": "Meals", "enabled": True,
                 "caps": {"per_transaction": {"INR": "-50.00"}}}]})

    def test_and_a_bill_cannot_come_to_less_than_nothing(self):
        # Lines may be negative one at a time; their sum may not. A total below
        # zero is a misreading - a credit note taken for an invoice - and both
        # paying against it and capping against it would be arithmetic on a
        # fiction.
        v = decide(items=[item("Thing", "10.00"), item("Credit", "-60.00")])
        self.assertIn("negative_total", blocking(v))
        self.assertEqual("0.00", v["receipt_total"])

    def test_the_verdict_is_json_serialisable(self):
        json.dumps(decide("830.00"))
class ABillMadeOutToUsNamesItsOwnCostCentre(unittest.TestCase):
    """A tax invoice prints who it is billed to. That is the answer.

    Somebody in six groups is asked, every receipt, which one a bill belongs
    to - and most of the time the bill already says, because a tax invoice
    made out to the company carries the buyer's registration. A group
    configured with that registration is a full, unambiguous answer to a
    question we would otherwise put to a person.

    Exact after normalisation, and never partial: a prefix match would
    attribute a receipt on the strength of a shared state code, and a wrongly
    attributed expense is worse than an unattributed one - the second is
    visibly unfinished, the first looks like an answer.
    """

    GROUPS = [{"id": "mobil80", "label": "Mobil80", "tax_id": "29AABCU9603R1ZM"},
              {"id": "wrktop", "label": "WRKTOP", "tax_id": "29AAACW1234R1Z5"},
              {"id": "prezence", "label": "Prezence"}]

    def test_a_registration_is_matched_however_it_is_printed(self):
        import taxid
        for printed in ("29AABCU9603R1ZM", "29 aabcu9603r 1zm",
                        "GSTIN: 29AABCU9603R1ZM", "GST No 29AABCU9603R1ZM"):
            self.assertEqual("mobil80", taxid.group_for(self.GROUPS, printed), printed)

    def test_a_registration_nobody_holds_matches_nothing(self):
        import taxid
        self.assertIsNone(taxid.group_for(self.GROUPS, "29ZZZZZ0000R1Z0"))

    def test_no_registration_is_not_a_match(self):
        # Most retail bills carry none, and every claim read before this field
        # existed has none. Empty must never match a group that also has none.
        import taxid
        for absent in ("", None, "   ", "GSTIN:"):
            self.assertIsNone(taxid.group_for(self.GROUPS, absent), repr(absent))

    def test_two_groups_sharing_one_registration_is_not_an_answer(self):
        # Picking the first would make attribution depend on the order somebody
        # happened to add them in.
        import taxid
        shared = [{"id": "a", "tax_id": "29AABCU9603R1ZM"},
                  {"id": "b", "tax_id": "29AABCU9603R1ZM"}]
        self.assertIsNone(taxid.group_for(shared, "29AABCU9603R1ZM"))

    def test_words_are_not_registrations(self):
        # OCR reads plenty of words where a number should be, and a word that
        # matched a group would attribute a receipt on nothing at all.
        import taxid
        for junk in ("INVOICE", "TAXINVOICE", "ORIGINAL", "x" * 40, "12"):
            self.assertEqual("", taxid.normalise(junk), junk)



class APolicySavedUnderTheOldRulesStillHasItsCap(unittest.TestCase):
    """The one migration failure that costs money rather than tidiness.

    Caps used to come in two kinds and a stored rule may still say `per_head`.
    The engine looks for `caps["per_transaction"]`, so an unmigrated policy
    reads as having *no cap* - and a type with no cap approves everything. A
    stored limit silently becoming no limit is not a cosmetic regression.
    """

    OLD = {"version": 3, "expense_types": [
        {"id": "meals", "label": "Meals & entertainment", "enabled": True,
         "hint": "", "categories": [],
         "caps": {"per_head": {"INR": "1500.00", "USD": "18.00"}},
         "requires_headcount": True, "requires_itemisation": False}]}

    def test_the_stored_amounts_survive_as_a_per_transaction_cap(self):
        rules = policy.rules_for({"rules": self.OLD})
        caps = rules["expense_types"][0]["caps"]
        self.assertEqual({"INR": "1500.00", "USD": "18.00"}, caps["per_transaction"])
        self.assertNotIn("per_head", caps)

    def test_and_the_engine_enforces_them(self):
        rules = policy.rules_for({"rules": self.OLD})
        self.assertEqual("approved", decide("1200.00", rules=rules)["verdict"])
        self.assertEqual("needs_review", decide("3705.00", rules=rules)["verdict"])

    def test_an_already_migrated_policy_is_left_alone(self):
        new = {"version": 4, "expense_types": [
            {"id": "meals", "label": "Meals", "enabled": True,
             "caps": {"per_transaction": {"INR": "3000.00"}}}]}
        rules = policy.rules_for({"rules": new})
        self.assertEqual({"INR": "3000.00"},
                         rules["expense_types"][0]["caps"]["per_transaction"])

    def test_the_normaliser_accepts_the_old_shape_on_save_too(self):
        # A browser holding a policy loaded before the change can still save it.
        cleaned = policy.normalise_rules(self.OLD)
        self.assertEqual({"INR": "1500.00", "USD": "18.00"},
                         cleaned["expense_types"][0]["caps"]["per_transaction"])
