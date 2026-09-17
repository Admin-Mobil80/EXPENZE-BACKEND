"""Every message that goes to a submitter is recorded against their claim.

The console has a panel headed "What the submitter is told". For it to be
worth anything, every path that sends something has to store it - a path that
sends and does not record leaves the panel showing an older message as though
it were current, which is the exact fault it had before: a claim paid last
week still reading "fully approved, no further action is needed".
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def src(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class EverySendIsRecorded(unittest.TestCase):

    def test_no_call_to_send_is_left_unrecorded(self):
        # Counted rather than named, so a path added later has to account for
        # itself. `low_credits` is the one exception: it goes to whoever can
        # top the account up, about the organisation, not about a claim.
        for module in ("auth.py", "auditor_worker.py", "alerts.py"):
            text = src("lambda_src", module)
            sends = len(re.findall(r"notify\.send\(", text))
            records = len(re.findall(r"notify\.record\(", text))
            low = len(re.findall(r'notify\.send\(\s*"low_credits"', text))
            self.assertEqual(sends - low, records,
                             f"{module}: {sends - low} claim notices sent, "
                             f"{records} recorded")

    def test_the_recorder_is_handed_the_result_of_the_send(self):
        auth = src("lambda_src", "auth.py")
        self.assertIn("notify.record(_submissions, submission_id, result)", auth)

    def test_the_worker_records_the_first_message_of_all(self):
        worker = src("lambda_src", "auditor_worker.py")
        self.assertIn('notify.record(_intake, str(row.get("submission_id") or ""), result)',
                      worker)

    def test_the_console_can_read_it_back(self):
        auth = src("lambda_src", "auth.py")
        view = auth.split("def _submission_view(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"last_notice"', view)
        app = src("..", "PORTAL", "app.html")
        self.assertIn("lastNotice: s.last_notice", app)


class OneAuthorOfTheseWords(unittest.TestCase):
    """The console composed its own settlement notice in JavaScript. Two
    implementations of one message drift, and nothing would have caught it."""

    def test_the_console_writes_no_notice_text_of_its_own(self):
        app = src("..", "PORTAL", "app.html")
        fn = app.split("function paintWhatTheyWereTold(", 1)[1].split("\nfunction ", 1)[0]
        # Sentences that belong to notify.py and were being re-typed here.
        for phrase in ("has been reimbursed", "Still owed", "Settled by:"):
            self.assertNotIn(phrase, fn)

    def test_and_notify_still_writes_them(self):
        notify = src("lambda_src", "notify.py")
        settled = notify.split("def settled_notice(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("reimbursed", settled)


if __name__ == "__main__":
    unittest.main()
