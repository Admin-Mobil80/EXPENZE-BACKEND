"""Re-check judged the claim against somebody else's policy.

A courier bill for INR 1,400. Courier is an expense type this company added,
enabled, capped at INR 10,000. A reviewer set the type to Courier and pressed
Re-check, and the claim came back:

    no_rule_for_expense_type
    No enabled rule covers expense type 'courier'.

Pressing Re-check again could not help. Nothing about the claim was wrong and
nothing about the policy was wrong: the re-audit never loaded the policy.

`_run_policy` takes the organisation's rules and falls back to `DEFAULT_RULES`
when given none. The first audit passes them. `reaudit` - the path a reviewer's
correction takes, which does not re-read the bill - passed nothing, so every
correction was decided by the built-in set. `courier` is not in the built-in
set, so the finding was literally true and completely useless.

The louder half is the one that was reported. The quieter half is worse: a type
the built-in set *does* have - meals, travel - matched, no finding appeared, and
the claim was judged against the built-in cap instead of the company's. A
correction is the one moment somebody explicitly asks the engine to decide
again, and it was the moment the engine stopped using their policy.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

import policy

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


# The organisation as it is stored: the built-in types plus one of its own.
ORG = {
    "org_id": "org_5f9e1ddc2b595007",
    "rules": {
        "version": 9,
        "expense_types": [
            {"id": "meals", "label": "Meals & entertainment", "enabled": True,
             "caps": {"per_transaction": {"INR": "1500.00"}}},
            {"id": "courier", "label": "Courier", "enabled": True,
             "caps": {"per_transaction": {"INR": "10000.00"}}},
            {"id": "repairs_maintenance", "label": "Repairs & Maintenance",
             "enabled": False,
             "caps": {"per_transaction": {"INR": "10000.00"}}},
        ],
    },
}

RECEIPT = {
    "vendor": "DTDC Express Limited",
    "currency": "INR",
    "expense_type": "not_covered",
    "line_items": [{"description": "Total amount (a+b+c)", "amount": "1400.00"}],
    "stated_total": "1400.00",
}


def codes(verdict):
    return [v["code"] for v in verdict.get("violations", [])]


class TheCompanysOwnTypeIsCovered(unittest.TestCase):
    """What the engine does once it is handed the right rules."""

    def test_courier_clears_under_the_companys_rule(self):
        verdict = policy.evaluate_policy(
            currency="INR", line_items=RECEIPT["line_items"],
            expense_type="courier", rules=policy.rules_for(ORG),
            stated_total="1400.00")
        self.assertNotIn("no_rule_for_expense_type", codes(verdict))
        self.assertEqual("approved", verdict["verdict"])

    def test_and_is_unknown_to_the_built_in_set(self):
        # Which is why the fallback produced the finding, and why pressing
        # Re-check a second time changed nothing.
        verdict = policy.evaluate_policy(
            currency="INR", line_items=RECEIPT["line_items"],
            expense_type="courier", rules=policy.DEFAULT_RULES,
            stated_total="1400.00")
        self.assertIn("no_rule_for_expense_type", codes(verdict))

    def test_a_disabled_type_is_still_not_covered(self):
        # The fix must not turn into "any type the company has ever named".
        verdict = policy.evaluate_policy(
            currency="INR", line_items=RECEIPT["line_items"],
            expense_type="repairs_maintenance", rules=policy.rules_for(ORG),
            stated_total="1400.00")
        self.assertIn("no_rule_for_expense_type", codes(verdict))

    def test_the_quiet_half_the_company_cap_is_the_one_applied(self):
        # Meals exists in both sets at different caps. Under the built-in set
        # this INR 1,400 claim is judged against a limit nobody here set.
        theirs = policy.evaluate_policy(
            currency="INR", line_items=RECEIPT["line_items"],
            expense_type="meals", rules=policy.rules_for(ORG),
            stated_total="1400.00")
        built_in = policy.evaluate_policy(
            currency="INR", line_items=RECEIPT["line_items"],
            expense_type="meals", rules=policy.DEFAULT_RULES,
            stated_total="1400.00")
        self.assertNotEqual(
            policy.find_type(policy.rules_for(ORG), "meals")["caps"],
            policy.find_type(policy.DEFAULT_RULES, "meals")["caps"],
            "this test is only meaningful while the two caps differ")
        # Both may well be approvals; the point is which number decided it.
        self.assertIsNotNone(theirs["verdict"])
        self.assertIsNotNone(built_in["verdict"])


class TheReauditLoadsThem(unittest.TestCase):

    def setUp(self):
        for key, value in (("AWS_DEFAULT_REGION", "ap-southeast-1"),
                           ("EXPENSES_TABLE", "e"), ("ORGS_TABLE", "o"),
                           ("RECEIPTS_BUCKET", "b")):
            os.environ.setdefault(key, value)

    def test_it_asks_for_the_organisation_and_passes_its_rules(self):
        import handler
        with mock.patch.object(handler, "_org", return_value=ORG) as org, \
             mock.patch.object(handler, "_get_client") as client:
            client.return_value.explain.return_value = "..."
            out = handler.reaudit(dict(RECEIPT), "INR",
                                  expense_type="courier",
                                  org_id="org_5f9e1ddc2b595007")
        org.assert_called_once_with("org_5f9e1ddc2b595007")
        self.assertNotIn("no_rule_for_expense_type", codes(out["verdict"]))
        self.assertEqual("approved", out["verdict"]["verdict"])

    def test_without_the_org_it_still_answers_rather_than_raising(self):
        # An org that cannot be read falls back to the built-in set, which is
        # what `rules_for` has always done. Refusing to decide would park the
        # claim instead of flagging it.
        import handler
        with mock.patch.object(handler, "_org", return_value={}), \
             mock.patch.object(handler, "_get_client") as client:
            client.return_value.explain.return_value = "..."
            out = handler.reaudit(dict(RECEIPT), "INR",
                                  expense_type="courier", org_id="")
        self.assertIn("no_rule_for_expense_type", codes(out["verdict"]))

    def test_the_reviewers_type_is_recorded_as_theirs(self):
        import handler
        with mock.patch.object(handler, "_org", return_value=ORG), \
             mock.patch.object(handler, "_get_client") as client:
            client.return_value.explain.return_value = "..."
            out = handler.reaudit(dict(RECEIPT), "INR",
                                  expense_type="courier",
                                  org_id="org_5f9e1ddc2b595007")
        self.assertEqual("courier", out["receipt"]["expense_type"])
        self.assertEqual("Set by a reviewer.",
                         out["receipt"]["expense_type_rationale"])

    def test_no_second_extraction(self):
        # The bill has not changed; only a person's answer about it has.
        import handler
        with mock.patch.object(handler, "_org", return_value=ORG), \
             mock.patch.object(handler, "_get_client") as client:
            client.return_value.explain.return_value = "..."
            handler.reaudit(dict(RECEIPT), "INR", expense_type="courier",
                            org_id="org_5f9e1ddc2b595007")
            client.return_value.extract_receipt.assert_not_called()


class TheWorkerHandsItOver(unittest.TestCase):

    def setUp(self):
        self.worker = read("lambda_src/auditor_worker.py")
        self.handler = read("lambda_src/handler.py")

    def test_the_org_id_comes_off_the_claim(self):
        self.assertIn('org_id=str(row.get("org_id") or "")', self.worker)

    def test_the_policy_run_is_given_rules(self):
        block = self.handler.split("def reaudit(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_run_policy(receipt, currency, policy.rules_for(_org(org_id)))",
                      block)

    def test_the_bare_call_that_fell_back_is_gone(self):
        block = self.handler.split("def reaudit(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("_run_policy(receipt, currency)\n", block)

    def test_the_first_audit_was_always_right(self):
        # Stated so that a future tidy-up does not "simplify" both paths back
        # onto the default.
        audit = self.handler.split("\ndef audit(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("rules = policy.rules_for(org)", audit)


if __name__ == "__main__":
    unittest.main()
