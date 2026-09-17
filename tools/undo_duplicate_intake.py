#!/usr/bin/env python3
"""Undo claims a bug let through, and give back the credits they cost.

On 16 September one email with two PDFs attached was sent three times. The
identical-file gate at intake should have absorbed the second and third sends
for nothing; it could not, because every successful intake released the content
fingerprint it had just claimed - a `!= 200` where the success code is 202. So
six claims were created, six credits spent and six outcome emails sent, where
there should have been two of each.

This closes the four that should never have existed and refunds them. It is a
one-off for a known incident, not a feature: nothing in the product may reject
somebody's claim without a person deciding, and nothing may mint credits.

What it does, per claim
-----------------------
Exactly what `_claim_review(action="rejected")` writes, and nothing it does not:
the review fields, and a release of every fingerprint the claim was holding so
that a genuine resubmission of the same bill is not turned away as a duplicate
of a claim that went nowhere.

Two deliberate differences from the live path:

**No email.** A rejection normally tells the submitter, because they are out of
pocket and the reason is the only thing they can act on. Here the submitter is
the person running this, the claims are ours rather than theirs, and four more
messages about a transaction that already generated six is the opposite of
helpful.

**An audit entry marked as what it is.** The log records the rejection with
this script named as the actor, so a reader a year from now sees a cleanup
rather than four decisions somebody made and cannot remember making.

Usage
-----
    python tools/undo_duplicate_intake.py            # prints what it would do
    python tools/undo_duplicate_intake.py --yes      # does it
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import boto3

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/lambda_src")

import duplicates  # noqa: E402

REGION = "ap-southeast-1"
SUBMISSIONS = "Expenze-Submissions"
ORGS = "Expenze-Orgs"
LEDGER = "Expenze-CreditLedger"
AUDIT = "Expenze-AuditLog"

# The four re-sends. Named one by one rather than found by a rule: a script
# that works out for itself which of somebody's claims to reject is a far more
# dangerous thing to have lying around than a list of four references.
#
# Exp-8 (the invoice, dated the 9th) and Exp-9 (its payment receipt, dated the
# 10th) are the originals and are left alone - they are a real pair for one
# transaction, and which of them to pay is a decision for a person.
REJECT = ["Mobil80-Exp-10", "Mobil80-Exp-11", "Mobil80-Exp-12", "Mobil80-Exp-13"]

REASON = ("Duplicate intake — the same email was received three times and the "
          "identical-file check failed to absorb the repeats. Closed as part "
          "of that fix; the credit was refunded.")

ACTOR = "tools/undo_duplicate_intake.py"

_ddb = boto3.resource("dynamodb", region_name=REGION)


def _find(reference: str) -> dict[str, Any] | None:
    rows = _ddb.Table(SUBMISSIONS).scan(
        FilterExpression="#r = :r",
        ExpressionAttributeNames={"#r": "reference"},
        ExpressionAttributeValues={":r": reference},
    ).get("Items", [])
    return rows[0] if rows else None


def _reject(row: dict[str, Any], now: int) -> None:
    sid = str(row["submission_id"])
    _ddb.Table(SUBMISSIONS).update_item(
        Key={"submission_id": sid},
        UpdateExpression=("SET review_action = :a, review_reason = :r, "
                          "review_by = :b, review_by_name = :n, review_at = :t, "
                          "approved_total = :amt"),
        ExpressionAttributeValues={
            ":a": "rejected", ":r": REASON, ":b": ACTOR,
            ":n": "Expenze (duplicate cleanup)", ":t": now, ":amt": "",
        },
    )
    # Every fingerprint it was holding. A rejected claim was never paid, so it
    # must not stand between the same bill and an honest resubmission.
    for key in (str(row.get("fingerprint") or ""),
                str(row.get("sender_fingerprint") or ""),
                str(row.get("shape_fingerprint") or "")):
        if key:
            duplicates.release(key, sid)
    sha = str(row.get("receipt_sha256") or "")
    if sha:
        duplicates.release(duplicates.file_key(str(row.get("org_id", "")), sha), sid)

    _ddb.Table(AUDIT).put_item(Item={
        "org_id": str(row.get("org_id") or ""),
        "ts": f"{now * 1000:013d}#cleanup",
        "at": now * 1000,
        "action": "claim rejected at review",
        "actor": ACTOR,
        "actor_name": "Expenze (duplicate cleanup)",
        "reference": str(row.get("reference") or ""),
        "submission_id": sid,
        "who": str(row.get("submitted_by") or ""),
        "reason": REASON,
        "detail": "duplicate intake, credit refunded",
        "expires_at": now + 2557 * 86400,
    })


def _refund(org_id: str, count: int, now: int) -> int:
    updated = _ddb.Table(ORGS).update_item(
        Key={"org_id": org_id},
        UpdateExpression="SET credits = if_not_exists(credits, :z) + :c",
        ExpressionAttributeValues={":c": count, ":z": 0},
        ReturnValues="ALL_NEW",
    )["Attributes"]
    balance = int(updated.get("credits") or 0)
    # Through the ledger, like every other balance change. A refund that only
    # moves the number is an unexplained balance a month from now.
    _ddb.Table(LEDGER).put_item(Item={
        "org_id": org_id,
        "ts": now * 1000,
        "delta": count,
        "balance_after": balance,
        "reason": f"Refund: {count} duplicate intakes closed (identical-file "
                  "check was not holding its fingerprints)",
        "by": ACTOR,
    })
    return balance


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true", help="apply the changes")
    args = ap.parse_args()
    now = int(time.time())

    rows, missing = [], []
    for ref in REJECT:
        row = _find(ref)
        (rows if row else missing).append(row or ref)

    for ref in missing:
        print(f"  {ref}: not found — skipping")

    already = [r for r in rows if r.get("review_action")]
    todo = [r for r in rows if not r.get("review_action")]

    for row in already:
        print(f"  {row['reference']}: already {row['review_action']} — skipping")

    print(f"\nWould reject {len(todo)} claim(s) and refund {len(todo)} credit(s):")
    for row in todo:
        print(f"  {row['reference']:<16} {row.get('submitted_by','')}  "
              f"{(row.get('receipt') or {}).get('vendor','')}")

    if not todo:
        print("\nNothing to do.")
        return 0
    if not args.yes:
        print("\nNothing written. Re-run with --yes to apply.")
        return 0

    org_id = str(todo[0].get("org_id") or "")
    for row in todo:
        _reject(row, now)
        print(f"  {row['reference']}: rejected, fingerprints released")
    balance = _refund(org_id, len(todo), now)
    print(f"\n{len(todo)} rejected, {len(todo)} credits refunded. "
          f"Balance now {balance}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
