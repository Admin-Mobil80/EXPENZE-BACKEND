"""Tell finance what came in, in batches rather than one email per receipt.

A finance executive needs to know when receipts arrive; they do not need forty
emails on the Friday a team files its month. The fortieth is read by nobody,
which makes the first thirty-nine worthless too - the habit it teaches is to
filter the lot, and then the one that mattered is filtered with them.

So: a window, not a receipt. Every run covers everything that arrived since the
last run and sends one message per organisation. One receipt in the window is
an email about one receipt; twenty is one email listing twenty. Nobody has to
choose a batch size and nothing changes behaviour at a threshold.

**Why a timer, when nothing else here has one.** `alerts.py` deliberately
checks the credit balance on the write that changes it - no schedule to drift,
no window where the state is bad and nobody has looked. That works because the
event *is* the thing worth reporting. A digest is the opposite: its whole
purpose is to wait and see whether more arrives, and the last batch of a quiet
afternoon would otherwise sit unsent until the next receipt came in - which
might be tomorrow. The wait is the feature, so it needs a clock.

**The high-water mark is per organisation and moves only on a successful
send.** If the email fails, the mark stays where it was and the next run covers
the same receipts again. A duplicate digest is a nuisance; a silently skipped
one is a claim nobody looked at.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import boto3

import identity
import notify

logger = logging.getLogger()
logger.setLevel(logging.INFO)

INTAKE_TABLE = os.environ["INTAKE_TABLE"]
ORGS_TABLE = os.environ["ORGS_TABLE"]

# How far back a first run reaches. Without a bound, switching this on would
# email every finance executive the entire history of their account.
FIRST_RUN_LOOKBACK = 3600

_ddb = boto3.resource("dynamodb")
_intake = _ddb.Table(INTAKE_TABLE)
_orgs = _ddb.Table(ORGS_TABLE)


def _since(org: dict[str, Any], now_ms: int) -> int:
    """The mark this run covers from, in milliseconds."""
    mark = int(org.get("finance_digest_at") or 0)
    return mark or (now_ms - FIRST_RUN_LOOKBACK * 1000)


def _arrivals(org_id: str, since_ms: int, until_ms: int) -> list[dict[str, Any]]:
    """Receipts this organisation received in the window.

    A scan with a filter, like every other read of this table, and for the same
    reason: it is small, the attribute is not a key, and an index to make it a
    query is a schema change worth making when volume asks rather than before a
    customer does.
    """
    rows, kwargs = [], {
        "FilterExpression": "org_id = :o AND received_at > :a AND received_at <= :b",
        "ExpressionAttributeValues": {":o": org_id, ":a": since_ms, ":b": until_ms},
    }
    while True:
        page = _intake.scan(**kwargs)
        rows.extend(page.get("Items", []))
        nxt = page.get("LastEvaluatedKey")
        if not nxt:
            break
        kwargs["ExclusiveStartKey"] = nxt
    rows.sort(key=lambda r: int(r.get("received_at") or 0))
    return rows


def _line(row: dict[str, Any]) -> dict[str, Any]:
    """One claim, in the few facts a digest line is made of."""
    verdict = row.get("verdict") or {}
    status = str(row.get("status") or "")
    blocked = any(v.get("blocks_automatic_decision")
                  for v in (verdict.get("violations") or []))
    # Still being read is not "needs review": nobody can act on it yet, and
    # calling for a reviewer on a claim with no figures wastes the trip.
    unread = status in ("queued", "auditing")
    return {
        "reference": str(row.get("reference") or ""),
        "submission_id": str(row.get("submission_id") or ""),
        "who": str(row.get("submitted_by") or ""),
        "vendor": str((row.get("receipt") or {}).get("vendor") or ""),
        "currency": str(verdict.get("currency") or ""),
        "total": str(verdict.get("receipt_total") or ""),
        "needs_review": bool(blocked or row.get("pulled_back")),
        "state": ("being read" if unread
                  else "cleared by the agent" if verdict.get("verdict") in
                  ("approved", "partially_approved") and not blocked
                  else str(verdict.get("verdict") or status)),
    }


def _finance(org_id: str) -> list[dict[str, Any]]:
    """Who is told. Finance executives, and only them.

    Deliberately not owners as well. An owner who also wants these can be given
    the finance role; sending to everyone with authority would make this the
    third notification most people in an organisation receive about a receipt
    they had nothing to do with.
    """
    return [m for m in identity.members_of(org_id)
            if m.get("role") == "finance" and m.get("status") == "active"
            and m.get("email")]


def _run_one(org: dict[str, Any], now_ms: int) -> int:
    org_id = str(org.get("org_id") or "")
    if not org_id:
        return 0
    since = _since(org, now_ms)
    rows = _arrivals(org_id, since, now_ms)
    if not rows:
        # Nothing arrived, so nothing is sent - and the mark still moves, or a
        # quiet week would make the next digest reach back over all of it.
        _stamp(org_id, now_ms)
        return 0

    people = _finance(org_id)
    if not people:
        logger.info("%s has no finance executive to tell", org_id)
        _stamp(org_id, now_ms)
        return 0

    claims = [_line(r) for r in rows]
    org_name = str(org.get("name") or "your organisation")
    sent = 0
    for member in people:
        if notify.email_digest(str(member["email"]), claims, org_name):
            sent += 1

    # Only on a send that worked. A duplicate digest is a nuisance; a silently
    # skipped one is a claim nobody looked at.
    if sent:
        _stamp(org_id, now_ms)
    else:
        logger.warning("%s: digest of %d claims reached nobody", org_id, len(claims))
    logger.info("%s: %d claims to %d of %d finance executives",
                org_id, len(claims), sent, len(people))
    return sent


def _stamp(org_id: str, now_ms: int) -> None:
    try:
        _orgs.update_item(
            Key={"org_id": org_id},
            UpdateExpression="SET finance_digest_at = :t",
            ExpressionAttributeValues={":t": now_ms},
        )
    except Exception:
        logger.exception("could not move the digest mark for %s", org_id)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    orgs, kwargs = [], {}
    while True:
        page = _orgs.scan(**kwargs)
        orgs.extend(page.get("Items", []))
        nxt = page.get("LastEvaluatedKey")
        if not nxt:
            break
        kwargs["ExclusiveStartKey"] = nxt

    sent = 0
    for org in orgs:
        try:
            sent += _run_one(org, now_ms)
        except Exception:
            # One organisation's failure is not the others'. The mark is not
            # moved, so the next run covers the same window again.
            logger.exception("digest failed for %s", org.get("org_id"))
    return {"organisations": len(orgs), "emails": sent}
