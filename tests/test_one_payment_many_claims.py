"""Finance pays a person, not a claim.

Somebody with four receipts waiting is owed one amount and gets one bank
transfer. Settling that four times over left four records each claiming to be
the payment, four rows in the audit log against one UTR, and four messages
about money that moved once - so a person owed 25,176.50 was told about it four
times, in four amounts, none of which was the figure on their statement.

What is recorded is still per claim, because that is what a claim is: each row
keeps its own share, and they share the reference that ties all of them to the
one line on the statement. What changes is that it is one act, and the person
hears about it as one payment.

The submitter filter above the list is how a mixed list becomes one person's,
and the button only appears once it is - a transfer goes to somebody, and
"settle these eight claims across three people" is three transfers however it
is recorded.
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
import notify  # noqa: E402


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class _Table:
    def __init__(self, rows):
        self.rows, self.writes = rows, []

    def get_item(self, Key):
        row = self.rows.get(Key["submission_id"])
        return {"Item": row} if row else {}

    def update_item(self, **kw):
        self.writes.append(kw)


def _row(sid, ref, who, **over):
    row = {"submission_id": sid, "reference": ref, "org_id": "org1",
           "submitted_by": who, "receipt": {"vendor": "A vendor"},
           # Ready to pay: an expense type the policy covers and a cost
           # centre. A claim missing either is refused at settlement now,
           # which the tests below check on purpose.
           "answered_expense_type": "meals", "group_id": "mobil80",
           "verdict": {"expense_type": "meals", "currency": "INR"}}
    row.update(over)
    return row


class TheEndpointRefusesBeforeItWrites(unittest.TestCase):
    """A batch that fails half way is worse than one that fails at the start:
    the money has gone and the record of it is partial."""

    def setUp(self):
        self.rows = {
            "a": _row("a", "Exp-1", "reena@x.com"),
            "b": _row("b", "Exp-2", "reena@x.com"),
            "c": _row("c", "Exp-3", "manoj@x.com"),
        }
        self.table = _Table(self.rows)
        self.sent = []
        # Saved and put back in tearDown. These are attributes on modules the
        # rest of the suite shares, and leaving a stub on `identity` made a
        # sign-in test three files away fail for no reason it could see.
        self._saved = {
            "submissions": getattr(auth, "_submissions", None),
            "identity_from_token": auth._identity_from_token,
            "resolve": auth.identity.resolve_by_email,
            "logged": auth._logged,
            "send": auth.notify.send,
            "record": auth.notify.record,
            "org_of": auth._org_of,
        }
        # The settlement checks read the organisation's policy and groups.
        # Without this the test reaches for real DynamoDB, which is both slow
        # and a different thing from what it is trying to test.
        auth._org_of = lambda actor: {
            "groups": [{"id": "mobil80", "label": "Mobil80"}],
            "expense_types": [{"id": "meals", "label": "Meals", "enabled": True}],
        }
        auth._submissions = self.table
        auth._identity_from_token = lambda t: "fin@x.com"
        auth.identity.resolve_by_email = lambda e, channel=None: (
            {"email": e, "org_id": "org1", "role": "finance", "name": "Madhusudhan"}
            if e == "fin@x.com"
            else {"email": e, "org_id": "org1", "role": "staff", "name": "Reena"})
        auth._logged = lambda *a, **k: None
        auth.notify.send = lambda kind, m, c, only="": (
            self.sent.append({"kind": kind, "only": only, "claim": c})
            or {"email": only != "whatsapp", "whatsapp": only != "email"})
        auth.notify.record = lambda *a, **k: None
        self.good = {
            "claims": [{"submission_id": "a", "amount": "100.00"},
                       {"submission_id": "b", "amount": "50.00"}],
            "paid": "150.00", "currency": "INR",
            "reference": "UTR1", "mode": "bank transfer",
        }

    def tearDown(self):
        auth._submissions = self._saved["submissions"]
        auth._identity_from_token = self._saved["identity_from_token"]
        auth.identity.resolve_by_email = self._saved["resolve"]
        auth._logged = self._saved["logged"]
        auth.notify.send = self._saved["send"]
        auth.notify.record = self._saved["record"]
        auth._org_of = self._saved["org_of"]

    def call(self, body):
        reply = auth._claim_settle_batch("tok", body, None)
        return reply["statusCode"], json.loads(reply["body"])

    def test_it_settles_one_persons_claims(self):
        code, out = self.call(self.good)
        self.assertEqual(200, code)
        self.assertEqual(["a", "b"], out["settled"])
        self.assertEqual(2, len(self.table.writes))

    def test_each_claim_keeps_its_own_share(self):
        # The payment is one; the claims are not. A row that recorded the
        # whole transfer would say this claim was worth the lot.
        self.call(self.good)
        paid = [w["ExpressionAttributeValues"][":p"] for w in self.table.writes]
        self.assertEqual(["100.00", "50.00"], paid)

    def test_and_they_share_the_reference(self):
        # It is the only thing tying several claims to one debit.
        self.call(self.good)
        for write in self.table.writes:
            self.assertEqual("UTR1", write["ExpressionAttributeValues"][":ref"])

    def test_a_payout_needs_a_reference(self):
        code, out = self.call({**self.good, "reference": ""})
        self.assertEqual(400, code)
        self.assertIn("bank statement", out["error"])
        self.assertEqual([], self.table.writes)

    def test_a_float_settlement_does_not(self):
        # No transfer was made, so there is no statement line to tie it to and
        # asking for one only gets a made-up reference.
        code, _ = self.call({**self.good, "reference": "", "source": "float"})
        self.assertEqual(200, code)

    def test_two_people_are_two_payments(self):
        code, out = self.call({**self.good, "claims": [
            {"submission_id": "a", "amount": "100.00"},
            {"submission_id": "c", "amount": "10.00"}]})
        self.assertEqual(400, code)
        self.assertIn("different people", out["error"])
        self.assertEqual([], self.table.writes)

    def test_a_total_that_no_longer_adds_up_stops_everything(self):
        # Something settled in another tab while the form was open. The safe
        # answer is to write nothing and let somebody look again.
        code, out = self.call({**self.good, "paid": "999.00"})
        self.assertEqual(409, code)
        self.assertIn("changed while this was open", out["error"])
        self.assertEqual([], self.table.writes)

    def test_a_claim_from_another_organisation_is_not_found(self):
        self.rows["a"]["org_id"] = "someone-else"
        code, _ = self.call(self.good)
        self.assertEqual(404, code)
        self.assertEqual([], self.table.writes)

    def test_an_amount_of_nothing_is_refused(self):
        code, out = self.call({**self.good, "claims": [
            {"submission_id": "a", "amount": "0"}]})
        self.assertEqual(400, code)
        self.assertIn("no amount to pay", out["error"])

    def test_an_empty_batch_is_refused(self):
        code, _ = self.call({**self.good, "claims": []})
        self.assertEqual(400, code)

    def test_a_batch_nobody_could_check_is_refused(self):
        many = [{"submission_id": "a", "amount": "1.00"}
                for _ in range(auth.BATCH_MAX_CLAIMS + 1)]
        code, out = self.call({**self.good, "claims": many, "paid": ""})
        self.assertEqual(400, code)
        self.assertIn("at most", out["error"])

    def test_a_top_up_adds_to_what_was_already_paid(self):
        # Same rule as a single settlement: outcome_paid is the running total.
        self.rows["a"]["outcome"] = "settled"
        self.rows["a"]["outcome_paid"] = "20.00"
        self.call(self.good)
        self.assertEqual("120.00", self.table.writes[0]["ExpressionAttributeValues"][":p"])

    def test_a_claim_with_no_expense_type_is_not_paid(self):
        # Every payment is reported under one. A payment filed under nothing
        # is a line in the accounts nobody can explain.
        self.rows["a"]["answered_expense_type"] = "not_covered"
        self.rows["a"]["verdict"] = {"expense_type": "not_covered"}
        code, out = self.call(self.good)
        self.assertEqual(409, code)
        self.assertIn("no expense type your policy covers", out["error"])
        self.assertIn("Exp-1", out["error"])
        self.assertEqual([], self.table.writes)

    def test_a_claim_with_no_cost_centre_is_not_paid(self):
        self.rows["b"]["group_id"] = ""
        code, out = self.call(self.good)
        self.assertEqual(409, code)
        self.assertIn("not attributed to a cost centre", out["error"])
        self.assertEqual([], self.table.writes)

    def test_the_check_names_the_claim_that_stopped_it(self):
        # Two claims in, one bad: the reviewer has to know which to open.
        self.rows["b"]["group_id"] = ""
        _, out = self.call(self.good)
        self.assertTrue(out["error"].startswith("Exp-2:"), out["error"])

    def test_only_somebody_who_can_settle_may_do_this(self):
        auth.identity.resolve_by_email = lambda e, channel=None: {
            "email": e, "org_id": "org1", "role": "staff", "name": "Someone"}
        code, _ = self.call(self.good)
        self.assertEqual(403, code)


class TheSubmitterHearsAboutItOnce(unittest.TestCase):

    def setUp(self):
        TheEndpointRefusesBeforeItWrites.setUp(self)

    def tearDown(self):
        TheEndpointRefusesBeforeItWrites.tearDown(self)

    def test_one_email_for_the_whole_payment(self):
        TheEndpointRefusesBeforeItWrites.call(self, self.good)
        emails = [s for s in self.sent if s["only"] == "email"]
        self.assertEqual(1, len(emails))
        self.assertEqual("settled_batch", emails[0]["kind"])
        self.assertEqual("150.00", emails[0]["claim"]["paid"])

    def test_and_one_whatsapp_per_claim(self):
        # Every WhatsApp notice goes as an approved template shaped for one
        # claim, naming one vendor. There is no template for a payment
        # covering several, and bending one of the others into the job would
        # put a total in the field meant for a single bill.
        TheEndpointRefusesBeforeItWrites.call(self, self.good)
        chats = [s for s in self.sent if s["only"] == "whatsapp"]
        self.assertEqual(2, len(chats))
        self.assertEqual(["settled", "settled"], [c["kind"] for c in chats])
        self.assertEqual(["100.00", "50.00"], [c["claim"]["paid"] for c in chats])

    def test_the_email_names_every_claim_it_covers(self):
        # "4 claims" is something to take on trust; the references are what
        # somebody checks against their statement.
        notice = notify.settled_batch_notice({
            "currency": "INR", "paid": "25176.50",
            "claim_refs": ["Exp-29", "Exp-30", "Exp-31", "Exp-32"],
            "mode": "bank transfer", "reference": "UTR9",
            "settled_by": "Madhusudhan", "source": "payout"})
        for ref in ("Exp-29", "Exp-30", "Exp-31", "Exp-32"):
            self.assertIn(ref, notice["text"])
        self.assertIn("INR 25,176.50", notice["subject"])
        self.assertIn("UTR9", notice["text"])

    def test_a_float_batch_does_not_promise_a_transfer(self):
        # No money moves: they spent the company's cash and this accounts for
        # it. Quoting a mode and a bank reference sends them looking for a
        # payment nobody made.
        notice = notify.settled_batch_notice({
            "currency": "INR", "paid": "500.00", "claim_refs": ["Exp-1"],
            "source": "float", "mode": "bank transfer", "reference": "UTR9"})
        self.assertIn("No payment has been made to you", notice["text"])
        self.assertNotIn("UTR9", notice["text"])

    def test_the_channel_split_is_the_callers_to_choose(self):
        send = read("lambda_src/notify.py").split("def send(", 1)[1].split(
            "\ndef ", 1)[0]
        self.assertIn('if email and only in ("", "email"):', send)
        self.assertIn('only in ("", "whatsapp")', send)


class TheConsoleOffersItOnlyWhenItMeansSomething(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_one_payee_or_nothing(self):
        # A transfer goes to somebody. Eight claims across three people is
        # three transfers however it is recorded.
        self.assertIn("const payees = new Set(awaiting.map(c => String(c.sub.who || \"\")));",
                      self.app)
        self.assertIn("awaiting.length > 1 && payees.size === 1 && allReady",
                      self.app)

    def test_and_only_when_every_claim_in_it_can_be_paid(self):
        # The server refuses the whole batch and names one claim, so a button
        # that opens a form doomed to be refused is a button that lies.
        self.assertIn("const allReady = awaiting.every(c => !payBlocker(c.sub));",
                      self.app)

    def test_the_form_lists_the_claims_rather_than_counting_them(self):
        fn = self.app.split("function batchForm(claims, ccy) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("c.sub.reference || c.sub.id", fn)
        self.assertIn("fmt(c.outstanding, ccy)", fn)

    def test_the_button_says_the_figure_it_will_record(self):
        # Two identically labelled buttons a few pixels apart is how a claim
        # got settled by somebody who never saw the form.
        fn = self.app.split("function batchForm(claims, ccy) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("Record ${esc(fmt(owed, ccy))} as paid", fn)

    def test_the_total_it_saw_is_sent_for_checking(self):
        fn = self.app.split("async function settleBatch(claims, ccy) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertIn("paid: (owed / 100).toFixed(2)", fn)

    def test_a_payout_without_a_reference_is_stopped_here_too(self):
        fn = self.app.split("async function settleBatch(claims, ccy) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertIn('source !== "float" && !reference', fn)


class RejectingReadsAsRejecting(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_only_one_reject_button_is_on_screen_at_a_time(self):
        # The row's button says "Reject" and the form's says "Reject claim",
        # so pressing the first produced two red buttons one under the other,
        # both apparently offering the same thing. Only the lower one does
        # anything; the upper one reopens the form that is already open.
        fn = self.app.split("function openReasonForm(sub, kind, action) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertIn('querySelectorAll("button.danger, button.caution")', fn)
        self.assertIn("openers.forEach(b => { b.hidden = true; });", fn)

    def test_and_it_comes_back_if_the_form_is_cancelled(self):
        fn = self.app.split("function openReasonForm(sub, kind, action) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertIn("restore()", fn)

    def test_hidden_rather_than_re_rendered(self):
        # A render rebuilds the actions row and clears the slot, which would
        # take the half-written reason with it.
        fn = self.app.split("function openReasonForm(sub, kind, action) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertNotIn("renderAll()", fn)

    def test_a_refusal_is_not_printed_in_the_same_ink_as_an_approval(self):
        self.assertIn('if (refused) s.classList.add("bad");', self.app)
        self.assertIn('abox.classList.toggle("refused", !!refused);', self.app)
        self.assertIn(".stamp.bad { color:var(--bad); }", self.app)

    def test_and_the_tint_does_not_survive_onto_the_next_claim(self):
        self.assertIn('abox.classList.remove("refused");', self.app)

    def test_finance_can_say_a_claim_came_too_late(self):
        reasons = self.app.split("const SETTLE_REJECT_REASONS = [", 1)[1].split(
            "];", 1)[0]
        self.assertIn("Claimed too long after the spend.", reasons)

    def test_which_is_not_offered_at_review(self):
        # The review reasons are judgments about the claim. This one is about
        # when it arrived.
        reasons = self.app.split("const REJECT_REASONS = [", 1)[1].split("];", 1)[0]
        self.assertNotIn("too long after", reasons)


if __name__ == "__main__":
    unittest.main()


class NothingIsPaidUntilItCanBeFiled(unittest.TestCase):
    """The same two facts the approval gate asks for, asked where money moves.

    Not redundancy. A claim reaches Pending settlement by two routes and only
    one of them passes that gate: the agent clears a claim against the policy
    without any person deciding it. In the ordinary case an agent-cleared
    claim has both, because `no_rule_for_expense_type` and `group_not_set` are
    blocking findings and send the doubtful ones to a reviewer. The gap is
    everything that can change afterwards - a type disabled in the policy, a
    group deleted, a claim approved before either rule existed.

    Asked here because this is the last moment it can be asked. Once a payment
    is recorded the claim is filed under whatever it says, and a payment filed
    under no expense type is a line in the accounts nobody can explain.
    """

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.app = read("../PORTAL/app.html")

    def test_the_rule_is_written_once(self):
        self.assertIn("def _unready_to_pay(", self.auth)

    def test_a_single_settlement_asks_it(self):
        outcome = self.auth.split("def _claim_outcome(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('if kind == "settled":', outcome)
        self.assertIn("_unready_to_pay(item,", outcome)

    def test_a_batch_asks_it_of_every_claim_before_writing_any(self):
        batch = self.auth.split("def _claim_settle_batch(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_unready_to_pay(item, org_for_pay)", batch)
        # Before the loop that writes.
        self.assertLess(batch.index("_unready_to_pay"), batch.index("update_item"))

    def test_a_rejection_is_not_asked_it(self):
        # Refusing a claim files nothing, so demanding it be filable first
        # would stop finance refusing exactly the claims most likely to be
        # missing something.
        outcome = self.auth.split("def _claim_outcome(", 1)[1].split("\ndef ", 1)[0]
        guard = outcome.split("_unready_to_pay", 1)[0]
        self.assertIn('if kind == "settled":', guard.rsplit("\n\n", 1)[-1])

    def test_the_console_withholds_the_button_and_says_why(self):
        # Said as the thing to do rather than as an error: finance holds the
        # expense type on this page and the group is on the form below, so
        # both answers are within reach of whoever reads the line.
        self.assertIn("function payBlocker(sub) {", self.app)
        fn = self.app.split("function renderSettleOnClaim(sub, maySettle) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertIn("const unready = payBlocker(sub);", fn)
        self.assertIn("No expense type is set. Set one above", self.app)
        self.assertIn("No cost centre is set. Set a group above", self.app)
