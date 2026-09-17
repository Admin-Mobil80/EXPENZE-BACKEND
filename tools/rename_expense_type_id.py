#!/usr/bin/env python3
"""Give an expense type a proper id, and move the claims that point at the old one.

Ids used to be assigned at creation and never revisited - `expense_type`,
`expense_type_2` - so a type named Utilities carried the id `expense_type` for
ever. The console creates them from the name now, which fixes every type made
from here on and does nothing for the ones already saved.

The id is not cosmetic. It is written onto every claim of that type, it is what
a rule is matched by, and it shows up in findings a person reads: "No enabled
rule covers expense type 'expense_type'" reads as a fault in the product rather
than as a policy that needs a line adding.

Why this is a script and not a button
-------------------------------------
Renaming an id is a migration, not an edit. The rule and every claim that
points at it have to move together, and the console cannot do that safely - it
would need to rewrite claims it is not otherwise allowed to touch, in a loop it
cannot make atomic. Doing it deliberately, from a list of exactly which claims
moved, is the honest shape.

What it does, in order
----------------------
1. Renames the id on the org's policy, keeping everything else about the type.
2. Repoints every claim whose stored verdict names the old id, and the
   `answered_expense_type` a reviewer may have set.
3. Records both in the audit log, because a policy change and a change to a
   decided claim are exactly the things that log exists for.

Claims are repointed rather than re-audited. The verdict stays the verdict that
was reached; only the name of the type it was reached under changes, which is
the definition of a rename. Anything genuinely needing a fresh decision is a
reviewer pressing Re-check, not this.

Usage
-----
    python tools/rename_expense_type_id.py OLD NEW          # shows the plan
    python tools/rename_expense_type_id.py OLD NEW --yes    # applies it
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from typing import Any

import boto3

REGION = "ap-southeast-1"
ORGS = "Expenze-Orgs"
SUBMISSIONS = "Expenze-Submissions"
AUDIT = "Expenze-AuditLog"
ACTOR = "tools/rename_expense_type_id.py"

VALID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

_ddb = boto3.resource("dynamodb", region_name=REGION)


def _orgs_with(old: str) -> list[dict[str, Any]]:
    return [o for o in _ddb.Table(ORGS).scan().get("Items", [])
            if any(str(t.get("id")) == old
                   for t in ((o.get("rules") or {}).get("expense_types") or []))]


def _claims_with(org_id: str, old: str) -> list[dict[str, Any]]:
    """Every claim that still names the old id anywhere it is stored.

    Three places, not two. `receipt.expense_type` is what the model answered
    and what a re-audit reads back, so a claim missing from this list keeps
    the old id in the one field the policy engine is handed on the next
    Re-check - and the reviewer presses the button and sees the same "no
    enabled rule covers expense type 'expense_type'" it was meant to clear.
    """
    rows = _ddb.Table(SUBMISSIONS).scan().get("Items", [])
    return [r for r in rows
            if r.get("org_id") == org_id
            and (str((r.get("verdict") or {}).get("expense_type") or "") == old
                 or str((r.get("receipt") or {}).get("expense_type") or "") == old
                 or str(r.get("answered_expense_type") or "") == old)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    if not VALID.match(args.new):
        print(f"{args.new!r} is not a usable id: lower case, digits and "
              "underscores, starting with a letter.")
        return 1

    orgs = _orgs_with(args.old)
    if not orgs:
        print(f"No organisation has an expense type with the id {args.old!r}.")
        return 0

    now = int(time.time())
    for org in orgs:
        org_id = str(org["org_id"])
        types = list((org.get("rules") or {}).get("expense_types") or [])
        if any(str(t.get("id")) == args.new for t in types):
            print(f"{org_id}: an expense type already uses the id {args.new!r}. "
                  "Pick another.")
            return 1

        label = next(str(t.get("label") or "") for t in types
                     if str(t.get("id")) == args.old)
        claims = _claims_with(org_id, args.old)

        print(f"\n{org.get('name')} ({org_id})")
        print(f"  {label}: {args.old}  ->  {args.new}")
        print(f"  {len(claims)} claim(s) repointed:")
        for c in claims:
            print(f"    {c.get('reference')}  {c.get('submitted_by')}")

        if not args.yes:
            continue

        for t in types:
            if str(t.get("id")) == args.old:
                t["id"] = args.new
        rules = dict(org.get("rules") or {})
        rules["expense_types"] = types
        # The version moves, because this is a policy the engine will read
        # differently from the one before it.
        rules["version"] = int(rules.get("version") or 1) + 1
        _ddb.Table(ORGS).update_item(
            Key={"org_id": org_id},
            UpdateExpression="SET #r = :r, rules_updated_by = :u, rules_updated_at = :t",
            ExpressionAttributeNames={"#r": "rules"},
            ExpressionAttributeValues={":r": rules, ":u": ACTOR, ":t": now},
        )

        for c in claims:
            sid = str(c["submission_id"])
            verdict = dict(c.get("verdict") or {})
            if str(verdict.get("expense_type") or "") == args.old:
                verdict["expense_type"] = args.new
            # The label is a snapshot of what the type was called when the
            # claim was decided, so renaming an id does not touch it - except
            # where it *is* the old id, which is not a name anybody chose. It
            # is what gets stored when a type never had a label, and leaving
            # it prints a raw id on the claim page under a dropdown that has
            # been showing the proper name since the rename.
            if str(verdict.get("expense_type_label") or "") == args.old:
                verdict["expense_type_label"] = label or args.new

            sets, vals = ["verdict = :v"], {":v": verdict}

            # What the model answered, and what a re-audit is handed.
            receipt = dict(c.get("receipt") or {})
            if str(receipt.get("expense_type") or "") == args.old:
                receipt["expense_type"] = args.new
                sets.append("receipt = :rc")
                vals[":rc"] = receipt

            if str(c.get("answered_expense_type") or "") == args.old:
                sets.append("answered_expense_type = :a")
                vals[":a"] = args.new
            _ddb.Table(SUBMISSIONS).update_item(
                Key={"submission_id": sid},
                UpdateExpression="SET " + ", ".join(sets),
                ExpressionAttributeValues=vals,
            )
            _ddb.Table(AUDIT).put_item(Item={
                "org_id": org_id,
                "ts": f"{now * 1000:013d}#rename-{sid[-8:]}",
                "at": now * 1000,
                "action": "expense type corrected",
                "actor": ACTOR,
                "actor_name": "Expenze (id migration)",
                "reference": str(c.get("reference") or ""),
                "submission_id": sid,
                "who": str(c.get("submitted_by") or ""),
                "detail": f"{args.old} → {args.new} (id renamed, verdict unchanged)",
                "expires_at": now + 2557 * 86400,
            })

        _ddb.Table(AUDIT).put_item(Item={
            "org_id": org_id,
            "ts": f"{now * 1000:013d}#rename-policy",
            "at": now * 1000,
            "action": "policy saved",
            "actor": ACTOR,
            "actor_name": "Expenze (id migration)",
            "detail": f"{label}: id {args.old} → {args.new}, "
                      f"{len(claims)} claim(s) repointed",
            "version": int(rules["version"]),
            "expires_at": now + 2557 * 86400,
        })
        print(f"  done — policy now v{rules['version']}")

    if not args.yes:
        print("\nNothing written. Re-run with --yes to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
