"""Telling finance what is ready to pay, and only that.

This emailed them every receipt that arrived. A receipt arriving is not
finance's business: it may still be with the agent, it may be about to be
refused, and in every case somebody else decides before there is anything to
pay. What they were given was a stream they could not act on, and a stream
nobody can act on is one they learn to filter - which takes the message that
mattered with it.

A claim clearing for settlement is the moment the work becomes theirs, so that
is the moment this fires. Still in batches: a team whose month is approved in
one sitting would otherwise send forty emails, the fortieth is read by nobody,
and that makes the first thirty-nine worthless too.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lambda_src"))
os.environ.setdefault("INTAKE_TABLE", "t")
os.environ.setdefault("ORGS_TABLE", "t")
os.environ.setdefault("USERS_TABLE", "t")
os.environ.setdefault("OTP_SENDER", "no-reply@expenze.ai")
os.environ.setdefault("INTAKE_ADDRESS", "receipts@expenze.ai")


class FinanceIsToldWhenThereIsSomethingToPay(unittest.TestCase):

    def setUp(self):
        for name, path in (("digest", "lambda_src/digest.py"),
                           ("notify", "lambda_src/notify.py"),
                           ("stack", "expensifyai/stack.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_a_receipt_arriving_is_not_an_event_finance_hears_about(self):
        # They cannot act on it: it may still be with the agent, and somebody
        # else decides before there is anything to pay.
        self.assertNotIn("def _arrivals(", self.digest)
        self.assertNotIn("received_at > :a AND received_at <= :b", self.digest)
        self.assertNotIn("def submissions_digest(", self.notify)

    def test_it_fires_when_a_claim_clears_for_settlement(self):
        self.assertIn("def _cleared_at(", self.digest)
        self.assertIn("def ready_to_pay_digest(", self.notify)

    def test_whether_a_claim_is_waiting_is_asked_in_one_place(self):
        # Three things ask it and they were drifting: the console builds
        # Pending settlement from it, this decides who finance hears about, and
        # `_claim_review` uses it to tell a rejection at settlement from one at
        # review. Three implementations of one sentence is how a tab, an email
        # and a permission come to disagree about the same claim.
        with open(os.path.join(ROOT, "lambda_src", "policy.py"),
                  encoding="utf-8") as h:
            pol = h.read()
        self.assertIn("def awaiting_payment(", pol)
        fn = self.digest.split("def _cleared_at(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if not policy.awaiting_payment(row):", fn)
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            self.assertIn("policy.awaiting_payment(item)", h.read())

    def test_both_ways_a_claim_can_clear_are_counted(self):
        # A person approved it, or the agent cleared it against the policy and
        # nobody had to. Both put money on finance's desk.
        import policy
        approved = {"review_action": "approved", "verdict": {}}
        self.assertTrue(policy.awaiting_payment(approved))
        released = {"verdict": {"verdict": "approved", "violations": []}}
        self.assertTrue(policy.awaiting_payment(released))

    def test_and_the_ones_that_are_not_finance_s_problem_are_left_out(self):
        # A second document of a claim is not a second thing to pay; a settled
        # one is not waiting; a rejected one is the opposite outcome.
        import policy
        base = {"review_action": "approved", "verdict": {}}
        self.assertFalse(policy.awaiting_payment({**base, "companion_of": "Exp-47"}))
        self.assertFalse(policy.awaiting_payment({**base, "outcome": "settled"}))
        self.assertFalse(policy.awaiting_payment({**base, "review_action": "rejected"}))
        self.assertFalse(policy.awaiting_payment(
            {"verdict": {"verdict": "approved",
                         "violations": [{"blocks_automatic_decision": True}]}}))

    def test_when_it_became_finance_s_is_this_module_s_own_question(self):
        # A claim a person approved has been finance's since they approved it;
        # one the agent released has been since it was read. Two stamps, and
        # the shared rule deliberately says nothing about either.
        fn = self.digest.split("def _cleared_at(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('row.get("review_at")', fn)
        self.assertIn('row.get("audited_at")', fn)

    def test_the_two_timestamp_units_on_one_row_are_normalised(self):
        # `received_at` is milliseconds and `review_at` and `audited_at` are
        # seconds. Mixing them has produced a confident wrong answer here
        # before, and a window comparison is exactly where it would happen.
        self.assertIn("def _ms(", self.digest)
        fn = self.digest.split("def _ms(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("10 ** 11", fn)
        import digest
        self.assertEqual(digest._ms(1790000237), 1790000237000)   # seconds
        self.assertEqual(digest._ms(1789986038604), 1789986038604)  # already ms
        self.assertEqual(digest._ms(None), 0)
        self.assertEqual(digest._ms("not a number"), 0)

    def test_one_message_covers_a_window(self):
        # Not a batch size anybody has to choose, and nothing that changes
        # behaviour at a threshold: one claim in the window is an email about
        # one claim, twenty is one email listing twenty.
        body = self.digest.split("def _run_one(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if since < at <= now_ms:", body)

    def test_the_mark_moves_only_when_an_email_actually_went(self):
        # A duplicate digest is a nuisance; a silently skipped one is money
        # nobody paid.
        body = self.digest.split("def _run_one(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if sent:\n        _stamp(org_id, now_ms)", body)
        self.assertIn("reached nobody", body)

    def test_a_quiet_window_still_moves_the_mark(self):
        # Otherwise a quiet week makes the next digest reach back over all of
        # it in one message.
        body = self.digest.split("def _run_one(", 1)[1].split("\ndef ", 1)[0]
        nothing = body.split("if not fresh:", 1)[1].split("return 0", 1)[0]
        self.assertIn("_stamp(org_id, now_ms)", nothing)

    def test_a_first_run_does_not_email_the_whole_history(self):
        self.assertIn("FIRST_RUN_LOOKBACK", self.digest)
        fn = self.digest.split("def _since(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("now_ms - FIRST_RUN_LOOKBACK * 1000", fn)

    def test_the_standing_total_travels_with_the_new_ones(self):
        # The question finance opens this to answer is "how much do we owe",
        # not "how much more than last time".
        body = self.digest.split("def _run_one(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("outstanding = [_line(r) for r in waiting]", body)
        self.assertIn("waiting to be ", self.notify)

    def test_only_active_finance_executives_are_told(self):
        fn = self.digest.split("def _finance(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('m.get("role") == "finance"', fn)
        self.assertIn('m.get("status") == "active"', fn)

    def test_but_an_organisation_with_none_is_not_left_unpaid(self):
        # The console lets an owner settle for exactly this reason. Without a
        # fallback the smallest accounts - the ones most likely to be one
        # person - would be the only ones this never reaches.
        fn = self.digest.split("def _finance(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('m.get("role") == "owner"', fn)
        self.assertIn("if finance:", fn)

    def test_one_organisations_failure_is_not_the_others(self):
        body = self.digest.split("def lambda_handler(", 1)[1]
        self.assertIn("except Exception:", body)
        self.assertIn("digest failed for", body)

    def test_it_is_email_only(self):
        # Money waiting to be paid is not a reason to buzz somebody's phone on
        # a Saturday. It is a list they work through when they sit down to it.
        fn = self.notify.split("def email_digest(", 1)[1]
        self.assertIn("_send_email(", fn)
        self.assertNotIn("_send_whatsapp", fn)

    def test_it_runs_on_a_clock_and_the_clock_is_wired(self):
        self.assertIn('function_name="expenze-digest"', self.stack)
        self.assertIn('handler="digest.lambda_handler"', self.stack)
        self.assertIn("events.Schedule.rate(Duration.minutes(15))", self.stack)
        self.assertIn("events_targets.LambdaFunction(digest_fn)", self.stack)

    def test_it_can_only_move_the_mark(self):
        # It reads receipts and people; the one thing it writes is the
        # high-water mark on the organisation.
        self.assertIn("intake_table.grant_read_data(digest_fn)", self.stack)
        self.assertIn("users_table.grant_read_data(digest_fn)", self.stack)
        self.assertIn("orgs_table.grant_read_write_data(digest_fn)", self.stack)


class TheFigureIsTheOneTheTabShows(unittest.TestCase):
    """An email saying three are waiting over a badge saying two is worse than
    no email at all. `_cleared_at` follows `payableClaims` line for line."""

    def setUp(self):
        import digest, notify
        self.digest, self.notify = digest, notify

    def _row(self, **over):
        row = {
            "reference": "Mobil80-Exp-48", "submitted_by": "rehaan@mobil80.com",
            "receipt": {"vendor": "Cursor"},
            "review_action": "approved", "review_by_name": "Riyad Rasheed",
            "review_at": 1790000237, "audited_at": 1789986051,
            "approved_total": "23.60",
            "payout_value": {"amount": "2266.47", "currency": "INR",
                             "rate": "96.03698000", "from": "USD"},
            "verdict": {"currency": "USD", "receipt_total": "23.60",
                        "reimbursable_total": "23.60", "verdict": "needs_review",
                        "violations": []},
        }
        row.update(over)
        return row

    def test_an_approved_claim_is_waiting_from_the_moment_it_was_approved(self):
        self.assertEqual(self.digest._cleared_at(self._row()), 1790000237000)

    def test_a_settled_one_is_not_waiting(self):
        self.assertEqual(self.digest._cleared_at(self._row(outcome="settled")), 0)

    def test_a_rejected_one_is_not_waiting(self):
        self.assertEqual(self.digest._cleared_at(self._row(review_action="rejected")), 0)

    def test_a_companion_document_is_not_a_second_payment(self):
        self.assertEqual(
            self.digest._cleared_at(self._row(companion_of="Mobil80-Exp-47")), 0)

    def test_an_agent_cleared_claim_is_waiting_from_when_it_was_audited(self):
        row = self._row(review_action="", verdict={
            "currency": "INR", "reimbursable_total": "1999.00",
            "verdict": "approved", "violations": []})
        self.assertEqual(self.digest._cleared_at(row), 1789986051000)

    def test_but_not_one_the_agent_could_not_release(self):
        row = self._row(review_action="", verdict={
            "currency": "INR", "reimbursable_total": "1999.00",
            "verdict": "approved",
            "violations": [{"blocks_automatic_decision": True}]})
        self.assertEqual(self.digest._cleared_at(row), 0)

    def test_the_figure_is_what_leaves_the_account(self):
        # USD 23.60 at the rate stamped on the claim, not today's.
        self.assertEqual(self.digest._owed(self._row()), ("2266.47", "INR"))

    def test_a_rupee_claim_needs_no_conversion(self):
        row = self._row(approved_total="1999.00", verdict={
            "currency": "INR", "reimbursable_total": "1999.00",
            "verdict": "approved", "violations": []},
            payout_value={"currency": "INR"})
        self.assertEqual(self.digest._owed(row), ("1999.00", "INR"))

    def test_what_a_reviewer_released_beats_what_the_engine_allowed(self):
        # A claim approved over a cap pays what the person approved. Reading
        # the engine's figure would report nil for every overridden claim.
        row = self._row(approved_total="23.60", verdict={
            "currency": "USD", "reimbursable_total": "0.00",
            "verdict": "needs_review", "violations": []})
        self.assertEqual(self.digest._owed(row)[0], "2266.47")

    def test_a_claim_with_no_rate_reports_no_figure_rather_than_nil(self):
        row = self._row(payout_value={"currency": "INR", "rate": "0"})
        self.assertEqual(self.digest._owed(row), ("", "USD"))

    def test_and_the_total_says_it_skipped_one(self):
        lines = [{"total": "100.00", "currency": "INR"},
                 {"total": "", "currency": "USD"}]
        self.assertEqual(self.notify._total_of(lines), "INR 100.00 (and 1 with no rate)")

    def test_a_total_across_two_currencies_is_not_offered(self):
        # It would be a number nobody could reconcile against anything.
        lines = [{"total": "100.00", "currency": "INR"},
                 {"total": "20.00", "currency": "USD"}]
        self.assertEqual(self.notify._total_of(lines), "")

    def test_the_email_names_who_released_each_claim(self):
        # A claim the agent cleared went out on the policy alone; one a person
        # approved has a name against the judgment.
        line = self.digest._line(self._row())
        self.assertEqual(line["cleared_by"], "Riyad Rasheed")
        agent = self.digest._line(self._row(review_action=""))
        self.assertEqual(agent["cleared_by"], "Expenze agent")

    def test_the_email_reads_as_a_working_list(self):
        note = self.notify.ready_to_pay_digest(
            [self.digest._line(self._row())], "Mobil80 Solutions")
        self.assertIn("1 claim ready to pay", note["subject"])
        self.assertIn("Mobil80-Exp-48", note["text"])
        self.assertIn("INR 2266.47", note["text"])
        self.assertIn("cleared by Riyad Rasheed", note["text"])
        self.assertIn("https://expenze.ai", note["text"])


class ADuplicateNamesAClaimSomebodyCanFind(unittest.TestCase):
    """It quoted the raw submission id, which is the one string nobody can use.

    `sub_1789477019786_595007` is an identifier. The whole reference scheme
    exists because nobody reads one down a phone or types one into a search
    box - and a reviewer told their claim matches one of those has nothing to
    go and look for, so they reasonably conclude there is no other claim and
    that the agent is wrong.

    Who sent it matters as much as which one it is. "The same bill twice" and
    "two people expensed one invoice" are different problems with different
    answers, and the second is invisible unless the other person is named.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "auditor_worker.py"),
                  encoding="utf-8") as h:
            self.worker = h.read()

    def test_the_finding_names_the_reference_and_the_sender(self):
        block = self.worker.split("held = duplicates.claim(", 1)[1] \
                           .split("blocks_automatic_decision", 1)[0]
        self.assertIn("_describe(held)", block)
        fn = self.worker.split("def _describe(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('row.get("reference")', fn)
        self.assertIn('row.get("submitted_by")', fn)

    def test_the_stored_pointer_is_the_reference_too(self):
        # It is what the console prints on the claim, so it has to be the
        # string somebody can look up.
        self.assertIn('verdict["duplicate_of"] = _reference_of(held) or held', self.worker)

    def test_a_claim_from_before_references_falls_back_honestly(self):
        # There is no other string for those, and inventing one would point at
        # nothing.
        fn = self.worker.split("def _describe(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('or submission_id', fn)

    def test_reading_the_other_claim_cannot_break_this_one(self):
        # The duplicate finding is worth having even if the lookup fails; the
        # audit is not worth losing over it.
        fn = self.worker.split("def _other_claim(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("except Exception:", fn)
        self.assertIn("return {}", fn)


class TheInvoiceNumberIsTheOnlySignalThatIsNotAGuess(unittest.TestCase):
    """A vendor does not issue two different invoices under one number.

    Everything else here is inference. "Same vendor, same day, same amount" was
    the best available while no invoice number was read off the bill - and it
    was wrong in the commonest case in business: two colleagues each holding a
    seat of the same SaaS product, billed on the same day for the same price.
    It called them duplicates of each other every month.

    A control that cries wolf on every subscription renewal is one people learn
    to dismiss, and then it is worth nothing on the day it is right.
    """

    def setUp(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "lambda_src"))
        import duplicates
        self.d = duplicates
        for name, path in (("worker", "lambda_src/auditor_worker.py"),
                           ("handler", "lambda_src/handler.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_the_model_is_asked_for_it(self):
        self.assertIn('"invoice_number"', self.handler)
        self.assertIn("'Invoice No', 'Bill No', 'Receipt No'", self.handler)
        # And told not to invent one, because most small bills carry none.
        self.assertIn("do not invent one", self.handler)

    def test_one_number_is_one_bill_however_the_shop_is_spelled(self):
        self.assertEqual(
            self.d.invoice_key("org", "Cursor", "INV-2026-0041"),
            self.d.invoice_key("org", "Cursor (Anysphere Inc.)", "inv 2026 0041"))

    def test_two_seats_of_one_subscription_are_not_duplicates(self):
        # Exactly the pair the old rule flagged: same vendor, same day, same
        # price, two people, two invoices.
        self.assertNotEqual(self.d.invoice_key("org", "Cursor", "INV-1001"),
                            self.d.invoice_key("org", "Cursor", "INV-1002"))

    def test_a_bill_with_no_number_is_left_to_the_sender_rule(self):
        # Normal for a handwritten bill, and `submitter_key` needs nothing
        # printed on the paper at all.
        self.assertEqual("", self.d.invoice_key("org", "Cafe", ""))
        self.assertTrue(self.d.submitter_key("org", "a@x.com", "2026-09-01", "460.00", "INR"))

    def test_a_number_with_no_digits_identifies_nothing(self):
        for junk in ("INVOICE", "-", "n/a"):
            self.assertEqual("", self.d.invoice_key("org", "Cafe", junk), junk)

    def test_the_old_guessing_rule_is_gone(self):
        self.assertFalse(hasattr(self.d, "receipt_key"))
        # Narrowly: `receipt_key` also names the S3 object key throughout this
        # codebase, which is a different thing entirely.
        self.assertNotIn("duplicates.receipt_key", self.worker)


class TheSturdiestFingerprintIsTheSenderNotTheVendor(unittest.TestCase):
    """Who sent it, what the bill is dated, and how much.

    Every part is a fact we hold rather than a string a model produced: the
    membership resolved at intake, the date off the bill, arithmetic. None of
    it varies between two readings of one receipt - which is exactly how the
    vendor-based fingerprint missed real duplicates when the same Cursor
    invoice came back as "Cursor", "Cursor Cursor" and "Cursor (Anysphere
    Inc.)".

    It catches the case that actually happens: somebody photographs a bill on
    WhatsApp and then forwards the emailed copy. Both arrive from the same
    person, for the same money, on the same bill date, whatever either reading
    called the shop.
    """

    def setUp(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "lambda_src"))
        import duplicates
        self.d = duplicates
        with open(os.path.join(ROOT, "lambda_src", "auditor_worker.py"),
                  encoding="utf-8") as h:
            self.worker = h.read()

    def test_one_person_one_bill_two_channels_is_one_fingerprint(self):
        self.assertEqual(
            self.d.submitter_key("org", "rijo@mobil80.com", "2026-08-27", "23.60", "USD"),
            self.d.submitter_key("org", "RIJO@Mobil80.com ", "2026-08-27", "23.6", "USD"))

    def test_it_keys_on_the_date_printed_on_the_bill(self):
        # Not the date it was sent in. The same receipt forwarded a week later
        # has a different arrival date and the same bill date, and it is the
        # bill date that makes it the same receipt.
        block = self.worker.split("sender_fp = duplicates.submitter_key(", 1)[1] \
                           .split("fingerprint = duplicates.receipt_key(", 1)[0]
        self.assertIn('receipt.get("date", "")', block)
        self.assertNotIn("received_at", block)

    def test_a_bill_with_no_date_gets_no_fingerprint(self):
        # Rather than colliding every undated receipt that person ever sent.
        self.assertEqual("", self.d.submitter_key("org", "r@x.com", "", "10.00", "INR"))
        self.assertEqual("", self.d.submitter_key("org", "", "2026-08-27", "10.00", "INR"))

    def test_different_days_are_different_receipts(self):
        self.assertNotEqual(
            self.d.submitter_key("org", "r@x.com", "2026-08-27", "10.00", "INR"),
            self.d.submitter_key("org", "r@x.com", "2026-08-28", "10.00", "INR"))

    def test_both_fingerprints_are_claimed(self):
        # The sender one cannot see two people claiming one invoice, because
        # the submitters differ; the vendor one can. Neither catches both.
        self.assertIn("held = (duplicates.claim(sender_fp, submission_id) if sender_fp else None)",
                      self.worker)
        self.assertIn("if not held and fingerprint:", self.worker)

    def test_the_sender_one_is_tried_first(self):
        # It is the one that does not quietly stop working when a vendor name
        # comes back slightly different.
        self.assertLess(self.worker.index("sender_fp = duplicates.submitter_key("),
                        self.worker.index("fingerprint = duplicates.invoice_key("))

    def test_two_people_one_invoice_is_still_caught(self):
        # Not by this rule - the submitters differ, so it cannot see them - but
        # by the invoice number, which is why that one is kept alongside.
        n = self.d.submitter_key("org", "neeta@x.com", "2026-08-16", "20.00", "USD")
        m = self.d.submitter_key("org", "manoj@x.com", "2026-08-16", "20.00", "USD")
        self.assertNotEqual(n, m)
        self.assertEqual(self.d.invoice_key("org", "Cursor", "INV-7"),
                         self.d.invoice_key("org", "Cursor (Anysphere)", "inv 7"))

    def test_two_people_two_invoices_are_not(self):
        # Which is the pair that was wrongly flagged: one seat each.
        self.assertNotEqual(self.d.invoice_key("org", "Cursor", "INV-7"),
                            self.d.invoice_key("org", "Cursor", "INV-8"))


class ATimedOutAuditDoesNotStrandTheReceipt(unittest.TestCase):
    """A Lambda that times out is killed where it stands.

    No exception is raised, no `finally` runs, and nothing releases the claim.
    The row is left in `auditing` and stays there: no retry can take it,
    because the guard only accepts `queued`. The receipt reads "Being read..."
    for ever and nobody is ever told why.

    That is not hypothetical - a 23-line handwritten grocery bill did exactly
    this the moment the extraction schema grew enough to push it past two
    minutes.
    """

    def setUp(self):
        for name, path in (("worker", "lambda_src/auditor_worker.py"),
                           ("stack", "expensifyai/stack.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())
        self.fn = self.worker.split("def _claim(", 1)[1].split("\ndef ", 1)[0]

    def test_a_claim_that_outlived_any_possible_run_can_be_taken_again(self):
        self.assertIn("audit_started_at < :stale", self.fn)
        self.assertIn('":stale": now - STALE_AUDIT_SECONDS', self.fn)

    def test_the_window_is_longer_than_the_function_can_run(self):
        # Otherwise a slow audit is stolen from itself half way through, and
        # the receipt is read twice - two model passes, two credits' worth of
        # work, and a race over which verdict lands.
        window = self.worker.split("STALE_AUDIT_SECONDS = ", 1)[1].split("\n", 1)[0]
        seconds = eval(window, {"__builtins__": {}})
        timeout = self.stack.split("timeout=Duration.seconds(", 1)
        # The auditor's own timeout, read from the block that defines it.
        block = self.stack.split('handler="auditor_worker.lambda_handler"', 1)[1]
        runs_for = int(block.split("timeout=Duration.seconds(", 1)[1].split(")", 1)[0])
        self.assertGreater(seconds, runs_for * 2)

    def test_a_row_claimed_before_this_existed_is_still_reclaimable(self):
        # Anything stranded by the old code carries no `audit_started_at`, and
        # would otherwise be unreachable for ever.
        self.assertIn("attribute_not_exists(audit_started_at)", self.fn)

    def test_a_queued_receipt_is_still_the_ordinary_path(self):
        self.assertIn('"#s = :queued"', self.fn)

    def test_the_claim_stamps_when_it_started(self):
        self.assertIn("SET #s = :auditing, audit_started_at = :now", self.fn)

    def test_the_auditor_has_room_for_a_long_bill(self):
        block = self.stack.split('handler="auditor_worker.lambda_handler"', 1)[1]
        self.assertIn("timeout=Duration.seconds(300)", block.split("\n        )", 1)[0])
