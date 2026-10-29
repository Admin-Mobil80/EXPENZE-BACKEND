"""Both spellings of the intake address reach the same rule.

`receipts@expenze.ai` is the one advertised - it is what `INTAKE_ADDRESS`
carries, what the channels page prints and what every message says. People
type the singular anyway, and a receipt that bounces is a claim nobody makes
twice.

So the singular is accepted and never advertised: one address to remember,
one forgiving alias behind it.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def src(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class BothSpellingsAreAccepted(unittest.TestCase):
    def setUp(self):
        self.stack = src("expensifyai", "stack.py")

    def test_the_singular_is_on_the_receipt_rule(self):
        self.assertIn('f"receipt@{zone_name}"', self.stack)

    def test_the_plural_is_still_there(self):
        self.assertIn('f"receipts@{zone_name}"', self.stack)

    def test_only_the_plural_is_advertised(self):
        """INTAKE_ADDRESS is what the product tells people to write to.

        If the singular ever became the advertised one, two addresses would be
        in circulation and the one printed on older messages would look wrong.
        """
        for line in self.stack.splitlines():
            if "INTAKE_ADDRESS" in line and "zone_name" in line:
                self.assertIn('f"receipts@{zone_name}"', line)


if __name__ == "__main__":
    unittest.main()
