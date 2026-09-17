"""BMS — the Expenze Business Management System.

The operator's back office, not a customer surface. It lists every organisation
that has signed up, what they have bought and burned, and lets an admin load
credits onto an account.

    GET  /admin/orgs      -> every organisation with owner, balance, usage
    POST /admin/credits   -> {org_id, credits, note} load credits onto an org
    GET  /admin/settings  -> platform settings: channels and credit pricing
    POST /admin/settings  -> update them (root only)
    GET  /admin/admins    -> who can get in here
    POST /admin/admins    -> {email} grant admin (root only)
    POST /admin/revoke    -> {email} remove an admin (root only)

Two things carry the security of this whole file:

**BMS identity is separate from customer identity.** A customer signing up for a
trial lands in ``Expenze-Users``. Admins live in ``Expenze-Admins``. Nothing in
the sign-up path can write to that table, so no amount of self-service
registration grants back-office access.

**The root admin comes from configuration, not data.** ROOT_ADMIN_EMAIL is
always root regardless of what the table says, so a bad write or an accidental
revoke cannot lock everyone out of the console.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import boto3

import pricing

logger = logging.getLogger()
logger.setLevel(logging.INFO)

USERS_TABLE = os.environ["USERS_TABLE"]
ORGS_TABLE = os.environ["ORGS_TABLE"]
ADMINS_TABLE = os.environ["ADMINS_TABLE"]
LEDGER_TABLE = os.environ["LEDGER_TABLE"]
SETTINGS_TABLE = os.environ["SETTINGS_TABLE"]
RZP_SECRET_ARN = os.environ.get("RZP_SECRET_ARN", "")
SESSION_SECRET_ARN = os.environ["SESSION_SECRET_ARN"]
ROOT_ADMIN_EMAIL = os.environ.get("ROOT_ADMIN_EMAIL", "").lower()
ALLOWED_ORIGINS = [o for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o]

MAX_LOAD = 1_000_000  # a typo should not mint a million dollars of credit

_ddb = boto3.resource("dynamodb")
_users = _ddb.Table(USERS_TABLE)
_orgs = _ddb.Table(ORGS_TABLE)
_admins = _ddb.Table(ADMINS_TABLE)
_ledger = _ddb.Table(LEDGER_TABLE)
_settings = _ddb.Table(SETTINGS_TABLE)
_secrets = boto3.client("secretsmanager")
_ses = boto3.client("sesv2", region_name=os.environ.get("SES_REGION", "us-east-1"))
SENDER = os.environ.get("OTP_SENDER", "noreply@expenze.ai")

# Mail clients show the display name, not the address, so a bare
# `noreply@expenze.ai` arrives from "noreply" - sitting in an inbox next to
# "OpenAI" and "Axis Bank Cards" looking like something that got past a filter.
# The address is unchanged; only what the reader sees is.
def _from(address: str, label: str = "Expenze") -> str:
    return address if "<" in address else f"{label} <{address}>"

_session_key: bytes | None = None

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def _key() -> bytes:
    global _session_key
    if _session_key is None:
        _session_key = _secrets.get_secret_value(SecretId=SESSION_SECRET_ARN)["SecretString"].encode()
    return _session_key


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _identity(token: str) -> str | None:
    """Return the email a session token proves, or None if it proves nothing."""
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(_key(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(sig)):
            return None
        claims = json.loads(_b64d(body))
    except (ValueError, binascii.Error, json.JSONDecodeError, KeyError):
        return None
    if int(claims.get("exp", 0)) < int(time.time()):
        return None
    email = str(claims.get("email", "")).lower()
    return email or None


def _admin_role(email: str) -> str | None:
    """'root', 'admin', or None. Root is configuration, not data."""
    if email and email == ROOT_ADMIN_EMAIL:
        return "root"
    item = _admins.get_item(Key={"email": email}).get("Item")
    if item and item.get("status") == "active":
        return str(item.get("role", "admin"))
    return None


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _cors(origin: str | None) -> dict[str, str]:
    allow = origin if origin in ALLOWED_ORIGINS else (ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else "*")
    return {
        "Access-Control-Allow-Origin": allow,
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Vary": "Origin",
    }


def _num(v: Any) -> int:
    return int(v) if isinstance(v, (int, Decimal)) else 0


def _reply(status: int, body: Any, origin: str | None) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **_cors(origin)},
        "body": json.dumps(body, default=str),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _headcounts() -> dict[str, dict[str, int]]:
    """People per organisation: how many exist, and how many can actually send.

    One scan for the whole page rather than a query per row. Invited and
    active are counted apart because they answer different questions - the
    first is how big the account is, the second is how much of it is real.
    A removed member is neither: they cannot send and nobody is waiting on
    them to accept.
    """
    counts: dict[str, dict[str, int]] = {}
    kwargs: dict[str, Any] = {}
    while True:
        page = _users.scan(**kwargs)
        for row in page.get("Items", []):
            if row.get("status") == "removed":
                continue
            seat = counts.setdefault(str(row.get("org_id", "")), {"users": 0, "active": 0})
            seat["users"] += 1
            # `invited` flips to `active` the first time somebody signs in,
            # which is what accepting an invitation is here.
            if row.get("status") == "active":
                seat["active"] += 1
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return counts


def _list_orgs() -> list[dict[str, Any]]:
    orgs = _orgs.scan().get("Items", [])
    people = _headcounts()
    rows = []
    for o in orgs:
        seat = people.get(o["org_id"], {"users": 0, "active": 0})
        rows.append(
            {
                "org_id": o["org_id"],
                "name": o.get("name", ""),
                "owner": o.get("root_email", ""),
                "credits": _num(o.get("credits")),
                "purchased": _num(o.get("credits_purchased", o.get("credits"))),
                "receipts": _num(o.get("receipts_processed")),
                "users": seat["users"],
                "active_users": seat["active"],
                "trial": bool(o.get("trial_granted")),
                "created_at": _num(o.get("created_at")),
            }
        )
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows


def _load_credits(actor: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    org_id = str(body.get("org_id", "")).strip()
    note = str(body.get("note", "")).strip()[:200]
    try:
        credits = int(body.get("credits", 0))
    except (TypeError, ValueError):
        return _reply(400, {"error": "Credits must be a whole number."}, origin)

    if credits <= 0 or credits > MAX_LOAD:
        return _reply(400, {"error": f"Enter between 1 and {MAX_LOAD:,} credits."}, origin)
    if not _orgs.get_item(Key={"org_id": org_id}).get("Item"):
        return _reply(404, {"error": "No such organisation."}, origin)

    updated = _orgs.update_item(
        Key={"org_id": org_id},
        UpdateExpression=(
            "SET credits = if_not_exists(credits, :z) + :c, "
            "credits_purchased = if_not_exists(credits_purchased, :z) + :c"
        ),
        ExpressionAttributeValues={":c": credits, ":z": 0},
        ReturnValues="ALL_NEW",
    )["Attributes"]

    # Every grant is recorded with who did it. Credits are money; an
    # unattributable balance change is not auditable.
    _ledger.put_item(
        Item={
            "org_id": org_id,
            "ts": int(time.time() * 1000),
            "delta": credits,
            "balance_after": _num(updated.get("credits")),
            "reason": note or "Credits loaded by admin",
            "by": actor,
        }
    )
    logger.info("%s loaded %s credits onto %s", actor, credits, org_id)
    return _reply(200, {"org_id": org_id, "credits": _num(updated.get("credits"))}, origin)


# Platform defaults. Stored on first read so the console always has something
# concrete to show, and so a partial write cannot leave a field undefined.
DEFAULT_SETTINGS: dict[str, Any] = {
    # Empty, not a plausible-looking number. A placeholder that reads as real
    # gets printed in the console as "send your receipts here", and staff
    # photograph bills into a number that answers nothing.
    "whatsapp_number": "",
    "intake_email": "receipts@expenze.ai",
    "trial_credits": 50,
    # Low-credit alerting. Whichever threshold is crossed first.
    "low_credit_alerts": True,
    "low_credit_credits": 50,
    "low_credit_days": 7,
    # Added on top of the INR price for Indian customers. Nothing is added to a
    # USD sale: that is an export of services and is handled on the invoice,
    # not at the checkout.
    "gst_percent": pricing.DEFAULT_GST_PERCENT,
    # One published price per slab, per currency, defined once in pricing.py
    # so the back office and the customer console cannot disagree about it.
    "pricing": pricing.DEFAULT_PRICING,
}


def _get_settings() -> dict[str, Any]:
    stored = _settings.get_item(Key={"key": "platform"}).get("Item") or {}
    merged = dict(DEFAULT_SETTINGS)
    for k, v in stored.items():
        if k != "key":
            merged[k] = v
    # Decimals from DynamoDB are not JSON-friendly numbers.
    merged["trial_credits"] = _num(merged.get("trial_credits"))
    merged["low_credit_credits"] = _num(merged.get("low_credit_credits"))
    merged["low_credit_days"] = _num(merged.get("low_credit_days"))
    merged["low_credit_alerts"] = bool(merged.get("low_credit_alerts"))
    # Merged per slab, not per table. A pricing row saved before a currency
    # column existed has no value in it, and taking that literally hands the
    # checkout an empty price - so a missing currency falls back to the seeded
    # one for that slab rather than blanking it. Adding a third currency later
    # will not silently break selling to everyone who saved settings today.
    merged["pricing"] = pricing.merge(merged.get("pricing"))
    merged["gst_percent"] = pricing.gst_percent(merged.get("gst_percent"))
    return merged


def _save_settings(actor: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    current = _get_settings()

    number = str(body.get("whatsapp_number", current["whatsapp_number"])).strip()
    if number and not re.match(r"^\+[\d\s\-()]{6,24}$", number):
        return _reply(400, {"error": "Enter the number in international format, starting with a country code."}, origin)

    email = str(body.get("intake_email", current["intake_email"])).strip().lower()
    if email and not EMAIL_RE.match(email):
        return _reply(400, {"error": "Enter a valid intake email address."}, origin)

    slabs = current["pricing"]
    if "pricing" in body:
        rows = []
        for row in body.get("pricing") or []:
            try:
                credits = int(row["credits"])
                usd, inr = int(row["usd"]), int(row["inr"])
            except (KeyError, TypeError, ValueError):
                return _reply(400, {
                    "error": "Each slab needs whole-number credits and a price in both USD and INR."
                }, origin)
            if credits <= 0 or usd < 0 or inr < 0:
                return _reply(400, {"error": "Slab sizes must be positive and prices cannot be negative."}, origin)
            # Both currencies are published prices somebody decided, never a
            # conversion of the other. A slab priced in one and not the other
            # cannot be sold in that market at all - see payments.price_of.
            rows.append({"credits": credits, "usd": usd, "inr": inr})
        if not rows:
            return _reply(400, {"error": "At least one pricing slab is required."}, origin)
        # Sorted by size so the table always reads smallest-first, whatever
        # order the client sent.
        slabs = sorted(rows, key=lambda r: r["credits"])

    try:
        trial = int(body.get("trial_credits", current["trial_credits"]))
    except (TypeError, ValueError):
        return _reply(400, {"error": "Trial credits must be a whole number."}, origin)
    if trial < 0 or trial > 100000:
        return _reply(400, {"error": "Trial credits must be between 0 and 100,000."}, origin)

    # Indian sales carry GST on top of the published price. Held as a rate
    # rather than baked into the slab so a rate change is one number here, and
    # so the customer sees the tax as a separate line rather than a price that
    # quietly went up 18%.
    try:
        gst = Decimal(str(body.get("gst_percent", current["gst_percent"])))
    except (TypeError, ValueError, InvalidOperation):
        return _reply(400, {"error": "GST must be a percentage."}, origin)
    if gst < 0 or gst > 50:
        return _reply(400, {"error": "GST must be between 0 and 50 percent."}, origin)

    try:
        low_credits = int(body.get("low_credit_credits", current["low_credit_credits"]))
        low_days = int(body.get("low_credit_days", current["low_credit_days"]))
    except (TypeError, ValueError):
        return _reply(400, {"error": "Low-credit thresholds must be whole numbers."}, origin)
    if not 0 <= low_credits <= 100000:
        return _reply(400, {"error": "The credit threshold must be between 0 and 100,000."}, origin)
    if not 0 <= low_days <= 365:
        return _reply(400, {"error": "The runway threshold must be between 0 and 365 days."}, origin)
    alerts_on = bool(body.get("low_credit_alerts", current["low_credit_alerts"]))

    _settings.put_item(Item={
        "key": "platform",
        "low_credit_alerts": alerts_on,
        "low_credit_credits": low_credits,
        "low_credit_days": low_days,
        "whatsapp_number": number,
        "intake_email": email,
        "trial_credits": trial,
        "pricing": slabs,
        "gst_percent": gst,
        "updated_by": actor,
        "updated_at": int(time.time()),
    })
    logger.info("%s updated platform settings", actor)
    return _reply(200, {"settings": _get_settings()}, origin)


# ---------------------------------------------------------------------------
# Payment keys
# ---------------------------------------------------------------------------
#
# Kept in Secrets Manager, never in the settings table. The settings row is
# read by several things and returned to the back office in full; a key secret
# in there would end up in a log, a response body, or a screenshot. This
# endpoint writes and never reads back: the answer to "what is the key" is
# always "set" or "not set", never the value.


def _payment_status(actor: str, origin: str | None) -> dict[str, Any]:
    """Which key sets exist and which one is switched on. No values, ever."""
    stored = _read_payment_secret()
    out = {"mode": str(stored.get("mode", "test")).lower(), "modes": {}}
    for name in ("test", "live"):
        keys = stored.get(name) or {}
        key_id = str(keys.get("key_id", ""))
        out["modes"][name] = {
            "configured": bool(key_id and keys.get("key_secret")),
            # The key id is not a secret - it travels to the browser on every
            # checkout - so showing it makes "did I paste the right one?"
            # answerable. The key secret never appears.
            "key_id": key_id,
            "mismatched": bool(key_id and not key_id.startswith(f"rzp_{name}_")),
        }
    out["webhook_secret_set"] = bool(stored.get("webhook_secret"))
    return _reply(200, {"payments": out}, origin)


def _read_payment_secret() -> dict[str, Any]:
    if not RZP_SECRET_ARN:
        return {}
    try:
        return json.loads(
            _secrets.get_secret_value(SecretId=RZP_SECRET_ARN)["SecretString"])
    except Exception:
        return {}


def _save_payments(actor: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Set a key pair, or switch which one is live. Root only.

    Switching to live is the single most consequential setting in the product -
    it is the difference between test cards and customers' money - so it is
    refused unless live keys are actually present, and it is logged with who
    did it.
    """
    stored = _read_payment_secret()

    mode = str(body.get("mode", stored.get("mode", "test"))).strip().lower()
    if mode not in ("test", "live"):
        return _reply(400, {"error": "Mode is either test or live."}, origin)

    for name in ("test", "live"):
        pair = body.get(name) or {}
        key_id = str(pair.get("key_id", "")).strip()
        key_secret = str(pair.get("key_secret", "")).strip()
        if not key_id and not key_secret:
            continue
        if not key_id or not key_secret:
            return _reply(400, {
                "error": f"Both the {name} key id and key secret are needed."}, origin)
        # A live key pasted into the test slot is a mistake worth catching
        # here: it would take real money on what everybody believes is a test.
        if not key_id.startswith(f"rzp_{name}_"):
            return _reply(400, {
                "error": f"That looks like a {'live' if 'live' in key_id else 'test'} "
                         f"key, but it was entered under {name}."}, origin)
        stored[name] = {"key_id": key_id, "key_secret": key_secret}

    if mode == "live":
        live = stored.get("live") or {}
        if not (live.get("key_id") and live.get("key_secret")):
            return _reply(400, {
                "error": "Add live keys before switching to live."}, origin)

    was = str(stored.get("mode", "test")).lower()
    stored["mode"] = mode
    # The original secret held one flat key pair. Once both sets exist those
    # top-level fields are dead weight that reads like a third configuration,
    # so they go rather than sitting there to be misread later.
    if stored.get("test") or stored.get("live"):
        stored.pop("key_id", None)
        stored.pop("key_secret", None)
    _secrets.put_secret_value(SecretId=RZP_SECRET_ARN, SecretString=json.dumps(stored))

    if was != mode:
        logger.warning("%s switched payments from %s to %s", actor, was, mode)
    else:
        logger.info("%s updated the %s payment keys", actor, mode)
    return _payment_status(actor, origin)


def _list_admins() -> list[dict[str, Any]]:
    rows = [
        {
            "email": a["email"],
            "role": a.get("role", "admin"),
            "status": a.get("status", "active"),
            "added_by": a.get("added_by", ""),
            "created_at": _num(a.get("created_at")),
        }
        for a in _admins.scan().get("Items", [])
    ]
    if ROOT_ADMIN_EMAIL and not any(r["email"] == ROOT_ADMIN_EMAIL for r in rows):
        rows.insert(0, {
            "email": ROOT_ADMIN_EMAIL, "role": "root", "status": "active",
            "added_by": "configuration", "created_at": 0,
        })
    rows.sort(key=lambda r: (r["role"] != "root", r["email"]))
    return rows


def _send_admin_invite(email: str, inviter: str) -> None:
    subject = "You now have Expenze BMS access"
    text = (
        f"{inviter} granted you administrator access to Expenze BMS.\n\n"
        "Sign in at https://bms.expenze.ai using this address. We email a "
        "six-digit code - there is no password.\n\n"
        "BMS is the operator console: every organisation on the platform, their "
        "credit balances, and the ability to load credits. Treat it accordingly.\n\n"
        "If you were not expecting this, tell the person who sent it.\n\n"
        "Expenze - expenze.ai\n"
    )
    html = f"""<!doctype html><html><body style="margin:0;padding:24px;background:#F4F5F2;
font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#17191C">
<div style="max-width:460px;margin:0 auto;background:#fff;border:1px solid #DCDDD7;border-radius:4px;padding:28px">
<p style="margin:0 0 4px;font-size:20px;font-weight:700;letter-spacing:-.02em">Expenze BMS</p>
<p style="margin:0 0 20px;color:#6A6E67;font-size:13px">Business Management System</p>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6"><strong>{inviter}</strong> granted you
administrator access.</p>
<p style="margin:0 0 20px"><a href="https://bms.expenze.ai"
style="display:inline-block;background:#1E7047;color:#F4F5F2;text-decoration:none;
padding:11px 20px;border-radius:4px;font-weight:600;font-size:15px">Open BMS</a></p>
<p style="margin:0 0 12px;font-size:14px;line-height:1.6">Sign in with this address &mdash; we email a
six-digit code, there is no password.</p>
<p style="margin:0;font-size:13px;line-height:1.55;color:#6A6E67">BMS shows every organisation on the
platform, their credit balances, and can load credits. If you were not expecting this, tell the
person who sent it.</p>
</div></body></html>"""
    _ses.send_email(
        FromEmailAddress=_from(SENDER),
        Destination={"ToAddresses": [email]},
        Content={"Simple": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": text, "Charset": "UTF-8"},
                     "Html": {"Data": html, "Charset": "UTF-8"}},
        }},
    )


def _grant_admin(actor: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    email = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email):
        return _reply(400, {"error": "Enter a valid email address."}, origin)
    _admins.put_item(
        Item={
            "email": email,
            "role": "admin",
            "status": "active",
            "added_by": actor,
            "created_at": int(time.time()),
        }
    )
    logger.info("%s granted admin to %s", actor, email)
    try:
        _send_admin_invite(email, actor)
    except Exception:
        # The grant already succeeded; say so rather than implying it failed.
        logger.exception("admin invite email failed")
        return _reply(200, {"email": email, "role": "admin",
                            "warning": "Access granted, but the email could not be sent."}, origin)
    return _reply(200, {"email": email, "role": "admin", "emailed": True}, origin)


def _revoke_admin(actor: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    email = str(body.get("email", "")).strip().lower()
    if email == ROOT_ADMIN_EMAIL:
        return _reply(400, {"error": "The root admin cannot be removed."}, origin)
    _admins.delete_item(Key={"email": email})
    logger.info("%s revoked admin from %s", actor, email)
    return _reply(200, {"email": email, "revoked": True}, origin)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    origin = headers.get("origin")
    method = event.get("httpMethod", "GET")
    path = event.get("path", "")

    if method == "OPTIONS":
        return _reply(204, {}, origin)

    try:
        auth = headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        email = _identity(token)
        if not email:
            return _reply(401, {"error": "Sign in to continue."}, origin)

        role = _admin_role(email)
        if not role:
            # A valid customer session is still not a BMS session.
            logger.warning("non-admin %s attempted BMS access", email)
            return _reply(403, {"error": "This console is for Expenze administrators."}, origin)

        if method == "GET" and path.endswith("/orgs"):
            return _reply(200, {"orgs": _list_orgs(), "you": {"email": email, "role": role}}, origin)
        if method == "GET" and path.endswith("/settings"):
            return _reply(200, {"settings": _get_settings(), "you": {"email": email, "role": role}}, origin)
        if method == "GET" and path.endswith("/admins"):
            return _reply(200, {"admins": _list_admins(), "you": {"email": email, "role": role}}, origin)
        if method == "GET" and path.endswith("/payments"):
            if role != "root":
                return _reply(403, {"error": "Only the root admin can see payment configuration."}, origin)
            return _payment_status(email, origin)

        body = json.loads(event.get("body") or "{}")

        if method == "POST" and path.endswith("/credits"):
            return _load_credits(email, body, origin)

        # Pricing and channel numbers are commercial decisions, not day-to-day
        # operations - same bar as appointing an administrator.
        if method == "POST" and path.endswith("/settings"):
            if role != "root":
                return _reply(403, {"error": "Only the root admin can change platform settings."}, origin)
            return _save_settings(email, body, origin)

        # Only root manages who else gets in. An admin who could appoint admins
        # makes the root distinction meaningless.
        if method == "POST" and path.endswith("/admins"):
            if role != "root":
                return _reply(403, {"error": "Only the root admin can add administrators."}, origin)
            return _grant_admin(email, body, origin)
        # Taking real money is the most consequential switch in the product.
        if method == "POST" and path.endswith("/payments"):
            if role != "root":
                return _reply(403, {"error": "Only the root admin can change payment keys."}, origin)
            return _save_payments(email, body, origin)

        if method == "POST" and path.endswith("/revoke"):
            if role != "root":
                return _reply(403, {"error": "Only the root admin can remove administrators."}, origin)
            return _revoke_admin(email, body, origin)

        return _reply(404, {"error": "Unknown endpoint."}, origin)
    except Exception:
        logger.exception("admin request failed")
        return _reply(500, {"error": "Something went wrong."}, origin)
