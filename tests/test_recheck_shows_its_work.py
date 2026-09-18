"""Re-check looked like nothing was happening, and then changed the buttons.

Pressing Re-check sent the claim back round the agent - a Lambda, a model call,
a few seconds - and the page did not move. The same findings, the same Reject
button, one line of text under the controls. Then, without anybody touching
anything, Reject became Approve and the findings changed underneath.

Two failures in one. There was no statement that work was in progress, so a
reviewer reasonably concluded the button had not registered and pressed it
again; and there was no statement of what came back, so the new verdict
arrived as an unexplained change to the controls rather than as an answer to
what they had just asked.

The wait was `setTimeout(loadRecords, 4000)` - a guess at how long a Lambda
takes, wrong in both directions. Too short and the reviewer watched the old
verdict refresh into the old verdict; too long and they sat in front of a
claim that had finished. `stillReading` is the question that actually has an
answer, and it is the same test the queue already uses, so the page now waits
on the claim rather than on the clock.

While it waits: no decision buttons at all, rather than disabled ones. The
verdict those buttons act on is being recomputed, so Approve pressed a beat
before the answer lands would approve a figure the page has already replaced.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def code_only(js):
    """The source with its prose taken out.

    These assertions are about what the page does, and the comments explaining
    it quote the very words being asserted absent - "no decision buttons at
    all, rather than disabled ones" contains "Approve". A test that passes on
    a comment, or fails on one, is measuring the wrong thing.
    """
    import re
    js = re.sub(r"/\*[\s\S]*?\*/", " ", js)
    return re.sub(r"(?m)^\s*//[^\n]*", " ", js)


class TheClaimIsVisiblyInFlight(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.detail = self.app.split("function renderDetail() {", 1)[1].split(
            "\n}", 1)[0]

    def test_the_page_knows_which_claim_is_being_re_checked(self):
        self.assertIn("let recheckOf = null;", self.app)
        self.assertIn(
            "const rechecking = recheckOf === sub.id || "
            "(!!sub.correctedAt && stillReading(sub));", self.detail)

    def test_the_server_s_own_answer_is_what_ends_the_wait(self):
        # Not a timer. `stillReading` is the same test the review queue uses.
        self.assertIn('const stillReading = (sub) => ["queued", "auditing", '
                      '"pending"].includes(sub.serverStatus);', self.app)

    def test_the_four_second_guess_is_gone(self):
        self.assertNotIn("setTimeout(loadRecords, 4000)", code_only(self.app))

    def test_the_controls_are_not_editable_while_it_runs(self):
        self.assertIn("const frozen = paid || unread || rechecking || "
                      "!reviewingHere();", self.detail)

    def test_the_decision_buttons_are_absent_not_disabled(self):
        # A disabled Approve still describes a verdict; there is no verdict to
        # describe while one is being computed.
        actions = self.detail.split('const abox = $("actions")', 1)[1]
        self.assertIn("if (rechecking) {", actions)
        branch = code_only(actions.split("if (rechecking) {", 1)[1].split(
            "} else if (decision || settlementStage(sub)) {", 1)[0])
        self.assertNotIn("Approve", branch)
        self.assertNotIn("Reject", branch)
        self.assertIn("Re-checking this claim against your policy", branch)

    def test_a_submitter_cannot_withdraw_mid_flight_either(self):
        actions = self.detail.split('const abox = $("actions")', 1)[1]
        self.assertIn("if (!rechecking && isMine(sub)", actions)

    def test_it_says_so_out_loud(self):
        # A spinner alone is decoration; the sentence is what tells somebody
        # the click registered.
        self.assertIn('s.setAttribute("role", "status");', self.detail)
        self.assertIn("The agent is deciding it again.", self.detail)


class AndSaysWhatCameBack(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.watch = self.app.split("async function watchRecheck(", 1)[1].split(
            "\n}", 1)[0]

    def test_the_poll_stops_when_the_claim_is_no_longer_being_read(self):
        self.assertIn("if (sub && stillReading(sub) && !givenUp) continue;",
                      self.watch)
        self.assertIn("recheckOf = null;", self.watch)

    def test_a_second_re_check_takes_over_from_the_first(self):
        # Two loops both calling loadRecords and both writing the result line
        # would race over one message.
        self.assertIn("if (recheckOf !== id) return;", self.watch)

    def test_the_outcome_names_what_is_left_to_decide(self):
        self.assertIn("still need a decision", self.watch)
        self.assertIn("Nothing in the policy stops it now", self.watch)

    def test_a_claim_that_never_comes_back_is_said_rather_than_hidden(self):
        # Waiting silently for ever is the failure this whole change is about.
        self.assertIn("const RECHECK_GIVE_UP = 90 * 1000;", self.app)
        self.assertIn("taking longer than usual", self.watch)
        self.assertIn('msg.className = "msg err";', self.watch)

    def test_the_result_belongs_to_the_claim_it_is_about(self):
        # Otherwise it survives Next and sits over somebody else's claim.
        self.assertIn("answerMsgFor = id;", self.watch)


class TheSpinnerIsStyled(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_the_busy_stamp_has_its_own_colour(self):
        self.assertIn(".stamp.working {", self.app)

    def test_it_respects_reduced_motion(self):
        block = self.app.split("@media (prefers-reduced-motion: reduce) {", 1)[1]
        self.assertIn(".spin { animation:none;", block[:400])


if __name__ == "__main__":
    unittest.main()
