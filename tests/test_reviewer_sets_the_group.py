"""A reviewer can set the cost centre, at review and not only at settlement.

It was settable on the settlement form alone, which is the wrong moment twice
over. A claim can be held *on* the group - somebody in several groups, on a
bill that names none - so the person unblocking it has to be able to answer
it; and a group the agent inferred from the paper is correctable only by
whoever reads the bill, who is the reviewer, not the person paying weeks later.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def src(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TheControlIsOnTheClaimPage(unittest.TestCase):

    def setUp(self):
        self.app = src("..", "PORTAL", "app.html")

    def test_it_sits_with_the_other_two_things_a_reviewer_can_change(self):
        controls = self.app.split('<div class="controls">', 1)[1].split(
            '<p class="hint" id="controls-hint">', 1)[0]
        for field in ('id="claim-group"', 'id="etype"', 'id="ccy"'):
            self.assertIn(field, controls, field)

    def test_widest_scope_first(self):
        # Group, then expense type, then currency. Expense type used to lead,
        # on the argument that it selects the cap the claim is measured
        # against - true, and not the order the questions get asked in. Whose
        # budget this comes out of is the first thing a reviewer settles, and
        # the type is how that budget reports it.
        controls = self.app.split('<div class="controls">', 1)[1].split(
            '<p class="hint" id="controls-hint">', 1)[0]
        self.assertLess(controls.index('id="claim-group"'), controls.index('id="etype"'))
        self.assertLess(controls.index('id="etype"'), controls.index('id="ccy"'))

    def test_it_is_hidden_for_an_organisation_with_no_groups(self):
        # An empty dropdown is a question about nothing.
        self.assertIn("gfield.hidden = !groups.length;", self.app)

    def test_and_it_is_the_only_one(self):
        """The settlement form carried a second, and it saved nothing.

        It wrote the choice into the working copy and passed a *label* along
        with the notice, so the cost centre on the record was unchanged. Once
        a settlement started being refused for want of a group, the button
        that opened that form stopped being drawn - so finance had one control
        that did not save and another they could not reach, on the one screen
        where the field had become theirs to fill in.
        """
        self.assertNotIn('<select id="sf-group">', self.app)
        self.assertNotIn('td.querySelector("#sf-group")', self.app)
        # This one writes through the endpoint that records it.
        self.assertIn("if (sub.reference) bits.push(esc(sub.reference));", self.app)
        self.assertIn('id="claim-group"', self.app)

    def test_it_freezes_with_every_other_control(self):
        # One rule, not its own. A settled claim must not offer an editable
        # anything.
        # The group is on the same rule as the expense type now: both say how
        # a claim is filed, filing is finance's, and the settlement form's
        # duplicate has gone.
        self.assertIn('["etype", "claim-group"].forEach', self.app)

    def test_changing_it_counts_as_a_change_worth_saving(self):
        changed = self.app.split("const changed =", 1)[1].split(";", 1)[0]
        self.assertIn("w.group", changed)

    def test_it_says_what_tagged_the_claim(self):
        # An attribution nobody can account for is one nobody corrects with
        # any confidence.
        self.assertIn("The bill is made out to this group's tax ID.", self.app)
        self.assertIn("The bill is made out to this group by name.", self.app)


class TheServerStoresIt(unittest.TestCase):

    def setUp(self):
        self.retype = src("lambda_src", "auth.py") \
            .split("def _claim_retype(", 1)[1].split("\ndef ", 1)[0]

    def test_clearing_a_group_is_a_change_and_not_a_no_op(self):
        # Absent means the reviewer never touched the control; empty means
        # they cleared one the agent got wrong. Collapsing the two makes a
        # wrong inference uncorrectable.
        self.assertIn('group_given = "group_id" in body', self.retype)
        self.assertIn("if group_given:", self.retype)

    def test_an_unknown_group_is_refused(self):
        self.assertIn("Choose one of your configured groups.", self.retype)

    def test_a_reviewer_s_answer_closes_the_question(self):
        # The auditor only consults the bill while the question is open, so a
        # reviewer's answer must not be overruled on the next re-audit.
        self.assertIn('"set_by_reviewer" if group_id else "unset"', self.retype)

    def test_it_is_written_into_the_audit_log(self):
        self.assertIn("group {item.get('group_id') or 'unset'}", self.retype)

    def test_a_request_changing_only_the_group_is_accepted(self):
        self.assertIn("if not expense_type and not currency and not group_given:",
                      self.retype)


class NobodyIsAskedWhichGroup(unittest.TestCase):

    def test_the_queue_no_longer_says_a_submitter_was_asked(self):
        app = src("..", "PORTAL", "app.html")
        chip = app.split("function groupChip(", 1)[1].split("\n}", 1)[0]
        # What it returns, not the comment above it explaining what it stopped
        # returning - which names the phrase it removed.
        returned = " ".join(re.findall(r"return (.+);", chip))
        self.assertNotIn("awaiting their reply", returned)
        self.assertIn("not set", returned)

    def test_an_unresolved_group_still_brings_the_claim_to_a_person(self):
        # It no longer stops the approval - that is asked at settlement now -
        # but it is still why the claim is in front of somebody, and the
        # finding says what it actually costs rather than what it blocks.
        worker = src("lambda_src", "auditor_worker.py")
        self.assertIn('"code": "group_not_set"', worker)
        self.assertIn("it cannot be paid without one", worker)
        self.assertNotIn("Set it before approving.", worker)


class PickingAGroupIsNotTheSameAsRecordingOne(unittest.TestCase):
    """Approve was offered on a claim whose group existed only on the screen.

    Mobil80-Exp-44 sat at `group_id: ""` with `group_status: "ask"`. The
    reviewer picked the cost centre from the dropdown, which writes it to the
    working copy and nowhere else, and three things then went wrong at once:
    the finding about the missing group cleared, because `groupOf` reads the
    working copy first; no Save button appeared, because the test for a change
    compared `w.group` against `groupOf(sub).id`, which is `w.group`; and
    Approve uncovered, because `isPristine` never looked at the group at all.

    So the console asked for a cost centre, accepted one, gave no way to record
    it, and offered a button the server refused three times. The claim never
    moved to Pending settlement, and it was right not to: nothing about it had
    changed.
    """

    def setUp(self):
        self.app = src("..", "PORTAL", "app.html")

    def test_an_unsaved_group_withholds_approve(self):
        # The gate that uncovers Approve has to see the group, or a reviewer
        # who has answered the question on screen is handed a button the
        # server will refuse for a question it still considers open.
        pristine = self.app.split("const isPristine = (sub,w) =>", 1)[1] \
                           .split(";", 1)[0]
        self.assertIn("sub.groupId", pristine)

    def test_and_it_is_the_record_that_is_compared_not_the_screen(self):
        # `groupOf` answers "what does this page show", which on an unsaved
        # pick is the reviewer's own choice. Comparing against it asks whether
        # the choice equals itself.
        for expr in (self.app.split("const isPristine = (sub,w) =>", 1)[1].split(";", 1)[0],
                     self.app.split("const changed =", 1)[1].split(";", 1)[0]):
            self.assertNotIn("groupOf(sub)", expr)
            self.assertIn("sub.groupId", expr)

    def test_a_group_left_alone_is_not_an_unsaved_change(self):
        # `w.group` is absent until somebody touches the control, so absence
        # is the test. Reading the value instead makes every claim that has a
        # group look edited, and parks a Save button on all of them.
        for expr in (self.app.split("const isPristine = (sub,w) =>", 1)[1].split(";", 1)[0],
                     self.app.split("const changed =", 1)[1].split(";", 1)[0]):
            self.assertIn('"group" in w', expr)

    def test_clearing_a_group_still_counts_as_a_change(self):
        # "" is a real answer - a reviewer removing a tag the agent got wrong -
        # and a truthiness test would drop it silently.
        pristine = self.app.split("const isPristine = (sub,w) =>", 1)[1].split(";", 1)[0]
        self.assertNotIn("!w.group", pristine)

    def test_the_working_copy_is_dropped_when_the_record_s_group_moves(self):
        # Otherwise a pick made here outlives the answer somebody else saved.
        seed = self.app.split("function seedOf(sub) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("sub.groupId", seed)

    def test_the_unsaved_notice_names_the_group(self):
        # "Unsaved change. Save it before approving." makes a reviewer hunt
        # for what they touched. The other two changes are named; this one was
        # not, so the one change that had no Save button was also the one
        # change the notice would not identify.
        pending = self.app.split("const pending = [", 1)[1].split("];", 1)[0]
        self.assertIn("Group \u2192", pending)

    def test_the_missing_group_is_reported_once(self):
        # The agent raises `group_not_set` itself and it prints with the rest
        # of the findings, so the console adding its own row unconditionally
        # put the same requirement on screen twice in two wordings.
        guard = self.app.split("const needsGroup = !decision && !stage", 1)[1] \
                        .split("{", 1)[0]
        self.assertIn('v.code === "group_not_set"', guard)


if __name__ == "__main__":
    unittest.main()


class FinanceCanSetTheGroupTheyAreBlockedOn(unittest.TestCase):
    """Madhusudhan could not set a cost centre anywhere on the claim page.

    The settlement is refused without one - correctly, since a payment out of
    a budget nobody named is the thing that rule exists to stop. But the
    refusal told him to "set a group above", and above there was nothing: the
    header's control hid itself whenever the payment form *could* be drawn,
    and the payment form was not being drawn, because the settlement was
    refused for want of a group.

    Two controls, one hidden and one unreachable, over a field that had just
    become his to fill in. The form's copy would not have helped either - it
    wrote to the working copy and passed a label with the notice, and left the
    record untouched.
    """

    def setUp(self):
        self.app = src("..", "PORTAL", "app.html")

    def test_the_header_control_no_longer_hides_itself(self):
        self.assertIn("gfield.hidden = !groups.length;", self.app)
        self.assertNotIn("const payingHere =", self.app)

    def test_finance_can_edit_it(self):
        # On the looser rule, with the expense type. Both say how a claim is
        # filed; neither prices it.
        self.assertIn('["etype", "claim-group"].forEach', self.app)
        self.assertIn("const classFrozen = busy || !(reviewingHere() || settlingHere());",
                      self.app)

    def test_and_what_they_set_is_recorded(self):
        # Through `/claim/retype`, which is the only path that writes the
        # field. The settlement form's version wrote nothing.
        save = self.app.split("async function saveAnswers(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("const chosenGroup = gs && !gs.disabled ? gs.value : null;", save)
        self.assertIn("group_id: chosenGroup", save)

    def test_the_refusal_points_at_a_control_that_exists(self):
        self.assertIn("No cost centre is set. Set a group above", self.app)


class ASettlementNoticeBelongsToOneClaim(unittest.TestCase):
    """"Settled in full. Manjunath K J was notified" sat on Rehaan's claim.

    Above a Reject button and an amount still owed. It was true when written,
    about a different claim and a different person, and nothing took it down -
    so every claim opened afterwards carried somebody else's receipt for money
    that had gone to somebody else.
    """

    def setUp(self):
        self.app = src("..", "PORTAL", "app.html")

    def test_it_is_stamped_with_the_claim_it_is_about(self):
        fn = self.app.split("function payMsg(text, good) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("payMsgFor =", fn)

    def test_and_taken_down_when_the_reader_moves_on(self):
        self.assertIn("if (payMsgFor && payMsgFor !== sub.id) {", self.app)

    def test_it_survives_the_render_that_follows_its_own_click(self):
        # Cleared on the way in rather than on the way out, the same as the
        # save confirmation beside it: a reader's own result has to outlive
        # the re-render their click causes, and only a different claim
        # invalidates it.
        clear = self.app.split("if (payMsgFor && payMsgFor !== sub.id) {", 1)[1] \
                        .split("}", 1)[0]
        self.assertIn('$("claim-pay-msg")', clear)
        self.assertIn("payMsgFor = null;", self.app)
