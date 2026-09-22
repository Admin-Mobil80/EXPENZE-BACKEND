"""Tell finance what is ready to pay, in batches rather than one email each.

**Approvals, not arrivals.** This used to email finance every receipt that
came in. A receipt arriving is not finance's business: it may still be with
the agent, it may be about to be rejected, and in every case somebody else
decides before there is anything to pay. Telling them anyway gave them a
stream they could not act on, and a stream nobody can act on is one they learn
to filter - taking the message that mattered with it.

A claim clearing for settlement *is* their business, because it is the moment
the work becomes theirs. Two ways a claim gets there and both count: a person
approved it after review, or the agent cleared it against the policy and
nobody had to. `_cleared_at` draws that line in the same place the console's
`payableClaims` does, so the email and the Pending settlement tab cannot
disagree about what is waiting.

**A window, not a claim.** Every run covers everything that cleared since the
last run and sends one message per organisation. One claim in the window is an
email about one claim; twenty is one email listing twenty. Nobody has to
choose a batch size and nothing changes behaviour at a threshold.

**Why a timer, when nothing else here has one.** `alerts.py` deliberately
checks the credit balance on the write that changes it - no schedule to drift,
no window where the state is bad and nobody has looked. That works because the
event *is* the thing worth reporting. A digest is the opposite: its whole
purpose is to wait and see whether more arrives, and the last approval of a
quiet afternoon would otherwise sit unsent until the next one came in - which
might be tomorrow. The wait is the feature, so it needs a clock.

**The high-water mark is per organisation and moves only on a successful
send.** If the email fails, the mark stays where it was and the next run covers
the same claims again. A duplicate digest is a nuisance; a silently skipped
one is money nobody paid.
"""
from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from typing import Any

import boto3

import identity
import notify
import policy

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


def _ms(value: Any) -> int:
    """Whatever unit it was written in, in milliseconds.

    `received_at` is milliseconds and `review_at` and `audited_at` are
    seconds - one row carries all three - and mixing them has produced a
    confident wrong answer in this codebase before, where a subtraction across
    the boundary came out negative and a floor elsewhere swallowed it.
    Normalising on the way in beats remembering which is which at every
    comparison.

    The boundary is 10^11, which is March 1973 in milliseconds and the year
    5138 in seconds. Nothing this product stores is near either.
    """
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return n * 1000 if 0 < n < 10 ** 11 else n


def _rows(org_id: str) -> list[dict[str, Any]]:
    """Every claim this organisation holds.

    A scan with a filter, like every other read of this table, and for the same
    reason: it is small, the attribute is not a key, and an index to make it a
    query is a schema change worth making when volume asks rather than before a
    customer does.

    Unfiltered by time, because the question "did this clear in the window" is
    not one the store can answer: a claim clears at `review_at` or at
    `audited_at` depending on who cleared it, and a filter expression cannot
    choose between two attributes. The choosing is `_cleared_at`'s job.
    """
    rows, kwargs = [], {
        "FilterExpression": "org_id = :o",
        "ExpressionAttributeValues": {":o": org_id},
    }
    while True:
        page = _intake.scan(**kwargs)
        rows.extend(page.get("Items", []))
        nxt = page.get("LastEvaluatedKey")
        if not nxt:
            break
        kwargs["ExclusiveStartKey"] = nxt
    return rows


def _cleared_at(row: dict[str, Any]) -> int:
    """When this claim became finance's to pay, in milliseconds. 0 if it is not.

    Whether it is waiting at all is `policy.awaiting_payment`, shared with the
    console's Pending settlement list and with the permission that lets finance
    refuse to pay one. An email saying three claims are waiting, over a tab
    badge saying two, is worse than no email at all.

    When is this function's own: a claim a person approved has been finance's
    since they approved it, and one the agent released has been since it was
    read.
    """
    if not policy.awaiting_payment(row):
        return 0
    if str(row.get("review_action") or "") == "approved":
        return _ms(row.get("review_at"))
    return _ms(row.get("audited_at"))


def _owed(row: dict[str, Any]) -> tuple[str, str]:
    """What a settlement would pay for this claim, in the currency it leaves in.

    A payout leaves the account in the organisation's own currency however many
    currencies the receipts were in, converted at the rate stamped on the claim
    when it was audited - never at today's, because an amount owed that moves
    between the day it was approved and the day it is paid is not an amount
    owed.

    What a reviewer released comes first and the engine's figure second, which
    is the order the console uses: a claim approved over a cap pays what the
    person approved, and reading the engine's figure instead would report nil
    for every overridden claim.

    An empty amount is a foreign claim whose rate could not be had. The figure
    is unknown rather than zero, and the digest says so rather than inventing
    one.
    """
    verdict = row.get("verdict") or {}
    payout = row.get("payout_value") or {}
    home = str(payout.get("currency") or "").upper()
    claim_ccy = str(verdict.get("currency") or "").upper()

    raw = row.get("approved_total")
    if raw in (None, ""):
        raw = verdict.get("reimbursable_total")
    try:
        owed = Decimal(str(raw or "0"))
    except (ArithmeticError, ValueError):
        return ("", claim_ccy or home)

    if not home or not claim_ccy or claim_ccy == home:
        return (f"{owed:.2f}", home or claim_ccy)
    try:
        rate = Decimal(str(payout.get("rate") or "0"))
    except (ArithmeticError, ValueError):
        rate = Decimal(0)
    if rate <= 0:
        return ("", claim_ccy)
    return (str((owed * rate).quantize(Decimal("0.01"))), home)


def _line(row: dict[str, Any]) -> dict[str, Any]:
    """One claim, in the few facts a digest line is made of."""
    amount, currency = _owed(row)
    action = str(row.get("review_action") or "")
    return {
        "reference": str(row.get("reference") or ""),
        "submission_id": str(row.get("submission_id") or ""),
        "who": str(row.get("submitted_by") or ""),
        "vendor": str((row.get("receipt") or {}).get("vendor") or ""),
        "currency": currency,
        "total": amount,
        # Who released it, which is the fact finance most often wants back:
        # a claim the agent cleared went out on the policy alone, and one a
        # person approved has a name against the judgment.
        "cleared_by": (str(row.get("review_by_name") or "a reviewer")
                       if action == "approved" else "Expenze agent"),
    }


def _finance(org_id: str) -> list[dict[str, Any]]:
    """Who is told. Finance executives, and only them - unless there are none.

    Deliberately not owners as well. An owner who also wants these can be given
    the finance role; sending to everyone with authority would make this the
    second notification most people in an organisation receive about a receipt
    they had nothing to do with.

    The fallback is a different question from that one. An organisation with no
    finance executive still has claims waiting to be paid, and somebody still
    pays them - the console lets an owner settle for exactly that reason. With
    no fallback, the smallest accounts, which are the ones most likely to be
    one person, would be the only ones this never reaches.
    """
    people = [m for m in identity.members_of(org_id)
              if m.get("status") == "active" and m.get("email")]
    finance = [m for m in people if m.get("role") == "finance"]
    if finance:
        return finance
    return [m for m in people if m.get("role") == "owner"]


def _run_one(org: dict[str, Any], now_ms: int) -> int:
    org_id = str(org.get("org_id") or "")
    if not org_id:
        return 0
    since = _since(org, now_ms)

    rows = _rows(org_id)
    waiting, fresh = [], []
    for row in rows:
        at = _cleared_at(row)
        if not at:
            continue
        # Everything unpaid, for the standing total; the ones that cleared in
        # this window, for the list. A digest that only ever reports the delta
        # makes the reader hold the running total themselves.
        waiting.append(row)
        if since < at <= now_ms:
            fresh.append(row)

    if not fresh:
        # Nothing cleared, so nothing is sent - and the mark still moves, or a
        # quiet week would make the next digest reach back over all of it.
        _stamp(org_id, now_ms)
        return 0

    people = _finance(org_id)
    if not people:
        logger.info("%s has nobody who could settle a claim", org_id)
        _stamp(org_id, now_ms)
        return 0

    fresh.sort(key=_cleared_at)
    claims = [_line(r) for r in fresh]
    outstanding = [_line(r) for r in waiting]
    org_name = str(org.get("name") or "your organisation")
    sent = 0
    for member in people:
        if notify.email_digest(str(member["email"]), claims, org_name, outstanding):
            sent += 1

    # Only on a send that worked. A duplicate digest is a nuisance; a silently
    # skipped one is money nobody paid.
    if sent:
        _stamp(org_id, now_ms)
    else:
        logger.warning("%s: digest of %d claims reached nobody", org_id, len(claims))
    logger.info("%s: %d claims ready to pay (%d waiting in all) to %d of %d",
                org_id, len(claims), len(waiting), sent, len(people))
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
