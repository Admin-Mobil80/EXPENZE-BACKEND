"""Storing an original, and who is allowed to look at one.

The S3 calls are exercised against the real bucket by hand; what is worth
holding still in a test suite is the part with no network in it - the filename
sanitising that ends up in a response header, what counts as a receipt, and
above all the rule that decides whose receipt a person may open.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

os.environ.setdefault("RECEIPTS_BUCKET", "test-bucket")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
# auth.py builds its table handles at import; these names are never called.
for var, value in {
    "AUTH_TABLE": "t", "USERS_TABLE": "t", "ORGS_TABLE": "t", "ADMINS_TABLE": "t",
    "INTAKE_TABLE": "t", "SESSION_SECRET_ARN": "arn:aws:secretsmanager:::secret:t",
}.items():
    os.environ.setdefault(var, value)

import receipts  # noqa: E402


class SafeName(unittest.TestCase):
    """The name goes into a Content-Disposition header, so it is a header
    injection vector before it is a nicety."""

    def test_strips_quotes_and_newlines(self):
        got = receipts.safe_name('bill";\r\nX-Evil: 1.png', "image/png")
        self.assertNotIn('"', got)
        self.assertNotIn("\n", got)
        self.assertNotIn("\r", got)

    def test_keeps_an_ordinary_name(self):
        self.assertEqual(receipts.safe_name("bombay-canteen.jpg", "image/jpeg"),
                         "bombay-canteen.jpg")

    def test_falls_back_to_the_type(self):
        self.assertEqual(receipts.safe_name("", "application/pdf"), "receipt.pdf")
        self.assertEqual(receipts.safe_name("   ", "image/jpeg"), "receipt.jpg")

    def test_caps_the_length(self):
        self.assertLessEqual(len(receipts.safe_name("a" * 500 + ".png", "image/png")), 120)


class AcceptedTypes(unittest.TestCase):
    def test_photographs_and_pdfs_are_receipts(self):
        for ctype in ("image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf"):
            self.assertTrue(receipts.is_supported(ctype), ctype)

    def test_nothing_else_is(self):
        for ctype in ("text/html", "application/zip", "image/svg+xml", "", "application/json"):
            self.assertFalse(receipts.is_supported(ctype), ctype)

    def test_parameters_are_ignored(self):
        self.assertTrue(receipts.is_supported("image/jpeg; charset=binary"))
        self.assertEqual(receipts.normalise_type("  IMAGE/PNG  "), "image/png")


class Keys(unittest.TestCase):
    def test_carries_no_identity(self):
        key = receipts.new_key("image/jpeg")
        self.assertTrue(key.startswith("originals/"))
        self.assertTrue(key.endswith(".jpg"))
        # 16 random bytes: guessing one is not an attack, it is a hobby.
        self.assertEqual(len(key.rsplit("/", 1)[-1]), 32 + len(".jpg"))

    def test_keys_do_not_repeat(self):
        self.assertEqual(len({receipts.new_key("image/png") for _ in range(200)}), 200)


class WhoMaySeeAReceipt(unittest.TestCase):
    """A submission id contains a timestamp, so it is guessable enough that the
    id alone must never be enough to read the receipt behind it."""

    def setUp(self):
        import auth
        self.may = auth._may_see

    def staff(self, email, org="org_a"):
        return {"email": email, "org_id": org, "role": "staff"}

    def sub(self, by, org="org_a"):
        return {"submitted_by": by, "org_id": org}

    def test_staff_see_their_own(self):
        self.assertTrue(self.may(self.staff("priya@x.com"), self.sub("priya@x.com")))

    def test_staff_do_not_see_a_colleagues(self):
        self.assertFalse(self.may(self.staff("priya@x.com"), self.sub("karan@x.com")))

    def test_finance_sees_the_organisations(self):
        finance = {"email": "meera@x.com", "org_id": "org_a", "role": "finance"}
        self.assertTrue(self.may(finance, self.sub("karan@x.com")))

    def test_owner_sees_the_organisations(self):
        owner = {"email": "arjun@x.com", "org_id": "org_a", "role": "owner"}
        self.assertTrue(self.may(owner, self.sub("karan@x.com")))

    def test_another_organisation_is_never_visible(self):
        finance = {"email": "meera@x.com", "org_id": "org_a", "role": "finance"}
        self.assertFalse(self.may(finance, self.sub("someone@y.com", org="org_b")))
        # Not even to someone with the same address in both organisations.
        self.assertFalse(self.may(finance, self.sub("meera@x.com", org="org_b")))

    def test_a_missing_org_matches_nothing(self):
        self.assertFalse(self.may({"email": "a@x.com", "role": "finance"}, {"submitted_by": "a@x.com"}))


if __name__ == "__main__":
    unittest.main()
