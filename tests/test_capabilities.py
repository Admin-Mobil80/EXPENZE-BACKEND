"""The read surface a voice channel asks, where nothing can be checked.

Every other reader of this data shows its working. Pending settlement lists
the rows it totals; the triage report prints the three counts its percentage
is made of; the finance digest names every claim in its sum. Spoken, a figure
arrives on its own - so the whole burden of being right falls on going through
the same predicates the screen goes through, and these tests exist to hold
that.

The three bugs this module is built to not repeat, all of them from the last
fortnight and all of them found only because two numbers were visible at once:

* the auto-clear rate read **69%** in one tile and **20%** in another about the
  same seventy-one receipts, because three readers asked it three ways;
* Pending settlement and Reports disagreed by **Rs 8,819.23**, because one of
  them counted a companion document as a claim;
* five approved claims worth **Rs 9,004.63** were invisible to finance,
  because one caller read `approved_total` and another read the verdict.

There is also a transposition waiting in the field names, and it is the kind
that produces a confident wrong answer rather than an error: `withdrawn` is a
**review action**, taken by the submitter before anybody judged anything,
while `outcome` is only ever `settled` or `rejected`. Reading a withdrawal off
`outcome` finds nothing and reports the claim as still in review - a claim
nobody is waiting on, in the number that says who is waiting.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
# `digest` is imported below to prove it reads the same money function this
# module does, and it reaches for these at import time.
os.environ.setdefault("INTAKE_TABLE", "t")
os.environ.setdefault("ORGS_TABLE", "t")
os.environ.setdefault("USERS_TABLE", "t")
os.environ.setdefault("OTP_SENDER", "no-reply@expenze.ai")
os.environ.setdefault("INTAKE_ADDRESS", "receipts@expenze.ai")

ROOT = os.path.join(os.path.dirname(__file__), "..")

import capabilities  # noqa: E402
import policy  # noqa: E402

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


def at(day: str) -> int:
    """That calendar day as a `received_at`, which is in milliseconds."""
    parts = [int(p) for p in day.split("-")]
    stamp = datetime(*parts, 12, 0, tzinfo=timezone.utc)
    return int(stamp.timestamp() * 1000)


def row(**over):
    """A claim the agent read and cleared, worth 1,000 rupees.

    Shaped like a real row rather than like the arguments the code wants.
    Five bugs survived a green suite because the fixtures were invented: the
    expense type is inside the verdict and not on the row, the printed date is
    inside `receipt`, and `payout_value` is `{}` on plenty of live claims.
    Every one of those read as a plausible figure rather than as an error.
    """
    base = {
        "org_id": "org-1",
        "submission_id": "s-1",
        "reference": "Mobil80-Exp-1",
        "received_at": at("2026-10-05"),
        "submitted_by": "riyad@mobil80.com",
        "vendor": "Indigo",
        "receipt": {"date": "2026-10-03", "vendor": "Indigo"},
        "verdict": {"verdict": "approved", "violations": [],
                    "reimbursable_total": "1000.00",
                    "receipt_total": "1000.00", "currency": "INR",
                    "expense_type": "travel",
                    "expense_type_label": "Travel"},
        "payout_value": {"currency": "INR", "rate": "1"},
        "budget_value": {"amount": "1000.00", "currency": "INR"},
    }
    base.update(over)
    return base


def blocked(**over):
    """A claim still waiting for a person: the agent would not release it.

    Still carries an expense type. Being held for review is usually about the
    receipt or a cap rather than about not knowing what kind of expense it
    is, and leaving the type off here would quietly file it under "Not set"
    in every test that totals by category.
    """
    verdict = {"verdict": "needs_review", "violations":
               [{"code": "no_receipt", "blocks_automatic_decision": True}],
               "reimbursable_total": "1000.00",
               "receipt_total": "1000.00", "currency": "INR",
               "expense_type": "travel", "expense_type_label": "Travel"}
    return row(verdict=verdict, **over)


class WhereAClaimHasGotTo(unittest.TestCase):
    """`policy.stage`, which has to say the same seven words as the console."""

    def test_a_claim_the_agent_would_not_release_is_in_review(self):
        self.assertEqual("in_review", policy.stage(blocked()))

    def test_a_claim_a_person_approved_is_approved(self):
        self.assertEqual("approved", policy.stage(
            blocked(review_action="approved", review_at=1_760_000_000)))

    def test_a_claim_the_agent_cleared_is_approved_too(self):
        # Two ways in and both count. Treating only the first as cleared is
        # how the Pending settlement tab would come to miss every claim
        # nobody had to look at.
        self.assertEqual("approved", policy.stage(row()))

    def test_withdrawn_is_a_review_action_not_an_outcome(self):
        self.assertEqual("withdrawn", policy.stage(row(review_action="withdrawn")))

    def test_a_reviewer_refusing_it_is_rejected(self):
        self.assertEqual("rejected", policy.stage(row(review_action="rejected")))

    def test_finance_declining_to_pay_it_is_rejected_too(self):
        # A different decision by a different person at a different moment,
        # and the same ending: nobody is paid and nobody is waiting.
        self.assertEqual("rejected", policy.stage(row(outcome="rejected")))

    def test_paid_in_full_is_settled(self):
        self.assertEqual("settled", policy.stage(
            row(outcome="settled", outcome_paid="1000.00")))

    def test_paid_short_is_part_settled(self):
        self.assertEqual("part_settled", policy.stage(
            row(outcome="settled", outcome_paid="400.00")))

    def test_a_settled_claim_whose_worth_cannot_be_read_stays_closed(self):
        """The two wrong answers here are both worse than `settled`.

        A foreign claim whose rate could not be had has no figure to compare a
        payment against. Calling it part-settled leaves finance looking like
        it still owes money; falling through to `in_review` puts a claim
        somebody has already been paid for back into the queue that says who
        is waiting. Finance recorded a payment, so it is closed.
        """
        unreadable = row(outcome="settled", outcome_paid="900.00",
                         approved_total="", payout_value={"currency": "INR",
                                                          "rate": "0"},
                         verdict={"verdict": "approved", "violations": [],
                                  "reimbursable_total": "100", "currency": "USD"})
        self.assertEqual("", policy.owed(unreadable)[0])
        self.assertEqual("settled", policy.stage(unreadable))

    def test_a_second_document_of_one_purchase_is_not_a_claim(self):
        self.assertEqual("companion", policy.stage(row(companion_of="s-9")))

    def test_approved_is_awaiting_payment_rather_than_a_test_of_its_own(self):
        # Defined through the predicate rather than beside it, so a spoken
        # total and the Pending settlement badge cannot come apart.
        for item in (row(), blocked(review_action="approved"),
                     blocked(), row(review_action="rejected"),
                     row(outcome="settled", outcome_paid="1000.00")):
            self.assertEqual(policy.awaiting_payment(item),
                             policy.stage(item) == "approved",
                             policy.stage(item))


class WhatAClaimIsWorth(unittest.TestCase):
    """One copy of the figure, now that three things read it."""

    def test_the_digest_reads_the_same_function(self):
        self.assertIs(policy.owed, __import__("digest")._owed)

    def test_what_a_reviewer_released_beats_the_engine_s_figure(self):
        # A claim approved over a cap pays what the person approved. Reading
        # the engine's figure instead reported nil for every overridden claim.
        self.assertEqual(("750.00", "INR"),
                         policy.owed(row(approved_total="750.00")))

    def test_an_amount_that_cannot_be_converted_is_empty_not_zero(self):
        foreign = row(approved_total="100",
                      payout_value={"currency": "INR", "rate": "0"},
                      verdict={"verdict": "approved", "violations": [],
                               "reimbursable_total": "100", "currency": "USD"})
        self.assertEqual("", policy.owed(foreign)[0])


class WhoClearedIt(unittest.TestCase):
    """The distinction the auto-clear rate is actually made of."""

    def test_a_person_s_approval_is_a_person_s_however_automatic_the_stage(self):
        self.assertEqual("human", policy.cleared_by(row(review_action="approved")))

    def test_the_agent_gets_the_ones_nobody_had_to_look_at(self):
        self.assertEqual("agent", policy.cleared_by(row()))

    def test_nobody_has_cleared_one_still_being_read(self):
        self.assertEqual("", policy.cleared_by(blocked()))

    def test_paying_a_claim_does_not_unclear_it(self):
        """The bug this function was rewritten for, and it only showed live.

        Asking `awaiting_payment` looked right and was wrong the moment
        finance paid anything: that predicate goes false as soon as an
        `outcome` is written, so every claim the agent released and finance
        then settled came back cleared by nobody. On the live account that
        read five agent clearances against twenty-nine human ones for
        October, with fourteen settled claims counted as undecided.

        It could not fail on an invented row, because an invented row is
        never both agent-cleared and already paid.
        """
        paid = row(outcome="settled", outcome_paid="1000.00")
        self.assertEqual("settled", policy.stage(paid))
        self.assertFalse(policy.awaiting_payment(paid))
        self.assertEqual("agent", policy.cleared_by(paid))

    def test_nor_does_paying_one_a_person_approved(self):
        self.assertEqual("human", policy.cleared_by(
            row(review_action="approved", outcome="settled",
                outcome_paid="1000.00")))

    def test_a_refusal_is_a_decision_but_not_a_clearance(self):
        for action in ("rejected", "withdrawn"):
            self.assertEqual("", policy.cleared_by(row(review_action=action)), action)

    def test_a_companion_document_was_never_cleared_by_anybody(self):
        self.assertEqual("", policy.cleared_by(row(companion_of="s-9")))

    def test_the_two_predicates_ask_the_verdict_the_same_question(self):
        # `awaiting_payment` is "is it waiting *now*", `cleared_by` is "what
        # happened when it was read". They differ only on claims that have
        # since ended, and must not differ on anything else.
        for item in (row(), blocked()):
            self.assertEqual(policy.awaiting_payment(item),
                             policy.agent_would_release(item))


class TheVocabularyMatchesTheConsole(unittest.TestCase):
    """A stage name that only this module uses is a silent disagreement."""

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_every_stage_is_one_the_console_also_says(self):
        # `companion` is this module's own addition: the console keeps
        # companions out by filtering with `claimsOnly` rather than by
        # stamping a stage on them, so it is the one word not to look for.
        for stage in ("withdrawn", "rejected", "in_review", "approved",
                      "part_settled", "settled"):
            self.assertIn(f'"{stage}"', self.app, stage)

    def test_the_trio_left_out_of_every_figure_is_the_console_s_trio(self):
        self.assertIn('const LIVE_STAGES_OUT = ["rejected", "withdrawn"];', self.app)
        for stage in ("rejected", "withdrawn"):
            self.assertIn(stage, capabilities.NOT_SPEND)
        self.assertIn("companion", capabilities.NOT_SPEND)


class ReadingAPeriodAloud(unittest.TestCase):
    """Saying the period back is the listener's only cue about what they got."""

    def test_the_current_month_admits_it_is_not_over(self):
        first, last, label = capabilities.period_bounds("month", NOW)
        self.assertEqual((date(2026, 10, 1), date(2026, 10, 14)), (first, last))
        self.assertEqual("October so far", label)

    def test_a_closed_month_is_named_with_its_year(self):
        first, last, label = capabilities.period_bounds("last_month", NOW)
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30)), (first, last))
        self.assertEqual("September 2026", label)

    def test_a_month_name_still_to_come_is_last_year_s(self):
        # Asked in October, "December" cannot mean the one eight weeks away:
        # there is nothing in it.
        first, _, label = capabilities.period_bounds("december", NOW)
        self.assertEqual(date(2025, 12, 1), first)
        self.assertEqual("December 2025", label)

    def test_a_month_already_gone_is_this_year_s(self):
        first, _, _ = capabilities.period_bounds("august", NOW)
        self.assertEqual(date(2026, 8, 1), first)

    def test_an_explicit_month_is_taken_as_written(self):
        first, last, _ = capabilities.period_bounds("2026-02", NOW)
        self.assertEqual((date(2026, 2, 1), date(2026, 2, 28)), (first, last))


class ThreeReadingsOfSpend(unittest.TestCase):
    """Claimed, approved and paid differ, and the answer says which it is."""

    def claims(self):
        return [
            row(submission_id="a", reference="Exp-1"),
            row(submission_id="b", reference="Exp-2",
                outcome="settled", outcome_paid="1000.00"),
            blocked(submission_id="c", reference="Exp-3"),
            row(submission_id="d", reference="Exp-4", review_action="rejected"),
            row(submission_id="e", reference="Exp-5", companion_of="a"),
        ]

    def test_the_three_figures_are_not_the_same_number(self):
        out = capabilities.spend(self.claims(), "month", NOW)
        # Three live claims at 1,000 each; two of them cleared; one paid.
        self.assertEqual("3000.00", out["claimed"])
        self.assertEqual("2000.00", out["approved"])
        self.assertEqual("1000.00", out["settled"])

    def test_and_the_two_gaps_are_spelled_out(self):
        out = capabilities.spend(self.claims(), "month", NOW)
        self.assertEqual("1000.00", out["awaiting_decision"])
        self.assertEqual("1000.00", out["awaiting_payment"])

    def test_the_gaps_are_summed_not_subtracted(self):
        """September 2026 reported minus Rs 45.88 awaiting a decision.

        `claimed` is each claim's own worth and `approved` is what a reviewer
        released, so the two are not the same measurement and their
        difference is not a quantity. A claim approved **above** its receipt
        total - which happens, and is the whole reason `approved_total` beats
        the engine's figure - drove the subtraction below zero.

        Summed from the claims actually in each state, it cannot.
        """
        generous = blocked(submission_id="g", reference="Exp-7",
                           review_action="approved", approved_total="1500.00")
        out = capabilities.spend([generous], "month", NOW)
        self.assertEqual("1000.00", out["claimed"])
        self.assertEqual("1500.00", out["approved"])
        self.assertEqual("0.00", out["awaiting_decision"])

    def test_awaiting_payment_is_the_same_total_the_payment_run_reports(self):
        # Two functions, one figure. They disagreed by Rs 8,819.23 once.
        claims = self.claims()
        self.assertEqual(capabilities.pending(claims)["total"],
                         capabilities.spend(claims, "month", NOW)["awaiting_payment"])

    def test_what_the_company_refused_is_not_spend(self):
        out = capabilities.spend(self.claims(), "month", NOW)
        self.assertEqual(3, out["claims"])

    def test_nor_is_the_second_photograph_of_one_purchase(self):
        # The Rs 8,819.23 disagreement, in one assertion.
        with_companion = self.claims()
        without = [r for r in with_companion if not r.get("companion_of")]
        self.assertEqual(capabilities.spend(without, "month", NOW)["claimed"],
                         capabilities.spend(with_companion, "month", NOW)["claimed"])

    def test_a_claim_whose_worth_cannot_be_read_is_named_not_zeroed(self):
        unreadable = row(submission_id="z", reference="Exp-9",
                         budget_value={}, verdict={"verdict": "approved",
                                                   "violations": [],
                                                   "currency": "USD"})
        out = capabilities.spend([row(), unreadable], "month", NOW)
        self.assertEqual(1, out["unreadable"])
        self.assertEqual("1000.00", out["claimed"])

    def test_the_shares_are_of_the_total_not_of_the_largest(self):
        out = capabilities.spend(self.claims(), "month", NOW)
        self.assertEqual("100%", out["by_category"][0]["share"])


class FilingAClaimUnderATypeOnlyOnce(unittest.TestCase):
    """The split that made one category into two on the live account."""

    def test_the_type_is_read_off_the_verdict_not_the_row(self):
        # No row has a top-level `expense_type`. Reading one filed all 155
        # claims on the account under "Not set".
        self.assertEqual("travel", capabilities._type_of(row()))

    def test_what_a_person_answered_beats_what_the_engine_read(self):
        self.assertEqual("meals", capabilities._type_of(
            row(answered_expense_type="meals")))

    def test_the_id_is_what_is_counted_rather_than_the_label(self):
        """A person's answer is an id; the engine's reading carries a label.

        Preferring whichever was present split every type in two, and
        "Software subscriptions" appeared at Rs 1,46,247.03 and again at
        Rs 41,249.60 with neither being the real figure. One of these two
        claims is filed by id and the other by label, and they are the same
        expense type.
        """
        answered = row(submission_id="a", answered_expense_type="travel")
        read = row(submission_id="b")
        out = capabilities.spend([answered, read], "month", NOW)
        self.assertEqual(1, len(out["by_category"]))
        self.assertEqual("2000.00", out["by_category"][0]["amount"])

    def test_it_is_spoken_by_the_name_the_organisation_gave_it(self):
        out = capabilities.spend([row()], "month", NOW, {"travel": "Travel"})
        self.assertEqual("Travel", out["by_category"][0]["name"])
        self.assertEqual("travel", out["by_category"][0]["id"])

    def test_a_type_the_rules_no_longer_name_is_still_said_tidily(self):
        # Renamed or switched off, with claims still filed under it.
        out = capabilities.spend(
            [row(answered_expense_type="software_subscription")], "month", NOW)
        self.assertEqual("Software subscription", out["by_category"][0]["name"])

    def test_a_claim_with_no_type_at_all_is_named_rather_than_dropped(self):
        bare = row(verdict={"verdict": "approved", "violations": [],
                            "reimbursable_total": "1000.00",
                            "receipt_total": "1000.00", "currency": "INR"})
        out = capabilities.spend([bare], "month", NOW)
        self.assertEqual("Not set", out["by_category"][0]["name"])

    def test_a_claim_outside_the_window_is_out(self):
        last_month = row(submission_id="old", received_at=at("2026-09-11"))
        out = capabilities.spend([row(), last_month], "month", NOW)
        self.assertEqual(1, out["claims"])
        self.assertEqual(1, capabilities.spend([row(), last_month],
                                               "last_month", NOW)["claims"])

    def test_the_period_is_measured_on_arrival_and_says_so(self):
        out = capabilities.spend([row()], "month", NOW)
        self.assertEqual("received", out["basis"])

    def test_with_the_bill_date_reading_carried_beside_it(self):
        """A dinner on 28 September forwarded on 2 October.

        It is October's on the screen, because the console measures a period
        by `received_at`. Somebody asking for "the month of October" may well
        mean the month the money was spent in, so both are computed and the
        gap can be mentioned when it is material.
        """
        late = row(submission_id="late", received_at=at("2026-10-02"),
                   receipt={"date": "2026-09-28"})
        out = capabilities.spend([late], "month", NOW)
        self.assertEqual("1000.00", out["claimed"])
        self.assertEqual("0.00", out["on_bill_date"])

    def test_the_printed_date_is_read_from_inside_the_receipt(self):
        # There is no top-level `date`. Reading one made the second figure an
        # exact copy of the first on every claim - which does not look like a
        # bug, it looks like the two readings agreeing.
        self.assertEqual("2026-10-03", capabilities._day(row(), "incurred"))
        self.assertEqual("2026-10-05", capabilities._day(row(), "received"))

    def test_a_claim_with_no_printed_date_is_left_out_of_that_reading(self):
        # Rather than falling back to the arrival date, which would put it in
        # the figure whose whole purpose is to exclude it.
        self.assertEqual("", capabilities._day(row(receipt={}), "incurred"))


class TheCurrencyOnARealRow(unittest.TestCase):

    def test_an_empty_payout_value_falls_back_to_the_claim_s_own(self):
        # `payout_value` and `budget_value` are both `{}` on plenty of live
        # rows - anything audited before they were written, and anything on
        # an account with nothing to convert.
        bare = row(payout_value={}, budget_value={})
        self.assertEqual("INR", capabilities.spend([bare], "month", NOW)["currency"])

    def test_an_account_with_no_currency_anywhere_is_not_assumed_to_be_rupees(self):
        nothing = row(payout_value={}, budget_value={},
                      verdict={"verdict": "approved", "violations": [],
                               "receipt_total": "1000.00"})
        self.assertEqual("", capabilities.spend([nothing], "month", NOW)["currency"])


class TheQueue(unittest.TestCase):

    def test_it_counts_what_is_waiting_for_a_person(self):
        out = capabilities.queue([row(), blocked(submission_id="b")])
        self.assertEqual(1, out["waiting"])

    def test_age_is_the_number_worth_hearing(self):
        # A queue of four is fine. A queue of four whose oldest has sat eleven
        # days is somebody waiting on their own money.
        import time
        old = blocked(submission_id="old",
                      received_at=int(time.time() * 1000) - 11 * 86_400_000)
        out = capabilities.queue([old])
        self.assertEqual(11, out["oldest_days"])
        self.assertEqual(1, out["over_a_week"])


class ThePaymentRun(unittest.TestCase):

    def test_it_goes_through_the_predicate_the_tab_builds_its_rows_from(self):
        claims = [row(), row(submission_id="b", outcome="settled",
                             outcome_paid="1000.00"), blocked(submission_id="c")]
        out = capabilities.pending(claims)
        self.assertEqual(1, out["claims"])
        self.assertEqual("1000.00", out["total"])

    def test_it_says_who_is_owed(self):
        claims = [row(), row(submission_id="b", submitted_by="lk@mobil80.com")]
        out = capabilities.pending(claims)
        self.assertEqual(2, out["people"])

    def test_an_unreadable_amount_is_counted_separately(self):
        foreign = row(submission_id="f", approved_total="100",
                      payout_value={"currency": "INR", "rate": "0"},
                      verdict={"verdict": "approved", "violations": [],
                               "reimbursable_total": "100", "currency": "USD"})
        out = capabilities.pending([foreign])
        self.assertEqual(1, out["unreadable"])
        self.assertEqual("0.00", out["total"])


class FindingAClaim(unittest.TestCase):
    """`Exp-134` on screen is "expense one three four" through a microphone."""

    def test_the_reference_as_printed(self):
        self.assertTrue(capabilities.claim([row()], "Exp-1")["found"])

    def test_punctuation_and_case_do_not_matter(self):
        self.assertTrue(capabilities.claim([row()], "exp 1")["found"])

    def test_the_number_alone_is_enough(self):
        found = capabilities.claim([row(reference="Exp-134")], "134")
        self.assertEqual("Exp-134", found["ref"])

    def test_it_reports_the_stage_and_what_it_is_worth(self):
        found = capabilities.claim([row()], "Exp-1")
        self.assertEqual("approved", found["stage"])
        self.assertEqual("1000.00", found["amount"])
        self.assertTrue(found["with_finance"])
        self.assertFalse(found["closed"])

    def test_nothing_found_is_said_rather_than_guessed(self):
        self.assertFalse(capabilities.claim([row()], "Exp-999")["found"])
        self.assertFalse(capabilities.claim([row()], "")["found"])


class TheAutoClearRate(unittest.TestCase):
    """69% and 20%, asked once."""

    def test_a_human_approval_is_not_an_automatic_clearance(self):
        claims = [row(), row(submission_id="b", review_action="approved")]
        out = capabilities.auto_clear(claims, "month", NOW)
        self.assertEqual(1, out["by_agent"])
        self.assertEqual(1, out["by_person"])
        self.assertEqual(50, out["rate"])

    def test_the_denominator_is_the_claims_that_turned_out_to_be_real(self):
        claims = [row(), row(submission_id="b", review_action="rejected"),
                  row(submission_id="c", companion_of="a")]
        self.assertEqual(100, capabilities.auto_clear(claims, "month", NOW)["rate"])

    def test_an_empty_period_is_not_a_division_by_zero(self):
        self.assertEqual(0, capabilities.auto_clear([], "month", NOW)["rate"])


class TheBriefing(unittest.TestCase):
    """What a scheduled job computes so a spoken answer touches no table."""

    def test_it_carries_both_months_and_the_movement_between_them(self):
        claims = [row(), row(submission_id="b", received_at=at("2026-09-10"))]
        out = capabilities.brief(claims, NOW)
        self.assertEqual("1000.00", out["this_month"]["claimed"])
        self.assertEqual("1000.00", out["last_month"]["claimed"])
        self.assertEqual("+0%", out["month_on_month"])

    def test_a_month_with_nothing_before_it_has_no_movement(self):
        self.assertIsNone(capabilities.brief([row()], NOW)["month_on_month"])

    def test_it_holds_the_queue_and_the_payment_run_too(self):
        out = capabilities.brief([row()], NOW)
        for key in ("queue", "pending_settlement", "auto_clear", "as_of"):
            self.assertIn(key, out)


class MixedUnitsOnOneRow(unittest.TestCase):
    """`received_at` is milliseconds; the other three stamps are seconds."""

    def test_both_units_land_on_the_same_instant(self):
        self.assertEqual(capabilities._ms(1_760_000_000),
                         capabilities._ms(1_760_000_000_000))

    def test_nothing_is_a_zero_rather_than_an_exception(self):
        for value in (None, "", "not a number", 0):
            self.assertEqual(0, capabilities._ms(value))


if __name__ == "__main__":
    unittest.main()
