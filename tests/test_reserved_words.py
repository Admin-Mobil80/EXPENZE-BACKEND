"""DynamoDB reserved words in expressions, which fail only at runtime.

Renaming somebody in the People tab did nothing. No error on screen, no
message, and the row still said "riyad" after a reload - because the update
was `SET name = :name`, `name` is a DynamoDB reserved word, and the whole
statement was rejected by the service. The staff id being set in the same
request went with it.

Nothing catches this before it runs: the expression is a string, boto3 passes
it through untouched, and the table only objects when the call is made. The
code around it already knew - `status` and `role` are written through `#s` and
`#r` - which is what makes the one that was missed so easy to miss.

So the check is here instead. The list is the four reserved words this
codebase actually stores attributes under, verified against the live service
rather than recalled: a longer list copied from documentation would rot, and
a shorter one would be this bug again.
"""
from __future__ import annotations

import ast
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
SOURCE = os.path.join(ROOT, "lambda_src")

# Reserved by DynamoDB *and* used as an attribute name somewhere in this
# codebase. Each one was checked against the real service with an update that
# could not commit; anything the service accepted is deliberately not here.
RESERVED = ("name", "status", "role", "timestamp")

# The keyword arguments whose value the service parses as an expression.
EXPRESSIONS = ("UpdateExpression", "ConditionExpression", "FilterExpression",
               "KeyConditionExpression", "ProjectionExpression")


def _literal(node: ast.AST) -> str | None:
    """The string an expression argument evaluates to, where that is knowable.

    Handles the two shapes in this codebase: a plain string, and adjacent
    strings joined across lines by the parser. A value built from a variable
    is not knowable here and is left to the reader.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal(v) or "" for v in node.values)
    return None


def expressions_in(path: str):
    """Every expression string written in one file, with its line number."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg in EXPRESSIONS:
                text = _literal(kw.value)
                if text:
                    yield kw.lineno, kw.arg, text


class NoExpressionNamesAReservedWordDirectly(unittest.TestCase):
    def test_every_reserved_attribute_goes_through_a_placeholder(self):
        offences = []
        for entry in sorted(os.listdir(SOURCE)):
            if not entry.endswith(".py"):
                continue
            path = os.path.join(SOURCE, entry)
            for line, kind, text in expressions_in(path):
                for word in RESERVED:
                    # Bare, not as part of a longer attribute (`org_name`,
                    # `group_status`) and not behind the `#` that makes it a
                    # placeholder reference.
                    if re.search(rf"(?<![#\w]){word}\b(?!\w)", text):
                        offences.append(f"{entry}:{line} {kind} uses bare {word!r}: {text}")
        self.assertEqual(offences, [], "\n".join(offences))


class TheOneThatShipped(unittest.TestCase):
    """The member update, specifically, because it is the one that was wrong."""

    def setUp(self):
        with open(os.path.join(SOURCE, "auth.py"), encoding="utf-8") as handle:
            self.update = handle.read().split(
                "def _member_update(", 1)[1].split("\ndef ", 1)[0]

    def test_the_name_is_written_through_a_placeholder(self):
        self.assertIn('names[f"#{field}"] = field', self.update)
        self.assertNotIn('updates.append(f"{field} = :{field}")', self.update)

    def test_the_staff_id_travels_the_same_way(self):
        # Both, not just the one that needs it. A loop where one field takes
        # a different path is where this reappears the next time a field is
        # added to it.
        self.assertIn('for field in ("name", "staff_id"):', self.update)



class TheOwnerHasAName(unittest.TestCase):
    """Nobody ever typed one in, so the console invented one from the address.

    A membership with no `name` is displayed by the local part of its email,
    which is how the person who created the organisation appeared throughout
    their own console - and on every claim they decided - as "riyad". The
    rename in People is the cure; asking at sign-up is the prevention.
    """

    def setUp(self):
        with open(os.path.join(SOURCE, "auth.py"), encoding="utf-8") as handle:
            self.auth = handle.read()
        with open(os.path.join(ROOT, "..", "PORTAL", "index.html"), encoding="utf-8") as handle:
            self.site = handle.read()

    def test_signup_asks_for_it(self):
        dispatch = self.auth.split('path.endswith("/signup")', 1)[1].split("if path", 1)[0]
        self.assertIn('body.get("full_name"', dispatch)
        self.assertIn('"Enter your name."', dispatch)

    def test_signup_stores_it_on_the_membership(self):
        signup = self.auth.split("def _signup(", 1)[1].split("\ndef ", 1)[0]
        members = signup.split("_users.put_item(", 1)[1]
        self.assertIn('"name":', members)

    def test_the_form_collects_it_and_sends_it(self):
        self.assertIn('id="fullname"', self.site)
        self.assertIn('document.getElementById("fullname")', self.site)
        self.assertIn("full_name,", self.site)


if __name__ == "__main__":
    unittest.main()
