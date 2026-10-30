"""Every handler module imports, and its names resolve.

Mobil80-Exp-221 through Exp-227 all came out as "Not read automatically",
"not printed" and 0.00, across two channels and four submitters, because
`auditor_worker` called `policy.blocks_on` without importing `policy`. Every
audit raised `NameError` and every claim was parked after three attempts.

The tests covering that change were all source-text assertions - they checked
the right string was present, and a string is present whether or not the name
behind it resolves. Nothing in the suite ever imported the module.

So this does, for every handler the stack deploys. It is the cheapest test in
the suite and the only one that would have caught a missing import.
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest

LAMBDA_SRC = os.path.join(os.path.dirname(__file__), "..", "lambda_src")
sys.path.insert(0, LAMBDA_SRC)

# The table and bucket names these read at import. Nothing here touches one.
for _k, _v in (
    ("AWS_DEFAULT_REGION", "ap-southeast-1"), ("INTAKE_TABLE", "t"),
    ("USERS_TABLE", "u"), ("ORGS_TABLE", "o"), ("EXPENSES_TABLE", "e"),
    ("INTAKE_BUCKET", "b"), ("RECEIPTS_BUCKET", "r"), ("MAIL_BUCKET", "m"),
    ("AUDIT_TABLE", "a"), ("API_KEYS_TABLE", "k"), ("IDEMPOTENCY_TABLE", "i"),
    ("FINGERPRINTS_TABLE", "f"), ("ADVANCES_TABLE", "ad"),
    ("WA_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:y"),
    ("MOBILE_INDEX", "by-mobile"), ("ADMINS_TABLE", "ad2"),
    ("AUTH_TABLE", "au"), ("LEDGER_TABLE", "l"), ("SETTINGS_TABLE", "se"),
    ("OPENAI_MODEL", "gpt-5.6-sol"),
    ("SESSION_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:s"),
):
    os.environ.setdefault(_k, _v)

# Every module the stack names as a Lambda handler.
HANDLERS = [
    "admin", "alerts", "apikeys", "auditor_worker", "auth", "budgets",
    "capabilities", "digest", "duplicates", "eum", "fx", "grouping",
    "handler", "identity", "intake", "llm", "mail", "money", "notify",
    "payments", "pdfpages", "policy", "pricing", "receipts", "reference",
    "rzp_webhook", "secrets_loader", "taxid", "wacfg", "whatsapp",
]


class EveryHandlerImports(unittest.TestCase):
    def test_each_module_imports_cleanly(self):
        for name in HANDLERS:
            with self.subTest(module=name):
                importlib.import_module(name)


class EveryNameAModuleUsesIsImported(unittest.TestCase):
    """A call to `foo.bar()` means `foo` has to be in scope.

    Source-text tests cannot see this: `policy.blocks_on(...)` reads the same
    whether or not `import policy` is above it. Compiling the module and
    resolving the attribute is what tells them apart.
    """

    def test_the_modules_handlers_reach_for_are_present(self):
        for name in HANDLERS:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                source = open(os.path.join(LAMBDA_SRC, f"{name}.py"),
                              encoding="utf-8").read()
                for other in HANDLERS:
                    if other == name or f"{other}." not in source:
                        continue
                    # Only count it as a use if the call looks like one.
                    if f"\n{' ' * 0}" and f"{other}." in source:
                        used = any(
                            line.strip().startswith(f"{other}.")
                            or f"= {other}." in line
                            or f"({other}." in line
                            or f"return {other}." in line
                            for line in source.splitlines())
                        if used:
                            self.assertTrue(
                                hasattr(module, other),
                                f"{name}.py uses {other}.* but never imports it")


if __name__ == "__main__":
    unittest.main()
