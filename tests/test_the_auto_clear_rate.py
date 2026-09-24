"""The number this product lives or dies on, asked three ways.

The Reports tab showed it twice, one scroll apart, and the two disagreed:
**69%** in the headline strip, **20%** in Intake & triage, about the same
seventy-one receipts. A third reading, the "Auto-cleared" column in By expense
type, agreed with the wrong one.

20% was the true one. 69% counted every claim that had *reached* a cleared
stage - `approved`, `part_settled`, `settled` - over the total. That includes
every claim a person read and approved, under a tile whose note said "Reviewed
with no human involved". It was not the auto-clear rate; it was the cleared
rate, which on an account whose queue is worked diligently approaches 100% and
says nothing about whether the agent is doing the job.

Two things were wrong and both are worth keeping fixed.

**Three readers, two definitions, no shared predicate.** `claimStage` has
always known the difference - it stamps `kind` as `"agent"` or `"human"` on
the claim it clears - and not one of the three asked it. A number on screen
three times is three chances to answer a different question from the one on
the label.

**And the true one was true by subtraction.** The triage tile computed
`period.length - queued - decided`, which is "everything nobody is waiting on
and nobody decided". That sweeps in a companion document - the second
attachment of one purchase, which is evidence and not a claim - and any claim
still being read. Neither was cleared by the agent, because neither is a claim
anybody is being paid for, so the honest figure is slightly lower than the one
that was on screen.

It is asked once now, of each claim, and shown in the report whose subject it
is - where the three counts that produce it sit beside it and a reader can
check the arithmetic.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class OneDefinition(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_it_asks_the_stage_who_cleared_the_claim(self):
        self.assertIn('const autoCleared = (sub) => claimStage(sub).kind === "agent";',
                      self.app)

    def test_and_the_rate_is_that_predicate_over_the_rows(self):
        fn = self.app.split("const autoClearRate = (subs) =>", 1)[1].split(";", 1)[0]
        self.assertIn("subs.filter(autoCleared).length / subs.length", fn)

    def test_an_empty_period_is_not_a_division_by_zero(self):
        fn = self.app.split("const autoClearRate = (subs) =>", 1)[1].split(";", 1)[0]
        self.assertIn("subs.length ?", fn)

    def test_the_cleared_stage_reading_is_gone_from_everywhere(self):
        # The one that called a human's approval an automatic clearance. It
        # was in two of the three readers.
        self.assertNotIn(
            'filter(r => ["approved","part_settled","settled"].includes(r.stage)).length;\n'
            '  $("r-count")',
            self.app)
        self.assertEqual(
            0,
            self.app.count('const auto = all.filter(r => ["approved","part_settled","settled"]'),
            "the By expense type column still counts human approvals as automatic")

    def test_every_reader_goes_through_it(self):
        for caller in ("const cleared = period.filter(autoCleared).length;",
                       "const rate = autoClearRate(period);",
                       "auto: autoClearRate(all.map(r => r.sub)),"):
            self.assertIn(caller, self.app, caller)


class ShownOnce(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_the_headline_strip_no_longer_carries_it(self):
        # That strip is what was claimed and what has been paid. The rate is a
        # triage question and belongs in the triage report, beside the counts
        # that produce it.
        self.assertNotIn('id="r-rate"', self.app)

    def test_nor_does_the_empty_period_path(self):
        # It wrote an em dash into a tile that no longer exists, which is a
        # TypeError on the first month with nothing in it.
        empty = self.app.split("function renderEmptyPeriod() {", 1)[1].split(
            "\n}", 1)[0]
        self.assertNotIn("r-rate", empty)

    def test_it_is_still_in_the_report_whose_subject_it_is(self):
        self.assertIn('<span class="k">Auto-clear rate</span>', self.app)
        self.assertEqual(1, self.app.count('<span class="k">Auto-clear rate</span>'))
        self.assertIn('id="t-rate"', self.app)

    def test_with_the_counts_that_produce_it_beside_it(self):
        triage = self.app.split('<section class="triage">', 1)[1].split(
            "</section>", 1)[0]
        for k in ("Cleared by agent", "Awaiting a human", "Decided by you",
                  "Auto-clear rate"):
            self.assertIn(k, triage, k)

    def test_the_by_type_column_is_a_different_cut_not_a_second_copy(self):
        # Per expense type is a real question with a real answer; it is the
        # same definition applied to a subset, which is why it may now be
        # shown at all.
        self.assertIn('<th scope="col" style="text-align:right">Auto-cleared</th>',
                      self.app)


class TheSubtractionIsGone(unittest.TestCase):
    """It counted things the agent never cleared."""

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.triage = self.app.split("function renderTriage() {", 1)[1].split(
            "\n}", 1)[0]

    def test_the_triage_tile_no_longer_subtracts(self):
        self.assertNotIn("period.length - queued - decided", self.triage)

    def test_a_companion_document_is_not_an_automatic_clearance(self):
        # It is the second attachment of one purchase - the invoice and its
        # receipt in one email - kept as evidence, out of the queue and out of
        # the payment run. Nobody is paid for it, so nothing was cleared.
        stage = self.app.split("function claimStage(sub) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn('kind: "agent"', stage)
        self.assertIn('kind: "human"', stage)

    def test_the_count_and_the_rate_come_from_the_same_predicate(self):
        # Otherwise "Cleared by agent 14" and "20%" can disagree with each
        # other on one screen, which is the bug one level down.
        self.assertIn("const cleared = period.filter(autoCleared).length;", self.triage)
        self.assertIn("const rate = autoClearRate(period);", self.triage)


if __name__ == "__main__":
    unittest.main()
