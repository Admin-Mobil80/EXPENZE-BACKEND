#!/usr/bin/env python3
"""Reconstruct the decisions made before the audit log existed.

The log started empty. Every decision taken before ``Expenze-AuditLog`` shipped
lives only as fields on the claim - ``review_action``, ``review_by_name``,
``review_at``, ``outcome``, ``pulled_by_name`` - and those are exactly the
fields the log exists because they are not a record. This reads what survives
of them and writes it into the log once.

Honest about what it is
-----------------------
Every row this writes carries ``reconstructed: true`` and the time it was
written, and the console prints "reconstructed from the claim" beside it. That
is the whole reason this is a separate script rather than a call into
``audit.py``: the production path cannot backdate an entry, by construction -
``record()`` stamps its own clock - and an admin reconstructing history must
not be able to produce rows indistinguishable from ones written at the time.

What it cannot recover, it does not invent:

* **Only the last decision on each claim survives.** A claim approved and then
  rejected has one set of review fields, and the approval is gone. This writes
  one entry per surviving decision, not a history - the history begins with the
  live log.
* **Two decisions are recoverable on one claim** where both were kept in
  different fields: ``pulled_*`` (sent back for review) and ``review_*`` sit
  side by side, so a claim overruled and then queried yields both, in the order
  their timestamps say.
* **Reasons and amounts come across where they were stored.** Where the field
  is absent it is left out rather than filled with a plausible default.
* **The actor's email is often not recoverable** - the claim stores
  ``review_by_name`` and sometimes not ``review_by``. The name is written and
  the actor left blank rather than guessed from the name.

Re-running is safe. Each reconstructed entry has a key derived from the claim
and the decision rather than from the clock, so a second run rewrites the same
rows instead of producing a second copy of everybody's history.

Usage
-----
    python tools/backfill_audit_log.py            # prints what it would write
    python tools/backfill_audit_log.py --yes      # writes it
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import boto3

REGION = "ap-southeast-1"
SUBMISSIONS = "Expenze-Submissions"
AUDIT = "Expenze-AuditLog"

RETENTION_DAYS = 2557          # seven years, as audit.py

# The three decisions that left a trace on the claim, and what to call them in
# the log. The names match what the live path writes, so a reconstructed
# rejection and a real one read as the same kind of thing - which they are.
#
# Each is (prefix, fields on the claim, the action's name).
SHAPES = [
    ("pull", {"at": "pulled_at", "by": "pulled_by", "name": "pulled_by_name",
              "reason": "pulled_reason"}, None),
    ("review", {"at": "review_at", "by": "review_by", "name": "review_by_name",
                "reason": "review_reason", "action": "review_action",
                "amount": "approved_total"}, None),
    ("outcome", {"at": "outcome_at", "by": "outcome_by", "name": "outcome_by_name",
                 "reason": "outcome_reason", "action": "outcome",
                 "amount": "outcome_paid"}, None),
]

# How a stored action value reads in the log. Unknown values are passed through
# rather than dropped: a value nobody anticipated is still what happened.
# No "queried": a reviewer can no longer ask a submitter anything, so the live
# path never writes that action and a reconstruction must not invent it. Any
# claim in the old data that carries it is passed through as `claim queried`
# by the fallback below - honest about what happened, in a vocabulary the log
# no longer grows.
ACTIONS = {
    "approved": "claim approved at review",
    "rejected": "claim rejected at review",
    "withdrawn": "claim withdrawn by submitter",
    "settled": "claim settled",
}


def _scan(table) -> list[dict[str, Any]]:
    rows, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        rows.extend(page.get("Items", []))
        nxt = page.get("LastEvaluatedKey")
        if not nxt:
            break
        kwargs["ExclusiveStartKey"] = nxt
    return rows


def _entries_for(row: dict[str, Any], written_at: int) -> list[dict[str, Any]]:
    """Every decision still legible on one claim, oldest first."""
    org_id = str(row.get("org_id") or "")
    submission_id = str(row.get("submission_id") or "")
    if not org_id or not submission_id:
        return []

    verdict = row.get("verdict") or {}
    out = []
    for prefix, fields, _ in SHAPES:
        when = row.get(fields["at"])
        if not when:
            continue
        at_ms = int(when) * 1000

        if prefix == "pull":
            # `pulled_*` has no action field: the flag is the decision.
            action = "sent back for review"
        else:
            stored = str(row.get(fields.get("action", "")) or "").strip().lower()
            if not stored:
                continue
            action = ACTIONS.get(stored, f"claim {stored}")

        entry: dict[str, Any] = {
            "org_id": org_id,
            # Derived from the claim and the decision, never from the clock -
            # so running this twice rewrites these rows rather than adding a
            # second copy of everybody's history.
            "ts": f"{at_ms:013d}#bf-{prefix}",
            "at": at_ms,
            "action": action,
            "actor": str(row.get(fields.get("by", "")) or ""),
            "actor_name": str(row.get(fields["name"]) or ""),
            "reference": str(row.get("reference") or ""),
            "submission_id": submission_id,
            "who": str(row.get("submitted_by") or ""),
            "currency": str(verdict.get("currency") or ""),
            # The two fields that make this row honest about itself.
            "reconstructed": True,
            "reconstructed_at": written_at,
            "expires_at": int(at_ms / 1000) + RETENTION_DAYS * 86400,
        }
        reason = str(row.get(fields.get("reason", "")) or "")
        if reason:
            entry["reason"] = reason[:600]
        amount = str(row.get(fields.get("amount", "")) or "")
        if amount and amount != "0":
            entry["amount"] = amount

        # The role is not recoverable. It was never stored on the claim, and
        # today's role is not the one they held then - so it is absent rather
        # than wrong, which is what `actor_role` missing means to the console.
        out.append({k: v for k, v in entry.items() if v not in (None, "")})

    out.sort(key=lambda e: e["at"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true",
                    help="write the entries. Without it, nothing is written.")
    args = ap.parse_args()

    ddb = boto3.resource("dynamodb", region_name=REGION)
    claims = _scan(ddb.Table(SUBMISSIONS))
    written_at = int(time.time() * 1000)

    entries = []
    for row in claims:
        entries.extend(_entries_for(row, written_at))
    entries.sort(key=lambda e: e["at"])

    if not entries:
        print(f"{len(claims)} claims, no decisions recorded on any of them. "
              "Nothing to reconstruct.")
        return 0

    print(f"{len(claims)} claims hold {len(entries)} recoverable decisions:\n")
    for e in entries:
        stamp = time.strftime("%d %b %Y %H:%M", time.gmtime(e["at"] / 1000))
        print(f"  {stamp}  {e.get('reference', '?'):<16} {e['action']:<28} "
              f"{e.get('actor_name') or 'unknown'}"
              + (f"  “{e['reason'][:60]}”" if e.get("reason") else ""))

    if not args.yes:
        print(f"\nNothing written. Re-run with --yes to write these "
              f"{len(entries)} entries.")
        return 0

    table = ddb.Table(AUDIT)
    for e in entries:
        table.put_item(Item=e)
    print(f"\n{len(entries)} entries written, each marked as reconstructed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
