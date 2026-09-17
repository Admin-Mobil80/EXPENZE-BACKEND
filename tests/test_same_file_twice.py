"""Sending the same email three times should cost one credit, not three.

Riyad forwarded one message with two PDFs attached, three times. It produced
six claims, six credits and six outcome emails. The second and third sends
should have been answered "you have already sent this" for nothing - which is
what the identical-file gate at intake exists to do, and it had never once
fired: the `#file#` prefix of the fingerprint table had no rows in it at all.

One comparison did it. A successful intake answers **202 Accepted**, and the
cleanup line read:

    if file_print and response.get("statusCode") != 200:
        duplicates.release(file_print, submission_id)

So every success released the fingerprint it had just claimed, microseconds
later, and nothing was ever left in the table for a second copy to collide
with. The line is there for a real case - a receipt refused for want of credit
must not hold a fingerprint, or the sender's next attempt after topping up is
turned away as a duplicate of a claim that never existed - which is why it was
written, and why it was easy to get the wrong way round.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as h:
        return h.read()


class ASuccessfulIntakeKeepsItsFingerprint(unittest.TestCase):

    def setUp(self):
        self.intake = read("lambda_src/intake.py")

    def test_the_accepted_status_counts_as_success(self):
        self.assertIn('response.get("statusCode") not in (200, 202)', self.intake)
        self.assertNotIn('response.get("statusCode") != 200', self.intake)

    def test_intake_really_does_answer_202(self):
        # The whole bug is that this and the check above disagreed. If the
        # success code ever changes, this fails rather than silently
        # re-opening the hole.
        self.assertIn("return _reply(202, {", self.intake)

    def test_a_refused_charge_still_releases_it(self):
        # The case the line exists for: out of credits, no claim created, so
        # the next attempt after topping up must not be called a duplicate.
        self.assertIn("duplicates.release(file_print, submission_id)", self.intake)

    def test_the_hash_is_written_onto_the_claim(self):
        # Used at intake and never stored, so `duplicates.release` on a
        # rejected claim - which reads it off the row - had nothing to release.
        self.assertIn('"receipt_sha256": str(payload.get("receipt_sha256", ""))',
                      self.intake)


class AnAcknowledgementCannotTakeDownTheReceipt(unittest.TestCase):
    """It is a courtesy message. The claim is already created and charged."""

    def setUp(self):
        self.mail = read("lambda_src/mail.py")

    def test_headers_from_the_sender_are_unfolded(self):
        # A References header on a threaded conversation arrives folded across
        # lines, and Python refuses to set a header containing a newline:
        # "Header values may not contain linefeed or carriage return
        # characters". That raised out of the handler after the claims existed.
        block = self.mail.split("def _compose_and_send(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('" ".join(str(msg["Message-ID"]).split())', block)
        refs = block.split('note["References"]', 1)[1][:260]
        self.assertIn(".split()", refs)

    def test_the_subject_is_unfolded_too(self):
        block = self.mail.split("def _compose_and_send(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('" ".join((msg.get("Subject") or "").split())', block)

    def test_composing_is_inside_the_guard_not_only_sending(self):
        fn = self.mail.split("def _reply_on_thread(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_compose_and_send(", fn)
        self.assertIn("except Exception:", fn)
        # The old shape guarded the SES call and nothing else.
        self.assertNotIn("EmailMessage()", fn)


class TheConsoleShowsTheVerdictTheEngineReached(unittest.TestCase):
    """A working copy kept past the thing it was a copy of.

    The console renders a receipt while the agent is still reading it, so
    `sub.type` is empty and the working copy defaults to "meals". It was seeded
    once and never revisited - so when the verdict arrived saying
    `software_subscription`, `isPristine` compared the stale "meals" against it,
    concluded a reviewer had re-typed the claim, and rendered its own local
    recomputation in place of the server's decision.

    A Gamma subscription the engine approved in full showed as Meals &
    entertainment, blocked on a headcount, reimbursable zero.
    """

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function workingCopy(sub) {", 1)[1].split("\n}", 1)[0]

    def test_the_copy_is_stamped_with_what_it_was_made_from(self):
        self.assertIn("function seedOf(sub)", self.app)
        self.assertIn("seed,", self.fn)

    def test_it_is_rebuilt_when_the_claim_changes_underneath(self):
        self.assertIn("edits[sub.id].seed !== seed", self.fn)
        self.assertIn("delete edits[sub.id]", self.fn)

    def test_the_seed_covers_everything_the_copy_derives_from(self):
        seed = self.app.split("function seedOf(sub) {", 1)[1].split("\n}", 1)[0]
        # The expense type and the currency are the whole of what a reviewer
        # can change on a claim. No headcount, no source, no line category:
        # none of them exists any more.
        for field in ("sub.type", "sub.currency"):
            self.assertIn(field, seed)
        for gone in ("i.category", "headcount", "sub.source"):
            self.assertNotIn(gone, seed)

    def test_an_unsaved_edit_is_not_thrown_away_by_a_refresh(self):
        # Only the seed is compared, never the reviewer's own values - so a
        # refresh that changed nothing leaves their work alone.
        self.assertNotIn("w.type !== sub.type", self.fn)


if __name__ == "__main__":
    unittest.main()
