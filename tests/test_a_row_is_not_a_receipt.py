"""Expenses typed on a spreadsheet, with no bill behind them.

Every other way into this product carries a receipt and the agent reads it:
the amount is what the paper says, the vendor is what is printed on it, a
duplicate is caught on the invoice number, and the tax registration decides
which cost centre it belongs to. A row of a spreadsheet carries none of that.
What it carries is what somebody typed.

That is what users asked for and it is worth building, but it is a different
kind of claim and the product has to say so. Two rules follow from it and this
file exists to hold them:

**It is marked, permanently.** Its own channel, a finding on the verdict, and
a flag on the receipt object - so a count, a filter, a report or a claim page
can all tell a typed row from a bill somebody sent in, without having the
upload in hand.

**It never clears automatically.** "Reviewed by the agent - it met policy with
nothing to decide" is a sentence about a bill that was read. There is no bill.
Saying it over a typed number would be the product claiming to have checked
something nobody has seen, so every row goes to a person.

What the engine can still do is worth having: a type no rule covers, a cost
centre that does not exist, a total over a cap. All answerable from the
figures, and all still asked.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from decimal import Decimal

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


class _Orgs:
    def __init__(self):
        self.seq = 0

    def update_item(self, **kw):
        self.seq += 1
        return {"Attributes": {"org_id": "org1", "name": "Mobil80",
                               "ref_seq": self.seq}}


class _Subs:
    def __init__(self):
        self.written = []

    def put_item(self, Item):
        self.written.append(Item)


class RowsBecomeClaims(unittest.TestCase):

    def setUp(self):
        self.subs, self.orgs = _Subs(), _Orgs()
        self.saved = {
            "submissions": auth._submissions, "orgs": auth._orgs,
            "ident": auth._identity_from_token,
            "resolve": auth.identity.resolve_by_email,
            "org_of": auth._org_of, "logged": auth._logged,
            "group": auth.intake.resolve_group,
        }
        auth._submissions, auth._orgs = self.subs, self.orgs
        auth._identity_from_token = lambda t: "reena@x.com"
        auth.identity.resolve_by_email = lambda e, channel=None: {
            "email": e, "org_id": "org1", "role": "staff", "name": "Reena"}
        auth._org_of = lambda actor: {
            "org_id": "org1", "name": "Mobil80", "default_currency": "INR",
            "groups": [{"id": "mobil80", "label": "Mobil80"}],
            "expense_types": [{"id": "meals", "label": "Meals", "enabled": True,
                               "caps": {}}],
        }
        auth._logged = lambda *a, **k: None
        auth.intake.resolve_group = lambda m, o: ("mobil80", "assigned")

    def tearDown(self):
        auth._submissions = self.saved["submissions"]
        auth._orgs = self.saved["orgs"]
        auth._identity_from_token = self.saved["ident"]
        auth.identity.resolve_by_email = self.saved["resolve"]
        auth._org_of = self.saved["org_of"]
        auth._logged = self.saved["logged"]
        auth.intake.resolve_group = self.saved["group"]

    def call(self, rows):
        reply = auth._claims_from_sheet("tok", {"rows": rows}, None)
        return reply["statusCode"], json.loads(reply["body"])

    def good(self, **over):
        row = {"date": "2026-10-01", "vendor": "Blue Tokai",
               "amount": "450.00", "currency": "INR", "expense_type": "meals"}
        row.update(over)
        return row

    def test_a_row_becomes_a_claim_with_a_reference(self):
        code, out = self.call([self.good()])
        self.assertEqual(200, code)
        self.assertEqual(1, len(self.subs.written))
        self.assertTrue(out["claims"][0]["reference"])

    def test_the_reference_comes_off_the_same_counter_as_every_other_claim(self):
        # A sheet row and a photographed bill are numbered in one sequence, so
        # nobody has to learn a second shape.
        code, out = self.call([self.good(), self.good(vendor="Third Wave")])
        refs = [c["reference"] for c in out["claims"]]
        self.assertEqual(len(refs), len(set(refs)))

    def test_it_carries_its_own_channel(self):
        self.call([self.good()])
        self.assertEqual("sheet", self.subs.written[0]["channel"])

    def test_and_a_flag_on_the_receipt_itself(self):
        # The claim page reads this object; the distinction has to survive
        # anywhere the row is not in hand.
        self.call([self.good()])
        self.assertTrue(self.subs.written[0]["receipt"]["typed_by_submitter"])

    def test_it_is_audited_rather_than_queued(self):
        # Leaving it queued would have the worker fetch a receipt that does
        # not exist.
        self.call([self.good()])
        self.assertEqual("audited", self.subs.written[0]["status"])
        self.assertEqual("", self.subs.written[0]["receipt_key"])

    def test_it_never_clears_automatically(self):
        self.call([self.good()])
        verdict = self.subs.written[0]["verdict"]
        self.assertEqual("needs_review", verdict["verdict"])

    def test_and_says_why_in_a_finding_a_reviewer_reads(self):
        self.call([self.good()])
        codes = [v["code"] for v in self.subs.written[0]["verdict"]["violations"]]
        self.assertIn("no_receipt", codes)
        first = self.subs.written[0]["verdict"]["violations"][0]
        self.assertEqual("no_receipt", first["code"])
        self.assertTrue(first["blocks_automatic_decision"])

    def test_the_policy_engine_still_runs_on_the_figures(self):
        # A type no rule covers is answerable without a bill, and is asked.
        self.call([self.good(expense_type="")])
        codes = [v["code"] for v in self.subs.written[0]["verdict"]["violations"]]
        self.assertIn("no_rule_for_expense_type", codes)

    def test_the_two_fingerprints_that_need_no_printed_number(self):
        # A typed row matching a bill somebody photographed is exactly the
        # case worth catching, and the invoice key cannot catch it.
        self.call([self.good()])
        row = self.subs.written[0]
        self.assertIn("#sender#", row["sender_fingerprint"])
        self.assertIn("#repeat#", row["repeat_fingerprint"])
        self.assertEqual("", row["fingerprint"])

    def test_no_credit_is_spent(self):
        # A credit pays for the model to read a receipt. Nothing is read.
        self.call([self.good()])
        for call in getattr(self.orgs, "calls", []):
            self.assertNotIn("credits", str(call))
        self.assertNotIn("credits", str(self.subs.written[0]))


class NothingIsWrittenUntilEveryRowIsGood(unittest.TestCase):
    """Half an upload is worse than none.

    The person has no way to tell which half landed, and re-uploading
    duplicates what already did.
    """

    def setUp(self):
        RowsBecomeClaims.setUp(self)

    def tearDown(self):
        RowsBecomeClaims.tearDown(self)

    call = RowsBecomeClaims.call
    good = RowsBecomeClaims.good

    def test_one_bad_row_writes_nothing(self):
        code, out = self.call([self.good(), self.good(amount="nope"),
                               self.good()])
        self.assertEqual(400, code)
        self.assertEqual([], self.subs.written)

    def test_and_names_the_line_to_fix(self):
        code, out = self.call([self.good(), self.good(amount="nope")])
        self.assertEqual(2, out["rows"][0]["row"])
        self.assertIn("amount", out["rows"][0]["error"])

    def test_a_missing_vendor_is_refused(self):
        code, out = self.call([self.good(vendor="  ")])
        self.assertEqual(400, code)
        self.assertIn("vendor", out["rows"][0]["error"])

    def test_a_date_that_is_not_a_date_is_refused(self):
        for bad in ("01/10/2026", "2026-13-01", "", "soon"):
            code, out = self.call([self.good(date=bad)])
            self.assertEqual(400, code, bad)

    def test_a_date_in_the_future_is_refused(self):
        # Money not yet spent, and the commonest cause is a column the
        # spreadsheet reformatted.
        code, out = self.call([self.good(date="2099-01-01")])
        self.assertEqual(400, code)
        self.assertIn("future", out["rows"][0]["error"])

    def test_a_type_this_organisation_does_not_have_is_refused(self):
        code, out = self.call([self.good(expense_type="helicopters")])
        self.assertEqual(400, code)
        self.assertIn("helicopters", out["rows"][0]["error"])

    def test_a_cost_centre_it_does_not_have_either(self):
        code, out = self.call([self.good(group_id="atlantis")])
        self.assertEqual(400, code)
        self.assertIn("atlantis", out["rows"][0]["error"])

    def test_an_empty_sheet_says_so(self):
        code, out = self.call([])
        self.assertEqual(400, code)
        self.assertIn("no rows", out["error"])

    def test_an_enormous_one_is_refused_with_the_number(self):
        # So a mistake is one somebody can still find.
        code, out = self.call([self.good() for _ in range(auth.SHEET_MAX_ROWS + 1)])
        self.assertEqual(400, code)
        self.assertIn(str(auth.SHEET_MAX_ROWS), out["error"])
        self.assertEqual([], self.subs.written)


class OnlyASignedInMember(unittest.TestCase):

    def setUp(self):
        RowsBecomeClaims.setUp(self)

    def tearDown(self):
        RowsBecomeClaims.tearDown(self)

    def test_no_session_no_claims(self):
        auth._identity_from_token = lambda t: ""
        reply = auth._claims_from_sheet("tok", {"rows": []}, None)
        self.assertEqual(401, reply["statusCode"])
        self.assertEqual([], self.subs.written)

    def test_a_token_with_no_membership_behind_it_either(self):
        auth.identity.resolve_by_email = lambda e, channel=None: None
        reply = auth._claims_from_sheet("tok", {"rows": []}, None)
        self.assertEqual(403, reply["statusCode"])

    def test_the_claim_is_filed_against_the_person_who_uploaded_it(self):
        # Not against a name in a column: a spreadsheet is editable, and
        # claiming on somebody else's behalf is not what this is.
        RowsBecomeClaims.call(self, [RowsBecomeClaims.good(self,
                                                           submitted_by="someone@else.com")])
        self.assertEqual("reena@x.com", self.subs.written[0]["submitted_by"])


class TheRouteIsWired(unittest.TestCase):

    def test_the_path_reaches_the_handler(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            src = h.read()
        self.assertIn('if path.endswith("/claims/sheet"):', src)
        self.assertIn("return _claims_from_sheet(tok, body, origin)", src)


if __name__ == "__main__":
    unittest.main()
