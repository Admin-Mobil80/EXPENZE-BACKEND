"""Settled claims and the settlement log, newest payment first.

Both lists were in the order the claims happened to be in. The claims list is
`payableClaims` filtered, and that walks `SUBMISSIONS`; the log sorted on
`b.seq - a.seq`, which looks like a sort and is not one - `seq` is minted when
a payment is recorded *in this session*, and every row read back off the
database carries nought. So the handful somebody had just entered came out in
order and everything else was tied, leaving last week above this morning on
the one tab whose whole subject is what has been paid.

`settledAt` is the instant the server stamps, in seconds, and it is also what
the last column of the claims table prints - so the list now sorts by the date
the reader can see rather than by one they cannot.

Two details worth keeping:

**`seq` stays, as a tiebreak.** Two payments recorded minutes apart can share a
stamped second, and when they do the order they were entered in is the right
answer. It is a poor primary key and a good secondary one.

**`paidOn` is the fallback, not the key.** It is a date somebody typed for when
the transfer actually left the bank, against `settledAt`'s "when this was
recorded here". Different questions; the visible column answers the second.
Only demo history carries it.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class OneInstantAskedInOnePlace(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function settledInstant(sub, payment) {", 1)[1] \
                          .split("\n}", 1)[0]

    def test_it_reads_the_stamp_the_server_set(self):
        self.assertIn("const stamped = Number((sub && sub.settledAt) || 0);", self.fn)
        self.assertIn("if (stamped) return stamped;", self.fn)

    def test_and_falls_back_to_a_typed_date_only_when_there_is_no_stamp(self):
        self.assertIn('const on = (payment && payment.paidOn) || "";', self.fn)
        self.assertLess(self.fn.index("return stamped;"), self.fn.index("paidOn"))

    def test_an_unparseable_date_sorts_last_rather_than_throwing(self):
        self.assertIn("return isNaN(parsed) ? 0 : Math.round(parsed / 1000);", self.fn)

    def test_the_field_is_the_one_the_claims_table_prints(self):
        # Sorting by a date the reader cannot see is how a list looks unsorted.
        self.assertIn('settledAt: s.outcome === "settled" ? (s.paid_at || 0) : 0,',
                      self.app)


class BothListsUseIt(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function renderSettled() {", 1)[1].split(
            "\nfunction ", 1)[0]

    def test_the_claims_list_sorts_newest_first(self):
        self.assertIn("settledInstant(y.sub, b) - settledInstant(x.sub, a)", self.fn)

    def test_and_so_does_the_log(self):
        self.assertIn("settledInstant(b.sub, b) - settledInstant(a.sub, a)", self.fn)

    def test_the_log_no_longer_sorts_on_a_field_that_is_zero(self):
        # `b.seq - a.seq` as the whole comparator. Every row from the server
        # carries nought, so it ordered the few entered in this session and
        # left the rest in whatever order they arrived.
        self.assertNotIn("entries.sort((a, b) => b.seq - a.seq);", self.app)

    def test_seq_survives_as_the_tiebreak(self):
        # Two payments recorded minutes apart can share a stamped second, and
        # then the order they were entered in is the right answer.
        self.assertEqual(2, self.fn.count("(b.seq || 0) - (a.seq || 0)"))

    def test_the_claims_list_reads_the_payment_that_settled_it(self):
        # A part-paid claim has several; the last one is the one that closed
        # it, and the one the row already prints in its final column.
        self.assertIn("const lastOf = (c) => (settlements[c.sub.id] || []).slice(-1)[0] || {};",
                      self.fn)


class TheDateLeadsTheList(unittest.TestCase):
    """It was last, sharing a cell with who recorded the payment and how.

    This tab is sorted by that date, and a sort key the eye has to travel to
    the right-hand edge to find reads as no sort at all. Every other list in
    the console leads with its date; this one now does too, and who and how
    stay together at the end where they are audit detail rather than the thing
    being looked up.
    """

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.head = self.app.split("<h2>Settled claims</h2>", 1)[1].split(
            "</tr>", 1)[0]
        self.fn = self.app.split("function renderSettled() {", 1)[1].split(
            "\nfunction ", 1)[0]

    def test_settled_on_is_the_first_column(self):
        import re
        cols = re.findall(r'<th scope="col"[^>]*>([^<]*)</th>', self.head)
        self.assertEqual("Settled on", cols[0])

    def test_the_old_trailing_column_now_names_what_it_holds(self):
        import re
        cols = re.findall(r'<th scope="col"[^>]*>([^<]*)</th>', self.head)
        self.assertEqual("Recorded by", cols[-1])
        self.assertNotIn("Settled</th>", self.head)

    def test_the_cell_formats_the_number_the_list_is_sorted_by(self):
        # Formatting `last.at` instead would let the column and the order
        # disagree, since only one of them would be reading the stamp.
        self.assertIn("const at = settledInstant(c.sub, last);", self.fn)
        self.assertIn('`<td class="day">${esc(at ? dayStamp(at * 1000) : (last.at || ""))}</td>`',
                      self.fn)

    def test_and_the_date_is_not_printed_twice(self):
        self.assertNotIn("${last.at || \"\"} &middot; ${last.by", self.app)

    def test_the_row_the_header_and_the_empty_state_all_count_seven(self):
        import re
        cols = len(re.findall(r'<th scope="col"', self.head))
        body = self.fn.split("const at = settledInstant(c.sub, last);", 1)[1] \
                      .split("tb.appendChild(tr);", 1)[0]
        self.assertEqual(cols, len(re.findall(r"`<td", body)))
        self.assertIn('colspan="7" class="empty"', self.fn)


class TheBoxesAreBigEnoughToHit(unittest.TestCase):
    """A 13px target in a 41px row, aimed at repeatedly, down a column.

    The default checkbox is what the browser gives you, and it was fine while
    nothing depended on hitting it. Assembling a payment run is somebody
    ticking their way down a list - and missing opens the claim, because the
    row underneath is a link to the claim page. So a miss is not a miss; it is
    a navigation away from the work.
    """

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.css = self.app.split("th.pick, td.pick {", 1)[1].split("\n\n", 1)[0]

    def test_the_box_is_larger_than_the_browser_default(self):
        self.assertIn("width:18px; height:18px;", self.css)

    def test_and_the_cell_around_it_is_a_real_target(self):
        # 44px wide with 10px above and below: near what a finger wants, and
        # comfortably past what a mouse needs.
        self.assertIn("width:44px;", self.css)
        self.assertIn("td.pick { padding-top:10px; padding-bottom:10px; }", self.app)

    def test_the_tick_is_the_products_own_colour(self):
        # The one place a browser would otherwise choose a colour for us.
        self.assertIn("accent-color:var(--ok);", self.css)

    def test_a_box_that_cannot_be_ticked_still_looks_like_one(self):
        self.assertIn("td.pick input:disabled { cursor:not-allowed; opacity:.35; }",
                      self.app)


if __name__ == "__main__":
    unittest.main()
