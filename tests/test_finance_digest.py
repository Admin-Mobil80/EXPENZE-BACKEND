"""Telling finance what came in, without telling them forty times.

A finance executive needs to know receipts are arriving. They do not need one
email per receipt: a team filing a month on a Friday would send forty, the
fortieth is read by nobody, and that makes the first thirty-nine worthless too
- the habit it teaches is to filter the lot, and then the one that mattered is
filtered with them.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")

class FinanceIsToldInBatchesNotPerReceipt(unittest.TestCase):
    """Forty receipts on a Friday is one email, not forty.

    The fortieth is read by nobody, which makes the first thirty-nine
    worthless too - the habit it teaches is to filter the lot, and then the one
    that mattered is filtered with them.
    """

    def setUp(self):
        for name, path in (("digest", "lambda_src/digest.py"),
                           ("notify", "lambda_src/notify.py"),
                           ("stack", "expensifyai/stack.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_one_message_covers_a_window(self):
        # Not a batch size anybody has to choose, and nothing that changes
        # behaviour at a threshold: one receipt in the window is an email about
        # one receipt, twenty is one email listing twenty.
        self.assertIn("def _arrivals(", self.digest)
        self.assertIn("received_at > :a AND received_at <= :b", self.digest)

    def test_the_mark_moves_only_when_an_email_actually_went(self):
        # A duplicate digest is a nuisance; a silently skipped one is a claim
        # nobody looked at.
        body = self.digest.split("def _run_one(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if sent:\n        _stamp(org_id, now_ms)", body)
        self.assertIn("reached nobody", body)

    def test_a_quiet_window_still_moves_the_mark(self):
        # Otherwise a quiet week makes the next digest reach back over all of
        # it in one message.
        body = self.digest.split("def _run_one(", 1)[1].split("\ndef ", 1)[0]
        nothing = body.split("if not rows:", 1)[1].split("return 0", 1)[0]
        self.assertIn("_stamp(org_id, now_ms)", nothing)

    def test_a_first_run_does_not_email_the_whole_history(self):
        self.assertIn("FIRST_RUN_LOOKBACK", self.digest)
        fn = self.digest.split("def _since(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("now_ms - FIRST_RUN_LOOKBACK * 1000", fn)

    def test_only_active_finance_executives_are_told(self):
        fn = self.digest.split("def _finance(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('m.get("role") == "finance"', fn)
        self.assertIn('m.get("status") == "active"', fn)

    def test_one_organisations_failure_is_not_the_others(self):
        body = self.digest.split("def lambda_handler(", 1)[1]
        self.assertIn("except Exception:", body)
        self.assertIn("digest failed for", body)

    def test_the_subject_says_how_many_need_a_person(self):
        # "2 new expense claims" is a count; "1 to review" is the reason to
        # open it now rather than later.
        fn = self.notify.split("def submissions_digest(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("to review", fn)
        self.assertIn('c.get("needs_review")', fn)

    def test_a_claim_still_being_read_is_not_called_reviewable(self):
        # Nobody can act on it yet, and calling for a reviewer on a claim with
        # no figures wastes the trip.
        fn = self.digest.split("def _line(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('unread = status in ("queued", "auditing")', fn)

    def test_it_is_email_only(self):
        # A working list, not an alert - it does not belong on somebody's
        # phone at the weekend.
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
