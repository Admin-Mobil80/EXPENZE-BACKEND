"""Once a claim reaches a person, the person decides it. Nothing else does.

Re-check is gone, and with it the whole idea it stood for: that a reviewer's
correction should send the claim back round the policy engine to be decided
again. It was a plausible design and it was wrong at the seam where the two
meet.

What it produced in practice:

* A courier bill re-checked four times, each time returning "No enabled rule
  covers expense type 'courier'" - because the re-audit path never loaded the
  organisation's policy and fell back to the built-in set, where `courier` does
  not exist. Four model calls, four identical wrong answers, and a reviewer
  with no way to tell the product was arguing with itself.
* Before that, the reverse: the re-audit found nothing wrong, so the claim read
  as cleared-by-the-agent, left the queue and landed in Pending settlement
  without anybody approving it. The correction had become the approval.
* And a write that woke the stream that wrote the row that woke the stream:
  576 attempts in twenty minutes.

Each was patched. The shape kept producing them, because it put a second
decider between a reviewer's answer and their decision.

So: a claim that lands on a human is decided by that human. They read the bill,
the findings and the figures, and they approve or reject. Setting the expense
type, the currency and the group records facts the agent could not supply -
what the claim will be reported under, what it is denominated in, which cost
centre pays it - and changes nothing else.

Which makes the type mandatory, and that is the other half of this file. Every
report groups by expense type, so a payment filed under nothing is a line in
the accounts nobody can explain. `not_covered` is the agent's honest "I cannot
tell", not an expense type, and it cannot be approved.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def code_only(js):
    """The source with its prose removed, so a comment cannot pass a test."""
    js = re.sub(r"/\*[\s\S]*?\*/", " ", js)
    return re.sub(r"(?m)^\s*//[^\n]*", " ", js)


class NothingReDecidesAClaimBehindTheReviewer(unittest.TestCase):

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.worker = read("lambda_src/auditor_worker.py")
        self.handler = read("lambda_src/handler.py")
        self.retype = self.auth.split("def _claim_retype(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_saving_an_answer_does_not_requeue_the_claim(self):
        # The status write is what woke the worker. Without it nothing else
        # has to be stopped.
        self.assertNotIn('"#s = :queued"', self.retype)
        self.assertNotIn('":queued": "queued"', self.retype)

    def test_it_still_writes_what_the_reviewer_answered(self):
        for field in ("answered_expense_type", "answered_currency", "group_id",
                      "group_status", "corrected_by", "corrected_at"):
            self.assertIn(field, self.retype)

    def test_it_still_refuses_a_claim_that_is_being_read(self):
        # The condition stays: an answer written over a claim mid-audit would
        # be overwritten by the audit that lands after it.
        self.assertIn('ConditionExpression="#s = :audited"', self.retype)

    def test_it_no_longer_claims_to_have_requeued_anything(self):
        self.assertIn('"status": "saved"', self.retype)
        self.assertNotIn('"status": "requeued"', self.retype)

    def test_the_worker_has_no_re_audit_branch(self):
        self.assertNotIn("_reaudit(", code_only(self.worker))
        self.assertNotIn("_abandon_reaudit", code_only(self.worker))

    def test_the_engine_has_no_re_audit_entry_point(self):
        self.assertNotIn("def reaudit(", self.handler)

    def test_the_first_audit_is_untouched(self):
        # Removing the second pass must not disturb the one that reads the
        # bill. It still loads the organisation's own policy.
        audit = self.handler.split("\ndef audit(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("rules = policy.rules_for(org)", audit)
        self.assertIn("_run_policy(", audit)


class EveryApprovedClaimCarriesAnExpenseType(unittest.TestCase):

    def setUp(self):
        self.auth = read("lambda_src/auth.py")
        self.review = self.auth.split("def _claim_review(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_the_gate_asks_about_the_claim_not_the_agents_finding(self):
        # `blocks_on(verdict, "no_rule_for_expense_type")` asked whether the
        # agent had objected, which is a different question and got both
        # answers wrong: a verdict older than the rule that now covers it was
        # refused with its type already right, and a claim tagged
        # `not_covered` without a blocking finding went through untagged.
        self.assertNotIn('policy.blocks_on(verdict, "no_rule_for_expense_type")',
                         self.review)
        self.assertIn('chosen = str(item.get("answered_expense_type")',
                      self.review)
        self.assertIn('or verdict.get("expense_type") or "")', self.review)

    def test_it_is_checked_against_the_enabled_types_in_the_policy(self):
        self.assertIn("known = set(policy.expense_type_ids(", self.review)
        self.assertIn("if chosen not in known:", self.review)

    def test_not_covered_cannot_be_approved(self):
        # It is the agent saying it cannot tell, which is exactly the case a
        # person is meant to answer. `expense_type_ids` lists configured
        # enabled types and never includes it.
        ids = read("lambda_src/policy.py").split(
            "def expense_type_ids(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('t.get("enabled", True)', ids)
        self.assertNotIn("not_covered", ids)

    def test_the_refusal_says_why_it_matters(self):
        self.assertIn("Set an expense type first. Every claim is reported ",
                      self.review)

    def test_rejecting_needs_no_type(self):
        # Refusing a claim attributes nothing to anything.
        gate = self.review.split('if action == "approved":', 1)[1].split(
            "\n\n", 1)[0]
        self.assertIn('action == "approved"',
                      self.review.split("known = set(", 1)[0][-900:])
        self.assertNotIn("rejected", gate)

    def test_a_group_is_still_required_too(self):
        self.assertIn("Set a group first.", self.review)


class TheConsoleAsksTheSameQuestion(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.detail = self.app.split("function renderDetail() {", 1)[1].split(
            "\nfunction ", 1)[0]

    def test_approve_appears_only_with_a_configured_type_on_the_claim(self):
        self.assertIn(
            "const typeOk = rules.types.some(t => t.enabled && t.id === w.type);",
            self.detail)
        self.assertIn("const choices = (!typeOk || unsaved || needsGroup)",
                      self.detail)

    def test_reject_is_always_offered(self):
        block = self.detail.split("const choices = (!typeOk || unsaved || needsGroup)",
                                  1)[1].split(";", 1)[0]
        self.assertEqual(2, block.count('"Rejected"'),
                         "both arms of the choice offer Reject")

    def test_the_stored_finding_no_longer_gates_anything(self):
        # It is the agent's record of its own reading, not a question about
        # whether this claim can be paid.
        self.assertNotIn(
            'const untagged = res.violations.some(v => v.code === "no_rule_for_expense_type");',
            self.detail)

    def test_the_message_states_the_requirement(self):
        self.assertIn("Every claim is reported under one, so this ", self.detail)


class TheButtonSavesAndStops(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("async function saveAnswers() {", 1)[1].split(
            "\n}", 1)[0]

    def test_there_is_no_re_check_anywhere(self):
        page = code_only(self.app)
        for gone in ("Re-check", "saveAndRecheck", "watchRecheck", "recheckOf",
                     "RECHECK_EVERY", "RECHECK_GIVE_UP"):
            self.assertNotIn(gone, page, gone)

    def test_the_button_says_what_it_does(self):
        self.assertIn('save.textContent = savingOf === sub.id ? "Saving…" : "Save";',
                      self.app)

    def test_it_appears_only_when_something_has_changed(self):
        # It used to appear on an unchanged claim too, as the way to ask for a
        # re-run. There is nothing to re-run.
        self.assertIn("if (unsaved) {", self.app)

    def test_it_posts_the_three_answers(self):
        self.assertIn("submission_id: sub.id, expense_type: chosen, currency: chosenCcy,",
                      self.fn)
        self.assertIn("group_id: chosenGroup", self.fn)

    def test_it_reloads_and_says_what_was_saved(self):
        self.assertIn("await loadRecords();", self.fn)
        self.assertIn("Approve or reject it when you are ready.", self.fn)

    def test_the_confirmation_no_longer_promises_an_automatic_release(self):
        # "It is re-checked against the rules for that type, and it may clear
        # for payment without further review" described the behaviour that has
        # just been removed - and was the behaviour nobody wanted.
        self.assertNotIn("may clear for payment without further review",
                         code_only(self.app))
        self.assertIn("the claim stays", self.fn)

    def test_one_save_at_a_time(self):
        self.assertIn("if (!sub || savingOf) return;", self.fn)
        self.assertIn("savingOf = sub.id;", self.fn)
        self.assertIn("savingOf = null;", self.fn)


if __name__ == "__main__":
    unittest.main()
