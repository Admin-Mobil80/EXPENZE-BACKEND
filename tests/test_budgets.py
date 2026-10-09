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


class TheFormOnlyEverShowsWhatThisScopeSet(unittest.TestCase):
    """One budget at the top looked like a budget on everybody.

    Setting the organisation's limits filled in every row of the Groups and
    the People tabs: the figure greyed inside each empty field as its
    placeholder, and spelled out again beside the label as "inherits
    ₹1,00,000.00 from the organisation". Nine expense types and ten people is
    ninety copies of a number nobody entered.

    Nothing was written - `groups` and `people` are empty on the record, and
    inheritance is computed at spend time - but a field with a number in it
    is a field with a number in it, whatever shade it is drawn in. A budget
    page exists to answer "what is set here", so that is the one question it
    cannot leave ambiguous.

    What covers the scope is still said, once, above the rows.
    """

    def setUp(self):
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "..", "PORTAL", "app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_an_empty_field_says_no_limit_not_the_figure_above_it(self):
        self.assertIn('input.placeholder = "No limit";', self.app)
        self.assertNotIn(
            'input.placeholder = row.inherited != null ? (row.inherited / 100).toFixed(2)',
            self.app)

    def test_and_so_does_the_label_beside_it(self):
        self.assertIn(
            '(row.value == null ? `<span class="blfrom">No limit</span>` : "")',
            self.app)
        self.assertNotIn("inherits ${fmt(row.inherited, orgCurrency())}", self.app)

    def test_what_covers_it_is_still_said_once_above_the_rows(self):
        # Dropping the fact along with the figures would be the other error:
        # a group with nothing of its own is still capped by the organisation,
        # and a page that says "No limit" ten times without that sentence
        # reads as uncapped.
        self.assertIn("Nothing is set here. The limits above this level still ",
                      self.app)
        guard = self.app.split("const covered = budScope !== \"org\"", 1)[1] \
                        .split(";", 1)[0]
        self.assertIn("r.value == null && r.inherited != null", guard)

    def test_the_organisation_tab_never_claims_to_inherit(self):
        # It has nothing above it, so the sentence would be false there.
        self.assertIn('const covered = budScope !== "org"', self.app)


if __name__ == "__main__":
    unittest.main()


class TheUsedColumnAnswersForEveryRow(unittest.TestCase):
    """"₹0.00 used" on some rows and nothing on others, meaning two things.

    The editor took its figures from `budgetLines`, which answers a different
    question - how is this person doing against their limits - and builds a
    line only for a type that *has* a limit somewhere. So the column was
    filled against Meals and Courier and blank against Computer Peripherals,
    and the blank did not mean nothing was spent. It meant there was no line
    to look the figure up in. On this account one of those blanks was hiding
    real spend.

    Which is the wrong way round for that screen. Somebody deciding whether a
    scope needs its own limit needs to know what it spends, and the rows with
    no limit yet are exactly the ones where that is the open question. The
    blanks were on the only rows anybody was there to think about.
    """

    def setUp(self):
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "..", "PORTAL", "app.html"), encoding="utf-8") as h:
            self.app = h.read()
        self.fn = self.app.split("function spendIn(belongs, over) {", 1)[1].split(
            "\n}", 1)[0]

    def test_spend_is_asked_on_its_own_terms(self):
        # Of every type the person submitted against, not of the types that
        # happen to be capped.
        self.assertIn("mine.forEach(s => {", self.fn)
        self.assertIn("types[id] = (types[id] || 0) + amount(s);", self.fn)

    def test_the_editor_reads_it_rather_than_the_limit_lines(self):
        editor = self.app.split("// ---- editor ----", 1)[1]
        self.assertIn("const spend = spendForScope(budScope, person);", editor)
        self.assertNotIn("spend.lines.find", self.app)

    def test_all_three_scopes_are_answered(self):
        """The organisation's and a group's spend were simply absent.

        A column of nothing reads as "this product does not know" rather than
        as an answer - and those are the two scopes where a limit is most
        often set first, so the figure that decides what to set it to was on
        the one tab that already had it.
        """
        fn = self.app.split("function spendForScope(scope, person) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertIn('if (scope === "org") return spendIn(() => true);', fn)
        self.assertIn("spendIn(s => groupOf(s).id === gid)", fn)
        self.assertIn("spendIn(s => s.who === person.name)", fn)

    def test_a_group_is_asked_the_same_way_the_reports_ask_it(self):
        # `groupOf` is the attribution, inferred or set; reading `sub.groupId`
        # directly would miss everything the agent worked out from the bill.
        fn = self.app.split("function spendForScope(scope, person) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertIn("groupOf(s).id", fn)

    def test_one_arithmetic_for_all_four(self):
        """Three scopes in the editor, and the watchlist behind them.

        `budgetLines` kept its own count of a person's spend, with no
        exclusions at all - so the watchlist and the two "over budget" badges
        saw a refused claim, a withdrawn one and a companion document while
        the editor beside them saw none of the three. The same person could
        read as over on the watchlist and under on the row you opened to look
        at it.
        """
        self.assertEqual(1, self.app.count("function spendIn(belongs, over) {"))
        lines = self.app.split("function budgetLines(person) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("const spend = spendIn(s => s.who === person.name, b.period);",
                      lines)
        self.assertNotIn("submittedBy(", lines)

    def test_a_personal_period_is_still_the_window_it_is_measured_over(self):
        # Somebody can be measured weekly while the organisation is monthly,
        # so the caller says which window it is asking about.
        fn = self.app.split("function spendIn(belongs, over) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn('const period = over || (ORG_PROFILE.budgets || {}).period',
                      fn)

    def test_budget_for_still_answers_the_other_half(self):
        # What the limits are and which level each came from, which is the
        # question `budgetLines` is actually for.
        lines = self.app.split("function budgetLines(person) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("const b = budgetFor(person);", lines)
        self.assertIn("from:b.totalFrom", lines)

    def test_what_the_company_refused_is_not_spend(self):
        """A rejected claim is the company deciding it is not spending that.

        A withdrawn one is the submitter saying it was never a claim at all.
        Both were eating into a limit nothing would ever be paid out of, which
        makes a budget read as fuller than it is and sends somebody looking
        for a conversation about money that is not going anywhere.

        Settled is not in the list, obviously, and neither is anything in
        review or waiting to be paid: a limit nobody notices until the payment
        run is a limit that has already been passed.
        """
        self.assertIn('const spent = (sub) => !["rejected", "withdrawn"]'
                      '.includes(claimStage(sub).stage);', self.fn)
        self.assertIn("&& spent(s) &&", self.fn)

    def test_and_the_banner_says_which_way_it_counts(self):
        # It said "everything submitted in the period", which stopped being
        # true the moment two of the endings came out.
        self.assertIn("Rejected and withdrawn claims are not counted.", self.app)
        self.assertIn("in review, waiting to be paid, and settled", self.app)

    def test_a_second_document_is_not_a_second_spend(self):
        # An invoice and its receipt arriving together are one purchase.
        # Counting both is the bug that had Reports disagreeing with the
        # payment run and the float reporting money outstanding with nothing
        # pending.
        self.assertIn("claimsOnly(SUBMISSIONS)", self.fn)

    def test_a_row_with_no_spend_reads_nought_rather_than_blank(self):
        # A nought is an answer; a blank is a question about the product.
        self.assertIn("(spend.types[row.id] || 0)", self.app)
        self.assertIn('used.textContent = amount === null ? "" '
                      ': `${fmt(amount, spend.ccy)} used`;', self.app)

    def test_and_the_total_row_still_totals(self):
        self.assertIn('row.id === "__total" ? spend.total', self.app)

    def test_the_currency_handling_matches_the_budget_arithmetic(self):
        # Converted where the server could, face value where it could not -
        # otherwise this column and the limit beside it count differently.
        self.assertIn("const converted = budgetValue(s);", self.fn)
        self.assertIn("converted === null ? evaluate(s).receiptTotal : converted",
                      self.fn)

    def test_and_what_could_not_be_converted_is_still_reported(self):
        self.assertIn("other: subs.length - mine.length", self.fn)
        self.assertIn("converted: mine.filter(s => budgetValue(s) !== null).length",
                      self.fn)

    def test_the_watchlist_still_asks_the_limit_question(self):
        # `budgetLines` is about statuses against caps and stays as it was.
        self.assertIn("function budgetLines(person) {", self.app)
        self.assertIn("return PEOPLE.map(budgetLines).filter(Boolean)", self.app)
