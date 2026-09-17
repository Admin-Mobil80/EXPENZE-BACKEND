"""Validate our JSON schemas against OpenAI strict-mode rules.

Strict mode rejects a schema unless, at every object level, `required` lists
every property and `additionalProperties` is false. Catching a violation here
costs a second; catching it in production costs a deploy cycle.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))


def walk_objects(node, path="$"):
    """Yield (path, schema) for every object-typed subschema."""
    if isinstance(node, dict):
        t = node.get("type")
        types = t if isinstance(t, list) else [t]
        if "object" in types:
            yield path, node
        for key, value in node.items():
            if key in ("properties", "items", "$defs", "definitions"):
                if key == "properties":
                    for prop, sub in value.items():
                        yield from walk_objects(sub, f"{path}.{prop}")
                else:
                    yield from walk_objects(value, f"{path}[]")


class StrictModeSchemaTest(unittest.TestCase):
    def _assert_strict(self, schema, label):
        found = list(walk_objects(schema))
        self.assertTrue(found, f"{label}: no object schemas found")
        for path, node in found:
            props = set(node.get("properties", {}))
            required = set(node.get("required", []))
            self.assertEqual(
                props,
                required,
                f"{label} at {path}: strict mode needs every property in 'required'. "
                f"Missing: {sorted(props - required)}",
            )
            self.assertIs(
                node.get("additionalProperties"),
                False,
                f"{label} at {path}: strict mode needs additionalProperties=false",
            )

    def test_receipt_schema_is_strict_mode_valid(self):
        # Imported lazily: handler pulls in boto3, absent from the CDK venv.
        import policy

        schema = _load_receipt_schema(policy)
        self._assert_strict(schema, "RECEIPT_SCHEMA")

    def test_a_line_is_a_description_and_an_amount(self):
        # The schema is what the model is allowed to answer with, so a key
        # nothing decides is a key the model spends attention on for nothing.
        import policy

        schema = _load_receipt_schema(policy)
        props = schema["properties"]["line_items"]["items"]["properties"]
        self.assertNotIn("category", props)
        self.assertEqual(
            ["amount", "description", "description_original", "quantity"],
            sorted(schema["properties"]["line_items"]["items"]["required"]))

    def test_the_expense_type_enum_carries_the_escape_hatch(self):
        # An enum of only the configured types forces a wrong answer on
        # anything else, and a wrong type is the one classification that does
        # change what somebody is paid.
        import policy

        schema = _load_receipt_schema(policy)
        self.assertIn("not_covered", schema["properties"]["expense_type"]["enum"])


def _read_handler_source():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "lambda_src", "handler.py")) as fh:
        return fh.read()


def _exec_literal(name, policy_module):
    """Evaluate one module-level literal from handler.py without importing it."""
    import ast

    tree = ast.parse(_read_handler_source())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            return eval(  # noqa: S307 - our own source, evaluated in a fixed namespace
                compile(ast.Expression(node.value), "<schema>", "eval"),
                {"policy": policy_module, "sorted": sorted},
            )
    raise AssertionError(f"{name} not found in handler.py")


def _load_receipt_schema(policy_module):
    return _exec_literal("RECEIPT_SCHEMA", policy_module)


if __name__ == "__main__":
    unittest.main(verbosity=2)
