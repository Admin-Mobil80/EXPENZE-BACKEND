"""The record of who decided what about somebody's money.

Expenze already wrote a reviewer's decision onto the claim - `review_action`,
`review_by_name`, `review_at`. That reads like an audit trail and is not one:
there is a single set of those fields and each decision overwrites the last.
Approve a claim and then reject it, and the approval is gone. Reopen it and the
approval is removed outright. "Who approved this before it was refused" had no
answer anywhere in the system.

So the tests here are almost entirely about the properties that make the new
table worth having rather than about its shape:

* entries are appended and nothing rewrites them - including two written in the
  same millisecond, which is the one collision that would silently lose one;
* the log never breaks the decision it describes, because a rejection somebody
  has already been told about must not be lost to a table being unreachable;
* one organisation's decisions cannot be read from another's, on either read
  path - the per-claim index is keyed by submission id alone, so it reaches
  across customers unless something checks;
* a reopen records what it destroyed, which is the specific hole this exists to
  close.

And one test that is not about audit.py at all: that the handler which erases a
claim's approval writes the erasure down first. That is the bug, and a test on
the module alone would pass with nothing calling it.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")

import audit  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class FakeAuditTable:
    """Enough DynamoDB to exercise the real queries, and nothing more."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.broken = False

    def put_item(self, Item):
        if self.broken:
            raise RuntimeError("table unreachable")
        self.rows[(Item["org_id"], Item["ts"])] = dict(Item)

    @staticmethod
    def _wanted(condition):
        attr, value = condition.get_expression()["values"]
        return attr.name, value

    def query(self, KeyConditionExpression, ScanIndexForward=True, Limit=100,
              IndexName=None, ExclusiveStartKey=None):
        if self.broken:
            raise RuntimeError("table unreachable")
        field, value = self._wanted(KeyConditionExpression)
        rows = [dict(r) for r in self.rows.values() if r.get(field) == value]
        rows.sort(key=lambda r: r["ts"], reverse=not ScanIndexForward)
        if ExclusiveStartKey:
            mark = ExclusiveStartKey["ts"]
            rows = [r for r in rows
                    if (r["ts"] < mark if not ScanIndexForward else r["ts"] > mark)]
        page, out = rows[:Limit], {}
        out["Items"] = page
        if len(rows) > Limit:
            out["LastEvaluatedKey"] = {"org_id": page[-1]["org_id"], "ts": page[-1]["ts"]}
        return out


class EntriesAreOnlyEverAppended(unittest.TestCase):
    """The single property the whole table exists for.

    A log that can be rewritten answers no question worth asking. It is worth
    being blunt about what that means in code: there is no update path and no
    delete path in this module, two entries written in the same instant are two
    entries, and the IAM grant does not carry the permissions to do otherwise.
    """

    def setUp(self):
        self.table = FakeAuditTable()
        patch = mock.patch.object(audit, "_table", self.table)
        patch.start()
        self.addCleanup(patch.stop)

    def test_two_decisions_in_one_millisecond_are_both_kept(self):
        # The realistic version of this is a reviewer clearing a queue with the
        # keyboard, or one request that records two things. Keyed on time
        # alone, the second would overwrite the first and the log would be
        # quietly wrong in exactly the case where somebody was working fast.
        with mock.patch.object(audit.time, "time", return_value=1_700_000_000.0):
            audit.record("org-1", "claim approved", "riyad@mobil80.com", reference="Exp-1")
            audit.record("org-1", "claim approved", "riyad@mobil80.com", reference="Exp-2")

        rows, _ = audit.read("org-1")
        self.assertEqual(2, len(rows))
        self.assertEqual({"Exp-1", "Exp-2"}, {r["reference"] for r in rows})

    def test_the_module_offers_no_way_to_change_or_remove_an_entry(self):
        source = open(os.path.join(ROOT, "lambda_src", "audit.py")).read()
        self.assertNotIn("update_item", source)
        self.assertNotIn("delete_item", source)
        self.assertNotIn("batch_writer", source)

    def test_the_lambda_is_not_granted_permission_to_rewrite_one(self):
        # Convention is not enough for this one table. `grant_read_write_data`
        # would hand the function UpdateItem and DeleteItem as well, and then
        # append-only would rest entirely on nobody ever calling them.
        stack = open(os.path.join(ROOT, "expensifyai", "stack.py")).read()
        self.assertIn('audit_table.grant(auth_fn, "dynamodb:PutItem", "dynamodb:Query")',
                      stack)
        self.assertNotIn("audit_table.grant_read_write_data", stack)

    def test_entries_outlive_the_year_they_document(self):
        # Seven years: long enough to cover the financial year and the audit
        # that follows it. A log that expires with the tax year is no use on
        # the day anybody actually asks.
        with mock.patch.object(audit.time, "time", return_value=1_700_000_000.0):
            audit.record("org-1", "claim settled", "riyad@mobil80.com")
        row = next(iter(self.table.rows.values()))
        self.assertGreater(row["expires_at"] - 1_700_000_000, 6 * 365 * 86400)


class TheLogNeverBreaksTheDecision(unittest.TestCase):
    """An audit gap is a real cost. It is a smaller one than this.

    By the time an entry is written the decision has been made, written to the
    claim and - for a rejection - already emailed to the person who is out of
    pocket. Failing the request at that point would tell them one thing and
    leave the system saying another.
    """

    def test_a_write_that_fails_does_not_raise(self):
        table = FakeAuditTable()
        table.broken = True
        with mock.patch.object(audit, "_table", table):
            audit.record("org-1", "claim rejected", "riyad@mobil80.com", reason="duplicate")

    def test_a_read_that_fails_returns_nothing_rather_than_an_error(self):
        table = FakeAuditTable()
        table.broken = True
        with mock.patch.object(audit, "_table", table):
            self.assertEqual(([], ""), audit.read("org-1"))
            self.assertEqual([], audit.history("org-1", "sub-1"))

    def test_nothing_is_written_before_the_table_exists(self):
        # Local runs and tests have no AUDIT_TABLE. The module has to be
        # importable and callable without one rather than failing at import,
        # which would take every other handler in auth.py down with it.
        with mock.patch.object(audit, "_table", None):
            audit.record("org-1", "claim approved", "riyad@mobil80.com")
            self.assertEqual(([], ""), audit.read("org-1"))


class OneCustomersDecisionsStayTheirs(unittest.TestCase):
    """Both read paths, because only one of them is keyed on the organisation."""

    def setUp(self):
        self.table = FakeAuditTable()
        patch = mock.patch.object(audit, "_table", self.table)
        patch.start()
        self.addCleanup(patch.stop)
        audit.record("org-1", "claim approved", "riyad@mobil80.com",
                     submission_id="sub-1", reference="Exp-1")
        audit.record("org-2", "claim approved", "someone@example.com",
                     submission_id="sub-2", reference="Exp-2")

    def test_the_org_log_shows_only_that_org(self):
        rows, _ = audit.read("org-1")
        self.assertEqual(["Exp-1"], [r["reference"] for r in rows])

    def test_a_claims_history_is_checked_against_the_org_as_well(self):
        # The index is partitioned by submission id alone, so this query
        # crosses customers by construction. A submission id is guessable
        # enough that the id must never be sufficient on its own.
        self.assertEqual([], audit.history("org-1", "sub-2"))
        self.assertEqual(1, len(audit.history("org-2", "sub-2")))


class AHistoryReadsForwards(unittest.TestCase):
    """A log is skimmed newest-down; one claim's history is read in order."""

    def setUp(self):
        self.table = FakeAuditTable()
        patch = mock.patch.object(audit, "_table", self.table)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_org_log_is_newest_first_and_a_claim_history_is_oldest_first(self):
        for n, action in enumerate(("claim approved", "sent back for review",
                                    "claim rejected")):
            with mock.patch.object(audit.time, "time",
                                   return_value=1_700_000_000.0 + n):
                audit.record("org-1", action, "riyad@mobil80.com", submission_id="sub-1")

        rows, _ = audit.read("org-1")
        self.assertEqual("claim rejected", rows[0]["action"])
        self.assertEqual(["claim approved", "sent back for review", "claim rejected"],
                         [r["action"] for r in audit.history("org-1", "sub-1")])

    def test_a_long_log_pages_rather_than_stopping(self):
        # A year of decisions is the point of keeping them. A log that silently
        # ends at its page size is one nobody can trust about last March.
        for n in range(25):
            with mock.patch.object(audit.time, "time",
                                   return_value=1_700_000_000.0 + n):
                audit.record("org-1", "claim settled", "riyad@mobil80.com",
                             reference=f"Exp-{n}")

        first, cursor = audit.read("org-1", limit=10)
        self.assertEqual(10, len(first))
        self.assertTrue(cursor)
        second, _ = audit.read("org-1", limit=20, before=cursor)
        seen = [r["reference"] for r in first] + [r["reference"] for r in second]
        self.assertEqual(25, len(set(seen)))


class WhatAnEntryCarries(unittest.TestCase):

    def setUp(self):
        self.table = FakeAuditTable()
        patch = mock.patch.object(audit, "_table", self.table)
        patch.start()
        self.addCleanup(patch.stop)

    def test_empty_fields_are_left_out_entirely(self):
        # Org-level entries - a policy save, a role change - have no claim. An
        # empty string in `submission_id` would be a key value the per-claim
        # index cannot hold, and DynamoDB rejects the whole write for it.
        audit.record("org-1", "policy saved", "riyad@mobil80.com",
                     submission_id="", reference="", detail="Raised the meal cap")
        row = next(iter(self.table.rows.values()))
        self.assertNotIn("submission_id", row)
        self.assertNotIn("reference", row)
        self.assertEqual("Raised the meal cap", row["detail"])

    def test_amounts_stay_numbers_and_prose_is_bounded(self):
        audit.record("org-1", "claim settled", "riyad@mobil80.com",
                     amount=Decimal("7010.00"), claims_withdrawn=3,
                     reason="x" * 2000)
        row = next(iter(self.table.rows.values()))
        self.assertEqual(Decimal("7010.00"), row["amount"])
        self.assertEqual(3, row["claims_withdrawn"])
        self.assertLessEqual(len(row["reason"]), 600)


class TheDecisionsThatUsedToVanish(unittest.TestCase):
    """The hole this was built for, tested where it actually was.

    `_claim_review` handles "reopened" by removing the review fields from the
    claim. That is right for the claim - a claim awaiting a decision and one
    that was never decided are the same state - and it means the approval it
    just erased exists nowhere unless something records it on the way past.
    """

    def setUp(self):
        self.source = open(os.path.join(ROOT, "lambda_src", "auth.py")).read()

    def _handler(self, name: str) -> str:
        start = self.source.index(f"def {name}(")
        rest = self.source[start + 4:]
        end = rest.index("\ndef ")
        return rest[:end]

    def test_a_reopen_records_the_approval_it_destroys(self):
        body = self._handler("_claim_review")
        reopen = body[body.index('if action == "reopened"'):body.index("if action == \"disputed\"")]
        self.assertIn("_logged(", reopen)
        # Not just that something was logged - that what was removed is in it.
        self.assertIn("review_action", reopen.split("_logged(")[1])
        self.assertIn("approved_total", reopen.split("_logged(")[1])

    def test_sending_a_claim_back_records_what_it_overruled(self):
        body = self._handler("_claim_review")
        disputed = body[body.index('if action == "disputed"'):body.index("verdict = item.get")]
        self.assertIn("_logged(", disputed)
        self.assertIn("review_action", disputed.split("_logged(")[1])

    def test_every_decision_on_a_claim_is_logged(self):
        for handler in ("_claim_review", "_claim_outcome", "_claim_retype"):
            self.assertIn("_logged(", self._handler(handler),
                          f"{handler} decides something and records nothing")

    def test_the_role_is_stamped_at_the_time_of_the_decision(self):
        # Roles change. Somebody who approved a claim as a finance executive
        # may be an owner by the time anyone asks, or gone entirely - and a log
        # that reports today's role for yesterday's decision is worse than one
        # reporting none, because it looks authoritative.
        helper = self._handler("_logged")
        self.assertIn('"actor_role": acting.get("role")', helper)

    def test_changes_to_authority_are_logged_and_housekeeping_is_not(self):
        body = self._handler("_member_update")
        self.assertIn('"role changed"', body)
        self.assertIn('"access changed"', body)
        # A corrected spelling of somebody's name is not an audit event. Filling
        # the log with them is how people stop reading it.
        self.assertNotIn('"name changed"', body)

    def test_the_log_is_not_readable_by_the_people_it_is_about(self):
        reader = self._handler("_audit_log")
        self.assertIn('not runs_the_org(acting)', reader)


class TheConsoleShowsIt(unittest.TestCase):
    """A log nobody can see is a table that costs money and settles no argument."""

    def setUp(self):
        self.html = open(os.path.join(ROOT, "..", "PORTAL", "app.html")).read()

    def test_there_is_a_place_to_read_the_whole_log(self):
        self.assertIn("/audit", self.html)
        self.assertIn("renderAuditLog", self.html)

    def test_a_claim_page_shows_its_own_history(self):
        self.assertIn("renderClaimHistory", self.html)

    def test_reasons_are_escaped(self):
        # Every word in this log was typed by a person into a rejection box.
        for call in re.findall(r"renderAuditLog[\s\S]{0,4000}?\n}", self.html):
            self.assertNotIn("innerHTML = `${", call)


class LoggingNeverBreaksTheThingItLogs(unittest.TestCase):
    """The doctrine, enforced where the callers actually touch it.

    `audit.record` swallows its own failures because the thing it describes has
    already happened. That guarantee was worthless while the wrapper in front of
    it could raise first - and it did: `_logged` passed its own defaults
    alongside `**fields`, so every caller naming one of them blew up with
    `TypeError: got multiple values for keyword argument 'who'`.

    Inviting somebody and changing a role both failed on it, *after* the write
    had gone through. The invitation was created, the email sent, and the user
    was shown "Something went wrong. Try again." - so they retried and invited
    the same person twice.
    """

    def setUp(self):
        self.auth = open(os.path.join(ROOT, "lambda_src", "auth.py"),
                         encoding="utf-8").read()
        self.fn = self.auth.split("def _logged(", 1)[1].split("\ndef ", 1)[0]

    def test_a_caller_may_name_any_field_the_wrapper_also_sets(self):
        # Built into a dict and merged, not splatted alongside the defaults.
        self.assertIn("entry.update(fields)", self.fn)
        self.assertNotIn("**fields,", self.fn)

    def test_the_callers_value_wins(self):
        # `who` from _invite is the person invited; from a claim it is the
        # submitter. The caller knows which of those it means.
        self.assertLess(self.fn.index('"who"'), self.fn.index("entry.update(fields)"))

    def test_it_cannot_raise_into_the_handler(self):
        self.assertIn("try:", self.fn)
        self.assertIn("except Exception:", self.fn)

    def test_every_caller_that_names_a_default_is_covered(self):
        # The three that triggered it, kept as a list so a fourth is noticed.
        for handler in ("_invite", "_member_update"):
            body = self.auth.split(f"def {handler}(", 1)[1].split("\ndef ", 1)[0]
            self.assertIn("who=", body, handler)


if __name__ == "__main__":
    unittest.main()
