#!/usr/bin/env python3
"""Recompute the verdicts of claims still waiting, under the rules in force now.

Why this exists
---------------
A verdict is a snapshot. It is computed once, stored on the claim, and read
back for ever after - which is right for a claim somebody has decided, because
the finding they decided against is part of what happened. It is wrong for a
claim still waiting, because the reviewer is being shown a reason that is no
longer a reason.

The simplification removed whole classes of finding - headcounts, itemisation
requirements, per-head caps, category exclusions - and every claim audited
before it still carries them. On screen that reads as a fault in the product:

    no_rule_for_expense_type
    No enabled rule covers expense type 'expense_type'.

sitting directly under a dropdown that says **Utilities**, on a claim whose
type is set and whose rule exists. Nothing is broken; the sentence was written
under a policy version that has since been replaced.

What it will and will not touch
-------------------------------
**Only claims nobody has decided.** No `review_action`, no `outcome`, nothing
paid. Rewriting the verdict under a claim a person approved or rejected would
change the stated basis of their decision after the fact, which is exactly the
thing the audit log exists to prevent. Those keep the verdict they were decided
against, stale or not.

**Findings the engine does not own are handled deliberately, one each.** Both
are added by the auditor after the policy runs, so a plain re-run produces
neither. `possible_duplicate` is carried across from the old verdict - it is a
fact about a *different* claim, established when this one arrived, and nothing
here can re-establish it; dropping it would release a claim held for a good
reason. `group_not_set` is re-derived from the claim's own fields, because
carrying it would under-report: a claim whose original audit predates the
finding has no copy to carry, and would come back with its group still
unresolved and nothing on the verdict saying so.

**An expense type left behind by an id rename is repaired first.** Where the
receipt names a type that no longer exists and the verdict names one that does,
the verdict's is adopted: it is the later of the two and the one the rename
migrated. Without this the re-audit is handed the old id and reproduces the
finding it was run to clear.

The prose is regenerated with the verdict, because it quotes figures that have
moved - and because the old prose talks about reimbursable, disallowed and
provisional totals, none of which the product computes any more.

Usage
-----
    python tools/reaudit_stale_verdicts.py          # prints what would change
    python tools/reaudit_stale_verdicts.py --yes    # applies it
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal
from typing import Any

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

REGION = "ap-southeast-1"
SUBMISSIONS = "Expenze-Submissions"
ORGS = "Expenze-Orgs"
AUDIT = "Expenze-AuditLog"

ACTOR = "tools/reaudit_stale_verdicts.py"
RETENTION_DAYS = 2557

# Added by the auditor after the policy has run, so a re-run of the policy
# alone does not produce them.
#
# `possible_duplicate` is carried across from the old verdict: it is a fact
# about a *different* claim, established when this one arrived, and nothing
# here can re-establish it.
#
# `group_not_set` is re-derived instead, from the claim's own group fields.
# Carrying it across silently under-reports: a claim whose original audit
# predates the finding has no copy to carry, so it came back from a re-audit
# with the group still unresolved and nothing on the verdict saying so.
CARRIED_OVER = {"possible_duplicate"}

_ddb = boto3.resource("dynamodb", region_name=REGION)


def _undecided(row: dict[str, Any]) -> bool:
    """Nobody has acted on it, so nothing is being rewritten under anybody."""
    if str(row.get("status") or "") != "audited":
        return False
    return not any(str(row.get(f) or "") for f in
                   ("review_action", "outcome", "paid", "paid_at"))


def _plain(value: Any) -> Any:
    """Decimals back to something the engine and json will both take."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _repair_type(receipt: dict[str, Any], verdict: dict[str, Any],
                 known: set[str]) -> str:
    """The id an id-rename left behind on the receipt. Returns what it fixed."""
    on_receipt = str(receipt.get("expense_type") or "")
    on_verdict = str(verdict.get("expense_type") or "")
    if on_receipt and on_receipt not in known and on_verdict in known:
        receipt["expense_type"] = on_verdict
        return f"{on_receipt} -> {on_verdict}"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true",
                    help="write the recomputed verdicts. Without it, nothing changes.")
    args = ap.parse_args()

    import grouping                                  # noqa: E402
    import handler                                   # noqa: E402
    import policy                                    # noqa: E402

    orgs = {str(o["org_id"]): o for o in _ddb.Table(ORGS).scan().get("Items", [])}
    rows = _ddb.Table(SUBMISSIONS).scan().get("Items", [])
    waiting = [r for r in rows if _undecided(r)]

    print(f"{len(rows)} claims, {len(waiting)} of them still waiting on somebody.\n")
    if not waiting:
        print("Nothing to recompute.")
        return 0

    planned, now = [], int(time.time())
    for row in waiting:
        org = orgs.get(str(row.get("org_id") or "")) or {}
        rules = policy.rules_for(org)
        known = {str(t.get("id")) for t in rules.get("expense_types", [])}

        receipt = _plain(dict(row.get("receipt") or {}))
        was = _plain(dict(row.get("verdict") or {}))
        repaired = _repair_type(receipt, was, known)

        currency = str(was.get("currency") or "") or str(org.get("default_currency") or "")
        now_verdict = handler._run_policy(receipt, currency, rules)

        # Whatever the auditor added on top of the engine, not the engine's to
        # reproduce and not this script's to discard.
        kept = [v for v in (was.get("violations") or [])
                if v.get("code") in CARRIED_OVER]

        # The group, re-derived rather than carried. The bill is consulted
        # first, exactly as the auditor consults it, so a claim that can be
        # settled from the paper is settled here too instead of being sent to
        # a person for a question the paper answers.
        group_id = str(row.get("group_id") or "")
        group_status = str(row.get("group_status") or "")
        if not group_id and group_status in ("ask", "unset"):
            matched, _how = grouping.group_for(org.get("groups") or [], receipt)
            group_id = matched or ""
        if not group_id and group_status == "ask":
            kept.append({
                "code": "group_not_set",
                "message": ("This submitter belongs to more than one group and "
                            "the bill does not say which. Set it before approving."),
                "amount": None,
                "blocks_automatic_decision": True,
            })

        if kept:
            now_verdict.setdefault("violations", []).extend(kept)
            if any(v.get("blocks_automatic_decision") for v in kept):
                now_verdict["verdict"] = "needs_review"
        for carried in ("duplicate_of",):
            if was.get(carried):
                now_verdict[carried] = was[carried]

        planned.append((row, receipt, was, now_verdict, repaired))

    width = max(len(str(r.get("reference") or "?")) for r, *_ in planned)
    changed = 0
    for row, _receipt, was, now_verdict, repaired in planned:
        before = str(was.get("verdict") or "?")
        after = str(now_verdict.get("verdict") or "?")
        old_codes = sorted({v.get("code") for v in (was.get("violations") or [])})
        new_codes = sorted({v.get("code") for v in (now_verdict.get("violations") or [])})
        moved = before != after or old_codes != new_codes
        changed += bool(moved)
        print(f"  {str(row.get('reference') or '?'):<{width}}  "
              f"{before:>12} -> {after:<12} {'*' if moved else ' '} "
              f"{','.join(c for c in old_codes if c) or '-'}"
              f"  ->  {','.join(c for c in new_codes if c) or '-'}"
              + (f"   [type {repaired}]" if repaired else ""))

    print(f"\n{changed} of {len(planned)} would change.")
    if not args.yes:
        print("\nNothing written. Re-run with --yes to apply, which also "
              "regenerates the prose the submitter is shown (one model call each).")
        return 0

    table = _ddb.Table(SUBMISSIONS)
    for row, receipt, was, now_verdict, repaired in planned:
        sid = str(row["submission_id"])
        rationale = handler._get_client().explain(receipt, now_verdict,
                                                  handler.AUDIT_SYSTEM)
        table.update_item(
            Key={"submission_id": sid},
            UpdateExpression=("SET verdict = :v, receipt = :r, rationale = :n, "
                              "reaudited_at = :t"),
            ExpressionAttributeValues={
                ":v": json.loads(json.dumps(now_verdict), parse_float=Decimal),
                ":r": json.loads(json.dumps(receipt), parse_float=Decimal),
                ":n": rationale,
                ":t": now,
            },
        )
        _ddb.Table(AUDIT).put_item(Item={
            "org_id": str(row.get("org_id") or ""),
            "ts": f"{now * 1000:013d}#reaudit-{sid[-8:]}",
            "at": now * 1000,
            "action": "claim re-audited under current rules",
            "actor": ACTOR,
            "actor_name": "Expenze (policy migration)",
            "reference": str(row.get("reference") or ""),
            "submission_id": sid,
            "who": str(row.get("submitted_by") or ""),
            "currency": str(now_verdict.get("currency") or ""),
            "detail": (f"{was.get('verdict')} -> {now_verdict.get('verdict')}"
                       + (f"; type {repaired}" if repaired else "")),
            "expires_at": now + RETENTION_DAYS * 86400,
        })
        print(f"  {row.get('reference')} rewritten.")

    print(f"\n{len(planned)} claims recomputed under the rules in force now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
