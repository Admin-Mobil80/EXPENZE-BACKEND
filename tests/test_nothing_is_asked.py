"""The product asks a submitter nothing, ever.

Every question the system used to put to the person who spent the money came
from a rule whose input a receipt does not carry: how many people a meal
covered, which cost centre a bill belonged to, what a reviewer wanted to know
before deciding. Each was reasonable alone. Together they made a system that
interrogated employees about arithmetic, and a queue with states that waited on
somebody outside finance.

So: the agent reads the bill, clears what it can, and hands the rest to a named
human who decides. A claimant hears twice - cleared and going to be paid, or
with a person - and is never asked for anything in between.

These tests are almost all assertions of absence. That is the point: the value
here is in what is not there, and absence is exactly what rots back in.
"""
from __future__ import annotations

import glob
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def server_sources():
    for path in sorted(glob.glob(os.path.join(ROOT, "lambda_src", "*.py"))):
        yield os.path.basename(path), open(path, encoding="utf-8").read()


def code_only(text, comment_starts=("#",)):
    """Source with comments and docstrings stripped, so prose describing what
    was removed does not read as the thing itself."""
    out, in_doc = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            in_doc = not in_doc or stripped.count('"""') == 2 and False
            continue
        if in_doc or stripped.startswith(comment_starts):
            continue
        out.append(line)
    return "\n".join(out)


class NoHeadcountAnywhere(unittest.TestCase):
    """A per-head cap is the one rule that cannot be decided from a receipt."""

    def test_no_server_module_mentions_one(self):
        for name, src in server_sources():
            self.assertNotIn("attendee", code_only(src), name)

    def test_the_console_does_not_either(self):
        app = open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8").read()
        for token in ("attendees", "attendeeCount", "answerHeadcount"):
            self.assertNotIn(token, app, token)

    def test_the_extraction_schema_does_not_ask_the_model_for_one(self):
        # Asking the model is where it starts: a field in the schema becomes a
        # value on the claim, which becomes a rule, which becomes a question.
        handler = open(os.path.join(ROOT, "lambda_src/handler.py"), encoding="utf-8").read()
        self.assertNotIn("attendee_count", handler)

    def test_the_module_that_collected_answers_is_gone(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "lambda_src/answers.py")))


class NoChannelCarriesAQuestion(unittest.TestCase):

    def test_whatsapp_asks_nothing(self):
        wa = code_only(open(os.path.join(ROOT, "lambda_src/whatsapp.py"), encoding="utf-8").read())
        for token in ("ask_group", "record_reply", "find_claim", "read_headcount"):
            self.assertNotIn(token, wa, token)

    def test_email_reads_replies_for_context_only(self):
        mail = code_only(open(os.path.join(ROOT, "lambda_src/mail.py"), encoding="utf-8").read())
        for token in ("answers.", "_ask_which", "_acknowledge_answer"):
            self.assertNotIn(token, mail, token)

    def test_there_is_no_notice_that_asks(self):
        notify = open(os.path.join(ROOT, "lambda_src/notify.py"), encoding="utf-8").read()
        self.assertNotIn("def queried_notice", notify)
        self.assertNotIn("def ask_group", notify)
        self.assertNotIn('"queried":', notify)

    def test_a_reviewer_cannot_query_a_submitter(self):
        auth = open(os.path.join(ROOT, "lambda_src/auth.py"), encoding="utf-8").read()
        actions = auth.split("REVIEW_ACTIONS = ", 1)[1].split("\n", 1)[0]
        self.assertNotIn("queried", actions)
        self.assertIn("approved", actions)
        self.assertIn("rejected", actions)

    def test_the_console_offers_no_such_button(self):
        app = open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8").read()
        self.assertNotIn("Ask submitter", app)
        self.assertNotIn("Query sent", app)

    def test_the_headcount_endpoint_is_gone(self):
        auth = open(os.path.join(ROOT, "lambda_src/auth.py"), encoding="utf-8").read()
        self.assertNotIn("/claim/answer", auth)
        self.assertNotIn("def _claim_answer", auth)


class AnUnresolvedGroupIsReviewWork(unittest.TestCase):
    """It is finance's question, answerable in one click by the person who is
    going to approve the claim anyway - not an accounting question to put to
    somebody who photographed a bill."""

    def test_the_auditor_raises_it_as_a_finding(self):
        worker = open(os.path.join(ROOT, "lambda_src/auditor_worker.py"), encoding="utf-8").read()
        self.assertIn('"code": "group_not_set"', worker)
        self.assertIn('"blocks_automatic_decision": True', worker)

    def test_and_does_not_message_anybody_about_it(self):
        worker = code_only(open(os.path.join(ROOT, "lambda_src/auditor_worker.py"), encoding="utf-8").read())
        self.assertNotIn("ask_group", worker)

    def test_the_bill_is_still_consulted_first(self):
        # A tax invoice made out to the company answers it outright, and that
        # costs nobody anything.
        worker = open(os.path.join(ROOT, "lambda_src/auditor_worker.py"), encoding="utf-8").read()
        self.assertIn("grouping.group_for(", worker)
        self.assertLess(worker.index("grouping.group_for("), worker.index("group_not_set"))


class TheSubmitterHearsTwoThings(unittest.TestCase):

    def setUp(self):
        self.notify = open(os.path.join(ROOT, "lambda_src/notify.py"), encoding="utf-8").read()
        self.fn = self.notify.split("def outcome_notice(", 1)[1].split("\ndef ", 1)[0]

    def test_cleared_or_with_a_person_and_nothing_else(self):
        body = code_only(self.fn)
        self.assertIn('verdict == "approved"', body)
        self.assertNotIn("partially_approved", body)
        self.assertNotIn("question", body)

    def test_it_does_not_recite_the_reasons_to_them(self):
        # Over a cap, a type nothing covers, a possible duplicate: a reviewer's
        # business. Telling a claimant invites them to argue a case to the
        # wrong audience, or to feel accused when the answer is usually yes.
        self.assertNotIn('claim.get("reasons")', self.fn)

    def test_nothing_in_it_asks_for_a_reply(self):
        self.assertNotIn("Just reply here", self.fn)
        self.assertNotIn("reply with the answer", self.fn)


class TheEngineAndItsPortAgree(unittest.TestCase):
    """The console carries a JavaScript port of policy.py. Two implementations
    of one rule set is a liability, and the whole point of the simplification is
    that there is now almost nothing for them to disagree about."""

    def setUp(self):
        self.app = open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8").read()
        self.port = self.app.split("function evaluatePolicy(", 1)[1].split("\n}", 1)[0]

    def test_the_port_takes_the_same_arguments(self):
        self.assertIn("function evaluatePolicy(currency, lineItems, expenseType, statedTotal)",
                      self.app)

    def test_it_reaches_the_same_two_verdicts(self):
        self.assertIn('blocked ? "needs_review" : "approved"', self.port)
        self.assertNotIn("partially_approved", self.port)

    def test_it_emits_only_codes_the_engine_emits(self):
        engine = open(os.path.join(ROOT, "lambda_src/policy.py"), encoding="utf-8").read()
        server = set(re.findall(r'"code": "(\w+)"', engine))
        port = set(re.findall(r'code:"(\w+)"', self.port))
        self.assertTrue(port <= server, f"console-only codes: {port - server}")

    def test_reimbursable_is_the_receipt_total_on_both_sides(self):
        self.assertIn("reimbursable: receiptTotal", self.port)
