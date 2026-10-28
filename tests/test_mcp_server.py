"""The MCP server, which is a protocol shim and must stay one.

It exists so that Claude - and, behind it, a voice channel - can ask Expenze
about the money. The whole design rests on it holding no arithmetic: every
figure comes from `POST /auth/reports`, so there is exactly one implementation
of "what did October cost" rather than one per reader. Three readers asking
one question three ways is how this codebase produced a 69% and a 20%
auto-clear rate about the same seventy-one receipts.

Two failures here are silent rather than loud, and both are tested for.

**A tool pointing at a report the backend does not have.** Nothing catches it
until somebody asks the question and gets a 400 back, phrased as if they had
typed something wrong. The two lists are cross-checked instead.

**Anything printed to stdout.** stdout carries the JSON-RPC stream. A stray
`print` for debugging is a parse error at the client, and the symptom is not
an error message - it is the server appearing not to exist.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "lambda_src"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "MCP"))
for _k, _v in (("AWS_DEFAULT_REGION", "ap-southeast-1"), ("USERS_TABLE", "u"),
               ("ORGS_TABLE", "o"), ("INTAKE_TABLE", "t"), ("AUTH_TABLE", "a"),
               ("ADMINS_TABLE", "d"), ("EXPENSES_TABLE", "e"),
               ("ADVANCES_TABLE", "adv"), ("OTP_SENDER", "no-reply@expenze.ai"),
               ("INTAKE_ADDRESS", "receipts@expenze.ai"),
               ("SESSION_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:y")):
    os.environ.setdefault(_k, _v)

import expenze_mcp  # noqa: E402

MCP_SOURCE = os.path.join(HERE, "..", "..", "MCP", "expenze_mcp.py")


def send(method: str, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return expenze_mcp.handle(message)


class TheHandshake(unittest.TestCase):

    def test_a_version_we_know_is_echoed_back(self):
        # Clients differ on the date stamp and do not disagree in substance.
        out = send("initialize", {"protocolVersion": "2024-11-05"})["result"]
        self.assertEqual("2024-11-05", out["protocolVersion"])

    def test_one_we_do_not_know_gets_ours(self):
        out = send("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(expenze_mcp.DEFAULT_PROTOCOL, out["protocolVersion"])

    def test_it_declares_tools_and_nothing_else(self):
        out = send("initialize", {})["result"]
        self.assertEqual({"tools": {}}, out["capabilities"])

    def test_it_tells_the_model_to_name_which_figure_it_is_quoting(self):
        # An unlabelled total is the whole hazard of this channel.
        said = send("initialize", {})["result"]["instructions"].lower()
        self.assertIn("claimed", said)
        self.assertIn("approved", said)
        self.assertIn("paid", said)


class NotificationsGetNothingBack(unittest.TestCase):
    """A reply to a notification is a protocol error at the client."""

    def test_initialized_is_silent(self):
        self.assertIsNone(send("notifications/initialized", {}, request_id=None))

    def test_so_is_anything_else_without_an_id(self):
        self.assertIsNone(send("tools/list", {}, request_id=None))


class TheToolList(unittest.TestCase):

    def tools(self):
        return send("tools/list")["result"]["tools"]

    def test_every_tool_says_it_only_reads(self):
        """The hints are the contract, not the documentation.

        A client uses them to decide what may run without stopping to ask a
        person. Expenze's claim is that money moves only after somebody has
        read a bill, and a tool call cannot show a bill - so every tool here
        reads, and says so in the field a client actually consults.
        """
        for tool in self.tools():
            self.assertTrue(tool["annotations"]["readOnlyHint"], tool["name"])
            self.assertFalse(tool["annotations"]["destructiveHint"], tool["name"])

    def test_the_surface_is_the_one_that_was_agreed(self):
        """Spelled out rather than pattern-matched, so adding a tool is a decision.

        A test looking for words like `approve` or `settle` reads well and
        catches nothing: `settle` is a substring of `pending_settlement`,
        which is a read. An explicit set fails the moment anything is added,
        which is the point - a tool that moves money needs an argument made
        for it, not a naming convention that happens to let it through.
        """
        self.assertEqual(
            {"expenze_briefing", "expenze_spend", "expenze_queue",
             "expenze_pending_settlement", "expenze_claim",
             "expenze_auto_clear"},
            {t["name"] for t in self.tools()})

    def test_every_tool_is_described_and_schema_d(self):
        for tool in self.tools():
            self.assertGreater(len(tool["description"]), 80, tool["name"])
            self.assertEqual("object", tool["inputSchema"]["type"])
            self.assertFalse(tool["inputSchema"]["additionalProperties"])

    def test_asking_after_one_claim_requires_the_reference(self):
        one = [t for t in self.tools() if t["name"] == "expenze_claim"][0]
        self.assertEqual(["ref"], one["inputSchema"]["required"])


class TheToolsMatchTheBackend(unittest.TestCase):
    """The silent failure: a tool pointing at a report that does not exist."""

    def test_every_tool_maps_to_a_report_auth_knows(self):
        import auth
        for tool, report in expenze_mcp.REPORT_OF.items():
            self.assertIn(report, auth.REPORTS, f"{tool} -> {report}")

    def test_and_every_tool_in_the_list_has_a_mapping(self):
        listed = {t["name"] for t in send("tools/list")["result"]["tools"]}
        self.assertEqual(listed, set(expenze_mcp.REPORT_OF))

    def test_the_reports_auth_offers_are_all_reachable(self):
        # A report nobody can ask for is dead weight; this fails when one is
        # added to the backend and not exposed here.
        import auth
        self.assertEqual(set(auth.REPORTS), set(expenze_mcp.REPORT_OF.values()))


class CallingATool(unittest.TestCase):

    def setUp(self):
        self.asked = []

        def fake(report, **args):
            self.asked.append((report, args))
            return {"report": report, "claimed": "1000.00"}

        self.real, expenze_mcp.ask = expenze_mcp.ask, fake
        self.addCleanup(setattr, expenze_mcp, "ask", self.real)

    def call(self, name, args=None):
        return send("tools/call", {"name": name, "arguments": args or {}})["result"]

    def test_the_period_is_passed_through(self):
        self.call("expenze_spend", {"period": "october"})
        self.assertEqual([("spend", {"period": "october"})], self.asked)

    def test_a_missing_period_is_left_to_the_backend_s_default(self):
        self.call("expenze_spend")
        self.assertEqual([("spend", {})], self.asked)

    def test_the_claim_reference_is_passed_through(self):
        self.call("expenze_claim", {"ref": "Exp-134"})
        self.assertEqual([("claim", {"ref": "Exp-134"})], self.asked)

    def test_the_briefing_takes_nothing(self):
        self.call("expenze_briefing")
        self.assertEqual([("brief", {})], self.asked)

    def test_the_answer_comes_back_as_readable_json(self):
        out = self.call("expenze_spend")
        self.assertFalse(out.get("isError"))
        self.assertEqual("1000.00",
                         json.loads(out["content"][0]["text"])["claimed"])

    def test_an_unknown_tool_is_an_error_not_a_crash(self):
        out = self.call("expenze_nonsense")
        self.assertTrue(out["isError"])

    def test_arguments_that_are_not_an_object_are_refused(self):
        out = send("tools/call", {"name": "expenze_spend", "arguments": []})
        self.assertEqual(-32602, out["error"]["code"])


class WhenExpenzeCannotBeReached(unittest.TestCase):
    """The answer has to say so, rather than look like a figure of nothing."""

    def setUp(self):
        # Both have to be present for the test to be about the one it blanks;
        # neither is set when the suite runs, so blanking one would otherwise
        # always report the other.
        self.kept = (expenze_mcp.API, expenze_mcp.TOKEN)
        expenze_mcp.API, expenze_mcp.TOKEN = "https://example.invalid", "t"

    def tearDown(self):
        expenze_mcp.API, expenze_mcp.TOKEN = self.kept

    def test_an_unconfigured_server_says_which_setting_is_missing(self):
        for setting, field in (("EXPENZE_API", "API"), ("EXPENZE_TOKEN", "TOKEN")):
            with self.subTest(setting):
                kept = getattr(expenze_mcp, field)
                setattr(expenze_mcp, field, "")
                try:
                    said = expenze_mcp.ask("brief").get("error") or ""
                finally:
                    setattr(expenze_mcp, field, kept)
                self.assertIn(setting, said)


class UnknownMethods(unittest.TestCase):

    def test_a_capability_we_never_advertised_gets_the_standard_answer(self):
        for method in ("resources/list", "prompts/list", "completion/complete"):
            out = send(method)
            self.assertEqual(-32601, out["error"]["code"], method)

    def test_ping_is_answered(self):
        self.assertEqual({}, send("ping")["result"])


class StdoutCarriesTheProtocol(unittest.TestCase):
    """A debugging print here is indistinguishable from a broken server."""

    def source(self):
        with open(MCP_SOURCE, encoding="utf-8") as handle:
            return handle.read()

    def test_nothing_prints_to_stdout(self):
        for line in self.source().splitlines():
            stripped = line.strip()
            if not stripped.startswith("print("):
                continue
            self.assertIn("file=sys.stderr", stripped, stripped)

    def test_and_the_reason_is_written_down_where_somebody_would_add_one(self):
        self.assertIn("stdout is the protocol", self.source())


class ItHoldsNoArithmeticOfItsOwn(unittest.TestCase):
    """Every figure through one function, so there is one of each figure."""

    def test_the_only_way_out_is_the_reports_endpoint(self):
        source = self.read()
        self.assertEqual(1, source.count('f"{API}/reports"'))

    def test_it_does_no_money_arithmetic(self):
        source = self.read()
        for sign in ("Decimal", "import decimal"):
            self.assertNotIn(sign, source)

    def read(self):
        with open(MCP_SOURCE, encoding="utf-8") as handle:
            # The module docstring explains at length why there is no
            # arithmetic here, and says words this class asserts the absence
            # of further down.
            return handle.read().split('"""', 2)[2]


if __name__ == "__main__":
    unittest.main()
