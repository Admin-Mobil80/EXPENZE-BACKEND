"""No module-level name is bound twice in a Lambda module.

This exists because of one silent outage. `auth.py` bound `_submissions` to the
submissions table at import, and four hundred lines later defined
`def _submissions(...)` as the endpoint that lists them. Python let both
statements run, the later one won, and every call that reached the table
through that name met a function object instead:

    AttributeError: 'function' object has no attribute 'scan'

It took out four endpoints at once - listing receipts, viewing a stored
original, and recording an approval or a rejection - while the intake path went
on accepting receipts and charging credits perfectly. A receipt arrived, was
audited correctly, the sender was answered on WhatsApp, and it simply never
appeared in the console. Nothing about the symptom pointed at the cause.

No linter in this project would have caught it and no unit test touched it,
because the modules cannot be imported without live AWS configuration. So this
reads the source instead: a name assigned at module level and then reused by a
`def`, a `class`, or a second assignment is almost always this mistake, and it
is worth refusing outright rather than trusting that the next one is harmless.
"""
from __future__ import annotations

import ast
import os
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "lambda_src")

# Rebinding a name inside a conditional is a deliberate fallback, not a
# collision, so only statements at the top level of the module are considered.
IGNORE = {"__all__"}


def _module_bindings(tree: ast.Module) -> dict[str, list[tuple[str, int]]]:
    """Every top-level name, with what bound it and on which line."""
    seen: dict[str, list[tuple[str, int]]] = {}

    def note(name: str, kind: str, line: int) -> None:
        if name not in IGNORE:
            seen.setdefault(name, []).append((kind, line))

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    note(target.id, "assignment", target.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            note(node.target.id, "assignment", node.target.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            note(node.name, "def", node.lineno)
        elif isinstance(node, ast.ClassDef):
            note(node.name, "class", node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                # `import urllib.request` and `import urllib.parse` both bind
                # `urllib`, to the same object. That is ordinary Python, not a
                # collision, so several submodules of one package count once.
                note(bound, f"import {bound}", node.lineno)

    for name, bindings in seen.items():
        kinds = {kind for kind, _ in bindings}
        if len(bindings) > 1 and kinds == {f"import {name}"}:
            seen[name] = bindings[:1]
    return seen


class NoCollisions(unittest.TestCase):
    def test_no_module_level_name_is_bound_twice(self):
        offences = []
        for filename in sorted(os.listdir(SRC)):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(SRC, filename)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename)
            for name, bindings in _module_bindings(tree).items():
                if len(bindings) > 1:
                    where = ", ".join(f"{kind} at line {line}" for kind, line in bindings)
                    offences.append(f"{filename}: '{name}' bound {len(bindings)} times ({where})")

        self.assertEqual(offences, [], "\n".join(
            ["a module-level name is bound more than once; the later binding "
             "silently replaces the earlier one:"] + offences))


class TheOriginalBug(unittest.TestCase):
    """A sanity check on the detector itself, using the shape that got us."""

    SOURCE = (
        "import boto3\n"
        "_submissions = boto3.resource('dynamodb').Table('T')\n"
        "def _submissions(token):\n"
        "    return _submissions.scan()\n"
    )

    def test_a_table_shadowed_by_a_handler_is_caught(self):
        found = _module_bindings(ast.parse(self.SOURCE))
        self.assertEqual([k for k, v in found.items() if len(v) > 1], ["_submissions"])
        self.assertEqual([kind for kind, _ in found["_submissions"]], ["assignment", "def"])

    def test_a_name_bound_once_is_not_flagged(self):
        found = _module_bindings(ast.parse(
            "_table = 1\n"
            "def read():\n"
            "    _table = 2\n"      # local, and none of our business
            "    return _table\n"))
        self.assertEqual([k for k, v in found.items() if len(v) > 1], [])


if __name__ == "__main__":
    unittest.main()


class NoFunctionShadowsAModuleItUses(unittest.TestCase):
    """A local named after an import, in a function that also calls the import.

    `_send_invite` builds the email body into a variable called `html`, and
    `auth.py` imports the `html` module to escape the inviter's name into it.
    Python binds every assignment in a function body at compile time, so from
    the first line of that function `html` is a *local* - and `html.escape(...)`
    at the top of it raised:

        UnboundLocalError: cannot access local variable 'html'

    Only on the invite path, only once the escaping was added, and the console
    showed "Something went wrong. Try again." The module-level check above
    cannot see this: neither binding is at module level, and each is perfectly
    reasonable on its own.
    """

    def test_no_local_hides_a_module_the_same_function_calls(self):
        offences = []
        for entry in sorted(os.listdir(SRC)):
            if not entry.endswith(".py"):
                continue
            with open(os.path.join(SRC, entry), encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=entry)

            imported = {
                (alias.asname or alias.name.split(".")[0])
                for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names
            }

            for func in [n for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                assigned = {
                    target.id
                    for node in ast.walk(func) if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Name)
                }
                clashes = assigned & imported
                if not clashes:
                    continue
                # Only a problem if the function also uses the name as the
                # module - `html.escape`, `json.dumps`. A local that merely
                # borrows the word is ugly, not broken.
                for node in ast.walk(func):
                    if (isinstance(node, ast.Attribute)
                            and isinstance(node.value, ast.Name)
                            and node.value.id in clashes):
                        offences.append(
                            f"{entry}:{node.lineno} {func.name}() assigns to "
                            f"'{node.value.id}' and also calls "
                            f"{node.value.id}.{node.attr} on it")
        self.assertEqual(offences, [], "\n".join(offences))
