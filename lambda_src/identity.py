"""Resolving an inbound receipt to the organisation that should be billed.

Receipts arrive from people, not accounts: an email lands at receipts@expenze.ai
from whatever address the employee happens to use, and a WhatsApp photo arrives
from a phone number. Neither carries an organisation. This module is the only
place that mapping is decided.

Rules, in order:

1. Look the sender up by email address, or by mobile number for WhatsApp.
2. Keep only **active** memberships.
3. If the person belongs to more than one organisation, resolve to the one they
   were **added to most recently**. Someone who changed employer should have
   their receipts billed to the current one, not whichever row happens to sort
   first.
4. If nothing resolves, the caller does not process the receipt - and for
   WhatsApp, does not reply at all.

That last point is deliberate. Replying "we don't recognise you" to an unknown
number confirms the service exists and that the number reached it, which is
exactly what a spammer probing the endpoint wants. Silence costs an unmapped
colleague one question to their finance team; a reply costs everyone.
"""
from __future__ import annotations

import os
import re
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

USERS_TABLE = os.environ["USERS_TABLE"]
ORGS_TABLE = os.environ["ORGS_TABLE"]
MOBILE_INDEX = os.environ.get("MOBILE_INDEX", "by-mobile")

_ddb = boto3.resource("dynamodb")
_users = _ddb.Table(USERS_TABLE)
_orgs = _ddb.Table(ORGS_TABLE)


def normalise_mobile(raw: str) -> str:
    """E.164-ish: keep digits, keep a leading +. Numbers arrive spaced,
    bracketed and hyphenated depending on the handset that sent them."""
    digits = re.sub(r"[^\d]", "", raw or "")
    return f"+{digits}" if digits else ""


def normalise_email(raw: str) -> str:
    return (raw or "").strip().lower()


def _latest_active(rows: list[dict[str, Any]], channel: str | None = None) -> dict[str, Any] | None:
    """Most recent membership that is accepted and has `channel` switched on.

    Two gates, not one:

    * ``status`` must be ``active``. A person an administrator has merely
      *added* is ``invited`` until they accept, so a mistyped address never
      starts accepting receipts on someone's behalf.
    * the channel itself must be enabled. Email switches on when the invitation
      is accepted, which proves control of the mailbox. WhatsApp stays off
      until the person adds their *own* number and verifies it - an
      administrator typing a number is not consent, and a number typed one
      digit wrong is a stranger who can now spend an organisation's credits.
    """
    usable = [r for r in rows if r.get("status") == "active"]
    if channel == "email":
        usable = [r for r in usable if r.get("email_channel", "active") == "active"]
    elif channel == "whatsapp":
        usable = [r for r in usable if r.get("whatsapp_channel") == "active"]
    if not usable:
        return None
    # Rule 3: most recently added membership wins.
    return max(usable, key=lambda r: int(r.get("added_at", 0)))


def resolve_by_email(email: str, channel: str | None = "email") -> dict[str, Any] | None:
    email = normalise_email(email)
    if not email:
        return None
    rows = _users.query(KeyConditionExpression=Key("email").eq(email)).get("Items", [])
    return _latest_active(rows, channel)


# Statuses a person can sign in under. `invited` belongs here and nowhere
# else: signing in IS how an invitation is accepted, so requiring `active`
# first is a deadlock - the invitation can never be accepted because accepting
# it requires already having accepted it. `removed` and `suspended` are absent
# on purpose, and stay absent.
SIGN_IN_STATUSES = ("active", "invited")


def members_of(org_id: str) -> list[dict[str, Any]]:
    """Everyone on one organisation's roll, in whatever state they are in.

    A scan with a filter: the table is keyed by address, so "who is in this
    organisation" is not a query it can answer. Same trade every other read of
    a non-key attribute in this codebase makes - the table is small, and an
    index to turn this into a query is a schema change worth making when volume
    asks for it rather than in advance of a customer.

    Filtering by role or status is the caller's business. This answers who is
    on the roll; who should be told something is a different question.
    """
    if not org_id:
        return []
    rows, kwargs = [], {
        "FilterExpression": "org_id = :o",
        "ExpressionAttributeValues": {":o": org_id},
    }
    while True:
        page = _users.scan(**kwargs)
        rows.extend(page.get("Items", []))
        nxt = page.get("LastEvaluatedKey")
        if not nxt:
            break
        kwargs["ExclusiveStartKey"] = nxt
    return rows


def membership_in(org_id: str, email: str) -> dict[str, Any] | None:
    """Anyone on this organisation's roll, in whatever state they are in.

    Administration is the one caller that has to see the people every other
    resolver here deliberately hides. `resolve_by_email` gates on `active`
    because its job is deciding whether a receipt from an address may be
    accepted - and an invitation that has not been accepted must not be.

    Using that same gate as an existence check made an invited person
    unmanageable: they were listed in People, and removing them, renaming them
    or changing their role all answered "that person is not in your
    organisation", because to the intake gate they were not yet in it. Somebody
    invited to a mistyped address could therefore never be taken off the list.

    The org is part of the lookup, not a check afterwards: an address can hold
    memberships of several organisations, and administering one of them must
    never reach into another.
    """
    email = normalise_email(email)
    if not email or not org_id:
        return None
    rows = [r for r in _users.query(
                KeyConditionExpression=Key("email").eq(email)).get("Items", [])
            if r.get("org_id") == org_id]
    if not rows:
        return None
    return max(rows, key=lambda r: int(r.get("added_at", 0)))


def resolve_for_signin(email: str) -> dict[str, Any] | None:
    """The membership a sign-in code may be sent to.

    Deliberately looser than every other resolver here, and only here. Sending
    a receipt from an address needs an accepted invitation; *accepting* the
    invitation cannot. The channel gates do not apply either - `email_channel`
    is `pending` until acceptance, which is the state this call exists to let
    somebody out of.
    """
    email = normalise_email(email)
    if not email:
        return None
    rows = _users.query(KeyConditionExpression=Key("email").eq(email)).get("Items", [])
    usable = [r for r in rows if r.get("status") in SIGN_IN_STATUSES]
    if not usable:
        return None
    return max(usable, key=lambda r: int(r.get("added_at", 0)))


def resolve_by_mobile(mobile: str) -> dict[str, Any] | None:
    mobile = normalise_mobile(mobile)
    if not mobile:
        return None
    rows = _users.query(
        IndexName=MOBILE_INDEX,
        KeyConditionExpression=Key("mobile").eq(mobile),
    ).get("Items", [])
    return _latest_active(rows, "whatsapp")


def resolve_sender(
    *, email: str = "", mobile: str = "", staff_id: str = "", channel: str = "email"
) -> dict[str, Any] | None:
    """Resolve however the channel identifies the person.

    `channel` decides which consent gate applies: a receipt arriving over
    WhatsApp needs a verified number, one arriving by email needs an accepted
    invitation. A third-party API call is trusted on the caller's API key
    rather than the sender's channel, so it checks membership only.
    """
    gate = None if channel == "api" else channel

    if staff_id:
        rows = _users.scan(
            FilterExpression="staff_id = :s",
            ExpressionAttributeValues={":s": staff_id},
        ).get("Items", [])
        hit = _latest_active(rows, gate)
        if hit:
            return hit
    if email:
        hit = resolve_by_email(email, gate)
        if hit:
            return hit
    if mobile:
        return resolve_by_mobile(mobile)
    return None


def org_for(membership: dict[str, Any]) -> dict[str, Any] | None:
    return _orgs.get_item(Key={"org_id": membership["org_id"]}).get("Item")


def has_credits(org: dict[str, Any]) -> bool:
    return int(org.get("credits", 0)) > 0
