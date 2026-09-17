"""Telling the people who can act, while acting is still cheap.

Running out of credits is not a loud failure. Receipts keep arriving and queue
unaudited, each sender is told their receipt was not processed, and the first
anyone in finance hears of it is a colleague asking why their claim vanished.
By then it is a backlog rather than a purchase.

Three decisions worth knowing about:

**Checked when the balance falls, not on a timer.** The moment a credit is
spent is the moment the number changed, and it is already a write we are
making - so there is no schedule to drift, nothing to poll, and no window in
which the balance is low and nobody has looked.

**Alerted once per crossing.** The threshold is passed on one receipt and every
receipt after it, so an organisation that sends forty a day would otherwise get
forty identical warnings. The crossing is stamped on the organisation and
cleared when the balance recovers, which is what makes the next one arrive.

**Never in the way of a receipt.** A claim has already been charged and
recorded by the time this runs. Losing it because an alert could not be sent
would be a far worse failure than an alert nobody got.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

import notify

logger = logging.getLogger()

ORGS_TABLE = os.environ.get("ORGS_TABLE", "")
USERS_TABLE = os.environ.get("USERS_TABLE", "")
SETTINGS_TABLE = os.environ.get("SETTINGS_TABLE", "")

_ddb = boto3.resource("dynamodb")
_orgs = _ddb.Table(ORGS_TABLE) if ORGS_TABLE else None
_users = _ddb.Table(USERS_TABLE) if USERS_TABLE else None
_settings = _ddb.Table(SETTINGS_TABLE) if SETTINGS_TABLE else None

# Who can do something about it. Telling everybody would mean telling the
# people who cannot buy credits that somebody should.
CAN_TOP_UP = ("owner", "finance")


def _thresholds() -> dict[str, Any]:
    if _settings is None:
        return {"on": False}
    try:
        row = _settings.get_item(Key={"key": "platform"}).get("Item") or {}
    except Exception:
        logger.exception("could not read the alerting thresholds")
        return {"on": False}
    return {
        "on": bool(row.get("low_credit_alerts", True)),
        "credits": int(row.get("low_credit_credits") or 0),
        "days": int(row.get("low_credit_days") or 0),
    }


def _recipients(org_id: str) -> list[dict[str, Any]]:
    if _users is None or not org_id:
        return []
    try:
        rows = _users.scan(
            FilterExpression="org_id = :o AND #s = :a",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":o": org_id, ":a": "active"},
        ).get("Items", [])
    except Exception:
        logger.exception("could not list the people to alert")
        return []
    return [r for r in rows if r.get("role") in CAN_TOP_UP]


def _runway_days(org: dict[str, Any], balance: int) -> int:
    """Days of credit left at the rate this organisation has actually used.

    Zero when there is no rate to project from - a new customer who has
    submitted nothing has no runway, only a balance, and inventing a figure
    would put a number on the alert that means nothing.
    """
    used = int(org.get("receipts_processed") or 0)
    since = int(org.get("created_at") or 0)
    if used <= 0 or since <= 0:
        return 0
    days = max(1, (int(time.time()) - since) // 86400)
    per_day = used / days
    return int(balance / per_day) if per_day > 0 else 0


def check_low_credits(org: dict[str, Any], balance: int) -> None:
    """Alert the people who can top up, once per crossing."""
    try:
        limits = _thresholds()
        if not limits.get("on"):
            return

        org_id = str(org.get("org_id") or "")
        days_left = _runway_days(org, balance)
        low = (balance <= limits["credits"]
               or (limits["days"] and days_left and days_left <= limits["days"]))

        if not low:
            # Recovered. Clearing the stamp is what lets the next crossing
            # alert; without it the first warning would be the only one ever.
            if org.get("low_credit_alerted_at"):
                _clear(org_id)
            return

        if org.get("low_credit_alerted_at"):
            return

        people = _recipients(org_id)
        if not people:
            logger.info("nobody to alert about credits in %s", org_id)
            return

        claim = {"balance": balance, "org_name": org.get("name", ""),
                 "days_left": days_left}
        for member in people:
            notify.send("low_credits", member, claim)
        _stamp(org_id)
        logger.info("low-credit alert for %s (%s left) to %d person(s)",
                    org_id, balance, len(people))
    except Exception:
        # A receipt has already been charged and recorded by the time this
        # runs. Losing it because an alert failed would be far worse than an
        # alert nobody got.
        logger.exception("low-credit check failed")


def _stamp(org_id: str) -> None:
    _orgs.update_item(
        Key={"org_id": org_id},
        UpdateExpression="SET low_credit_alerted_at = :t",
        ExpressionAttributeValues={":t": int(time.time())},
    )


def _clear(org_id: str) -> None:
    try:
        _orgs.update_item(
            Key={"org_id": org_id},
            UpdateExpression="REMOVE low_credit_alerted_at",
        )
    except Exception:
        logger.exception("could not clear the low-credit stamp for %s", org_id)
