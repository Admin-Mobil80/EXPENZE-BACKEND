"""Nothing in the product asks the person who sent a receipt anything.

That is the whole of the simplification: the agent decides or a named human
does, and the submitter is told what happened. The risk is not that a question
comes back - the code that asked them is deleted - but that a message still
*sounds* like it opens a conversation. An invitation to reply is as good as a
question when the reply is answered by a stock line and the thing the sender
wanted undone stays in the queue with its credit spent.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def src(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class NoOutboundMessageInvitesAReply(unittest.TestCase):

    def test_the_whatsapp_acknowledgement_ends_where_it_should(self):
        # It invited a reply once - answered by the same stock line every
        # non-receipt gets, with the wrong claim still queued and its credit
        # spent. Then it gave instructions for withdrawing, which is the
        # exception rather than the path: almost everybody sends the right
        # receipt, and the last line of the one message they read is the worst
        # place to spend their attention on a mistake they have not made.
        wa = src("lambda_src", "whatsapp.py")
        body = wa.split("def _received(", 1)[1].split("\ndef ", 1)[0]
        lines = [l for l in body.splitlines() if not l.strip().startswith("#")]
        code = "\n".join(lines)
        self.assertNotIn("Reply to this message", code)
        self.assertNotIn("Withdraw the claim", code)
        self.assertIn("come back ", code)

    def test_and_the_portal_still_offers_the_way_out(self):
        # Nothing tells them about it in the thread any more, so the control
        # itself is the whole of the promise.
        app = src("..", "PORTAL", "app.html")
        self.assertIn('wd.textContent = "Withdraw";', app)
        self.assertIn("isMine(sub) && !mayReview && !isPaid(sub)", app)

    def test_the_outcome_message_asks_for_nothing(self):
        notice = src("lambda_src", "notify.py") \
            .split("def outcome_notice(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("Nothing is needed from you", notice)
        self.assertNotIn("?", notice.split('"""', 2)[2])


class NoScreenSaysAnybodyIsAsked(unittest.TestCase):
    """The words outlive the feature, and are worse than the feature was.

    A label saying somebody will be asked something is a promise the product
    no longer keeps. Left standing, it tells an administrator that a person in
    several groups will sort their own attribution out - so nobody sets it,
    and the claims queue up on a question that is never put to anyone.
    """

    def test_the_people_list_does_not_say_they_are_asked_per_receipt(self):
        app = src("..", "PORTAL", "app.html")
        # The rendered label, not the comment recording what it used to say.
        self.assertNotIn('n.textContent = "asked per receipt"', app)
        self.assertNotIn(".grpnote {", app)

    def test_nor_does_the_queue_say_a_submitter_was_asked(self):
        app = src("..", "PORTAL", "app.html")
        chip = app.split("function groupChip(", 1)[1].split("\n}", 1)[0]
        self.assertNotIn('"state wait"', chip)

    def test_and_the_group_a_bill_names_is_read_off_the_bill(self):
        worker = src("lambda_src", "auditor_worker.py")
        self.assertIn("grouping.group_for(", worker)


class TheWayBackIsDeleted(unittest.TestCase):

    def test_there_is_no_answer_module(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "lambda_src", "answers.py")))

    def test_no_review_action_asks_anything(self):
        auth = src("lambda_src", "auth.py")
        actions = auth.split("REVIEW_ACTIONS = ", 1)[1].split(")", 1)[0]
        self.assertNotIn("queried", actions)

    def test_and_no_notice_kind_carries_a_question(self):
        notify = src("lambda_src", "notify.py")
        kinds = notify.split("NOTICES = {", 1)[1].split("}", 1)[0]
        self.assertNotIn("queried", kinds)


if __name__ == "__main__":
    unittest.main()
