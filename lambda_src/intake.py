"""Inbound receipts: resolve the sender, or do nothing.

Every channel lands here before a receipt is ever read:

    POST /intake/email      forwarded to receipts@expenze.ai
    POST /intake/whatsapp    photo sent to the Expenze number
    POST /intake/api         a third-party system submitting on someone's behalf

The gate is the same for all three. A receipt is only processed when the sender
resolves to an active membership of an organisation with credits. Nothing else
is read, stored, or charged for.

**Unresolved senders get silence, not an error.** Replying "we don't recognise
you" to an unknown WhatsApp number confirms the service exists and that the
number reached a live endpoint - which is precisely what someone probing it
wants. The HTTP response to the channel adapter says "ignored" so the adapter
can stop; no message goes back to the sender.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3

import alerts
import reference
import apikeys
import duplicates
import identity

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ORGS_TABLE = os.environ["ORGS_TABLE"]
INTAKE_TABLE = os.environ["INTAKE_TABLE"]
IDEMPOTENCY_TABLE = os.environ.get("IDEMPOTENCY_TABLE", "")
ALLOWED_ORIGINS = [o for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o]

# How long a repeated idempotency key is honoured. Long enough to cover any
# retry a sane integration makes, short enough that the table does not become
# a permanent record of every receipt ever sent.
IDEMPOTENCY_DAYS = 30

_ddb = boto3.resource("dynamodb")
_orgs = _ddb.Table(ORGS_TABLE)
_intake = _ddb.Table(INTAKE_TABLE)
_idem = _ddb.Table(IDEMPOTENCY_TABLE) if IDEMPOTENCY_TABLE else None


def _reply(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def resolve_group(membership: dict[str, Any], org: dict[str, Any]) -> tuple[str, str]:
    """Which group this expense belongs to, and how confident we are.

    Returns (group_id, status) where status is one of:

      assigned  - the person belongs to exactly one group, so there is nothing
                  to ask. The overwhelmingly common case.
      ask       - they belong to several. Only the person knows which one this
                  receipt is for, so we ask them rather than guess; a wrong
                  cost centre is worse than an unset one.
      unset     - they belong to none, or the organisation uses no groups.
                  Finance assigns it when they settle.
    """
    mine = [g for g in (membership.get("groups") or [])
            if any(og["id"] == g for og in (org.get("groups") or []))]
    if len(mine) == 1:
        return mine[0], "assigned"
    if len(mine) > 1:
        return "", "ask"
    # Nobody should have no group: fall back to the organisation's default so
    # spend is attributable from day one rather than piling up unattributed.
    fallback = next((g["id"] for g in (org.get("groups") or []) if g.get("default")), "")
    return (fallback, "assigned") if fallback else ("", "unset")


def _reference_of(submission_id: str) -> str:
    """The claim reference of a submission, or empty if it cannot be read.

    Best effort on purpose: this decorates a reply that is already correct
    without it, and losing the whole answer because a read failed would put
    the sender back where they started - hearing nothing.
    """
    try:
        row = _intake.get_item(Key={"submission_id": submission_id}).get("Item") or {}
        return str(row.get("reference") or "")
    except Exception:
        logger.exception("could not read the reference of %s", submission_id)
        return ""


def _new_id(org_id: str, now: int) -> str:
    """The id this submission will carry.

    Minted before anything is written, because the duplicate fingerprint has to
    be claimed before the credit is spent and a claim must name its real owner
    - otherwise the next genuine duplicate is pointed at a submission that was
    never created.
    """
    return f"sub_{now}_{org_id[-6:]}"


def _charge_and_record(org: dict[str, Any], membership: dict[str, Any], channel: str,
                       payload: dict[str, Any], submission_id: str = "") -> dict[str, Any]:
    """Spend one credit and record the submission. One receipt, one credit."""
    org_id = org["org_id"]
    now = int(time.time() * 1000)
    submission_id = submission_id or _new_id(org_id, now)

    # Conditional decrement: two receipts arriving at once must not both spend
    # the last credit.
    try:
        updated = _orgs.update_item(
            Key={"org_id": org_id},
            UpdateExpression=(
                "SET credits = credits - :one, "
                "receipts_processed = if_not_exists(receipts_processed, :z) + :one "
                # The reference number, minted by the same write that spends
                # the credit. One receipt, one credit, one number: they cannot
                # drift because there is no second write to fail, and two
                # receipts arriving together get consecutive numbers from
                # DynamoDB rather than the same one from a read-then-write.
                "ADD ref_seq :one"
            ),
            ConditionExpression="credits >= :one",
            ExpressionAttributeValues={":one": 1, ":z": 0},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except _orgs.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info("org %s has no credits; receipt not processed", org_id)
        return _reply(402, {
            "status": "no_credits",
            "org_id": org_id,
            "message": "Out of credits. Top up to resume auditing.",
        })

    # The balance only ever falls here, so this is where it is worth looking.
    alerts.check_low_credits(org, int(updated.get("credits") or 0))

    group_id, group_status = resolve_group(membership, org)

    # `ALL_NEW` already carries the incremented counter and, for an account
    # created since references existed, the prefix pinned at sign-up.
    ref = reference.of(updated, updated.get("ref_seq"))

    _intake.put_item(Item={
        "submission_id": submission_id,
        "org_id": org_id,
        # Quotable. The submission id is an identifier - nobody reads it down
        # a phone or recognises it on a statement.
        "reference": ref,
        "submitted_by": membership.get("email", ""),
        "submitted_from": membership.get("mobile", ""),
        "channel": channel,
        "received_at": now,
        "status": "queued",
        "group_id": group_id,
        "group_status": group_status,
        "source_ref": str(payload.get("source_ref", ""))[:400],
        # What the sender typed alongside the receipt - a WhatsApp caption, an
        # email subject and body. Stored as sent and never edited, because a
        # headcount that came from the claimant rather than from the bill has
        # to stay attributable to them.
        "sender_note": str(payload.get("sender_note", ""))[:600],
        # The original, as it arrived. This row is what authorises reading it
        # back later - the object key alone proves nothing - so the two have to
        # be written together or the receipt becomes unreachable.
        "receipt_key": str(payload.get("receipt_key", ""))[:200],
        "receipt_type": str(payload.get("receipt_type", ""))[:80],
        "receipt_name": str(payload.get("receipt_name", ""))[:160],
        "receipt_bytes": int(payload.get("receipt_bytes") or 0),
        # The identity of the bytes. Used at intake to refuse a second copy and
        # never written down, so `duplicates.release` on a rejected claim -
        # which reads it off this row - had nothing to release and left the
        # content fingerprint held by a claim that went nowhere.
        "receipt_sha256": str(payload.get("receipt_sha256", ""))[:64],
    })

    return _reply(202, {
        "reference": ref,
        "status": "queued",
        "submission_id": submission_id,
        "org_id": org_id,
        "receipt_key": str(payload.get("receipt_key", ""))[:200],
        "org_name": org.get("name", ""),
        "group_id": group_id,
        "group_status": group_status,
        # The label, not just the id: an acknowledgement that says "Group:
        # bengaluru_office" reads like a database row, not a message.
        "group_label": next((g.get("label", "") for g in (org.get("groups") or [])
                             if g.get("id") == group_id), ""),
        # The caller needs these to ask the question over its own channel.
        "groups": [g for g in (org.get("groups") or [])
                   if g["id"] in (membership.get("groups") or [])],
        "credits_remaining": int(updated.get("credits", 0)),
    })


def _claim_idempotency(org_id: str, key: str) -> tuple[bool, str]:
    """Reserve an idempotency key, or report what it was used for before.

    Returns (is_new, submission_id). A retry - the same key, the same
    organisation - gets back the original submission and is not charged again.
    Without this, an integration that retries a timeout pays twice for one
    receipt, and the customer sees a duplicate claim they have to go and find.

    The reservation is written before the credit is spent and points at the
    submission afterwards, so a crash in between leaves a reservation with no
    submission: the retry then finds an empty pointer and is allowed through
    rather than being told about a claim that does not exist.
    """
    if _idem is None or not key:
        return True, ""
    record_id = f"{org_id}:{key}"
    try:
        _idem.put_item(
            Item={"idem_id": record_id, "org_id": org_id,
                  "created_at": int(time.time()),
                  "expires_at": int(time.time()) + IDEMPOTENCY_DAYS * 86400},
            ConditionExpression="attribute_not_exists(idem_id)",
        )
        return True, ""
    except _idem.meta.client.exceptions.ConditionalCheckFailedException:
        existing = _idem.get_item(Key={"idem_id": record_id}).get("Item") or {}
        return False, str(existing.get("submission_id") or "")


def _record_idempotency(org_id: str, key: str, submission_id: str) -> None:
    if _idem is None or not key:
        return
    try:
        _idem.update_item(
            Key={"idem_id": f"{org_id}:{key}"},
            UpdateExpression="SET submission_id = :s",
            ExpressionAttributeValues={":s": submission_id},
        )
    except Exception:
        logger.exception("could not point an idempotency key at its submission")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    path = event.get("path", "")
    channel = ("whatsapp" if path.endswith("/whatsapp")
               else "portal" if path.endswith("/portal")
               else "api" if path.endswith("/api")
               else "email")

    try:
        body = json.loads(event.get("body") or "{}")

        # The API is the one channel a stranger can reach directly. WhatsApp
        # and email arrive through adapters that already proved where they came
        # from; a bare HTTP POST proves nothing, so it carries a key.
        key_org = ""
        if channel == "api":
            key = apikeys.bearer(event.get("headers") or {})
            key_org = apikeys.resolve(key) or ""
            if not key_org:
                logger.warning("rejected an API submission with no usable key")
                return _reply(401, {
                    "error": "An API key is required. Create one in the console "
                             "under Channels & API.",
                })

        membership = identity.resolve_sender(
            email=body.get("from_email", body.get("email", "")),
            mobile=body.get("from_mobile", body.get("mobile", "")),
            staff_id=body.get("staff_id", ""),
            channel=channel,
        )

        if not membership:
            # Deliberately silent. See the module docstring: an informative
            # reply to an unknown sender is a free confirmation for anyone
            # probing the address or the number.
            logger.info("unresolved %s sender; ignoring without reply", channel)
            return _reply(200, {"status": "ignored"})

        org = identity.org_for(membership)
        if not org:
            logger.warning("membership %s points at a missing org", membership.get("org_id"))
            return _reply(200, {"status": "ignored"})

        # A key belongs to one organisation and may only claim for that one.
        # Otherwise any customer's key could submit against any other, since
        # the employee is resolved from the body.
        if key_org and org.get("org_id") != key_org:
            logger.warning("API key for %s tried to submit for %s", key_org, org.get("org_id"))
            return _reply(403, {
                "error": "That person is not a member of the organisation this key belongs to.",
            })

        # The same bytes twice is not a judgment call - it is the same
        # photograph. Caught here rather than after the audit so nobody is
        # charged a credit for a file we have already read, and answered so the
        # sender knows it arrived the first time instead of sending a third.
        submission_id = _new_id(org["org_id"], int(time.time() * 1000))
        sha = str(body.get("receipt_sha256", ""))
        file_print = duplicates.file_key(org["org_id"], sha) if sha else ""
        if file_print:
            held = duplicates.claim(file_print, submission_id)
            if held:
                logger.info("identical file already submitted as %s", held)
                return _reply(200, {
                    "status": "duplicate",
                    "submission_id": held,
                    # The claim it already is, by the name every other message
                    # about it uses. A sender told "already submitted" and not
                    # told *as what* has no way to check, and the obvious next
                    # move is to send it again.
                    "reference": _reference_of(held),
                    "message": "This exact receipt has already been submitted. "
                               "No credit was spent and no second claim was created.",
                })

        idem = str(body.get("idempotency_key", ""))[:120].strip()
        if idem:
            fresh, seen = _claim_idempotency(org["org_id"], idem)
            # A reservation with nothing behind it is not a duplicate. It is
            # an attempt that never produced a claim - it ran out of credits,
            # or died between reserving and recording - and answering "already
            # done" would point the caller at a submission that does not
            # exist and lose the receipt for good. Let it through instead.
            if not fresh and seen:
                logger.info("idempotency key replayed; returning the original submission")
                return _reply(200, {
                    "status": "duplicate",
                    "submission_id": seen,
                    "message": "This idempotency_key was already used. "
                               "No credit was spent and no second claim was created.",
                })

        logger.info(
            "resolved %s sender to org %s (%s memberships considered)",
            channel, org["org_id"], membership.get("org_id"),
        )
        response = _charge_and_record(org, membership, channel, body, submission_id)

        # The charge was refused - out of credits. Hand the fingerprint back,
        # or the sender's next attempt at the same bill, after topping up, is
        # turned away as a duplicate of a claim that was never created.
        #
        # `not in (200, 202)`, not `!= 200`. A successful intake answers **202**
        # - accepted, being read - so this released the fingerprint on every
        # successful submission, microseconds after claiming it. The
        # identical-file gate above could therefore never fire: there was never
        # a fingerprint left in the table for the second copy to collide with,
        # and the `#file#` prefix had no rows in it at all.
        #
        # Three sends of one email cost three credits and produced three
        # claims and three outcome messages, when the second and third should
        # have been answered "you have already sent this" for nothing.
        if file_print and response.get("statusCode") not in (200, 202):
            duplicates.release(file_print, submission_id)
        if idem:
            outcome = json.loads(response.get("body") or "{}")
            if outcome.get("submission_id"):
                _record_idempotency(org["org_id"], idem, outcome["submission_id"])
        return response
    except Exception:
        logger.exception("intake failed")
        return _reply(500, {"error": "Could not accept the receipt."})
