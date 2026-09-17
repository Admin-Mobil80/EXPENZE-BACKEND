"""People whose access was taken away, and where they went.

A membership is never deleted. Removing somebody sets `status` to `removed`,
which every resolver already treats as unusable - so nothing they send is
accepted from that moment - and the row stays, deliberately: money they are
still owed survives them leaving, and a question about last quarter will name
somebody who has gone.

The console then sorted the roll into two buckets on one test:

    invite: p.status === "active" ? "accepted" : "pending"

`removed` and `suspended` both fell into the `else`. So a person taken off the
roll appeared under **Pending**, labelled **"invitation not accepted"**, and was
counted in "32 invitations outstanding" in the banner above the list. Three
wrong things from one missing branch: a false statement about somebody who did
accept and did work, an inflated chase list, and the product suggesting you
chase the colleague you had just removed.

The same shape as rejected claims being in no list: a state that leaves one
list and lands somewhere that says something untrue about it.

So: three states, three tabs. The tests below hold the sorting - which is the
defect - and then the things the third tab exists to make possible, chiefly that
removing the wrong person can now be undone.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class ARemovedPersonIsNotAnOutstandingInvitation(unittest.TestCase):
    """The defect, and the count it corrupted."""

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_only_an_invited_person_counts_as_pending(self):
        self.assertIn('p.status === "invited" ? "pending" : "inactive"', self.app)

    def test_the_old_two_way_sort_is_gone(self):
        self.assertNotIn('invite: p.status === "active" ? "accepted" : "pending"',
                         self.app)

    def test_the_outstanding_invitations_count_excludes_them(self):
        body = self.app.split("function renderPeople() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("const pending = lists.pending.length;", body)
        # The old expression counted everybody who was not active.
        self.assertNotIn("const pending = pendingList.length;", body)

    def test_they_are_not_told_their_invitation_was_not_accepted(self):
        # They accepted it. That is how they came to have claims.
        body = self.app.split("shown.forEach(person => {", 1)[1][:2500]
        not_accepted = body.index("invitation not accepted")
        inactive = body.index("isInactivePerson(person)")
        self.assertLess(inactive, not_accepted,
                        "the inactive branch must be tested before the "
                        "unaccepted-invitation branch, or it never runs")


class ThreeStatesThreeTabs(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.body = self.app.split("function renderPeople() {", 1)[1].split("\n}", 1)[0]

    def test_the_tab_exists_and_opens_last(self):
        self.assertIn('["inactive", "Inactive"]', self.app)
        self.assertIn('let peopleTab = "active";', self.app)

    def test_suspended_and_removed_share_it(self):
        # Different things - one reversible, one a departure - but the same
        # answer to "is this person sending receipts or waiting to". The row
        # says which.
        self.assertIn('person.status === "suspended" ? "suspended" : "removed"',
                      self.app)

    def test_each_tab_says_what_its_number_means(self):
        for phrase in ("can send receipts", "awaiting acceptance",
                       "no longer sending receipts"):
            self.assertIn(phrase, self.body)

    def test_an_empty_tab_explains_itself(self):
        self.assertIn("Nobody has been removed or suspended", self.app)

    def test_the_column_is_relabelled(self):
        # "Can send from" is a question about somebody who still can.
        self.assertIn('peopleTab === "inactive" ? "Access" : "Can send from"',
                      self.body)

    def test_the_invite_form_is_not_offered_there(self):
        self.assertIn('rw && peopleTab !== "inactive" ? "flex" : "none"', self.body)

    def test_the_banner_does_not_talk_about_invitations(self):
        banner = self.body.split("<strong>No longer on the roll.</strong>", 1)[1]
        banner = banner.split("</span>", 1)[0]
        self.assertNotIn("invitation", banner.lower())
        self.assertIn("claims are kept", banner)


class WhenAccessWentAndWhoTookIt(unittest.TestCase):
    """"Removed" with no date beside it is the useless half of a fact."""

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.member = self.auth.split("def _member_update(", 1)[1].split("\ndef ", 1)[0]

    def test_a_status_change_is_stamped(self):
        self.assertIn("status_changed_at = :sca", self.member)
        self.assertIn("status_changed_by = :scb", self.member)

    def test_it_is_only_stamped_when_the_status_actually_changes(self):
        # Saving a person's name without touching their status must not rewrite
        # the date they were removed.
        self.assertIn('if status != str(member.get("status") or ""):', self.member)

    def test_it_reaches_the_console(self):
        view = self.auth.split("def _people(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"status_changed_at"', view)
        self.assertIn('"status_changed_by"', view)
        self.assertIn("statusAt", read("../PORTAL/app.html"))

    def test_the_audit_log_still_records_it_too(self):
        # This stamp is for rendering one line per person. The log is the
        # authority, and keeps every change rather than only the last.
        self.assertIn('"access changed"', self.member)


class RemovingTheWrongPersonCanBeUndone(unittest.TestCase):
    """The server always accepted it; nothing could reach it.

    `_member_update` has always taken `status: active`, but a removed person
    appeared under Pending among the unaccepted invitations, where the only
    control is Manage - so putting somebody back meant a database write.
    """

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_the_tab_offers_reinstate(self):
        self.assertIn('back.textContent = "Reinstate"', self.app)
        self.assertIn('{ email: person.email, status: "active" }', self.app)

    def test_it_is_confirmed_first(self):
        # It hands back the ability to spend the organisation's credits and to
        # send receipts in its name.
        block = self.app.split('back.textContent = "Reinstate"', 1)[1][:900]
        self.assertIn("confirm(", block)
        self.assertIn("if (!confirm", block)

    def test_the_list_is_reloaded_afterwards(self):
        # Otherwise the person stays on a tab they no longer belong to.
        block = self.app.split('back.textContent = "Reinstate"', 1)[1][:1400]
        self.assertIn("await loadRecords();", block)

    def test_a_failure_re_enables_the_button(self):
        block = self.app.split('back.textContent = "Reinstate"', 1)[1][:1400]
        self.assertIn("back.disabled = false", block)

    def test_the_server_accepts_it(self):
        member = read("lambda_src/auth.py").split(
            "def _member_update(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('("active", "suspended", "removed")', member)


class TheirClaimsAreStillTheirs(unittest.TestCase):
    """The reason the row is kept at all."""

    def test_the_receipts_column_is_unchanged_for_them(self):
        # Their history is why this tab is worth having beyond the bug fix: a
        # finance question about last quarter names people who have left.
        app = read("../PORTAL/app.html")
        body = app.split("shown.forEach(person => {", 1)[1][:3000]
        self.assertIn("person.settled", body)

    def test_the_endpoint_counts_claims_for_everyone_on_the_roll(self):
        # Not only the active ones - it scans the whole org and joins by email.
        view = read("lambda_src/auth.py").split(
            "def _people(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("settled_by_person", view)
        self.assertNotIn('r.get("status") == "active"', view)


if __name__ == "__main__":
    unittest.main()
