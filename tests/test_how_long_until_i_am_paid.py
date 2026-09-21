"""The product answered "where do I send this" twice and "when do I get my
money" nowhere.

My expenses carried two boxes - the WhatsApp number and the intake mailbox -
and somebody photographing a bill wants to know two things, not one. The
second is the one they will actually chase somebody about.

    Usually reimbursed in
    3 days
    1 to 22 days so far

Measured end to end: the receipt arriving to the payment being recorded. That
is the whole of what the person who spent the money experiences. Measuring
from approval instead would report the half of the wait this product is
fastest at and hide the half it is not, which is a flattering number and a
useless one - it is also the number a process-efficiency metric most needs to
be honest about.

Three decisions worth keeping made:

**The median, not the mean.** One claim that sat over a holiday for three
weeks drags an average far enough to make the figure worthless. The question
is "how long will mine take", which is the middle of the distribution rather
than its centre of mass.

**Over the organisation, not over the reader.** A submitter sees only their
own claims, so computing this from the rows on their screen would report the
median of the two they happen to have had settled. It is a property of the
organisation's process; one aggregate over everybody's claims discloses
nothing about anybody's.

**Silent below three.** A new account saying "typically 1 day" on the
strength of one lucky Tuesday has promised something it cannot keep, and the
first person to wait a fortnight will remember that it did.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
for _k, _v in (("AWS_DEFAULT_REGION", "ap-southeast-1"), ("USERS_TABLE", "u"),
               ("ORGS_TABLE", "o"), ("INTAKE_TABLE", "t"), ("AUTH_TABLE", "a"),
               ("ADMINS_TABLE", "d"), ("EXPENSES_TABLE", "e"),
               ("ADVANCES_TABLE", "adv"),
               ("SESSION_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:y")):
    os.environ.setdefault(_k, _v)

ROOT = os.path.join(os.path.dirname(__file__), "..")
DAY = 86400


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def settled(*days):
    """Claims settled that many days after they arrived."""
    return [{"received_at": 1_000_000, "outcome_at": 1_000_000 + int(d * DAY),
             "outcome": "settled"} for d in days]


class TheFigureItself(unittest.TestCase):

    def setUp(self):
        import auth
        self.turnaround = auth._turnaround

    def test_it_is_the_median(self):
        # 2, 4, 9 -> 4. A mean would say 5.
        self.assertEqual(4, self.turnaround(settled(2, 4, 9))["days"])

    def test_an_even_count_takes_the_middle_pair(self):
        # 1, 3, 5, 11 -> (3 + 5) / 2 = 4.
        self.assertEqual(4, self.turnaround(settled(1, 3, 5, 11))["days"])

    def test_one_holiday_does_not_move_it(self):
        # 2, 2, 3, 3, 21. The mean is 6.2 and describes nobody's experience.
        self.assertEqual(3, self.turnaround(settled(2, 2, 3, 3, 21))["days"])

    def test_the_spread_is_reported_beside_it(self):
        t = self.turnaround(settled(2, 2, 3, 3, 21))
        self.assertEqual(2, t["fastest"])
        self.assertEqual(21, t["slowest"])

    def test_same_day_is_a_day(self):
        # "0 days" reads as a missing value, and anything settled the same day
        # is a day to somebody waiting for money.
        self.assertEqual(1, self.turnaround(settled(0.2, 0.3, 0.4))["days"])


class WhatItCounts(unittest.TestCase):

    def setUp(self):
        import auth
        self.turnaround = auth._turnaround

    def test_only_settled_claims(self):
        # A rejected one was never reimbursed. Counting it as a fast nil or a
        # slow never would both be lies about a different thing.
        rows = settled(2, 3, 4) + [
            {"received_at": 1, "outcome_at": 9_000_000, "outcome": "rejected"}]
        self.assertEqual(3, self.turnaround(rows)["claims"])
        self.assertEqual(3, self.turnaround(rows)["days"])

    def test_a_claim_missing_either_timestamp_is_skipped(self):
        rows = settled(2, 3, 4) + [{"outcome": "settled", "received_at": 0,
                                    "outcome_at": 5_000_000}]
        self.assertEqual(3, self.turnaround(rows)["claims"])

    def test_a_backdated_settlement_floors_at_nil(self):
        # Clock skew and backdating both produce negatives, and a claim cannot
        # be paid before it arrived.
        rows = settled(2, 3, 4) + [{"received_at": 1_000_000,
                                    "outcome_at": 900_000, "outcome": "settled"}]
        t = self.turnaround(rows)
        self.assertEqual(4, t["claims"])
        self.assertGreaterEqual(t["fastest"], 1)


class ItStaysQuietUntilItKnows(unittest.TestCase):

    def setUp(self):
        import auth
        self.turnaround = auth._turnaround

    def test_nothing_settled(self):
        self.assertIsNone(self.turnaround([])["days"])

    def test_one_is_not_a_median(self):
        self.assertIsNone(self.turnaround(settled(3))["days"])

    def test_nor_are_two(self):
        self.assertIsNone(self.turnaround(settled(3, 5))["days"])

    def test_three_is_enough(self):
        self.assertIsNotNone(self.turnaround(settled(3, 5, 7))["days"])

    def test_the_count_is_reported_even_when_the_figure_is_not(self):
        self.assertEqual(2, self.turnaround(settled(3, 5))["claims"])


class ItIsTheOrganisationsFigureNotTheReadersOwn(unittest.TestCase):

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.fn = self.auth.split("def _submissions_list(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_it_is_computed_before_the_rows_are_narrowed(self):
        # A submitter sees only their own claims. Computing this after the
        # filter would report the median of the two they happen to have had
        # settled, which is not an answer to "how long will I wait".
        self.assertLess(self.fn.index("turnaround = _turnaround(rows)"),
                        self.fn.index("if not everyone:"))

    def test_it_reaches_the_console(self):
        self.assertIn('"turnaround": turnaround,', self.fn)


class TheBoxSaysItPlainly(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function turnaroundBox() {", 1)[1].split(
            "\n}", 1)[0]

    def test_it_sits_beside_the_two_that_raise_the_question(self):
        self.assertIn("${wa}${mail}${turnaroundBox()}", self.app)

    def test_it_draws_nothing_without_a_figure(self):
        self.assertIn("if (!t.days) return \"\";", self.fn)

    def test_it_reads_as_a_typical_wait_not_a_promise(self):
        self.assertIn("Usually reimbursed in", self.fn)

    def test_one_day_is_not_one_days(self):
        self.assertIn('n === 1 ? "day" : "days"', self.fn)

    def test_the_spread_is_shown_where_there_is_one(self):
        self.assertIn("so far", self.fn)
        # And a count instead where every claim took the same time, rather
        # than "3 to 3 days".
        self.assertIn("t.slowest !== t.fastest", self.fn)

    def test_the_value_comes_from_the_server(self):
        self.assertIn("TURNAROUND = subs.data.turnaround || null;", self.app)


class TheUnitsOfTheTwoTimestamps(unittest.TestCase):
    """`received_at` is milliseconds and `outcome_at` is seconds.

    Two writers, two conventions, and nothing between them to notice.
    Subtracting one from the other gives about minus fifty-six thousand years,
    which the floor at nought then quietly turned into a span of nil: every
    claim instant, the median nil, and the product reporting a confident
    "1 day" for an organisation that takes a fortnight.

    It surfaced only because this account has one settled claim and the figure
    stays silent below three. A clamp that makes a wrong answer look like a
    plausible one is worse than no clamp, so the normalisation happens before
    it.
    """

    def setUp(self):
        import auth
        self.turnaround = auth._turnaround

    def test_the_real_row_reads_as_days_not_nil(self):
        # Mobil80-Exp-1 as it is actually stored: ms in, seconds out, and
        # 1.75 days between them.
        rows = [{"outcome": "settled",
                 "received_at": 1789477019787, "outcome_at": 1789628056}] * 3
        self.assertEqual(2, self.turnaround(rows)["days"])

    def test_both_spellings_give_the_same_answer(self):
        ms, sec = 1789477019787, 1789477019
        in_ms = [{"outcome": "settled", "received_at": ms,
                  "outcome_at": sec + d * DAY} for d in (2, 4, 9)]
        in_sec = [{"outcome": "settled", "received_at": sec,
                   "outcome_at": sec + d * DAY} for d in (2, 4, 9)]
        self.assertEqual(self.turnaround(in_ms)["days"],
                         self.turnaround(in_sec)["days"])
        self.assertEqual(4, self.turnaround(in_ms)["days"])

    def test_the_normaliser_runs_before_the_floor(self):
        fn = read("lambda_src/auth.py").split("def _turnaround(", 1)[1].split(
            "\ndef ", 1)[0]
        self.assertLess(fn.index("def secs("), fn.index("max(0, paid - sent)"))
        self.assertIn("return n // 1000 if n > 10 ** 11 else n", fn)


class TheProcessMetricOnReports(unittest.TestCase):
    """The other half of what was asked for.

    The box in My expenses answers "when do I get my money". This answers "how
    are we doing", which is a different question with a different population:
    scoped to the month on screen, like every other figure on that tab, rather
    than the organisation's all-time figure the server sends.
    """

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function renderTriage() {", 1)[1].split(
            "\n}", 1)[0]

    def test_it_sits_with_the_auto_clear_rate(self):
        self.assertIn('<span class="k">Time to reimburse</span>', self.app)
        self.assertIn('id="t-days"', self.app)

    def test_it_is_scoped_to_the_period_like_everything_else_there(self):
        # The note above those tiles promises it.
        self.assertIn("const spans = period", self.fn)

    def test_it_normalises_the_two_timestamp_units(self):
        self.assertIn("const secs = (n) => (n > 1e11 ? Math.round(n / 1000) : n);",
                      self.fn)

    def test_the_settled_instant_is_a_number_not_a_printed_date(self):
        # `settlement.at` is "17 Sep", formatted for a row to print and
        # useless to arithmetic.
        self.assertIn('settledAt: s.outcome === "settled" ? (s.paid_at || 0) : 0,',
                      self.app)

    def test_three_is_the_floor_here_too(self):
        self.assertIn("if (spans.length < 3) {", self.fn)
        self.assertIn("three needed for a median", self.fn)

    def test_it_says_how_many_it_is_based_on(self):
        self.assertIn("Median receipt to payment, over ${spans.length} reimbursed",
                      self.fn)


if __name__ == "__main__":
    unittest.main()
