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
        self.assertIn("const onePayee = payees.size === 1 && awaiting.length > 1;",
                      self.app)

    def test_it_offers_what_can_be_paid_rather_than_nothing(self):
        # It used to require the whole list to be ready, and then simply was
        # not there - no button, no reason. Tolerable while a claim could not
        # be approved without a type and a group; not tolerable once those
        # became finance's to fill in. Three of twenty were ready and the
        # control vanished without saying so.
        self.assertIn("const ready = awaiting.filter(c => !payBlocker(c.sub));",
                      self.app)
        self.assertIn("const held = awaiting.filter(c => payBlocker(c.sub));",
                      self.app)
        self.assertIn("onePayee && ready.length > 1 && can.seeQueue()", self.app)

    def test_the_count_says_both_numbers(self):
        # So "settle together" cannot be read as "settle everything of
        # theirs" when two of seven are being left behind.
        self.assertIn("Settle ${ready.length} of ${awaiting.length} as one payment",
                      self.app)

    def test_what_is_left_out_is_named_on_the_form(self):
        # Finance makes one transfer and ticks a person off. If two of their
        # seven are quietly not in it, the person is short and nothing said so.
        fn = self.app.split("function batchForm(claims, ccy, held) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertIn("Not in this payment:", fn)
        self.assertIn("c.sub.reference || c.sub.id", fn)
        self.assertIn("payBlocker(c.sub)", fn)

    def test_and_the_absence_of_the_button_is_explained(self):
        # A control that is simply absent is indistinguishable from one that
        # was never built.
        self.assertIn('const batchWhy = $("pay-batch-why");', self.app)
        for line in ("Filter by submitter to",
                     "Only one of these can be paid yet",
                     "None of these can be paid yet"):
            self.assertIn(line, self.app)

    def test_the_form_lists_the_claims_rather_than_counting_them(self):
        fn = self.app.split("function batchForm(claims, ccy, held) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("c.sub.reference || c.sub.id", fn)
        self.assertIn("fmt(c.outstanding, ccy)", fn)

    def test_the_button_says_the_figure_it_will_record(self):
        # Two identically labelled buttons a few pixels apart is how a claim
        # got settled by somebody who never saw the form.
        fn = self.app.split("function batchForm(claims, ccy, held) {", 1)[1].split(
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


class TheFloatIsOnlyOfferedToSomebodyWhoHoldsOne(unittest.TestCase):
    """The batch form offered "Their float" to everybody.

    The single settlement form has always shown that choice only to a person
    who actually holds an advance - for everybody else there is no choice to
    make, and a dropdown with one real option is furniture that can be got
    wrong. The batch form, added later, did not follow it: four claims could
    have been drawn down against a float that does not exist.
    """

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function batchForm(claims, ccy, held) {", 1)[1] \
                          .split("\n}", 1)[0]

    def test_it_asks_whether_they_hold_one(self):
        self.assertIn("const holdsFloat = !!(ADVANCES.holders || []).find(", self.fn)

    def test_the_option_is_not_drawn_otherwise(self):
        self.assertIn('(holdsFloat ? `<option value="float">Their float</option>` : "")',
                      self.fn)

    def test_nor_is_the_row_it_sits_in(self):
        self.assertIn('`<div class="sf"${holdsFloat ? "" : " hidden"}>', self.fn)

    def test_a_payout_is_the_first_option_either_way(self):
        # So the default is right for the common case without anybody
        # choosing, and reading the select before anything reveals it cannot
        # say "float" - which is the bug the single form documents.
        opts = self.fn.split('<select id="bf-src">', 1)[1].split("</select>", 1)[0]
        self.assertLess(opts.index('value="payout"'), opts.find('value="float"')
                        if 'value="float"' in opts else len(opts))

    def test_the_single_form_still_does_the_same(self):
        # Both readers check the wrapper is visible, not just the value.
        single = self.app.split("function settleForm(", 1)[1].split("\n}", 1)[0]
        self.assertIn("srcWrap0 && !srcWrap0.hidden", single)
        self.assertIn("srcWrap1 && !srcWrap1.hidden", single)


class ElevenNoticesGoOutAtOnce(unittest.TestCase):
    """Eleven claims took four seconds, and API Gateway cuts a request at 29.

    Each notice is an SES call and a WhatsApp call, sent one after another.
    The writes were already done by the time the notices started, so a batch
    that outran the clock would have left every claim approved and the
    reviewer looking at an error - the worst shape a failure can take, because
    there is nothing on screen to say which half happened.
    """

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.fn = self.auth.split("def _tell_everybody(", 1)[1].split("\ndef ", 1)[0]

    def test_they_are_sent_together(self):
        self.assertIn("futures.ThreadPoolExecutor", self.fn)
        self.assertIn("from concurrent import futures", self.auth)

    def test_with_a_ceiling_on_how_many_at_once(self):
        self.assertIn("max_workers=min(8, len(jobs))", self.fn)

    def test_one_failed_send_does_not_fail_the_batch(self):
        # The claim is approved either way, and `notify.record` writes what
        # actually went out - so a message that did not arrive is visible on
        # the claim rather than inferred from an exception nobody saw.
        self.assertIn("except Exception:", self.fn)
        self.assertIn("could not tell %s about %s", self.fn)

    def test_the_writes_happen_before_any_of_them(self):
        batch = self.auth.split("def _claim_review_batch(", 1)[1].split("\ndef ", 1)[0]
        self.assertLess(batch.index("update_item"), batch.index("_tell_everybody("))


class TheButtonIsReleasedWhenTheWorkIsDone(unittest.TestCase):
    """It said "Approving…" through the reload that follows, which is another
    second or two, with every row still ticked. Eleven claims read as stuck."""

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("async function approvePicked() {", 1)[1] \
                          .split("\n}", 1)[0]

    def test_it_repaints_as_soon_as_the_server_answers(self):
        head = self.fn.split("if (!ok) {", 1)[0]
        self.assertIn("approving = false;", head)
        self.assertIn("paintApproveButton();", head)

    def test_and_says_what_it_is_doing_during_the_reload(self):
        self.assertIn("approved. Refreshing", self.fn)

    def test_a_network_failure_does_not_claim_nothing_was_recorded(self):
        # The writes may well have happened; the answer just did not arrive.
        # "Nothing was recorded" would be a guess, and the wrong one.
        self.assertIn("it is not clear what was recorded", self.fn)


class SettingAFieldOnAStackAtOnce(unittest.TestCase):
    """Eighteen of twenty claims were missing an expense type or a group.

    Both are facts about what a receipt *is*, and somebody working a stack of
    a colleague's WhatsApp receipts knows the answer for all of them at once -
    the same cost centre, frequently the same type. Opening eighteen pages to
    set two dropdowns is how a queue stops getting worked, and it is the
    reason the approval gate had to come out.

    Nothing here is a decision. It writes down what the agent could not work
    out; the claims stay exactly where they are, and approving or rejecting
    them is still done one at a time by somebody who has read them.
    """

    def setUp(self):
        TheEndpointRefusesBeforeItWrites.setUp(self)
        # A reviewer, not a finance executive: these claims are in the review
        # queue, which is where this control lives. The finance case gets its
        # own test below.
        auth.identity.resolve_by_email = lambda e, channel=None: {
            "email": e, "org_id": "org1", "role": "admin", "name": "Rana"}
        self.good = {"submission_ids": ["a", "b"], "expense_type": "meals"}

    def tearDown(self):
        TheEndpointRefusesBeforeItWrites.tearDown(self)

    def call(self, body):
        reply = auth._claim_retype_batch("tok", body, None)
        return reply["statusCode"], json.loads(reply["body"])

    def test_it_sets_the_type_on_every_claim(self):
        code, out = self.call(self.good)
        self.assertEqual(200, code)
        self.assertEqual(2, out["count"])
        for write in self.table.writes:
            self.assertEqual("meals",
                             write["ExpressionAttributeValues"][":t"])

    def test_it_sets_the_group_and_closes_the_question(self):
        # "set_by_reviewer" stops the auditor consulting the bill again: a
        # person's answer is final in a way an inference is not.
        code, _ = self.call({"submission_ids": ["a"], "group_id": "mobil80"})
        self.assertEqual(200, code)
        values = self.table.writes[0]["ExpressionAttributeValues"]
        self.assertEqual("mobil80", values[":g"])
        self.assertEqual("set_by_reviewer", values[":gs"])

    def test_clearing_a_group_is_a_change_and_not_a_no_op(self):
        # A reviewer removing a tag the agent got wrong is an answer.
        code, _ = self.call({"submission_ids": ["a"], "group_id": ""})
        self.assertEqual(200, code)
        self.assertEqual("unset",
                         self.table.writes[0]["ExpressionAttributeValues"][":gs"])

    def test_an_invented_type_is_refused(self):
        code, out = self.call({**self.good, "expense_type": "made_up"})
        self.assertEqual(400, code)
        self.assertIn("configured expense types", out["error"])
        self.assertEqual([], self.table.writes)

    def test_an_invented_group_is_refused(self):
        code, out = self.call({"submission_ids": ["a"], "group_id": "nope"})
        self.assertEqual(400, code)
        self.assertIn("configured groups", out["error"])
        self.assertEqual([], self.table.writes)

    def test_asking_for_nothing_is_refused(self):
        code, out = self.call({"submission_ids": ["a"]})
        self.assertEqual(400, code)
        self.assertIn("pick an expense type or a group", out["error"])

    def test_a_settled_claim_is_closed_to_it(self):
        self.rows["b"]["outcome"] = "settled"
        code, out = self.call(self.good)
        self.assertEqual(409, code)
        self.assertIn("Exp-2", out["error"])
        self.assertEqual([], self.table.writes)

    def test_nothing_is_written_until_every_claim_has_been_checked(self):
        # A batch that stops half way leaves a stack somebody has to go
        # through claim by claim to find out what happened.
        self.rows["b"]["outcome"] = "settled"
        self.call(self.good)
        self.assertEqual([], self.table.writes)

    def test_a_claim_from_another_organisation_is_not_found(self):
        self.rows["a"]["org_id"] = "someone-else"
        code, _ = self.call(self.good)
        self.assertEqual(404, code)

    def test_a_submitter_cannot_do_it(self):
        auth.identity.resolve_by_email = lambda e, channel=None: {
            "email": e, "org_id": "org1", "role": "staff", "name": "Someone"}
        code, _ = self.call(self.good)
        self.assertEqual(403, code)

    def test_finance_may_only_touch_a_claim_that_has_cleared(self):
        # The same split as setting one: finance corrects the filing on a
        # claim that has cleared, and a claim still under review belongs to
        # the reviewer until they release it.
        auth.identity.resolve_by_email = lambda e, channel=None: {
            "email": e, "org_id": "org1", "role": "finance", "name": "Madhu"}
        self.rows["a"].pop("review_action", None)
        self.rows["a"]["verdict"] = {"verdict": "needs_review",
                                     "violations": [{"blocks_automatic_decision": True}]}
        code, out = self.call({"submission_ids": ["a"], "expense_type": "meals"})
        self.assertEqual(403, code)
        self.assertIn("still under review", out["error"])


class TheConsoleOffersItOnASelection(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function paintBulkFields() {", 1)[1].split(
            "\n}", 1)[0]

    def test_only_for_more_than_one_claim(self):
        # Setting a field on a single claim is what the claim page is for,
        # and it shows the whole bill while you do it.
        self.assertIn("const show = can.review() && chosen.length > 1;", self.fn)

    def test_the_lists_are_rebuilt_rather_than_cached(self):
        # Both are editable on other screens, so a list built at load goes
        # stale the moment somebody adds a type - which is exactly when a
        # reviewer would reach for it.
        self.assertIn("rules.types.filter(t => t.enabled)", self.fn)
        self.assertIn("(ORG_PROFILE.groups || [])", self.fn)

    def test_a_disabled_type_is_not_offered(self):
        self.assertIn("filter(t => t.enabled)", self.fn)

    def test_the_group_setter_is_absent_where_there_are_no_groups(self):
        self.assertIn('groupSel.hidden = !show || !(ORG_PROFILE.groups || []).length;',
                      self.fn)

    def test_it_shows_no_current_value(self):
        # Twelve claims may carry twelve different types, and a dropdown
        # showing one of them would be lying about the other eleven.
        self.assertIn("Set expense type", self.fn)
        self.assertIn('typeSel.value = "";', self.fn)
        self.assertIn("Set group", self.fn)

    def test_the_selection_survives_setting_a_field(self):
        # Setting the type on twelve and then the group on the same twelve is
        # the ordinary way this gets used.
        fn = self.app.split("async function setOnPicked(field, value) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertNotIn("picked.clear()", fn)
