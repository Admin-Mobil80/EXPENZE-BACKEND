"""Seventy-one settled claims, and finance reading them by scrolling.

The list only grows, and the thing somebody is looking for in it is usually
one of four: a person, a shop, a claim number quoted at them, or the reference
printed on a bank line they are reconciling against. One box that matches any
of them beats four labelled filters - a finance executive holding a statement
has the UTR, not the column it lives in.

It sits in the tab strip beside the period selector, which is there precisely
because it governs all three sections, and the search governs them too.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class OneBoxForTheWholeTab(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_it_sits_with_the_control_that_already_governs_all_three(self):
        aside = self.app.split('<nav class="subtabs" id="s-subtabs"', 1)[1].split(
            "</nav>", 1)[0]
        self.assertIn('id="done-find"', aside)
        self.assertIn('id="done-period"', aside)

    def test_every_word_must_match_somewhere(self):
        # "apple lakshmi" finds the Apple invoice of Lakshmikanth's without
        # either word being a prefix of anything. Matching the phrase against
        # each field would miss it; matching any one word returns half the
        # list.
        fn = self.app.split("function matchesFind(terms, ...haystack) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertIn("terms.every(t => hay.includes(t))", fn)

    def test_and_order_and_case_do_not_matter(self):
        find = self.app.split("function settledFind() {", 1)[1].split("\n}", 1)[0]
        self.assertIn(".toLowerCase().split(", find)
        fn = self.app.split("function matchesFind(terms, ...haystack) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertIn(".toLowerCase()", fn)

    def test_an_empty_box_matches_everything(self):
        fn = self.app.split("function matchesFind(terms, ...haystack) {", 1)[1] \
                     .split("\n}", 1)[0]
        self.assertIn("if (!terms.length) return true;", fn)


class EachSectionIsSearchableByWhatItHolds(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_a_settled_claim_by_person_vendor_reference_or_bank_line(self):
        fn = self.app.split("function renderSettled() {", 1)[1].split(
            "\nfunction ", 1)[0]
        block = fn.split("matchesFind(find,", 1)[1].split("));", 1)[0]
        for field in ("c.sub.who", "c.sub.whoEmail", "c.sub.vendor",
                      "c.sub.reference", ".txn"):
            self.assertIn(field, block, field)

    def test_a_rejection_by_its_reason_as_well(self):
        # The column that makes a refusal reviewable. "Claimed too late" is a
        # thing finance goes looking for across a quarter.
        fn = self.app.split("function renderRejected() {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("(decisions[sub.id] || {}).reason", fn)
        self.assertIn("(sub.review || {}).reason", fn)

    def test_a_payment_by_its_reference_mode_and_who_recorded_it(self):
        fn = self.app.split("function renderSettled() {", 1)[1].split(
            "\nfunction ", 1)[0]
        log = fn.split("Object.entries(settlements).forEach", 1)[1]
        for field in ("p.txn", "p.mode", "p.note", "personName(p.by)"):
            self.assertIn(field, log, field)

    def test_the_counts_narrow_with_the_list(self):
        # Three figures describing everybody over a list showing one is the
        # shape of bug this console has been bitten by twice.
        fn = self.app.split("function renderSettled() {", 1)[1].split(
            "\nfunction ", 1)[0]
        self.assertLess(fn.index("matchesFind(find,"), fn.index('$("done-sub")'))

    def test_an_empty_result_says_which_of_the_two_emptied_it(self):
        # A period with nothing in it and a search that found nothing are
        # different problems with different answers.
        self.assertIn("Nothing settled matches that search.", self.app)
        self.assertIn("No rejected claim matches that search.", self.app)
        self.assertIn("No payment matches that search.", self.app)
        self.assertIn("Widen it above.", self.app)


class TypingInItDoesNotThrowYouOut(unittest.TestCase):
    """The tab strip is rebuilt on every render, and the box lives in it.

    `nav.textContent = ""` detaches the aside, and detaching an element that
    contains the focus sends the focus to the body - re-appending does not
    bring it back. Every keystroke would have re-rendered the list and dropped
    the cursor, one character per click, which is the kind of thing that makes
    somebody stop using a feature rather than report it.
    """

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function renderSettledTabs() {", 1)[1].split(
            "\n}", 1)[0]

    def test_only_the_buttons_are_rebuilt(self):
        self.assertIn('nav.querySelectorAll("button").forEach(b => b.remove());',
                      self.fn)
        # Comments stripped: the prose above this code names the call it
        # replaced, and a test that reads the explanation as the thing it
        # warns against fails on the fix.
        code = re.sub(r"/\*[\s\S]*?\*/", " ", self.fn)
        code = re.sub(r"(?m)^\s*//[^\n]*", " ", code)
        self.assertNotIn('nav.textContent = ""', code)

    def test_and_the_new_ones_go_in_before_the_aside(self):
        self.assertIn("nav.insertBefore(b, aside);", self.fn)

    def test_the_listener_is_added_once_not_once_per_render(self):
        self.assertIn("if (find && !find.dataset.wired) {", self.fn)
        self.assertIn('find.dataset.wired = "1";', self.fn)

    def test_clearing_the_box_with_its_own_x_also_searches(self):
        # Clicking the X in a search field fires `search` and not `input`, and
        # a box that still filters after it has been visibly cleared is the
        # worst of both.
        self.assertIn('find.addEventListener("search", again);', self.fn)
        self.assertIn('find.addEventListener("input", again);', self.fn)


if __name__ == "__main__":
    unittest.main()
