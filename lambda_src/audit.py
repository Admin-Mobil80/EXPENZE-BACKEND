"""Who did what, kept because somebody will ask.

Expenze already records the *current* state of a decision on the claim itself:
`review_action`, `review_by_name`, `review_at`. That reads like an audit trail
and is not one. There is one set of those fields per claim and every decision
overwrites them - approve, reopen, reject, and only "rejected" survives; reopen
removes the approval outright. The question "who approved this before it was
refused" had no answer, because the answer was never kept.

So: a second, separate record that is only ever appended to.

**Append-only is the whole property.** Nothing here updates a row and nothing
deletes one. A log that can be edited answers no question worth asking, and the
moment this is used to settle a dispute about somebody's money it has to be
worth more than the memory of the person being disputed with.

**It never blocks the thing it is recording.** A decision that has been made
and written down must not be lost because the log was unreachable. Failures are
logged and swallowed - an audit gap is a real cost, and it is a smaller one
than refusing to record a rejection somebody has already been told about.

**One log, not one per screen.** The questions an audit actually asks cut
across claims: everything one person rejected last month, every claim approved
by whoever submitted it, who changed a cap before a month closed. None of those
can be answered by reading claims one at a time, and a log per section means
building the same thing three times and still not answering them.

Keyed by organisation and time, so "September, newest first" is a query rather
than a scan, and one customer's log can never be read from another's.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from decimal import Decimal
from typing import Any, Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()

AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")
CLAIM_INDEX = os.environ.get("AUDIT_CLAIM_INDEX", "by-claim")

# Long enough to outlive the financial year it documents and the audit that
# follows it. Deliberately generous: the cost of a row is nothing beside the
# cost of not being able to answer a question about somebody's money.
RETENTION_DAYS = 2557          # seven years

_table = boto3.resource("dynamodb").Table(AUDIT_TABLE) if AUDIT_TABLE else None

# What a reader is entitled to see about an entry, in the order it reads.
FIELDS = ("action", "reference", "who", "actor", "actor_role", "amount",
          "currency", "reason", "detail")


def record(org_id: str, action: str, actor: str, **fields: Any) -> None:
    """Write one entry. Never raises.

    `action` is a short verb phrase in the product's own words - "claim
    rejected", "policy saved", "role changed" - because this is read by a
    person looking for something that happened, not by code branching on it.
    """
    if _table is None or not org_id or not action:
        return
    now = int(time.time() * 1000)
    item: dict[str, Any] = {
        "org_id": org_id,
        # Milliseconds plus a suffix: two decisions in the same millisecond are
        # rare and would otherwise overwrite one another, which is the one
        # thing this table must never do.
        "ts": f"{now:013d}#{uuid.uuid4().hex[:8]}",
        "at": now,
        "action": str(action)[:80],
        "actor": str(actor or "")[:254],
        "expires_at": int(now / 1000) + RETENTION_DAYS * 86400,
    }
    for key, value in fields.items():
        if value in (None, ""):
            continue
        item[key] = value if isinstance(value, (int, Decimal)) else str(value)[:600]
    try:
        _table.put_item(Item=item)
    except Exception:
        # Deliberately swallowed. The decision this describes has already been
        # made and written down; losing it here must not lose that.
        logger.exception("could not record %s for %s", action, org_id)


def read(org_id: str, limit: int = 200,
         before: Optional[str] = None) -> tuple[list[dict[str, Any]], str]:
    """Newest first, one page at a time.

    Returns the entries and a cursor for the next page, or "" when there are no
    more. Paged rather than capped: a year of decisions is the point of keeping
    them, and a log that silently stops at two hundred rows is one nobody can
    trust to answer a question about last March.
    """
    if _table is None or not org_id:
        return [], ""
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("org_id").eq(org_id),
        "ScanIndexForward": False,
        "Limit": max(1, min(int(limit or 200), 500)),
    }
    if before:
        kwargs["ExclusiveStartKey"] = {"org_id": org_id, "ts": before}
    try:
        page = _table.query(**kwargs)
    except Exception:
        logger.exception("could not read the audit log for %s", org_id)
        return [], ""
    rows = page.get("Items", [])
    nxt = (page.get("LastEvaluatedKey") or {}).get("ts", "")
    return rows, str(nxt or "")


def history(org_id: str, submission_id: str) -> list[dict[str, Any]]:
    """Everything that happened to one claim, oldest first.

    Oldest first, unlike `read`. A log is skimmed newest-down; a single claim's
    history is *read* - submitted, questioned, answered, approved, sent back,
    rejected - and that only makes sense forwards.

    The organisation is checked against every row rather than trusted from the
    caller. The index is keyed by submission id alone, so a query against it
    reaches across customers by construction; the caller's authorisation to see
    this claim was established on the submission row, and that has to be
    re-established on what comes back.
    """
    if _table is None or not submission_id:
        return []
    try:
        page = _table.query(
            IndexName=CLAIM_INDEX,
            KeyConditionExpression=Key("submission_id").eq(submission_id),
            ScanIndexForward=True,
            Limit=200,
        )
    except Exception:
        logger.exception("could not read the history of %s", submission_id)
        return []
    return [r for r in page.get("Items", []) if r.get("org_id") == org_id]
