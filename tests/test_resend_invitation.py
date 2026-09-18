"""Asking again, for the people who meant to accept and forgot.

Twenty-six invitations outstanding is not twenty-six refusals. It is mostly a
message read on a phone, meant to be dealt with later, and never dealt with -
and the cheapest way to onboard those people is to ask a second time.

The restraint matters as much as the feature. A second copy before the first
has been read teaches somebody the sender is noise, and a nudge that can be
repeated daily ends up in a spam folder taking every future invitation with it.
So forty-eight hours is the floor between any two sends, enforced on the server
rather than only offered in the console.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def src(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class OnlyWhatIsGenuinelyOutstanding(unittest.TestCase):

    def setUp(self):
        whole = src("lambda_src", "auth.py") \
            .split("def _reinvite(", 1)[1].split("\ndef ", 1)[0]
        # Code only, and past the docstring: both describe what the function
        # deliberately does not touch, naming the very things asserted absent.
        body = whole.split('"""', 2)[2]
        self.fn = "\n".join(l for l in body.splitlines()
                             if not l.strip().startswith("#"))

    def test_somebody_who_accepted_is_refused(self):
        self.assertIn('if str(member.get("status") or "") != "invited":', self.fn)
        self.assertIn("already accepted", self.fn)

    def test_and_only_a_role_you_may_manage(self):
        # An administrator may chase a submitter; nobody chases the owner.
        self.assertIn('if not may_manage(org["_role"], member.get("role")):', self.fn)

    def test_the_membership_is_left_exactly_as_it_was(self):
        # This re-sends a message. The role, the groups and the staff id they
        # were given still stand - re-inviting must not re-invent them.
        self.assertNotIn('"role":', self.fn)
        self.assertNotIn("groups", self.fn)


class TheFloorIsEnforcedWhereItMatters(unittest.TestCase):

    def setUp(self):
        self.auth = src("lambda_src", "auth.py")
        self.fn = self.auth.split("def _reinvite(", 1)[1].split("\ndef ", 1)[0]

    def test_forty_eight_hours_is_a_server_rule_not_a_console_one(self):
        self.assertIn("REINVITE_AFTER = 48 * 3600", self.auth)
        self.assertIn("waited < REINVITE_AFTER", self.fn)
        self.assertIn("429", self.fn)

    def test_it_measures_from_the_last_send_not_the_first(self):
        # Otherwise a nudge can be nudged, daily, for ever.
        self.assertIn('last = int(member.get("reinvited_at") or member.get("added_at") or 0)',
                      self.fn)

    def test_the_refusal_says_when_they_may_try_again(self):
        self.assertIn("in about {hours} hours", self.fn)

    def test_the_console_holds_the_same_number(self):
        app = src("..", "PORTAL", "app.html")
        self.assertIn("const REINVITE_AFTER = 48 * 3600;", app)


class NothingIsCountedThatWasNotSent(unittest.TestCase):

    def setUp(self):
        self.fn = src("lambda_src", "auth.py") \
            .split("def _reinvite(", 1)[1].split("\ndef ", 1)[0]

    def test_the_clock_starts_after_the_email_leaves(self):
        # A counter that goes up when nothing left the building starts a
        # 48-hour wait on a message nobody got.
        self.assertLess(self.fn.index("_send_invite("), self.fn.index("reinvited_at = :t"))

    def test_a_failed_send_changes_nothing(self):
        self.assertIn("Nothing was changed.", self.fn)

    def test_and_it_is_written_into_the_audit_log(self):
        self.assertIn('"invitation sent again"', self.fn)


class TheConsoleOffersItWhereTheJobIs(unittest.TestCase):

    def setUp(self):
        self.app = src("..", "PORTAL", "app.html")

    def test_the_button_is_absent_rather_than_disabled_before_48_hours(self):
        # A greyed button on somebody invited this morning invites a hunt for
        # why; the row already says when it went out.
        self.assertIn("if (mayReinvite(person)) {", self.app)

    def test_the_row_says_how_long_it_has_sat(self):
        self.assertIn("sent ${esc(agoOf(person.invitedAt))}", self.app)
        self.assertIn("reminder", self.app)

    def test_there_is_a_way_to_chase_all_of_them(self):
        # Twenty-six Resend buttons and twenty-six scrolls is why they stay
        # outstanding.
        self.assertIn("async function resendAllInvites(", self.app)
        self.assertIn('id="people-chase"', self.app)

    def test_and_it_names_the_number_before_sending(self):
        # "Resend all" hides how many inboxes that is.
        fn = self.app.split("async function resendAllInvites(", 1)[1].split("\n}", 1)[0]
        self.assertIn("window.confirm(", fn)
        self.assertIn("${due.length}", fn)

    def test_a_partial_failure_is_reported_honestly(self):
        fn = self.app.split("async function resendAllInvites(", 1)[1].split("\n}", 1)[0]
        self.assertIn("could not be", fn)
        self.assertIn("unchanged", fn)

    def test_the_bulk_button_is_hidden_when_it_would_do_nothing(self):
        self.assertIn("chase.hidden = !due;", self.app)


if __name__ == "__main__":
    unittest.main()
