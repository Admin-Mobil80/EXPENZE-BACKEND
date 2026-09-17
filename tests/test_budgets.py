"""Budget inheritance, periods, and what counts as over.

The inheritance rule is the part worth pinning down: a person given one limit
of their own must keep every limit they did not restate. Getting that wrong
drops budgets silently, and silently is the worst way for a control to fail.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

import budgets  # noqa: E402


CONFIG = {
    "period": "month",
    "currency": "INR",
    "org": {"total": "60000.00", "types": {"meals": "20000.00", "travel": "15000.00"}},
    "groups": {"sales": {"total": "90000.00", "types": {"travel": "40000.00"}}},
    "people": {"priya@x.com": {"types": {"meals": "6000.00"}},
               "karan@x.com": {"total": "12000.00", "period": "week"}},
}


class Normalise(unittest.TestCase):
    def test_round_trips_a_whole_configuration(self):
        got = budgets.normalise(CONFIG, "INR")
        self.assertEqual(got["period"], "month")
        self.assertEqual(got["org"]["total"], "60000.00")
        self.assertEqual(got["groups"]["sales"]["types"]["travel"], "40000.00")
        self.assertEqual(got["people"]["karan@x.com"]["period"], "week")

    def test_amounts_are_tidied(self):
        got = budgets.normalise({"org": {"total": " 1,250 "}}, "INR")
        self.assertEqual(got["org"]["total"], "1250.00")

    def test_a_blank_limit_means_no_limit(self):
        got = budgets.normalise({"org": {"total": "", "types": {"meals": None}}}, "INR")
        self.assertEqual(got["org"], {})

    def test_negative_budgets_are_refused(self):
        with self.assertRaises(budgets.BudgetInputError):
            budgets.normalise({"org": {"total": "-5"}}, "INR")

    def test_nonsense_amounts_are_refused(self):
        with self.assertRaises(budgets.BudgetInputError):
            budgets.normalise({"org": {"types": {"meals": "lots"}}}, "INR")

    def test_only_a_week_or_a_month(self):
        with self.assertRaises(budgets.BudgetInputError):
            budgets.normalise({"period": "quarter"}, "INR")

    def test_addresses_are_lowercased_so_a_lookup_matches(self):
        got = budgets.normalise({"people": {"Priya@X.com": {"total": "100"}}}, "INR")
        self.assertIn("priya@x.com", got["people"])


class Inheritance(unittest.TestCase):
    def setUp(self):
        self.cfg = budgets.normalise(CONFIG, "INR")

    def test_somebody_with_nothing_of_their_own_gets_the_organisations(self):
        got = budgets.effective(self.cfg, email="new@x.com", group_id="engineering")
        self.assertEqual(got["total"], "60000.00")
        self.assertEqual(got["total_from"], "organisation")
        self.assertEqual(got["types"]["meals"]["limit"], "20000.00")

    def test_a_group_total_beats_the_organisations(self):
        got = budgets.effective(self.cfg, email="new@x.com", group_id="sales")
        self.assertEqual(got["total"], "90000.00")
        self.assertEqual(got["total_from"], "group")
        # ...and the group's travel limit replaces the organisation's...
        self.assertEqual(got["types"]["travel"]["limit"], "40000.00")
        self.assertEqual(got["types"]["travel"]["from"], "group")
        # ...while meals, which the group said nothing about, is inherited.
        self.assertEqual(got["types"]["meals"]["limit"], "20000.00")
        self.assertEqual(got["types"]["meals"]["from"], "organisation")

    def test_a_person_overriding_one_type_keeps_everything_else(self):
        got = budgets.effective(self.cfg, email="priya@x.com", group_id="sales")
        self.assertEqual(got["types"]["meals"]["limit"], "6000.00")
        self.assertEqual(got["types"]["meals"]["from"], "person")
        # The group's travel limit and total survive her meal override.
        self.assertEqual(got["types"]["travel"]["limit"], "40000.00")
        self.assertEqual(got["total"], "90000.00")

    def test_a_person_can_be_on_a_different_cadence(self):
        self.assertEqual(budgets.effective(self.cfg, email="karan@x.com")["period"], "week")
        self.assertEqual(budgets.effective(self.cfg, email="priya@x.com")["period"], "month")

    def test_no_budgets_at_all_is_not_an_error(self):
        got = budgets.effective(budgets.empty("INR"), email="a@x.com", group_id="sales")
        self.assertIsNone(got["total"])
        self.assertFalse(got["has_any"])


class Periods(unittest.TestCase):
    def test_a_month_runs_to_its_last_day(self):
        self.assertEqual(budgets.period_bounds("month", date(2026, 2, 17)),
                         (date(2026, 2, 1), date(2026, 2, 28)))

    def test_february_in_a_leap_year(self):
        self.assertEqual(budgets.period_bounds("month", date(2028, 2, 5))[1], date(2028, 2, 29))

    def test_december_rolls_into_the_next_year(self):
        self.assertEqual(budgets.period_bounds("month", date(2026, 12, 9)),
                         (date(2026, 12, 1), date(2026, 12, 31)))

    def test_a_week_runs_monday_to_sunday(self):
        # 2026-09-09 is a Wednesday.
        self.assertEqual(budgets.period_bounds("week", date(2026, 9, 9)),
                         (date(2026, 9, 7), date(2026, 9, 13)))

    def test_a_sunday_belongs_to_the_week_that_just_ended(self):
        self.assertEqual(budgets.period_bounds("week", date(2026, 9, 13))[0], date(2026, 9, 7))

    def test_a_week_can_straddle_two_months(self):
        start, end = budgets.period_bounds("week", date(2026, 10, 1))
        self.assertEqual((start, end), (date(2026, 9, 28), date(2026, 10, 4)))


class Status(unittest.TestCase):
    def test_no_limit_is_not_a_breach(self):
        self.assertEqual(budgets.status("99999", None), "none")
        self.assertEqual(budgets.status("99999", ""), "none")

    def test_comfortably_inside(self):
        self.assertEqual(budgets.status("100", "1000"), "under")

    def test_a_warning_arrives_before_the_money_runs_out(self):
        self.assertEqual(budgets.status("800", "1000"), "near")
        self.assertEqual(budgets.status("999.99", "1000"), "near")

    def test_exactly_on_budget_is_not_over(self):
        self.assertEqual(budgets.status("1000", "1000"), "near")

    def test_a_penny_over_is_over(self):
        self.assertEqual(budgets.status("1000.01", "1000"), "over")

    def test_a_zero_budget_means_spend_nothing(self):
        self.assertEqual(budgets.status("0", "0"), "under")
        self.assertEqual(budgets.status("1", "0"), "over")


if __name__ == "__main__":
    unittest.main()
