"""Email OTP sign-in for Expenze, and the console API that sits behind it.

Sign-in is two routes:

    POST /auth/request  {"email": "..."}   -> emails a six-digit code
    POST /auth/verify   {"email","code"}   -> returns a signed session token

Everything else here is a console action that requires the token sign-in hands
back: the organisation profile, groups, people, WhatsApp number verification,
and the stored original of a receipt. They live alongside sign-in because each
one has to verify that token, and the key that verifies it should reach as few
functions as possible.

Design notes that matter more than the code:

* **The code is never stored.** Only a salted SHA-256 of it is, so a dump of the
  table does not let anyone sign in. Comparison is constant-time.
* **Codes expire and are single-use.** DynamoDB TTL removes the row, and a
  successful verify deletes it immediately so a code cannot be replayed.
* **Attempts are capped.** Five wrong guesses burns the code - six digits is
  only a million possibilities, which is trivially brute-forced otherwise.
* **Requests are rate limited per address** - a 60s cooldown and 5/hour. An
  unauthenticated endpoint that sends email is a spam cannon otherwise, and it
  would burn a sending quota shared with every other product in this account.
* **The response never reveals whether an address is known.** Same body either
  way; enumeration through this endpoint returns nothing useful.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import audit
import budgets as budget_rules
import fx
import wacfg
import identity
import intake
import money
import apikeys
import duplicates
import notify
import payments
import policy
import pricing
import reference
import taxid
import receipts

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AUTH_TABLE = os.environ["AUTH_TABLE"]
USERS_TABLE = os.environ["USERS_TABLE"]
ORGS_TABLE = os.environ["ORGS_TABLE"]
ADMINS_TABLE = os.environ["ADMINS_TABLE"]
INTAKE_TABLE = os.environ["INTAKE_TABLE"]
SETTINGS_TABLE = os.environ.get("SETTINGS_TABLE", "")
LEDGER_TABLE = os.environ.get("LEDGER_TABLE", "")
ADVANCES_TABLE = os.environ.get("ADVANCES_TABLE", "")
WA_SECRET_ARN = os.environ.get("WA_SECRET_ARN", "")
ROOT_ADMIN_EMAIL = os.environ.get("ROOT_ADMIN_EMAIL", "").lower()
SES_REGION = os.environ.get("SES_REGION", "us-east-1")
SENDER = os.environ.get("OTP_SENDER", "noreply@expenze.ai")

# Mail clients show the display name, not the address, so a bare
# `noreply@expenze.ai` arrives from "noreply" - sitting in an inbox next to
# "OpenAI" and "Axis Bank Cards" looking like something that got past a filter.
# The address is unchanged; only what the reader sees is.
def _from(address: str, label: str = "Expenze") -> str:
    return address if "<" in address else f"{label} <{address}>"

SESSION_SECRET_ARN = os.environ["SESSION_SECRET_ARN"]
ALLOWED_ORIGINS = [o for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o]

CODE_TTL_SECONDS = 10 * 60
SESSION_TTL_SECONDS = 12 * 60 * 60
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60
MAX_REQUESTS_PER_HOUR = 5
TRIAL_CREDITS = 50

# A person's name, as it appears on every claim they decide, in the invitation
# that introduces them, and in the queue beside their receipts. Required: a
# membership with no name is displayed by the local part of its email, which
# is how the account that created this organisation spent a fortnight being
# called "riyad". Short, because it is a name and not a description - and
# because it sits in table cells and on tabs that have to hold it.
MIN_NAME = 3
MAX_NAME = 20

# What payroll and finance find somebody by. Short enough to sit beside a name
# in a table cell without pushing the address off the row.
MAX_STAFF_ID = 10

# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------
#
# Four roles in one order, and every question about authority is answered by
# comparing two numbers.
#
#   owner   one per organisation. Appoints and removes admins, and is the only
#           role that can hand the organisation to somebody else.
#   admin   as many as are needed. Runs the organisation day to day, but may
#           not touch another admin or the owner - so an administrator cannot
#           quietly promote themselves or lock the owner out.
#   finance reviews claims and settles them, and manages the people who submit.
#   staff   sends receipts.
#
# The rule everything below reads: **you may act on a role strictly beneath
# your own, and never on your own rank or above it.** That is what stops an
# admin demoting the admin who appointed them, and it is one comparison rather
# than a list of pairs that has to be kept in step with itself.
#
# `owner` is the exception it looks like: exactly one exists, so appointing
# another is not appointing, it is transferring - a different act with a
# different confirmation, handled in `_transfer_ownership`.
RANK = {"staff": 0, "finance": 1, "admin": 2, "owner": 3}
ROLES = tuple(RANK)

# What each is called on screen. Kept here beside the ranks so the server and
# the console cannot drift about what a role is named.
ROLE_LABEL = {"staff": "Submitter", "finance": "Finance Executive",
              "admin": "Administrator", "owner": "Owner"}


def rank_of(member: Any) -> int:
    """How much authority a membership carries. Unknown roles carry none."""
    if isinstance(member, str):
        return RANK.get(member, 0)
    return RANK.get(str((member or {}).get("role") or "staff"), 0)


def runs_the_org(who: Any) -> bool:
    """Whether this person works on the organisation rather than only in it.

    Every gate on the administration surface was written as the literal tuple
    `("owner", "finance")` - policy rules, budgets, groups, people, the review
    queue, settlement, reports, channels, the lot. That tuple was correct on
    the day it was written, when those were the only roles above a submitter.

    Administrators were added afterwards: a rank, a label, a role picker, a
    rule about who may appoint whom. The gates were not revisited, so `admin`
    matched none of them and an administrator - who *outranks* a finance
    executive - could reach less of the product than the person below them and
    no more than a submitter. Rana Ghosh signed in as an Administrator and the
    console said SUBMITTER over his name, which was wrong about his role and
    right about what the server would have let him do.

    A rank comparison instead of a list, so the next role to be added is
    placed by where it ranks rather than by remembering every gate it belongs
    in. Finance is the floor: below it is somebody who only sends receipts.
    """
    return rank_of(who) >= RANK["finance"]


def may_review(who: Any) -> bool:
    """Whether this person decides claims, as opposed to paying them.

    Reviewing and paying are two jobs, and giving both to one person removes
    the only check the product has: a reviewer decides what the company owes,
    and somebody else moves the money. A finance executive who could also
    approve could approve their own work into their own payment run, and
    nothing downstream would notice.

    So the floor is administrator, not finance - deliberately higher than
    `runs_the_org`, which is the gate for everything a finance executive does
    need: the settlement, the payment run, the people list, the reports.

    One review action is not here, and deliberately. Sending a claim *back*
    for review - `disputed` - is a finance executive saying they do not agree
    with what the agent cleared, and it decides nothing: it hands the claim to
    the people whose job deciding is. Taking that away would leave finance
    with pay-it-or-refuse-it on a claim they doubt, which is the exact
    position that button was built to end.
    """
    return rank_of(who) >= RANK["admin"]


def may_manage(actor_role: Any, target_role: Any) -> bool:
    """Whether somebody may act on a person holding this role.

    Strictly greater, deliberately. Equal ranks managing each other is how two
    administrators end up able to remove one another, and how the last one
    standing is whoever clicked first.
    """
    return rank_of(actor_role) > rank_of(target_role)


def owners_of(org_id: str) -> list[dict[str, Any]]:
    """Every active owner of one organisation.

    There should be exactly one. The list is returned rather than a count so a
    caller can say who, and so a repair can see an organisation that somehow
    has two.
    """
    return [m for m in identity.members_of(org_id)
            if str(m.get("role") or "") == "owner"
            and str(m.get("status") or "active") != "removed"]


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

_ddb = boto3.resource("dynamodb")
_table = _ddb.Table(AUTH_TABLE)
_users = _ddb.Table(USERS_TABLE)
_orgs = _ddb.Table(ORGS_TABLE)
_admins = _ddb.Table(ADMINS_TABLE)
_submissions = _ddb.Table(INTAKE_TABLE)
_settings = _ddb.Table(SETTINGS_TABLE) if SETTINGS_TABLE else None
_ledger = _ddb.Table(LEDGER_TABLE) if LEDGER_TABLE else None
_advances = _ddb.Table(ADVANCES_TABLE) if ADVANCES_TABLE else None
_ses = boto3.client("sesv2", region_name=SES_REGION)
_secrets = boto3.client("secretsmanager")
_session_key: bytes | None = None


def _key() -> bytes:
    global _session_key
    if _session_key is None:
        raw = _secrets.get_secret_value(SecretId=SESSION_SECRET_ARN)["SecretString"]
        _session_key = raw.encode()
    return _session_key


# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------


def _hash_code(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def _new_code() -> str:
    """Six digits, uniformly distributed, from a CSPRNG."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _sign_session(email: str) -> str:
    expires = int(time.time()) + SESSION_TTL_SECONDS
    payload = json.dumps({"email": email, "exp": expires}, separators=(",", ":"))
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _send_code(email: str, code: str) -> None:
    # The code leads the subject so it is readable in a phone's notification
    # preview without opening the mail.
    subject = f"{code} is your Expenze sign-in code"
    text = (
        f"{code}\n\n"
        "Enter this code to sign in to Expenze. It expires in 10 minutes and can "
        "be used once.\n\n"
        "If you did not ask to sign in, ignore this email - nobody can use the "
        "code without it.\n\n"
        "Expenze - expenze.ai\n"
    )
    body_html = f"""<!doctype html><html><body style="margin:0;padding:24px;background:#F7F6F2;
font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#171A18">
<div style="max-width:420px;margin:0 auto;background:#fff;border:1px solid #DDDCD4;border-radius:4px;padding:28px">
<p style="margin:0 0 4px;font-size:20px;font-weight:700;letter-spacing:-.02em">Expenze<span style="color:#1D4E3A">.</span></p>
<p style="margin:0 0 20px;color:#6E736C;font-size:13px">Agentic expense auditing</p>
<p style="margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#6E736C">Your sign-in code</p>
<p style="margin:0 0 18px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:34px;font-weight:700;letter-spacing:.16em;color:#1D4E3A">{code}</p>
<p style="margin:0 0 12px;font-size:14px;line-height:1.55">Enter this code to sign in. It expires in 10 minutes and can be used once.</p>
<p style="margin:0;font-size:13px;line-height:1.55;color:#6E736C">If you did not ask to sign in, ignore this email &mdash; nobody can use the code without it.</p>
</div></body></html>"""

    _ses.send_email(
        FromEmailAddress=_from(SENDER),
        Destination={"ToAddresses": [email]},
        Content={
            "Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": text, "Charset": "UTF-8"},
                    "Html": {"Data": body_html, "Charset": "UTF-8"},
                },
            }
        },
    )


def _send_invite(email: str, org_name: str, inviter: str, role: str) -> None:
    """Tell someone they have been added, and how to accept.

    Acceptance is simply their first successful sign-in: it proves they control
    the mailbox, which is exactly the thing that gates receipts from it. There
    is no separate accept link to expire or leak.

    `inviter` is a person's name where one is on file. This used to be their
    email address, so an invitation read "riyad@mobil80.com added you" - an
    address is how a system refers to somebody, not how a colleague does, and
    the first thing the new person sees should read like a person.
    """
    subject = f"You have been added to {org_name} on Expenze"
    what = ("submit expense receipts" if role == "staff"
            else "review expense claims" if role == "finance"
            else "administer expenses")
    # Both are things people type. They were being dropped into the HTML body
    # untouched, which an apostrophe in a company name would survive and an
    # angle bracket would not - and a name is free text up to 120 characters.
    inviter_html, org_html = html.escape(inviter), html.escape(org_name)
    text = (
        f"{inviter} added you to {org_name} on Expenze, where you can {what}.\n\n"
        f"Sign in at https://expenze.ai/login.html using this address ({email}). "
        "We email you a six-digit code - there is no password.\n\n"
        "Signing in once activates your account. Until then nothing sent from "
        "this address is processed.\n\n"
        "TO SEND RECEIPTS FROM WHATSAPP TOO\n"
        "Email works the moment you sign in. WhatsApp needs one extra step, "
        "and only you can take it: sign in to the portal, add your own mobile "
        "number under My expenses, and verify it with the code we send you on "
        "WhatsApp. Nobody can add a number on your behalf - which is what "
        "stops anyone claiming expenses as you.\n\n"
        "Once it is verified you can photograph a bill and send it on "
        "WhatsApp, and the answer comes back in the same chat. No app, and no "
        "signing in again.\n\n"
        "If you were not expecting this, ignore the email - nothing happens "
        "until you sign in.\n\nExpenze - expenze.ai\n"
    )
    body_html = f"""<!doctype html><html><body style="margin:0;padding:24px;background:#F7F6F2;
font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#171A18">
<div style="max-width:460px;margin:0 auto;background:#fff;border:1px solid #DDDCD4;border-radius:4px;padding:28px">
<p style="margin:0 0 4px;font-size:20px;font-weight:700;letter-spacing:-.02em">Expenze<span style="color:#1D4E3A">.</span></p>
<p style="margin:0 0 20px;color:#6E736C;font-size:13px">Agentic expense auditing</p>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6"><strong>{inviter_html}</strong> added you to
<strong>{org_html}</strong>, where you can {what}.</p>
<p style="margin:0 0 20px"><a href="https://expenze.ai/login.html"
style="display:inline-block;background:#1D4E3A;color:#F7F6F2;text-decoration:none;
padding:11px 20px;border-radius:4px;font-weight:600;font-size:15px">Sign in to Expenze</a></p>
<p style="margin:0 0 12px;font-size:14px;line-height:1.6">Use this address ({email}). We email a
six-digit code &mdash; there is no password. <strong>Signing in once activates your account;</strong>
until then nothing sent from this address is processed.</p>
<p style="margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:#6E736C">To send receipts from WhatsApp too</p>
<p style="margin:0 0 12px;font-size:14px;line-height:1.6">Email works the moment you sign in.
WhatsApp needs one extra step, and only you can take it: sign in to the portal, add your own
mobile number under <strong>My expenses</strong>, and verify it with the code we send you on
WhatsApp. Nobody can add a number on your behalf &mdash; which is what stops anyone claiming
expenses as you.</p>
<p style="margin:0 0 12px;font-size:14px;line-height:1.6">Once it is verified you can photograph a
bill and send it on WhatsApp, and the answer comes back in the same chat. No app, and no signing
in again.</p>
<p style="margin:0;font-size:13px;line-height:1.55;color:#6E736C">If you were not expecting this,
ignore this email &mdash; nothing happens until you sign in.</p>
</div></body></html>"""
    _ses.send_email(
        FromEmailAddress=_from(SENDER),
        Destination={"ToAddresses": [email]},
        Content={"Simple": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": text, "Charset": "UTF-8"},
                     "Html": {"Data": body_html, "Charset": "UTF-8"}},
        }},
    )


def _invite(actor_token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Add someone to the inviter's organisation and email them."""
    actor = _identity_from_token(actor_token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)

    membership = identity.resolve_by_email(actor, channel=None)
    # Ranked, not listed. The literal tuple here was ("owner", "finance"),
    # written before administrators existed as a role - so an administrator,
    # who outranks a finance executive, was refused permission to invite
    # anybody at all while the person below them could. Which role they may
    # hand out is a separate question, and `may_manage` below answers it.
    if not membership or rank_of(membership) < RANK["finance"]:
        return _reply(403, {"error": "Only an owner, administrator or finance "
                                     "executive can invite people."}, origin)

    email = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email):
        return _reply(400, {"error": "Enter a valid email address."}, origin)
    # The same rules the console checks, worded the same way, so a refusal
    # reads identically wherever it came from - and enforced here because the
    # console is a convenience, not a gate.
    name = str(body.get("name", "")).strip()
    if not name:
        return _reply(400, {
            "error": "Enter their name. It is how they appear on every claim they send."
        }, origin)
    if not MIN_NAME <= len(name) <= MAX_NAME:
        return _reply(400, {
            "error": f"A name is between {MIN_NAME} and {MAX_NAME} characters."}, origin)

    staff_id = str(body.get("staff_id", "")).strip()
    if not staff_id:
        return _reply(400, {
            "error": "Enter their staff ID — it is how payroll and finance find them."
        }, origin)
    if len(staff_id) > MAX_STAFF_ID:
        return _reply(400, {
            "error": f"A staff ID is at most {MAX_STAFF_ID} characters."}, origin)

    role = str(body.get("role", "staff")).strip().lower()
    if role not in ROLES:
        return _reply(400, {
            "error": "Role must be " + ", ".join(ROLE_LABEL[r] for r in ROLES) + "."}, origin)
    if role == "owner":
        return _reply(400, {
            "error": "An organisation has one owner, and it cannot be created by "
                     "invitation. Invite them, then transfer ownership."}, origin)
    # The same rule `_member_update` applies to changing somebody's role. An
    # invitation is the quieter of the two doors to the same privilege, and the
    # one nobody thinks to lock: without this an administrator could invite a
    # second administrator and then be unable to explain where they came from.
    if not may_manage(membership.get("role"), role):
        return _reply(403, {
            "error": f"Only an owner can invite an {ROLE_LABEL[role]}."
                     if role == "admin" else
                     f"You cannot invite a {ROLE_LABEL[role]}."}, origin)

    org_id = membership["org_id"]
    org_name = membership.get("org_name", "your organisation")
    org = _orgs.get_item(Key={"org_id": org_id}).get("Item") or {}
    now = int(time.time())

    # Which group their receipts are tagged with from the first one they send.
    # Asked at the invitation because that is the moment somebody knows the
    # answer - they are adding a named person to a named team. Everyone landing
    # in the default group and being moved afterwards meant the move was a
    # second job nobody remembered, and spend sat under the wrong cost centre
    # until a report looked wrong.
    #
    # Everybody belongs to one. A person with no group has receipts that
    # attribute to nothing, and they land in the unattributed bucket every
    # report then has to explain - so an unknown or missing group falls back to
    # the default rather than being stored as nothing. The console asks for it
    # outright; this is the floor under that, for any caller that does not.
    all_groups = org.get("groups") or []
    known = {g["id"] for g in all_groups}
    # The default if one is marked, otherwise the first - an organisation whose
    # list was rebuilt without the flag still has somewhere to put people.
    fallback = next((g["id"] for g in all_groups if g.get("default")),
                    all_groups[0]["id"] if all_groups else None)
    wanted = str(body.get("group", "")).strip()
    chosen = wanted if wanted in known else fallback
    groups = [chosen] if chosen else []

    _users.put_item(Item={
        "email": email,
        "org_id": org_id,
        "org_name": org_name,
        "name": name,
        "staff_id": staff_id,
        "role": role,
        "groups": groups,
        # Invited, not active. Nothing sent from this address is processed
        # until they sign in, which is what proves they own the mailbox.
        "status": "invited",
        "email_channel": "pending",
        "whatsapp_channel": "not_added",
        "invited_by": actor,
        "added_at": now,
    })
    # Named, not addressed. Falls back to the address only when the inviter
    # has no name on file - which is better than "someone", and is a state the
    # sign-up form no longer creates.
    try:
        _send_invite(email, org_name,
                     str(membership.get("name") or "").strip() or actor, role)
    except ClientError:
        logger.exception("invite email failed")
        return _reply(502, {"error": "Added, but the invitation email could not be sent."}, origin)

    _logged({"org_id": org_id, "name": str(membership.get("name") or ""),
             "role": membership.get("role") or ""}, actor,
            "person invited", who=email,
            detail=f"as {role}" + (f", group {groups[0]}" if groups else ""))
    logger.info("%s invited %s to %s as %s in %s", actor, email, org_id, role,
                groups[0] if groups else "no group")
    return _reply(200, {"invited": email, "role": role, "org_name": org_name,
                        "groups": groups}, origin)


# ---------------------------------------------------------------------------
# WhatsApp number, added and verified by its owner
# ---------------------------------------------------------------------------

def _wa() -> dict[str, str]:
    """The WhatsApp identity to send from - see wacfg.py for why it expires."""
    return wacfg.config(WA_SECRET_ARN)


def _send_wa_code(number: str, code: str) -> None:
    """Send the code over WhatsApp, to the number being claimed.

    Deliberately not email. The thing being authorised is "receipts from this
    WhatsApp account are yours", so the proof has to be control of that
    account - an emailed code proves only that they still read their email.
    """
    import urllib.request

    cfg = _wa()
    # An approved AUTHENTICATION template, not free-form text. WhatsApp only
    # delivers free-form messages to someone who wrote to the business number
    # in the last 24 hours - and a person adding their number for the first
    # time has, by definition, never written to it. Sent as text this code
    # would be accepted by Meta and quietly never arrive.
    #
    # Template name and language come from the secret so that moving to a
    # dedicated Expenze number with its own approved template is a secret
    # update, exactly like the number itself.
    template = cfg.get("otpTemplate", "cloudmeter_verify_code")
    language = cfg.get("otpTemplateLanguage", "en")
    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to": re.sub(r"[^\d]", "", number),
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": language},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": code}]},
                # An authentication template's copy-code button carries the code
                # again; omitting it is rejected as a mismatched component.
                {"type": "button", "sub_type": "url", "index": "0",
                 "parameters": [{"type": "text", "text": code}]},
            ],
        },
    }).encode()
    req = urllib.request.Request(
        f"https://graph.facebook.com/v21.0/{cfg['phoneNumberId']}/messages", data=payload)
    req.add_header("Authorization", f"Bearer {cfg['accessToken']}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=25) as r:
        r.read()


def _wa_add(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """A person claims their own number. Nobody can do this on their behalf."""
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)

    membership = identity.resolve_by_email(actor, channel=None)
    if not membership:
        return _reply(403, {"error": "No membership for this account."}, origin)

    number = identity.normalise_mobile(str(body.get("mobile", "")))
    if len(re.sub(r"[^\d]", "", number)) < 8:
        return _reply(400, {"error": "Enter your number in international format, e.g. +91 98450 11237."}, origin)

    # One number, one claimant. Two active memberships on the same number would
    # make an inbound receipt ambiguous, and ambiguity here spends the wrong
    # organisation's credits.
    for row in _users.query(
        IndexName=os.environ.get("MOBILE_INDEX", "by-mobile"),
        KeyConditionExpression=Key("mobile").eq(number),
    ).get("Items", []):
        if row.get("whatsapp_channel") == "active" and row.get("email") != actor:
            return _reply(409, {"error": "That number is already verified on another account."}, origin)

    now = int(time.time())
    code = _new_code()
    salt = secrets.token_hex(8)
    _table.put_item(Item={
        "email": f"wa:{actor}",
        "code_hash": _hash_code(code, salt),
        "salt": salt,
        "attempts": 0,
        "mobile": number,
        "last_sent_at": now,
        "window_start": now,
        "sent_in_window": 1,
        "expires_at": now + CODE_TTL_SECONDS,
    })
    _users.update_item(
        Key={"email": actor, "org_id": membership["org_id"]},
        UpdateExpression="SET mobile = :m, whatsapp_channel = :p",
        ExpressionAttributeValues={":m": number, ":p": "pending"},
    )
    try:
        _send_wa_code(number, code)
    except Exception:
        logger.exception("could not send the WhatsApp code")
        return _reply(502, {"error": "Could not send a WhatsApp message to that number. Check it and try again."}, origin)

    logger.info("%s claimed a WhatsApp number; code sent", actor)
    return _reply(200, {"mobile": number, "status": "pending", "expires_in": CODE_TTL_SECONDS}, origin)


def _wa_verify(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)

    membership = identity.resolve_by_email(actor, channel=None)
    if not membership:
        return _reply(403, {"error": "No membership for this account."}, origin)

    item = _table.get_item(Key={"email": f"wa:{actor}"}).get("Item")
    invalid = _reply(401, {"error": "That code is not valid or has expired."}, origin)
    if not item or int(item.get("expires_at", 0)) < int(time.time()):
        return invalid

    attempts = int(item.get("attempts", 0))
    if attempts >= MAX_ATTEMPTS:
        _table.delete_item(Key={"email": f"wa:{actor}"})
        return _reply(429, {"error": "Too many attempts. Add the number again."}, origin)

    code = re.sub(r"[^\d]", "", str(body.get("code", "")))
    if not hmac.compare_digest(str(item["code_hash"]), _hash_code(code, str(item["salt"]))):
        _table.update_item(
            Key={"email": f"wa:{actor}"},
            UpdateExpression="SET attempts = :a",
            ExpressionAttributeValues={":a": attempts + 1},
        )
        return invalid

    _table.delete_item(Key={"email": f"wa:{actor}"})
    _users.update_item(
        Key={"email": actor, "org_id": membership["org_id"]},
        UpdateExpression="SET whatsapp_channel = :a, mobile = :m, whatsapp_verified_at = :t",
        ExpressionAttributeValues={":a": "active", ":m": str(item["mobile"]), ":t": int(time.time())},
    )
    logger.info("%s verified their WhatsApp number", actor)
    return _reply(200, {"mobile": item["mobile"], "status": "active"}, origin)


def _platform_channels() -> dict[str, str]:
    """The WhatsApp number and intake mailbox, as configured in BMS.

    Returned empty rather than guessed at: a console that invents a number
    tells staff to photograph receipts into something that answers nothing.
    """
    if _settings is None:
        return {"whatsapp": "", "email": ""}
    try:
        row = _settings.get_item(Key={"key": "platform"}).get("Item") or {}
    except Exception:
        logger.exception("could not read platform settings")
        return {"whatsapp": "", "email": ""}
    return {"whatsapp": str(row.get("whatsapp_number", "") or ""),
            "email": str(row.get("intake_email", "") or "")}


def _me(token: str, origin: str | None) -> dict[str, Any]:
    """What the signed-in person's own account looks like."""
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    m = identity.resolve_by_email(actor, channel=None)
    if not m:
        return _reply(403, {"error": "No membership for this account."}, origin)
    return _reply(200, {"email": actor, "name": m.get("name", ""), "role": m.get("role", "staff"),
                        "org_name": m.get("org_name", ""), "status": m.get("status"),
                        "mobile": m.get("mobile", ""),
                        "whatsapp_channel": m.get("whatsapp_channel", "not_added"),
                        # Where receipts are sent. One number and one mailbox
                        # serve every customer, set in BMS - so the console
                        # reads them rather than carrying its own copy to drift.
                        "channels": _platform_channels()}, origin)


# ---------------------------------------------------------------------------
# Organisation profile and groups
# ---------------------------------------------------------------------------

# One list, in money.py, because every country on it needs a currency and the
# two drifting apart is how an organisation ends up with no default at all.
COUNTRIES = money.COUNTRIES


def _org_of(actor: str) -> dict[str, Any] | None:
    m = identity.resolve_by_email(actor, channel=None)
    if not m:
        return None
    org = _orgs.get_item(Key={"org_id": m["org_id"]}).get("Item")
    if org:
        org["_role"] = m.get("role", "staff")
    return org


def _address_from(body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    def pick(key, limit=120):
        return str(body.get(key, current.get(key, "")) or "").strip()[:limit]
    return {
        "line1": pick("line1"), "line2": pick("line2"),
        "city": pick("city", 80), "state": pick("state", 80),
        "postcode": pick("postcode", 24), "country": pick("country", 8).upper(),
    }


def _billing_view(org: dict[str, Any]) -> dict[str, Any]:
    """The published slabs, priced for this organisation.

    Computed here rather than in the browser so the figure on the button is the
    same one the order is created for. A page that works out its own price will
    eventually show one the checkout disagrees with.
    """
    pricing, gst = _platform_pricing()
    currency = payments.checkout_currency(org)
    slabs = []
    for slab in pricing:
        try:
            quote = payments.price_breakdown(slab, currency, gst)
        except payments.PaymentError as exc:
            # A slab with no price in this currency cannot be sold here. Listed
            # anyway, marked, so the gap is visible rather than a missing row.
            slabs.append({"credits": int(slab.get("credits") or 0),
                          "sellable": False, "why": str(exc)})
            continue
        slabs.append({
            "credits": int(slab.get("credits") or 0),
            "sellable": True,
            "currency": currency,
            "base": quote["base"], "tax": quote["tax"],
            "tax_label": quote["tax_label"], "total": quote["total"],
            "per_receipt": str(Decimal(quote["total"]) / Decimal(max(1, int(slab.get("credits") or 1)))),
        })
    return {"currency": currency, "gst_percent": str(gst), "slabs": slabs}


# ---------------------------------------------------------------------------
# Cash advances - the float a handful of people hold and spend from
# ---------------------------------------------------------------------------
#
# Housekeeping and the canteen are not bought on anybody's own card. A few
# people are handed cash, they spend it through the month, and they submit the
# receipts here like everybody else. What was missing was the other half of
# that arrangement: how much of the company's money each of them is holding
# right now.
#
# A float holder's claim reaches Pending settlement like anybody's. What is
# different is the choice finance makes there, and it is a choice between two
# genuinely different acts that the word "settle" hides:
#
# **From the float.** No money moves. They already spent the company's cash;
# the settlement is the company accounting for it, so the advance is drawn
# down by that amount. Finance replenishes by advancing more.
#
# **A separate payout.** An ordinary reimbursement - a bill too large for the
# float, or one they paid personally. It must leave the float alone, or the
# next replenishment is calculated against a balance that never moved.
#
# So:
#
#     still out with them = advanced - returned - settled from the float
#
# and the claims they have spent but which are not yet settled are reported
# beside it rather than inside it. That gap is the honest thing to show: the
# cash is gone from their hands and the advance has not yet been drawn down
# for it, so the two figures answer different questions - what the company has
# not accounted for, and what the holder is actually carrying.
#
# Append-only. A balance that is stored drifts from the events that produced
# it, and the events are what somebody asks about when the money does not add
# up. A correction is another row, never an edit.

# What a movement can be. `advance` is cash out to the holder; `return` is cash
# they hand back - leaving the role, or finance reducing a float that is larger
# than the job needs.
ADVANCE_KINDS = ("advance", "return")


def _advance_record(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Log cash paid to a float holder, or handed back by one."""
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    acting = identity.resolve_by_email(actor, channel=None)
    if not acting:
        return _reply(403, {"error": "No membership for this account."}, origin)
    # The same gate as recording a settlement: this is money leaving the
    # company, and it is the same people who record money leaving the company.
    if not runs_the_org(acting):
        return _reply(403, {
            "error": "Only an owner, administrator or finance executive can "
                     "record an advance."}, origin)

    org = _org_of(actor) or {}
    org_id = str(org.get("org_id") or acting.get("org_id") or "")

    email = str(body.get("holder", "")).strip().lower()
    if not EMAIL_RE.match(email):
        return _reply(400, {"error": "Say who the advance is for."}, origin)
    holder = identity.resolve_by_email(email, channel=None)
    if not holder or str(holder.get("org_id") or "") != org_id:
        return _reply(404, {"error": "No such person in this organisation."}, origin)
    # A float is cash in somebody's hands. Handing more to somebody whose
    # access has been taken away is the one case worth refusing outright.
    if str(holder.get("status") or "active") == "removed":
        return _reply(409, {
            "error": f"{holder.get('name') or email} is no longer on the roll. "
                     "Record what they hand back, not more cash out."}, origin)

    kind = str(body.get("kind", "advance")).strip().lower()
    if kind not in ADVANCE_KINDS:
        return _reply(400, {"error": "An advance is either paid out or handed back."},
                      origin)

    amount = _as_decimal(body.get("amount"))
    if amount <= 0:
        return _reply(400, {"error": "Enter the amount handed over."}, origin)

    currency = money.normalise(str(body.get("currency", ""))) or money.default_for_org(org)
    now = int(time.time())
    row = {
        "org_id": org_id,
        # Milliseconds, because two advances recorded in one sitting must not
        # collide on the sort key and silently overwrite each other.
        "ts": int(time.time() * 1000),
        "kind": kind,
        "holder": email,
        "holder_name": str(holder.get("name") or email),
        "amount": str(amount),
        "currency": currency,
        "mode": str(body.get("mode", ""))[:40],
        "reference": str(body.get("reference", ""))[:80],
        "note": str(body.get("note", ""))[:300],
        "by": actor,
        "by_name": acting.get("name") or actor,
        "at": now,
    }
    if _advances is None:
        return _reply(503, {"error": "Advances are not configured."}, origin)
    _advances.put_item(Item=row)

    _logged(acting, actor,
            "advance paid" if kind == "advance" else "advance returned",
            None, detail=f"{email} {amount} {currency}"
            + (f" · {row['mode']}" if row["mode"] else ""),
            amount=str(amount), currency=currency, reason=row["note"])
    logger.info("%s recorded a %s of %s %s for %s", actor, kind, amount, currency, email)
    return _reply(200, {"status": kind, "holder": email, "amount": str(amount),
                        "currency": currency, "ts": row["ts"]}, origin)


def _advances_view(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Every float holder, what they hold, and the movements behind it.

    The three components are returned separately and the balance with them.
    Finance does not have to take one number on trust, and when it looks wrong
    they can see which of the three is the surprise without opening a ledger.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    acting = identity.resolve_by_email(actor, channel=None)
    if not acting:
        return _reply(403, {"error": "No membership for this account."}, origin)
    if not runs_the_org(acting):
        return _reply(403, {
            "error": "Only an owner, administrator or finance executive can see "
                     "the float."}, origin)

    org = _org_of(actor) or {}
    org_id = str(org.get("org_id") or acting.get("org_id") or "")
    base = money.default_for_org(org)

    rows = []
    if _advances is not None:
        rows = _advances.query(
            KeyConditionExpression=Key("org_id").eq(org_id),
            ScanIndexForward=False,
        ).get("Items", [])

    # Somebody holds a float because cash was handed to them, not because a
    # box was ticked. Deriving it from the ledger means there is no flag to
    # set, none to forget, and none that can disagree with the money.
    holders: dict[str, dict[str, Any]] = {}
    for r in rows:
        email = str(r.get("holder") or "").lower()
        if not email:
            continue
        who = holders.setdefault(email, {
            "email": email, "name": str(r.get("holder_name") or email),
            "advanced": Decimal("0"), "returned": Decimal("0"),
            "from_float": Decimal("0"), "paid_out": Decimal("0"),
            "pending": Decimal("0"),
            "currency": str(r.get("currency") or base),
            "last_at": 0, "movements": 0,
        })
        amount = _as_decimal(r.get("amount"))
        if str(r.get("kind") or "") == "return":
            who["returned"] += amount
        else:
            who["advanced"] += amount
        who["movements"] += 1
        who["last_at"] = max(who["last_at"], int(r.get("at") or 0))

    # Read off the claims themselves rather than stored anywhere, so a claim
    # settled a minute ago is already in the figure.
    if holders:
        for claim in _submissions_tbl_scan(org_id):
            email = str(claim.get("submitted_by") or "").lower()
            who = holders.get(email)
            if who is None:
                continue
            # Withdrawn claims never happened.
            if str(claim.get("review_action") or "") == "withdrawn":
                continue
            settled = str(claim.get("outcome") or "") == "settled"
            if settled and str(claim.get("outcome_source") or "") == "float":
                # Accounted for out of the advance: this is what draws it down.
                who["from_float"] += _as_decimal(claim.get("outcome_paid"))
            elif settled:
                # Reimbursed separately. Recorded because finance will ask why
                # a holder's spend and their float do not line up, and this is
                # the answer - but it does not touch the balance.
                who["paid_out"] += _as_decimal(claim.get("outcome_paid"))
            else:
                # Spent, not yet settled. The cash has left their hands and
                # the advance has not been drawn down for it yet.
                who["pending"] += _as_decimal(
                    (claim.get("verdict") or {}).get("receipt_total"))

    people = []
    for who in holders.values():
        out = who["advanced"] - who["returned"] - who["from_float"]
        people.append({
            "email": who["email"],
            "name": who["name"],
            "currency": who["currency"],
            "advanced": str(who["advanced"]),
            "returned": str(who["returned"]),
            "from_float": str(who["from_float"]),
            "paid_out": str(who["paid_out"]),
            "pending": str(who["pending"]),
            # What the company has advanced and not yet accounted for.
            "held": str(out),
            # And what they are actually carrying, once the claims already
            # spent but not yet settled are taken off it. The figure that
            # tells finance whether they can still buy next week's groceries.
            "in_hand": str(out - who["pending"]),
            "last_at": who["last_at"],
            "movements": who["movements"],
        })
    people.sort(key=lambda p: _as_decimal(p["held"]))

    return _reply(200, {
        "currency": base,
        "holders": people,
        "movements": [{
            "ts": int(r.get("ts") or 0),
            "at": int(r.get("at") or 0),
            "kind": str(r.get("kind") or "advance"),
            "holder": str(r.get("holder") or ""),
            "holder_name": str(r.get("holder_name") or ""),
            "amount": str(r.get("amount") or "0"),
            "currency": str(r.get("currency") or base),
            "mode": str(r.get("mode") or ""),
            "reference": str(r.get("reference") or ""),
            "note": str(r.get("note") or ""),
            "by": str(r.get("by_name") or r.get("by") or ""),
        } for r in rows[:200]],
    }, origin)


def _credits_ledger(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Every credit movement on this organisation, newest first.

    Credits are money. A balance that changed with no record of who changed it
    or why is not auditable, so this is the record rather than a summary.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    org = _org_of(actor)
    if not org:
        return _reply(403, {"error": "No organisation for this account."}, origin)

    rows = _ledger.query(
        KeyConditionExpression=Key("org_id").eq(org["org_id"]),
        ScanIndexForward=False,
        Limit=60,
    ).get("Items", [])
    return _reply(200, {"entries": [{
        "ts": int(r.get("ts") or 0),
        "delta": int(r.get("delta") or 0),
        "reason": r.get("reason", ""),
        "by": r.get("by", ""),
        "amount": str(r.get("amount", "")),
        "currency": r.get("currency", ""),
        "note": r.get("note", ""),
    } for r in rows], "balance": int(org.get("credits") or 0)}, origin)


def _org_get(token: str, origin: str | None) -> dict[str, Any]:
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    org = _org_of(actor)
    if not org:
        return _reply(403, {"error": "No organisation for this account."}, origin)
    country = (org.get("address") or {}).get("country", "")
    return _reply(200, {
        "org_id": org["org_id"],
        "name": org.get("name", ""),
        "tax_id": org.get("tax_id", ""),
        "billing_email": org.get("billing_email", org.get("root_email", "")),
        "address": org.get("address", {}),
        "groups": org.get("groups", []),
        # What revision of the group list this is. Sent back on a save so a
        # second administrator's additions cannot be silently replaced by a
        # copy of the list that predates them.
        "groups_rev": int(org.get("groups_rev") or 0),
        "countries": COUNTRIES,
        # Credits are the commercial surface: the balance, what it has cost so
        # far, and when this organisation started - which is also the earliest
        # month any report can cover.
        "credits": int(org.get("credits") or 0),
        "receipts_processed": int(org.get("receipts_processed") or 0),
        "trial_granted": bool(org.get("trial_granted")),
        "created_at": int(org.get("created_at") or 0),
        # Priced where the customer is. An Indian organisation is quoted in
        # rupees with GST on top; everyone else in dollars.
        "billing": _billing_view(org),
        # What a receipt is read as when it does not say - which is most
        # handwritten bills. Shown, not hidden, because it changes verdicts.
        "default_currency": money.default_for_org(org),
        "currency_follows_country": not money.is_currency(org.get("default_currency")),
        "country_currency": money.for_country(country),
        "currencies": {code: meta["name"] for code, meta in sorted(money.CURRENCIES.items())},
        # What was meant to be spent. Never enforced on intake - see budgets.py.
        "budgets": org.get("budgets") or budget_rules.empty(money.default_for_org(org)),
        # The policy this organisation is actually judged by, and who changed
        # it last. Falls back to the built-in set for an account that has never
        # saved one, which is what the auditor falls back to as well.
        "rules": policy.rules_for(org),
        "rules_changelog": [h for h in (org.get("rules_changelog") or [])
                            if isinstance(h, dict)][:50],
        "can_edit": runs_the_org(org["_role"]),
    }, origin)


def _rules_put(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Replace the organisation's expense policy.

    This did not exist, and its absence was invisible: the Policy rules tab
    kept every change in the browser, bumped a version number nobody stored,
    and the auditor went on judging every receipt by the built-in set. An owner
    could add a type, change a cap, disable one, watch the console recompute in
    front of them - and nothing about any receipt ever changed. The console was
    describing a policy the engine had never been given.

    Owner only, which is stricter than budgets. A budget flags spend after the
    fact; a policy decides what is paid, and it is the same bar as buying
    credits.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    org = _org_of(actor)
    if not org:
        return _reply(403, {"error": "No organisation for this account."}, origin)
    if org["_role"] != "owner":
        return _reply(403, {
            "error": "Only an administrator or the owner can change the expense policy."}, origin)

    try:
        cleaned = policy.normalise_rules(body.get("rules"))
    except policy.PolicyShapeError as exc:
        return _reply(400, {"error": str(exc)}, origin)
    except policy.PolicyInputError as exc:
        return _reply(400, {"error": str(exc)}, origin)

    # The version is ours to set, not the caller's: it is how a claim records
    # which policy judged it, so it has to move forward on every save whatever
    # the browser thinks it is.
    current = int((org.get("rules") or {}).get("version") or 0)
    cleaned["version"] = max(current, int(cleaned.get("version") or 1)) + 1

    # What changed, in the words of whoever changed it. Bounded: this is an
    # audit trail, not a log, and an unbounded list on a row read on every
    # audit is a cost paid by every receipt.
    entry = str(body.get("what", "")).strip()[:300]
    history = [h for h in (org.get("rules_changelog") or []) if isinstance(h, dict)]
    history.insert(0, {
        "version": cleaned["version"],
        "by": acting_name(org, actor),
        "at": int(time.time()),
        "what": entry or "Policy updated.",
    })

    _orgs.update_item(
        Key={"org_id": org["org_id"]},
        UpdateExpression=("SET #r = :r, rules_changelog = :h, "
                          "rules_updated_by = :u, rules_updated_at = :t"),
        ExpressionAttributeNames={"#r": "rules"},
        ExpressionAttributeValues={
            ":r": json.loads(json.dumps(cleaned)),
            ":h": history[:50],
            ":u": actor, ":t": int(time.time()),
        },
    )
    # Also in the audit log, and not only in `rules_changelog` above. That list
    # lives on the org row, is capped at fifty, and is rewritten on every save -
    # so the change that mattered is the one that eventually falls off the end,
    # and any later write can alter what it says happened.
    _logged({"org_id": org["org_id"], "name": acting_name(org, actor),
             "role": org["_role"]}, actor, "policy saved",
            detail=entry or "Policy updated.",
            version=int(cleaned["version"]),
            types=len(cleaned.get("expense_types") or []))

    logger.info("%s saved policy v%s for %s", actor, cleaned["version"], org["org_id"])
    return _reply(200, {"rules": cleaned, "changelog": history[:50]}, origin)


def _budgets_put(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Replace the organisation's budget configuration.

    Only an owner or a finance executive: a budget is a control on other
    people's spending, and someone being measured cannot be the one setting it.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    org = _org_of(actor)
    if not org:
        return _reply(403, {"error": "No organisation for this account."}, origin)
    if not runs_the_org(org["_role"]):
        return _reply(403, {"error": "Only an owner, administrator or finance executive can set budgets."}, origin)

    try:
        cleaned = budget_rules.normalise(body.get("budgets"), money.default_for_org(org))
    except budget_rules.BudgetInputError as exc:
        return _reply(400, {"error": str(exc)}, origin)

    _orgs.update_item(
        Key={"org_id": org["org_id"]},
        UpdateExpression="SET budgets = :b, budgets_updated_by = :u, budgets_updated_at = :ts",
        ExpressionAttributeValues={":b": cleaned, ":u": actor, ":ts": int(time.time())},
    )
    _logged({"org_id": org["org_id"], "name": acting_name(org, actor),
             "role": org["_role"]}, actor, "budgets saved",
            detail=(f"{cleaned.get('period', '')} · org {cleaned.get('org') or 'none'} · "
                    f"{len(cleaned.get('groups') or {})} groups · "
                    f"{len(cleaned.get('people') or {})} people"),
            currency=str(cleaned.get("currency") or ""))
    logger.info("%s updated budgets for %s", actor, org["org_id"])
    return _reply(200, {"budgets": cleaned}, origin)


def _org_put(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    org = _org_of(actor)
    if not org:
        return _reply(403, {"error": "No organisation for this account."}, origin)
    if not runs_the_org(org["_role"]):
        return _reply(403, {"error": "Only an owner, administrator or finance executive can change these."}, origin)

    address = _address_from(body.get("address") or {}, org.get("address") or {})
    if address["country"] and address["country"] not in COUNTRIES:
        return _reply(400, {"error": "Choose a country from the list."}, origin)

    billing_email = str(body.get("billing_email", org.get("billing_email", ""))).strip().lower()
    if billing_email and not EMAIL_RE.match(billing_email):
        return _reply(400, {"error": "Enter a valid billing email address."}, origin)

    # An empty value means "follow the country", so changing country moves the
    # currency with it. Only an explicit choice pins it, and only then does a
    # company incorporated in Singapore keep accounting in dollars.
    currency = money.normalise(body.get("default_currency"))
    if currency and not money.is_currency(currency):
        return _reply(400, {"error": "Choose a currency from the list."}, origin)

    _orgs.update_item(
        Key={"org_id": org["org_id"]},
        UpdateExpression=("SET tax_id = :t, address = :a, billing_email = :b, "
                          "default_currency = :c, "
                          "profile_updated_by = :u, profile_updated_at = :ts"),
        ExpressionAttributeValues={
            ":t": str(body.get("tax_id", org.get("tax_id", ""))).strip()[:40],
            ":a": address,
            ":b": billing_email,
            ":c": currency,
            ":u": actor,
            ":ts": int(time.time()),
        },
    )
    # The base currency is in here, and it is the one field on this form that
    # silently changes what every future claim is worth.
    _logged({"org_id": org["org_id"], "name": acting_name(org, actor),
             "role": org["_role"]}, actor, "organisation profile changed",
            currency=currency or "",
            detail=("base currency now "
                    + (currency or f"whatever {address['country'] or 'the country'} uses")))
    logger.info("%s updated the organisation profile", actor)
    return _org_get(token, origin)


def _groups_put(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Replace the group list. Groups tag an expense to a team, site or cost centre.

    The whole list is sent, so the write is a replace - and a replace driven by
    a copy a browser loaded some minutes ago will happily delete whatever
    somebody else added in between. Two people administering an organisation at
    the same time is not an edge case; it is the first week of any account.

    So the list carries a revision. The browser sends the one it loaded, and a
    write against a stale one is refused rather than applied. The caller is
    handed the current list with the refusal so it can show what it missed
    instead of simply losing the edit.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    org = _org_of(actor)
    if not org or not runs_the_org(org["_role"]):
        return _reply(403, {"error": "Only an owner, administrator or finance executive can manage groups."}, origin)

    current_rev = int(org.get("groups_rev") or 0)
    sent_rev = body.get("rev")
    if sent_rev is not None and int(sent_rev) != current_rev:
        return _reply(409, {
            "error": ("Someone else changed the groups while you were editing. "
                      "The list below is the current one - make your change again."),
            "groups": org.get("groups", []),
            "groups_rev": current_rev,
        }, origin)

    # The default group cannot be deleted: it is what makes every receipt
    # attributable without anyone configuring anything.
    existing_default = next((g for g in (org.get("groups") or []) if g.get("default")), None)
    seen, groups = set(), []
    if existing_default:
        seen.add(existing_default["id"])
        groups.append(existing_default)
    for raw in (body.get("groups") or [])[:60]:
        label = str(raw.get("label", "")).strip()[:60]
        if not label:
            continue
        gid = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or f"g{len(groups)}"
        if gid in seen:
            continue
        seen.add(gid)
        entry = {"id": gid, "label": label}
        # The registration this group is billed under. Normalised the way the
        # receipt will be, so a GSTIN typed with spaces here still matches one
        # printed without them.
        tax_id = taxid.normalise(raw.get("tax_id"))
        if tax_id:
            entry["tax_id"] = tax_id
        groups.append(entry)

    # The default group carries the organisation's own registration unless it
    # has been given one of its own - which is what makes matching work for the
    # common case of a company with one GSTIN and no per-site registrations.
    if groups and not groups[0].get("tax_id"):
        org_tax = taxid.normalise(org.get("tax_id"))
        if org_tax:
            groups[0]["tax_id"] = org_tax

    try:
        _orgs.update_item(
            Key={"org_id": org["org_id"]},
            UpdateExpression="SET #g = :g, groups_rev = :next",
            # Belt as well as braces. The check above closes the window a
            # person opens by leaving a tab sitting; this one closes the
            # milliseconds between reading the revision and writing it.
            ConditionExpression="attribute_not_exists(groups_rev) OR groups_rev = :cur",
            ExpressionAttributeNames={"#g": "groups"},
            ExpressionAttributeValues={":g": groups, ":cur": current_rev,
                                       ":next": current_rev + 1},
        )
    except _orgs.meta.client.exceptions.ConditionalCheckFailedException:
        fresh = _orgs.get_item(Key={"org_id": org["org_id"]}).get("Item") or {}
        return _reply(409, {
            "error": ("Someone else changed the groups at the same moment. "
                      "The list below is the current one - make your change again."),
            "groups": fresh.get("groups", []),
            "groups_rev": int(fresh.get("groups_rev") or 0),
        }, origin)

    # A deletion here is invisible on the row afterwards - the list is replaced
    # whole, so the group that is gone leaves nothing behind saying it existed.
    was = {str(g.get("id")) for g in (org.get("groups") or [])}
    now_ids = {g["id"] for g in groups}
    changes = ([f"added {g}" for g in sorted(now_ids - was)]
               + [f"removed {g}" for g in sorted(was - now_ids)])
    if changes:
        _logged({"org_id": org["org_id"], "name": acting_name(org, actor),
                 "role": org["_role"]}, actor, "groups changed",
                detail=", ".join(changes)[:600])
    logger.info("%s set %d groups (rev %d)", actor, len(groups), current_rev + 1)
    return _reply(200, {"groups": groups, "groups_rev": current_rev + 1}, origin)


# How long an invitation is left alone before it may be sent again.
#
# Two days, because the common reason one is unaccepted on the first morning is
# that the person has not opened their mail yet - and a second copy of the same
# message before they have read the first teaches them the sender is noise.
# It is also the floor between *any* two sends: nudging a nudge daily is how a
# product ends up in a spam folder, taking every future invitation with it.
REINVITE_AFTER = 48 * 3600


def _reinvite(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Send an outstanding invitation again.

    Twenty-six people invited and not signed in is not twenty-six people who
    declined - it is mostly a message read on a phone, meant to be dealt with
    later, and never dealt with. The cheapest way to onboard them is to ask
    again.

    Only what is genuinely outstanding, and only once the first one has had
    time to work. Everything else about the membership is left exactly as it
    was: this re-sends a message, it does not re-invite - the role, the groups
    and the staff id they were given still stand.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    org = _org_of(actor)
    if not org:
        return _reply(403, {"error": "No membership for this account."}, origin)

    target = identity.normalise_email(str(body.get("email", "")))
    if not target:
        return _reply(400, {"error": "Name the person to invite again."}, origin)

    member = identity.membership_in(org["org_id"], target)
    if not member:
        return _reply(404, {"error": "No member of this organisation by that address."},
                      origin)
    if not may_manage(org["_role"], member.get("role")):
        return _reply(403, {
            "error": f"You cannot manage a "
                     f"{ROLE_LABEL.get(str(member.get('role')), 'member')}."}, origin)
    if str(member.get("status") or "") != "invited":
        return _reply(400, {
            "error": "They have already accepted — there is nothing outstanding."}, origin)

    now = int(time.time())
    last = int(member.get("reinvited_at") or member.get("added_at") or 0)
    waited = now - last
    if last and waited < REINVITE_AFTER:
        hours = max(1, (REINVITE_AFTER - waited) // 3600)
        return _reply(429, {
            "error": f"That invitation went out less than 48 hours ago. "
                     f"You can send it again in about {hours} hours."}, origin)

    org_name = str(org.get("name") or member.get("org_name") or "your organisation")
    acting = identity.resolve_by_email(actor, channel=None) or {}
    try:
        _send_invite(target, org_name,
                     str(acting.get("name") or "").strip() or actor,
                     str(member.get("role") or "staff"))
    except ClientError:
        logger.exception("re-invite email failed for %s", target)
        return _reply(502, {
            "error": "The invitation could not be sent. Nothing was changed."}, origin)

    # Written after the send, not before: a counter that goes up when nothing
    # left the building is worse than no counter, because it starts the
    # 48-hour clock on a message nobody got.
    try:
        _users.update_item(
            Key={"email": target, "org_id": org["org_id"]},
            UpdateExpression=("SET reinvited_at = :t, "
                              "reinvites = if_not_exists(reinvites, :z) + :one"),
            ExpressionAttributeValues={":t": now, ":z": 0, ":one": 1},
        )
    except Exception:
        logger.exception("re-invited %s but could not record it", target)

    _logged(acting, actor, "invitation sent again", {"org_id": org["org_id"]},
            who=target,
            detail=f"invited {max(0, waited) // 86400} days ago, still outstanding")
    logger.info("%s re-invited %s to %s", actor, target, org["org_id"])
    return _reply(200, {"status": "sent", "email": target,
                        "name": member.get("name") or target}, origin)


def _transfer_ownership(token: str, body: dict[str, Any],
                        origin: str | None) -> dict[str, Any]:
    """Hand the organisation to somebody else.

    One act, not two. Making a second person the owner and then demoting
    yourself is the same thing done in an order that can be interrupted -
    leaving two owners if the second step is forgotten, or none if it is done
    the other way round. This writes both sides or neither.

    The outgoing owner becomes an administrator rather than a submitter. They
    were running the organisation a moment ago; dropping them to the bottom on
    the way out is a second decision nobody asked for, and the new owner can
    make it afterwards if they want to.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    org = _org_of(actor)
    if not org:
        return _reply(403, {"error": "No membership for this account."}, origin)
    if org["_role"] != "owner":
        return _reply(403, {"error": "Only the owner can transfer ownership."}, origin)

    target = identity.normalise_email(str(body.get("email", "")))
    if not target or target == actor:
        return _reply(400, {"error": "Name the person to hand it to."}, origin)

    org_id = org["org_id"]
    member = identity.membership_in(org_id, target)
    if not member:
        return _reply(404, {"error": "No member of this organisation by that address."},
                      origin)
    if str(member.get("status") or "active") == "removed":
        return _reply(400, {
            "error": "That person has been removed from the organisation."}, origin)
    # An invitation that has not been accepted is not a person who can sign in,
    # and handing the organisation to one locks everybody out of the things
    # only an owner may do until they happen to accept.
    if str(member.get("status") or "") == "invited":
        return _reply(400, {
            "error": "They have not accepted their invitation yet. Ownership can "
                     "only be handed to somebody who has signed in."}, origin)

    now = int(time.time())
    by = acting_name(org, actor)
    try:
        _users.update_item(
            Key={"email": target, "org_id": org_id},
            UpdateExpression="SET #r = :owner",
            ConditionExpression="attribute_exists(email)",
            ExpressionAttributeNames={"#r": "role"},
            ExpressionAttributeValues={":owner": "owner"},
        )
        _users.update_item(
            Key={"email": actor, "org_id": org_id},
            UpdateExpression="SET #r = :admin",
            ExpressionAttributeNames={"#r": "role"},
            ExpressionAttributeValues={":admin": "admin"},
        )
    except Exception:
        logger.exception("could not transfer ownership of %s to %s", org_id, target)
        return _reply(500, {
            "error": "Could not transfer ownership. Nothing was changed."}, origin)

    # The organisation's root address follows the owner. It is what the billing
    # and the recovery path read, and leaving it pointing at somebody who is no
    # longer the owner is how an account becomes unrecoverable.
    try:
        _orgs.update_item(
            Key={"org_id": org_id},
            UpdateExpression="SET root_email = :e, profile_updated_at = :t, "
                             "profile_updated_by = :b",
            ExpressionAttributeValues={":e": target, ":t": now, ":b": by},
        )
    except Exception:
        logger.exception("ownership moved but root_email did not, for %s", org_id)

    acting = identity.resolve_by_email(actor, channel=None) or {}
    _logged(acting, actor, "ownership transferred", {"org_id": org_id},
            who=target, detail=f"{by} → {member.get('name') or target}")
    logger.info("%s transferred ownership of %s to %s", actor, org_id, target)
    return _reply(200, {
        "status": "transferred", "email": target,
        "name": member.get("name") or target,
    }, origin)


def _member_groups(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Assign someone to zero, one or many groups.

    One group means their receipts are tagged with no question asked. More than
    one means we ask them on WhatsApp, because only they know which. None means
    finance assigns it at settlement.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    org = _org_of(actor)
    if not org or not runs_the_org(org["_role"]):
        return _reply(403, {"error": "Only an owner, administrator or finance executive can assign groups."}, origin)

    target = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(target):
        return _reply(400, {"error": "Enter a valid email address."}, origin)

    valid = {g["id"] for g in (org.get("groups") or [])}
    groups = [g for g in (body.get("groups") or []) if g in valid][:20]

    member = identity.membership_in(org["org_id"], target)
    if not member:
        return _reply(404, {"error": "That person is not in your organisation."}, origin)

    _users.update_item(
        Key={"email": target, "org_id": org["org_id"]},
        UpdateExpression="SET #g = :g",
        ExpressionAttributeNames={"#g": "groups"},
        ExpressionAttributeValues={":g": groups},
    )
    logger.info("%s set %d groups on %s", actor, len(groups), target)
    return _reply(200, {"email": target, "groups": groups}, origin)


# A claim is in flight until it has been paid, refused, or taken back. These
# are the states from which something can still happen to somebody's money.
OPEN_OUTCOMES = ("settled", "rejected")


def open_claims_for(org_id: str, email: str) -> list[dict[str, Any]]:
    """This person's claims that could still be paid or still need a decision.

    Anything already settled, rejected or withdrawn is history: it is not
    waiting on anybody and nothing further happens to it. History is kept
    whatever becomes of the person - a payment made last quarter does not stop
    having been made because they left.
    """
    if not org_id or not email:
        return []
    rows, kwargs = [], {
        "FilterExpression": "org_id = :o AND submitted_by = :e",
        "ExpressionAttributeValues": {":o": org_id, ":e": email.lower()},
    }
    while True:
        page = _submissions.scan(**kwargs)
        rows.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey") or len(rows) > 2000:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    return [r for r in rows
            if r.get("outcome") not in OPEN_OUTCOMES
            and r.get("review_action") not in ("rejected", "withdrawn")]


def _withdraw_open_claims(org_id: str, email: str, actor: str, by_name: str) -> int:
    """Take a departing person's in-flight claims out of circulation.

    Withdrawn rather than deleted. The claim, the receipt and the reasoning are
    all evidence somebody may need later - what has to stop is the claim being
    payable, and being in a queue waiting for a decision nobody can act on now
    that the claimant is gone.
    """
    now = int(time.time())
    closed = 0
    for row in open_claims_for(org_id, email):
        submission_id = str(row.get("submission_id") or "")
        if not submission_id:
            continue
        try:
            _submissions.update_item(
                Key={"submission_id": submission_id},
                UpdateExpression=("SET review_action = :w, review_reason = :r, "
                                  "review_by = :b, review_by_name = :n, review_at = :t "
                                  "REMOVE approved_total"),
                ExpressionAttributeValues={
                    ":w": "withdrawn",
                    ":r": f"{email} was removed from the organisation.",
                    ":b": actor, ":n": by_name, ":t": now},
            )
            # Nothing was paid, so the receipt must stop holding its
            # fingerprint - somebody re-submitting the same bill legitimately
            # later should not be told it is a duplicate of a claim that was
            # cancelled when a colleague left.
            duplicates.release_all({**row, "submission_id": submission_id,
                                    "org_id": org_id})
            closed += 1
        except Exception:
            logger.exception("could not withdraw %s", submission_id)
    return closed


def _member_update(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Change someone's role, or take their access away.

    Removing a person sets `status` to `removed`, which the resolver already
    treats as unusable - so nothing sent from their address or number is
    accepted from that moment. It deliberately does NOT delete the membership
    row or any claim: money they are still owed survives them leaving, and an
    audit needs to see who submitted what.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    org = _org_of(actor)
    if not org or not runs_the_org(org["_role"]):
        return _reply(403, {"error": "Only an owner, administrator or finance executive can administer people."}, origin)

    target = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(target):
        return _reply(400, {"error": "Enter a valid email address."}, origin)

    # Whatever state they are in. An invited person is on the roll and shows in
    # People; `resolve_by_email` gates on `active` because it decides whether a
    # receipt may be accepted, which is a different question from whether
    # somebody may be administered.
    member = identity.membership_in(org["org_id"], target)
    if not member:
        return _reply(404, {"error": "That person is not in your organisation."}, origin)

    updates, values, names = [], {}, {}

    if "status" in body:
        status = str(body["status"]).strip().lower()
        if status not in ("active", "suspended", "removed"):
            return _reply(400, {"error": "Status must be active, suspended or removed."}, origin)
        # Locking yourself out is never the intent, and recovering from it
        # needs someone with database access.
        if target == actor and status != "active":
            return _reply(400, {"error": "You cannot suspend or remove your own access."}, origin)
        updates.append("#s = :s"); names["#s"] = "status"; values[":s"] = status
        # When their access changed and who changed it.
        #
        # The audit log records this too, and is the authority on it. This is
        # for the People list, which shows one line per person and cannot query
        # a log for each of them - and "removed" with no date beside it is a
        # fact with the useful half missing.
        if status != str(member.get("status") or ""):
            updates.append("status_changed_at = :sca")
            updates.append("status_changed_by = :scb")
            values[":sca"] = int(time.time())
            values[":scb"] = acting_name(org, actor)

    if "role" in body:
        role = str(body["role"]).strip().lower()
        if role not in ROLES:
            return _reply(400, {
                "error": "Role must be " + ", ".join(ROLE_LABEL[r] for r in ROLES) + "."
            }, origin)

        # Ownership moves, it is not assigned. There is one owner, so making
        # somebody else the owner necessarily takes it off whoever holds it -
        # a different act from a promotion, with a different confirmation, and
        # not something to do by accident from a dropdown.
        if role == "owner":
            return _reply(400, {
                "error": "An organisation has one owner. Use Transfer ownership, "
                         "which hands it over and records both sides."}, origin)

        # You may act on a role beneath your own and no other. Checked against
        # what the target *is* as well as what they would become: without the
        # first, an administrator could demote the owner; without the second,
        # they could promote a submitter to administrator.
        if not may_manage(org["_role"], member.get("role")):
            return _reply(403, {
                "error": f"You cannot change the role of a "
                         f"{ROLE_LABEL.get(str(member.get('role')), 'member')}."}, origin)
        if not may_manage(org["_role"], role):
            return _reply(403, {
                "error": f"Only an owner can appoint an {ROLE_LABEL[role]}."
                         if role == "admin" else
                         f"You cannot appoint a {ROLE_LABEL[role]}."}, origin)

        # An organisation with no owner cannot buy credits or change policy.
        #
        # This used to query the *target's* memberships for owners - the wrong
        # question - through `IndexName=None`, which boto3 rejects outright. So
        # the guard meant to protect the last owner raised instead, and every
        # attempt to demote any owner came back "Could not change the role."
        if str(member.get("role") or "") == "owner":
            if len(owners_of(org["org_id"])) <= 1:
                return _reply(400, {
                    "error": "This is the only owner. Transfer ownership first."}, origin)

        updates.append("#r = :r"); names["#r"] = "role"; values[":r"] = role

    # Both through placeholders, not only the one that needs it. `name` is a
    # DynamoDB reserved word, so `SET name = :name` is rejected outright - and
    # the whole update went with it, so renaming somebody silently did nothing
    # while the staff id in the same request was lost too.
    # Renaming somebody is held to the same rule as naming them in the first
    # place: a blank here would put them back to being called by their inbox.
    if "name" in body:
        renamed = str(body["name"]).strip()
        if not renamed:
            return _reply(400, {"error": "A name cannot be blank."}, origin)
        if not MIN_NAME <= len(renamed) <= MAX_NAME:
            return _reply(400, {
                "error": f"A name is between {MIN_NAME} and {MAX_NAME} characters."}, origin)
        body = {**body, "name": renamed}

    for field in ("name", "staff_id"):
        if field in body:
            updates.append(f"#{field} = :{field}")
            names[f"#{field}"] = field
            values[f":{field}"] = str(body[field]).strip()[
                :MAX_NAME if field == "name" else MAX_STAFF_ID]

    if not updates:
        return _reply(400, {"error": "Nothing to change."}, origin)

    _users.update_item(
        Key={"email": target, "org_id": org["org_id"]},
        UpdateExpression="SET " + ", ".join(updates),
        **({"ExpressionAttributeNames": names} if names else {}),
        ExpressionAttributeValues=values,
    )
    # Removing somebody stops anything of theirs still being decided or paid.
    # Deliberately after the membership is written: if this half fails, they
    # are still removed - nothing they send is accepted - and a claim left in
    # a queue is visible, where a removal that silently did not happen is not.
    closed = 0
    if body.get("status") == "removed":
        closed = _withdraw_open_claims(
            org["org_id"], target, actor, acting_name(org, actor))

    # Only the two that change what somebody is allowed to do. A name or a
    # staff id corrected on the People tab is housekeeping, and putting it in
    # the same log as "made an owner" is how a log stops being read.
    if "role" in body and str(body["role"]).strip().lower() != member.get("role"):
        _logged({"org_id": org["org_id"], "name": acting_name(org, actor),
                 "role": org["_role"]}, actor, "role changed", who=target,
                detail=f"{member.get('role') or 'none'} → {str(body['role']).strip().lower()}")
    if "status" in body and str(body["status"]).strip().lower() != member.get("status"):
        _logged({"org_id": org["org_id"], "name": acting_name(org, actor),
                 "role": org["_role"]}, actor, "access changed", who=target,
                detail=f"{member.get('status') or 'none'} → {str(body['status']).strip().lower()}",
                claims_withdrawn=closed)

    logger.info("%s updated %s (%s)", actor, target, ", ".join(values))
    return _reply(200, {"email": target, "updated": list(values),
                        "claims_withdrawn": closed}, origin)


# ---------------------------------------------------------------------------
# The original receipt
# ---------------------------------------------------------------------------
#
# These sit here rather than on the intake function because reading a receipt
# back is a signed-in console action, and intake is the unauthenticated
# endpoint the channel adapters post to. Giving that function the session
# signing key so it could check a token would be handing the key to the one
# part of the system anybody on the internet can reach.


def _may_see(membership: dict[str, Any], submission: dict[str, Any]) -> bool:
    """Whether this person is entitled to the original behind this submission.

    A submission id carries a timestamp, which makes it guessable enough that
    the id alone must never be sufficient. Two gates: the same organisation
    always, and for ordinary staff their own receipts only - a colleague's
    restaurant bill is none of their business.
    """
    # Both ids have to be present as well as equal. Two absent values compare
    # equal, which would let a row that somehow lost its org_id be read by
    # anyone - an authorisation check has to fail closed on missing data.
    org_id = membership.get("org_id")
    if not org_id or submission.get("org_id") != org_id:
        return False

    if runs_the_org(membership):
        return True

    actor = membership.get("email")
    return bool(actor) and submission.get("submitted_by") == actor


def _receipt_view(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Hand back a five-minute link to one stored original."""
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    m = identity.resolve_by_email(actor, channel=None)
    if not m:
        return _reply(403, {"error": "No membership for this account."}, origin)

    submission_id = str(body.get("submission_id", "")).strip()[:80]
    if not submission_id:
        return _reply(400, {"error": "Which receipt?"}, origin)

    item = _submissions.get_item(Key={"submission_id": submission_id}).get("Item")

    # One response for "no such receipt" and for "not yours". Distinguishing
    # them would turn this into a way to test whether an id exists.
    if not item or not _may_see(m, item):
        return _reply(404, {"error": "No receipt found."}, origin)

    key = str(item.get("receipt_key", ""))
    if not key:
        return _reply(404, {
            "error": "No original was stored for this receipt.",
            "reason": "no_original",
        }, origin)

    ctype = str(item.get("receipt_type", ""))
    name = str(item.get("receipt_name", ""))
    logger.info("%s viewed the original for %s", actor, submission_id)
    return _reply(200, {
        "url": receipts.view_url(key, name, ctype),
        "content_type": ctype,
        "filename": receipts.safe_name(name, ctype),
        "bytes": int(item.get("receipt_bytes") or 0),
        "channel": item.get("channel", ""),
        "received_at": int(item.get("received_at") or 0),
        "expires_in": receipts.VIEW_TTL_SECONDS,
    }, origin)


def _receipt_upload(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Issue a write-only link so the browser can send the file straight to S3."""
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    if not identity.resolve_by_email(actor, channel=None):
        return _reply(403, {"error": "No membership for this account."}, origin)

    issued = receipts.upload_url(str(body.get("content_type", "")))
    if not issued:
        return _reply(400, {
            "error": "Receipts must be a photo (JPEG, PNG, WebP, HEIC) or a PDF."
        }, origin)

    key, url = issued
    return _reply(200, {"key": key, "url": url, "expires_in": receipts.UPLOAD_TTL_SECONDS}, origin)


def _receipt_submit(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Put an uploaded original through the same gate every channel goes through."""
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    if not identity.resolve_by_email(actor, channel=None):
        return _reply(403, {"error": "No membership for this account."}, origin)

    # Trust the object, not the claim about it: the browser could say anything
    # about what it uploaded, so the type and size come from S3 itself.
    stored = receipts.describe(str(body.get("key", "")).strip()[:200])
    if not stored:
        return _reply(400, {"error": "That upload didn't complete. Try again."}, origin)
    stored["receipt_name"] = receipts.safe_name(
        str(body.get("filename", "")), stored["receipt_type"]
    )

    response = intake.lambda_handler(
        {"path": "/intake/portal", "body": json.dumps({"email": actor, **stored})}, None
    )
    outcome = json.loads(response.get("body") or "{}")
    if outcome.get("status") != "queued":
        receipts.discard(stored["receipt_key"])
        if outcome.get("status") == "no_credits":
            return _reply(402, {"error": "Out of credits. Top up to resume auditing."}, origin)
        # Whatever intake actually said, rather than a shrug. "That receipt
        # couldn't be accepted" is the sentence that hid an API key check from
        # the one caller that could never have a key.
        logger.warning("portal submission refused for %s: %s", actor, outcome)
        return _reply(400, {
            "error": outcome.get("error") or outcome.get("message")
                     or "That receipt couldn't be accepted."
        }, origin)

    logger.info("%s submitted %s from the portal", actor, outcome.get("submission_id"))
    return _reply(202, {
        "submission_id": outcome.get("submission_id"),
        "credits_remaining": outcome.get("credits_remaining"),
        "group_status": outcome.get("group_status"),
    }, origin)


# ---------------------------------------------------------------------------
# Claim outcomes
# ---------------------------------------------------------------------------


# Decisions a reviewer makes about somebody else's claim.
# A headcount above this is far likelier to be a stray number than a table.


def _claim_retype(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """A reviewer says what this expense actually is, and it is decided again.

    The model answers `not_covered` when nothing on the bill matches a
    configured type, which is the honest answer for it to give - a payment to
    a named individual is not a meal, a subscription or a trip. But a claim
    cannot be paid under a type no rule covers, so a person has to supply one.

    The EXPENSE TYPE control recomputed the figures in the browser and wrote
    nothing down, so a reviewer could re-tag a claim, watch the verdict change
    in front of them, approve it, and have the stored verdict still say the
    type was not covered. This writes the type and sends the claim back round
    the auditor, which runs the whole policy against it - caps, exclusions,
    headcount requirement and all. Anything else would be the console's
    arithmetic deciding what somebody is paid.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    acting = identity.resolve_by_email(actor, channel=None)
    if not acting:
        return _reply(403, {"error": "No membership for this account."}, origin)
    # Re-tagging changes what a claim is worth, so it is a reviewer's act -
    # and reviewing is not a finance executive's job. See `may_review`.
    if not may_review(acting):
        return _reply(403, {
            "error": "Only an owner or administrator can set the expense type."},
            origin)

    # The organisation's own types, not the built-ins. Now that a policy is
    # stored per account, checking against `DEFAULT_RULES` would refuse a type
    # the owner added themselves.
    org = _org_of(actor) or {}
    expense_type = str(body.get("expense_type", "")).strip()[:60]
    if expense_type:
        known = set(policy.expense_type_ids(policy.rules_for(org)))
        if expense_type not in known:
            return _reply(400, {
                "error": "Choose one of the configured expense types."}, origin)

    # And what it is denominated in. A $180 invoice read as ₹180 is three
    # orders of magnitude under the cap and clears in silence, so correcting
    # the currency has to be possible - and has to re-run the policy, because
    # the caps are per currency.
    currency = money.normalise(str(body.get("currency", "")).strip())
    if body.get("currency") and not currency:
        return _reply(400, {"error": "That is not a currency we support."}, origin)

    # And which cost centre it comes out of.
    #
    # Settable here and not only at settlement. A claim can be held *on* the
    # group - a submitter who belongs to several, on a bill naming none - so
    # the person unblocking it has to be able to answer it; and a group the
    # agent inferred from the paper is correctable only by whoever reads the
    # bill, which is the reviewer rather than the person paying weeks later.
    #
    # Absent and empty are different answers. Absent means the reviewer did not
    # touch the control; empty means they cleared a group the agent got wrong,
    # which is a change and has to be stored as one.
    group_given = "group_id" in body
    group_id = str(body.get("group_id", "")).strip()[:60]
    if group_id:
        known = {str(g.get("id")) for g in (org.get("groups") or []) if g.get("id")}
        if group_id not in known:
            return _reply(400, {"error": "Choose one of your configured groups."}, origin)

    if not expense_type and not currency and not group_given:
        return _reply(400, {
            "error": "Nothing to change — pick an expense type, a currency or a group."},
            origin)

    submission_id = str(body.get("submission_id", "")).strip()[:80]
    item = _submissions.get_item(Key={"submission_id": submission_id}).get("Item") if submission_id else None
    if not item or item.get("org_id") != acting.get("org_id"):
        return _reply(404, {"error": "No claim found."}, origin)
    if item.get("outcome") == "settled":
        return _reply(409, {
            "error": "This claim has already been reimbursed."}, origin)

    # Saved, and that is all. The claim is not sent back round the agent.
    #
    # It used to be: the status went to `queued`, the stream woke the worker,
    # and the whole policy ran again under the reviewer's type. That was built
    # on the idea that a corrected claim should be re-decided by the engine -
    # and it is the wrong idea for a claim that has already reached a human.
    # Once it is in front of a person, they decide it: they read the bill, the
    # findings and the figures, and they approve or reject. Nothing in between.
    #
    # What a reviewer is doing here is answering a question of fact the agent
    # could not - what kind of expense this is, what it is denominated in,
    # which cost centre it belongs to. Those are needed for the claim to be
    # paid and reported, not for it to be re-judged. So they are written down,
    # the claim stays where it is, and the decision stays the human's.
    #
    # That also removes the loop this endpoint used to open: a write that woke
    # the stream that wrote the row that woke the stream, which cost 576
    # attempts in twenty minutes the first time it went wrong.
    sets = ["answered_by = :who", "corrected_by = :who", "corrected_at = :cat"]
    values: dict[str, Any] = {":audited": "audited",
                              ":cat": int(time.time()),
                              ":who": acting.get("name") or actor}
    if expense_type:
        sets.append("answered_expense_type = :t")
        values[":t"] = expense_type
    if currency:
        sets.append("answered_currency = :c")
        values[":c"] = currency
    if group_given:
        # `group_status` moves with it, so the console can say what tagged the
        # claim. A reviewer's answer is final in a way an inference is not:
        # the auditor only consults the bill while the question is still open,
        # and "set_by_reviewer" closes it.
        sets.append("group_id = :g")
        values[":g"] = group_id
        sets.append("group_status = :gs")
        values[":gs"] = "set_by_reviewer" if group_id else "unset"

    try:
        _submissions.update_item(
            Key={"submission_id": submission_id},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="#s = :audited",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    except Exception:
        logger.info("%s is not in a state to be re-typed", submission_id)
        return _reply(409, {
            "error": "That claim is being read right now — reload and try again."}, origin)

    # Recorded, because it is how the claim will be reported on and which cost
    # centre it will be paid from. Not a decision about whether it is paid -
    # that is the reviewer's next click, and theirs alone.
    _logged(acting, actor, "expense type corrected", item, detail=", ".join(
        p for p in (
            (f"{(item.get('verdict') or {}).get('expense_type') or 'untagged'} → {expense_type}"
             if expense_type else ""),
            (f"currency {(item.get('verdict') or {}).get('currency') or '?'} → {currency}"
             if currency else ""),
            (f"group {item.get('group_id') or 'unset'} → {group_id or 'unset'}"
             if group_given else ""),
        ) if p))

    logger.info("%s re-typed by %s (%s, %s)", submission_id, actor,
                expense_type or "type unchanged", currency or "currency unchanged")
    return _reply(200, {"status": "saved", "expense_type": expense_type,
                        "currency": currency, "submission_id": submission_id}, origin)


def _as_decimal(value: Any) -> Decimal:
    """A stored money string as a number, or zero if it is not one."""
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


# No "queried". Asking the submitter a question was a way of not deciding: the
# claim stopped, the person who spent the money was asked to justify it, and
# the queue grew a state that waited on somebody outside finance. A reviewer
# now approves, refuses, or hands it back to the agent.
REVIEW_ACTIONS = ("approved", "rejected", "reopened", "disputed")

# The ones that decide whether somebody is paid. `disputed` is not among them:
# it hands the claim to a reviewer rather than deciding it, which is why a
# finance executive may do it and may not do these. See `may_review`.
DECIDING_ACTIONS = ("approved", "rejected", "reopened")

# And the one a person makes about their own. Separate because the
# authorisation is the opposite way round: a Finance Executive may not withdraw
# an employee's claim on their behalf, and an employee may not approve one.
CLAIMANT_ACTIONS = ("withdrawn",)


def _logged(acting: dict[str, Any], actor: str, action: str,
            item: dict[str, Any] | None = None, **fields: Any) -> None:
    """Append one decision to the organisation's audit log.

    A wrapper rather than calling `audit.record` directly, because every entry
    wants the same three things and forgetting one of them is how a log becomes
    unreadable a year later, when the person who wrote the code is not the
    person reading it: who did it, what authority they held at the time, and
    which claim it was about.

    The role is stamped at the moment of the decision and never resolved
    afterwards. Roles change - somebody who approved a claim as finance may be
    an owner by the time anyone asks, or gone from the organisation entirely -
    and a log that reports today's role for yesterday's decision is worse than
    one that reports none, because it looks authoritative.

    The claim's own reference, not its id: the log is read by a person holding
    a claim reference, not a UUID.

    Two things this must never do, both learned the hard way.

    **It must not collide with its caller.** The defaults below were passed
    straight through alongside `**fields`, so any caller supplying one of them
    by name - `_invite` passing `who=email`, `_member_update` passing
    `who=target` - raised `TypeError: got multiple values for keyword argument`.
    Inviting somebody and changing a role both failed on it. The caller's value
    wins, because a caller naming `who` knows something more specific than a
    claim's submitted_by: the person invited, or the person whose access
    changed.

    **It must not raise.** `audit.record` swallows its own failures for a
    stated reason - the thing being recorded has already happened, and losing
    the log must not lose it - but that guarantee was worthless while this
    wrapper could raise before ever reaching it. It did, and the cost was
    exactly what the doctrine predicts: the invitation was created and the
    email sent, then a 500 told the user it had failed, so they retried and
    invited the same person twice. The guarantee belongs at the boundary the
    callers actually touch, which is here.
    """
    entry: dict[str, Any] = {
        "actor_name": acting.get("name") or "",
        "actor_role": acting.get("role") or "",
        "reference": str((item or {}).get("reference") or ""),
        "submission_id": str((item or {}).get("submission_id") or ""),
        "who": str((item or {}).get("submitted_by") or ""),
    }
    entry.update(fields)
    try:
        audit.record(
            str(acting.get("org_id") or (item or {}).get("org_id") or ""),
            action, actor, **entry)
    except Exception:
        logger.exception("could not log %s", action)


def _claim_review(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """A reviewer's decision on a queued claim, written down.

    This did not exist. The console's Approve / Reject / Ask buttons wrote to a
    variable in the browser and called a re-render, so a claim moved to Pending
    settlement on screen and was back in the queue on the next reload - and the
    employee, who is out of pocket, was never told anything either way.

    Distinct from `_claim_outcome`, which records what happened to the *money*.
    This records what happened to the *claim*: whether it may be paid at all.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)

    acting = identity.resolve_by_email(actor, channel=None)
    if not acting:
        return _reply(403, {"error": "No membership for this account."}, origin)

    action = str(body.get("action", "")).strip().lower()
    if action not in REVIEW_ACTIONS + CLAIMANT_ACTIONS:
        return _reply(400, {"error": "Say what is being done to this claim."}, origin)

    # Deciding a claim is a reviewer's act; sending one back is not.
    #
    # `disputed` is a finance executive saying they do not agree with what the
    # agent cleared. It decides nothing - it puts the claim in front of the
    # people whose job that is - so it sits at the lower gate. Everything else
    # here settles whether somebody is paid, and that is an owner's or an
    # administrator's to settle.
    if action in DECIDING_ACTIONS and not may_review(acting):
        return _reply(403, {
            "error": "Only an owner or administrator can decide a claim. A "
                     "finance executive can send it back for review."
        }, origin)
    if action in REVIEW_ACTIONS and action not in DECIDING_ACTIONS \
            and not runs_the_org(acting):
        return _reply(403, {
            "error": "Only an owner, administrator or finance executive can "
                     "send a claim back for review."
        }, origin)

    # A rejection and a question are both useless to the employee without the
    # words. An approval needs none - nothing is being asked of them.
    reason = str(body.get("reason", "")).strip()[:notify.MAX_REASON]
    if action in ("rejected", "disputed") and len(reason) < 4:
        return _reply(400, {
            "error": ("Give a reason. It is the only thing the submitter can act on."
                      if action == "rejected" else
                      "Say what the agent got wrong, or the reviewer has nothing to go on.")
        }, origin)

    submission_id = str(body.get("submission_id", "")).strip()[:80]
    item = _submissions.get_item(Key={"submission_id": submission_id}).get("Item") if submission_id else None
    if not item or item.get("org_id") != acting.get("org_id"):
        return _reply(404, {"error": "No claim found."}, origin)

    # Money has moved. Nothing decided from here on can be true of a claim that
    # has already been paid: approving it changes nothing, rejecting it says a
    # payment should not have been made without unmaking it, and asking the
    # submitter a question implies the outcome is still open when they have the
    # money. Withdrawal was already refused; the rest were not, so the console
    # hiding the buttons was the only thing stopping it.
    #
    # Reopening included. It was excepted here on the reasoning that undoing a
    # decision decides nothing - true, but it puts the claim back in the queue
    # with the money already gone, which is a claim awaiting a decision that
    # has also been reimbursed. Reversing the payment is the act that has to
    # come first, and it is not one this can stand in for.
    if item.get("outcome") == "settled":
        return _reply(409, {
            "error": ("This claim has already been reimbursed. Reverse the payment "
                      "first if it needs deciding again.")
        }, origin)

    if action in CLAIMANT_ACTIONS:
        # Your own claim, and only your own. An owner withdrawing somebody
        # else's would erase a record the employee is relying on without ever
        # telling them a decision was made.
        if str(item.get("submitted_by", "")).lower() != actor.lower():
            return _reply(403, {"error": "You can only withdraw a claim you submitted."}, origin)

    now = int(time.time())
    decided_by = acting.get("name") or actor

    # Reopening puts the claim back in the queue. The attributes are removed
    # rather than set to a "reopened" state, because a claim awaiting a
    # decision and a claim that was never decided are the same thing, and two
    # representations of one state is how a queue starts disagreeing with
    # itself.
    if action == "reopened":
        _submissions.update_item(
            Key={"submission_id": submission_id},
            UpdateExpression=("REMOVE review_action, review_reason, review_by, "
                              "review_by_name, review_at, approved_total"),
        )
        # The one write that destroys evidence. Removing the attributes is
        # right for the *claim* - a claim awaiting a decision and one that was
        # never decided are the same state - but it erases who approved it and
        # when, and that is exactly the question somebody asks afterwards. What
        # was removed is recorded here before it goes.
        _logged(acting, actor, "claim reopened", item,
                undid=str(item.get("review_action") or ""),
                undid_by=str(item.get("review_by_name") or ""),
                amount=str(item.get("approved_total") or ""))
        logger.info("%s reopened %s", actor, submission_id)
        return _reply(200, {"status": "reopened", "submission_id": submission_id}, origin)

    # The agent cleared it and finance does not agree.
    #
    # Distinct from a reopen, which undoes a *human* decision. This overrules
    # the automation, and until now there was no way to: an auto-released claim
    # went straight to Pending settlement, where the only two things a finance
    # executive could do were pay it or reject it outright. Disagreeing with
    # the agent on a claim that is probably still legitimate - the wrong
    # expense type, a cap that should not have applied - had no button.
    #
    # `pulled_back` is a flag of its own rather than a value of `review_action`
    # because it is not a decision: it is the absence of one, restored. The
    # queue reads it as "this needs a human after all", and it outlives any
    # later reopen, so a claim sent back once does not quietly auto-release
    # itself a second time.
    if action == "disputed":
        _submissions.update_item(
            Key={"submission_id": submission_id},
            UpdateExpression=("SET pulled_back = :yes, pulled_reason = :r, "
                              "pulled_by = :b, pulled_by_name = :n, pulled_at = :t "
                              "REMOVE review_action, review_reason, review_by, "
                              "review_by_name, review_at, approved_total"),
            ExpressionAttributeValues={":yes": True, ":r": reason, ":b": actor,
                                       ":n": decided_by, ":t": now},
        )
        _logged(acting, actor, "sent back for review", item, reason=reason,
                undid=str(item.get("review_action") or ""),
                undid_by=str(item.get("review_by_name") or ""),
                amount=str(item.get("approved_total") or ""))
        logger.info("%s sent %s back for review", actor, submission_id)

        # The submitter was already told this was approved and awaiting
        # settlement. Somebody who believes the money is coming does not chase
        # it; leaving that standing while the claim quietly goes back in the
        # queue is the one thing this must not do.
        result: dict[str, Any] = {}
        claimant = identity.resolve_by_email(str(item.get("submitted_by", "")), channel=None)
        if claimant:
            verdict = item.get("verdict") or {}
            result = notify.send("disputed", claimant, {
                "vendor": (item.get("receipt") or {}).get("vendor", ""),
                "claim_ref": str(item.get("reference", "")),
                "currency": verdict.get("currency", ""),
                "approved": verdict.get("reimbursable_total") or verdict.get("provisional_total"),
                "reason": reason,
                "disputed_by": decided_by,
            })
            notify.record(_submissions, submission_id, result)
        return _reply(200, {"status": "disputed", "submission_id": submission_id,
                            "by": decided_by, "at": now, **result}, origin)

    verdict = item.get("verdict") or {}

    # Every approved claim carries an expense type this organisation has
    # configured. No exceptions, and not because of anything the agent found.
    #
    # The check used to be "does the stored verdict block on
    # no_rule_for_expense_type", which asked about the agent's reading rather
    # than about the claim - so a claim whose verdict predated a rule was
    # refused while its type was already right, and a claim tagged
    # `not_covered` by an agent that never blocked on it could go through
    # untagged. Either way the type is what every report groups by, and a
    # payment filed under nothing is a line in the accounts nobody can explain.
    #
    # Asked of the claim as it now stands - the reviewer's answer if they gave
    # one, the agent's reading otherwise - against the enabled types in the
    # policy as it stands now.
    if action == "approved":
        chosen = str(item.get("answered_expense_type")
                     or verdict.get("expense_type") or "")
        known = set(policy.expense_type_ids(
            policy.rules_for(_org_of(actor) or {})))
        if chosen not in known:
            return _reply(409, {
                "error": ("Set an expense type first. Every claim is reported "
                          "under one, so a payment cannot be approved without "
                          "it.")
            }, origin)

    # And a claim attributed to nothing cannot be approved into one either.
    #
    # Where the organisation runs cost centres, every payment comes out of
    # one. The agent settles it from the bill when the bill says - a buyer's
    # registration, a buyer's name - and where it cannot, that is frequently
    # the whole reason the claim is in front of a person. Approving past it
    # leaves the question that stopped the agent unanswered by the one human
    # it was handed to, and the money leaves a budget nobody named.
    #
    # Checked here and not only in the console, because the console is a page
    # somebody can have open from before this shipped.
    if action == "approved" and ((_org_of(actor) or {}).get("groups") or []) and not str(
            item.get("group_id") or ""):
        return _reply(409, {
            "error": ("Set a group first. This claim is not attributed to any "
                      "cost centre, so there is no budget it could be paid from.")
        }, origin)

    approved_total = ""
    if action == "approved":
        approved_total = str(verdict.get("reimbursable_total") or "0")
        if _as_decimal(approved_total) <= 0:
            approved_total = str(verdict.get("provisional_total") or "0")

    sets = ("SET review_action = :a, review_reason = :r, review_by = :b, "
            "review_by_name = :n, review_at = :t, approved_total = :amt")
    values: dict[str, Any] = {":a": action, ":r": reason, ":b": actor,
                              ":n": decided_by, ":t": now, ":amt": approved_total}
    _submissions.update_item(
        Key={"submission_id": submission_id},
        UpdateExpression=sets,
        ExpressionAttributeValues=values,
    )

    # A rejected claim was never paid, so it must stop holding the receipt's
    # fingerprint - otherwise the employee who fixes the problem and resends is
    # turned away as a duplicate of a claim that went nowhere.
    # A withdrawn claim, like a rejected one, was never paid - so it must stop
    # holding the receipt's fingerprint, or the corrected resubmission the
    # person withdrew in order to make is turned away as a duplicate of it.
    if action in ("rejected", "withdrawn"):
        duplicates.release_all({**item, "submission_id": submission_id})

    _logged(acting, actor, {
        "approved": "claim approved at review",
        "rejected": "claim rejected at review",
        "withdrawn": "claim withdrawn by submitter",
    }.get(action, f"claim {action}"), item, reason=reason,
        amount=approved_total, currency=str(verdict.get("currency") or ""),
        claimed=str(verdict.get("receipt_total") or ""))

    logger.info("%s marked %s as %s at review", actor, submission_id, action)

    # Both halves of a human decision reach the person who sent the receipt.
    #
    # An approval used to be treated as an internal step - the employee would
    # hear about it when they were paid. But a claim that reaches review is
    # told "sent to your finance team, nothing is needed from you" and then
    # told nothing else until settlement, which can be days. That silence is
    # the one stretch where they have been promised an answer and given no
    # sign that anything moved, and it is also the moment the outcome stops
    # being in doubt.
    #
    # Not `withdrawn`: they did that themselves and do not need telling. Not
    # `reopened`: nothing has been decided yet, so there is nothing to say.
    result: dict[str, Any] = {}
    if action in ("approved", "rejected"):
        claimant = identity.resolve_by_email(str(item.get("submitted_by", "")), channel=None)
        if claimant:
            verdict = item.get("verdict") or {}
            claim = {
                "vendor": (item.get("receipt") or {}).get("vendor", ""),
                "claim_ref": str(item.get("reference", "")),
                "currency": verdict.get("currency", ""),
            }
            if action == "approved":
                # What the reviewer released, not the engine's figure - which
                # is nothing while a finding blocks the claim, and a claim in
                # front of a reviewer is usually blocked by one.
                # `or` is not enough: `approved_total` is a string, and the
                # string "0" is truthy - a claim whose engine figures were
                # both nothing would be announced as approved for 0.00.
                released = approved_total
                if _as_decimal(released) <= 0:
                    released = str(verdict.get("receipt_total") or "")
                claim.update({"approved": released, "approved_by": decided_by})
            else:
                claim.update({
                    "approved": verdict.get("provisional_total")
                                or verdict.get("receipt_total"),
                    "reason": reason,
                    "rejected_by": decided_by,
                    "rejected_on": time.strftime("%d %b %Y", time.gmtime(now)),
                })
            result = notify.send(action, claimant, claim)
            notify.record(_submissions, submission_id, result)

    return _reply(200, {
        "status": action,
        "submission_id": submission_id,
        "by": decided_by,
        "at": now,
        # What the approval released. The engine's own figure is nothing while
        # a finding blocks the claim, so a reviewer who approves it anyway is
        # the only source of this number - and without it in the reply the
        # console had to guess, guessed zero, and dropped the claim out of
        # Pending settlement until somebody reloaded the page.
        "approved_total": approved_total,
        **result,
    }, origin)


def _claim_outcome(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Record a settlement or a rejection, and tell the person who claimed.

    A rejection needs a reason. Not as validation theatre - the employee is out
    of pocket and the reason is the only thing they can act on, so a rejection
    without one is refused here rather than sent as an empty explanation.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)

    acting = identity.resolve_by_email(actor, channel=None)
    if not acting or not runs_the_org(acting):
        return _reply(403, {
            "error": "Only an owner, administrator or finance executive can settle or reject a claim."
        }, origin)

    kind = str(body.get("kind", "")).strip().lower()
    if kind not in notify.NOTICES:
        return _reply(400, {"error": "Say whether this is a settlement or a rejection."}, origin)

    reason = str(body.get("reason", "")).strip()[:notify.MAX_REASON]
    if kind == "rejected" and len(reason) < 4:
        return _reply(400, {
            "error": "Give a reason. It is the only thing the submitter can act on."
        }, origin)

    # A settlement carries the reference that ties it to the bank statement.
    #
    # It was optional, and the amount arrives prefilled, so a payment could be
    # recorded by pressing one button with every field untouched. That is how
    # a claim came to be settled in full by somebody who had not seen the form
    # - and the record it left said INR 6,632.00 paid, by nobody's transfer,
    # on no reference anybody could look up.
    #
    # Requiring it means a payment cannot be recorded without somebody having
    # looked up what they actually paid, which is the same act that makes the
    # record worth keeping. Checked here as well as in the console, because
    # the console is a page somebody can have open from before this shipped.
    # Not on a float settlement: no transfer was made, so there is no line on
    # any statement to tie it to and demanding a reference would only get a
    # made-up one. The choice of source is the record there.
    if (kind == "settled" and str(body.get("source", "")) != "float"
            and not str(body.get("reference", "")).strip()):
        return _reply(400, {
            "error": "Give the transaction reference - the UTR, cheque number "
                     "or transaction id. It is what ties this claim to the "
                     "line on the bank statement."}, origin)

    submission_id = str(body.get("submission_id", "")).strip()[:80]
    item = _submissions.get_item(Key={"submission_id": submission_id}).get("Item") if submission_id else None
    if not item or item.get("org_id") != acting.get("org_id"):
        return _reply(404, {"error": "No claim found."}, origin)

    claimant = identity.resolve_by_email(str(item.get("submitted_by", "")), channel=None)
    if not claimant:
        return _reply(409, {"error": "That claim has no active claimant to notify."}, origin)

    now = int(time.time())
    claim = {
        "vendor": str(body.get("vendor", ""))[:120],
        "currency": str(body.get("currency", ""))[:3].upper(),
        "approved": body.get("approved"),
        "paid": body.get("paid"),
        "outstanding": body.get("outstanding"),
        "mode": str(body.get("mode", ""))[:40],
        "reference": str(body.get("reference", ""))[:80],
        # Where the money came from, on a claim by somebody holding a float.
        #
        # Two genuinely different acts wearing one word. Settling *from the
        # float* moves no money at all: they already spent the company's cash,
        # and the settlement is the company accounting for it - so it draws
        # their advance down. A *separate payout* is an ordinary
        # reimbursement, for a bill too large for the float or one they paid
        # personally, and it must leave the float alone or the next
        # replenishment is calculated against a balance that never moved.
        #
        # Defaulted rather than required: every claim before this shipped, and
        # every claim by somebody with no float, is a payout.
        "source": "float" if str(body.get("source", "")) == "float" else "payout",
        # Ours, off the row - not the bank's, and not the caller's to name.
        "claim_ref": str(item.get("reference", "")),
        "paid_on": str(body.get("paid_on", ""))[:24],
        "group": str(body.get("group", ""))[:60],
        "note": str(body.get("note", ""))[:400],
        "reason": reason,
        "settled_by": acting.get("name") or actor,
        "rejected_by": acting.get("name") or actor,
        "rejected_on": time.strftime("%d %b %Y", time.gmtime(now)),
    }

    # Recorded before the notice goes out: a message the employee acts on must
    # never describe a state that was never written down.
    _submissions.update_item(
        Key={"submission_id": submission_id},
        UpdateExpression=("SET outcome = :o, outcome_reason = :r, outcome_by = :b, "
                          "outcome_at = :ts, outcome_by_name = :bn, outcome_paid = :p, "
                          "outcome_mode = :m, outcome_reference = :ref, "
                          "outcome_source = :src, "
                          "outcome_paid_on = :on"),
        ExpressionAttributeValues={
            ":o": kind, ":r": reason, ":b": actor, ":ts": now,
            ":bn": acting.get("name") or actor,
            ":p": str(claim.get("paid") or "0"),
            ":m": claim.get("mode") or "",
            ":src": claim.get("source") or "payout",
            ":ref": claim.get("reference") or "",
            ":on": claim.get("paid_on") or "",
        },
    )

    # A rejected claim was never paid, so it must stop holding the receipt's
    # fingerprint. Otherwise the employee who corrects the problem and sends
    # the bill again is turned away as a duplicate of a claim that went
    # nowhere - which is the one way this feature could cost somebody money
    # they were genuinely owed.
    if kind == "rejected":
        duplicates.release_all({**item, "submission_id": submission_id})

    _logged(acting, actor,
            "claim settled" if kind == "settled" else f"claim {kind}", item,
            reason=reason, amount=str(claim.get("paid") or ""),
            currency=claim.get("currency") or "",
            detail=" ".join(p for p in (claim.get("mode") or "",
                                        claim.get("reference") or "") if p))

    result = notify.send(kind, claimant, claim)
    notify.record(_submissions, submission_id, result)
    logger.info("%s marked %s as %s", actor, submission_id, kind)
    return _reply(200, {"status": kind, "submission_id": submission_id, **result}, origin)


# ---------------------------------------------------------------------------
# Buying credits
# ---------------------------------------------------------------------------


def _platform_pricing() -> tuple[list[dict[str, Any]], Any]:
    """The published slabs and the tax rate, exactly as BMS shows them.

    Through pricing.merge rather than reading the row raw. Reading it raw is
    what made BMS display a full INR column while every slab in the customer's
    Credits tab said "no INR price set" - two readers of one setting, quietly
    disagreeing.
    """
    if _settings is None:
        return pricing.merge(None), pricing.gst_percent(None)
    row = _settings.get_item(Key={"key": "platform"}).get("Item") or {}
    return pricing.merge(row.get("pricing")), pricing.gst_percent(row.get("gst_percent"))


def _credits_order(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Start a purchase. The browser names a slab and nothing else.

    Every figure - the credits, the price, the tax, the currency - is read
    server-side. A request that could name its own amount could buy a hundred
    thousand credits for one rupee, and the signature on the way back would be
    perfectly valid over that wrong figure.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    org = _org_of(actor)
    if not org:
        return _reply(403, {"error": "No organisation for this account."}, origin)
    # Deliberately a list and not `runs_the_org`, which is what every other
    # gate here became. Spending the company's money is not part of running
    # the organisation's settings, and widening the administration surface to
    # administrators is not a reason to hand them the card. The console offers
    # this to the owner alone; this is the wider of the two on purpose,
    # because finance could already do it.
    if org["_role"] not in ("owner", "finance"):
        return _reply(403, {"error": "Only an owner or finance executive can buy credits."}, origin)

    pricing, gst = _platform_pricing()
    slab = payments.find_slab(pricing, body.get("credits"))
    if not slab:
        return _reply(400, {"error": "That is not a published credit slab."}, origin)

    try:
        currency = payments.checkout_currency(org)
        quote = payments.price_breakdown(slab, currency, gst)
        order = payments.create_order(
            quote["total_minor"], currency,
            receipt=f"exp-{org['org_id'][-8:]}-{int(time.time())}",
            notes={"org_id": org["org_id"], "org": org.get("name", ""),
                   "credits": str(slab["credits"]), "tax_id": org.get("tax_id", ""),
                   "bought_by": actor},
        )
        row = payments.purchase_row(order, org["org_id"], int(slab["credits"]),
                                    currency, quote["total"], actor)
        row.update({"base": quote["base"], "tax": quote["tax"],
                    "tax_label": quote["tax_label"]})
        payments.record_order(row)
    except payments.PaymentError as exc:
        return _reply(400, {"error": str(exc)}, origin)

    logger.info("%s started a purchase of %s credits", actor, slab["credits"])
    return _reply(200, {
        "order_id": order["id"],
        "amount_minor": quote["total_minor"],
        "currency": currency,
        "credits": int(slab["credits"]),
        "base": quote["base"], "tax": quote["tax"],
        "tax_label": quote["tax_label"], "total": quote["total"],
        "key_id": payments.config()["key_id"],
        "mode": payments.mode(),
        "org_name": org.get("name", ""),
        "buyer_email": actor,
    }, origin)


def _credits_verify(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """The browser came back from checkout. Credit, if the signature holds.

    The webhook credits the same purchase independently, so this endpoint is
    about showing the customer their new balance immediately rather than about
    being the only path that works.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)

    order_id = str(body.get("razorpay_order_id", ""))[:60]
    payment_id = str(body.get("razorpay_payment_id", ""))[:60]
    signature = str(body.get("razorpay_signature", ""))[:200]

    try:
        if not payments.checkout_signature_ok(order_id, payment_id, signature):
            logger.warning("%s posted a bad checkout signature", actor)
            return _reply(400, {"error": "That payment could not be verified."}, origin)
        result = payments.credit_purchase(order_id, payment_id, "checkout")
    except payments.PaymentError as exc:
        return _reply(400, {"error": str(exc)}, origin)

    return _reply(200, {
        "credits_added": result["credits"],
        "balance": result.get("balance"),
        "already_credited": result["already"],
    }, origin)


# ---------------------------------------------------------------------------
# What the console reads
# ---------------------------------------------------------------------------


def _turnaround(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How long this organisation takes to reimburse somebody, in days.

    One figure for the whole organisation, over every claim ever submitted,
    and the same figure wherever it is shown. It was briefly two - an
    all-time one on My expenses and a period-scoped one on Reports - which is
    two numbers that can disagree on two screens about one question.

    From the moment a receipt arrives to the moment the money is recorded as
    paid, which is the whole of what the person who spent it experiences.
    Measuring from approval would report the half of the wait this product is
    fastest at and hide the half it is not.

    The median, not the mean. One claim that sat over a holiday for three
    weeks drags an average far enough to make the figure useless, and the
    question is "how long will mine take" - the middle of the distribution
    rather than its centre of mass.

    **And what is still waiting, beside it.** Only a settled claim has an
    elapsed time to measure, so a median over settled claims alone is the
    classic survivorship lie: this account would have read "2 days" on the
    strength of its one reimbursed claim while twenty-one others had been
    waiting up to six. The count of those and the age of the oldest travel
    with the figure so it cannot flatter - a fast median over a long queue is
    a fact about the queue, not about the process.

    Nothing is returned until three claims have been settled. Two is not a
    median, and a product that says "typically 1 day" on the strength of one
    lucky Tuesday has told somebody something it cannot support.
    """
    def secs(value: Any) -> int:
        """Epoch seconds, whatever unit the row happens to hold.

        `received_at` is written in milliseconds and `outcome_at` in seconds -
        two writers, two conventions, and nothing between them to notice.
        Subtracting one from the other gives about minus fifty-six thousand
        years, which `max(0, ...)` below then quietly turned into nought: every
        span nil, the median nil, and this reporting a confident "1 day" for
        an organisation that takes a fortnight.

        Caught only because this account has one settled claim and the figure
        stays silent below three. Normalised here rather than at the writers,
        because rows already in the table carry both.
        """
        n = int(value or 0)
        return n // 1000 if n > 10 ** 11 else n

    now = int(time.time())
    spans, waiting = [], []
    for r in rows:
        # Neither happened to a claim that was taken back or refused.
        if str(r.get("review_action") or "") in ("rejected", "withdrawn"):
            continue
        # A companion is the second document of one purchase, not a second
        # purchase, and counting it would count one wait twice.
        if str(r.get("companion_of") or ""):
            continue
        sent = secs(r.get("received_at"))
        if not sent:
            continue

        if str(r.get("outcome") or "") == "settled":
            paid = secs(r.get("outcome_at"))
            if paid:
                # Clock skew and backdated settlements both produce negatives.
                # A claim cannot be paid before it arrived.
                spans.append(max(0, paid - sent))
            continue

        # Cleared and not yet paid - by a reviewer, or by the agent with
        # nobody having disagreed or corrected it since.
        verdict = r.get("verdict") or {}
        cleared = str(r.get("review_action") or "") == "approved" or (
            verdict.get("verdict") == "approved"
            and not r.get("pulled_back") and not r.get("corrected_at"))
        if cleared:
            waiting.append(max(0, now - sent))

    def days(seconds: float) -> int:
        # Whole days, with a floor of one: "0 days" reads as a missing value,
        # and anything settled the same day is a day to somebody waiting.
        return max(1, round(seconds / 86400))

    outstanding = {
        "waiting": len(waiting),
        "waiting_oldest": days(max(waiting)) if waiting else 0,
    }

    if len(spans) < 3:
        return {"days": None, "claims": len(spans), **outstanding}

    spans.sort()
    mid = len(spans) // 2
    median = (spans[mid] if len(spans) % 2
              else (spans[mid - 1] + spans[mid]) / 2)
    return {
        "days": days(median),
        "claims": len(spans),
        "fastest": days(spans[0]),
        "slowest": days(spans[-1]),
        **outstanding,
    }


def _submissions_list(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Every receipt this organisation has, as one row each.

    Staff see their own; an owner or finance executive sees the organisation's.
    The same rule the stored original follows - a colleague's restaurant bill
    is none of an ordinary employee's business.

    A scan, deliberately, and only until it hurts. This table is small, the
    filter is on a non-key attribute, and an index that would let it be a query
    is a schema change worth making when volume asks for it rather than in
    advance of a customer.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    me = identity.resolve_by_email(actor, channel=None)
    if not me:
        return _reply(403, {"error": "No membership for this account."}, origin)

    org_id = me.get("org_id", "")
    rows = _submissions_tbl_scan(org_id)
    everyone = runs_the_org(me)
    # Computed before the rows are narrowed to the caller's own, and
    # deliberately. How long this organisation takes to reimburse somebody is
    # a property of the organisation, not of the person asking - and a
    # submitter with two settled claims to their name would otherwise be shown
    # the median of two, which is not an answer to "how long will I wait".
    # One aggregate number over everybody's claims tells them nothing about
    # anybody's claims.
    turnaround = _turnaround(rows)
    if not everyone:
        rows = [r for r in rows if r.get("submitted_by") == actor]

    rows.sort(key=lambda r: int(r.get("received_at") or 0), reverse=True)

    # The currency budgets are kept in, so a claim in another one can be told
    # apart from a claim that simply predates the conversion. Read once for the
    # whole page rather than per row.
    org = _orgs.get_item(Key={"org_id": org_id}).get("Item") if org_id else None
    budget_ccy = money.default_for_org(org) if org else ""

    return _reply(200, {
        "submissions": [_submission_view(r, budget_ccy) for r in rows[:400]],
        "budget_currency": budget_ccy,
        "can_see_everyone": everyone,
        "turnaround": turnaround,
    }, origin)


def _submissions_tbl_scan(org_id: str) -> list[dict[str, Any]]:
    rows, kwargs = [], {
        "FilterExpression": "org_id = :o",
        "ExpressionAttributeValues": {":o": org_id},
    }
    while True:
        page = _submissions.scan(**kwargs)
        rows.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey") or len(rows) > 2000:
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _budget_value(row: dict[str, Any], budget_currency: str) -> dict[str, Any]:
    """This claim in the budget's currency: stored if it is, computed if not.

    Stored wins. The rate on the row is the rate of the day the claim was
    audited, and re-deriving it at read time would move a month-old figure
    every time the market did - so two people opening the same budget on
    different days would disagree about how much a team had spent.

    Computed only for claims audited before this existed, and for those the
    rate is today's. Said so on the row, so nobody reads it as the historical
    one.
    """
    stored = row.get("budget_value") or {}
    if stored.get("amount"):
        return {k: str(v) for k, v in stored.items()}

    verdict = row.get("verdict") or {}
    frm = str(verdict.get("currency") or "")
    if not frm or not budget_currency or frm.upper() == budget_currency.upper():
        return {}
    live = fx.convert(verdict.get("receipt_total"), frm, budget_currency)
    if not live:
        return {}
    return {**live, "at": "today"}


def _submission_view(row: dict[str, Any], budget_currency: str = "") -> dict[str, Any]:
    """One receipt, flattened for the console.

    Only what a list needs. The full extraction stays on the row and is fetched
    when somebody opens a claim, so a page of forty receipts does not carry
    forty sets of line items nobody is looking at.
    """
    verdict = row.get("verdict") or {}
    receipt = row.get("receipt") or {}
    return {
        "id": row.get("submission_id"),
        "who": row.get("submitted_by", ""),
        "channel": row.get("channel", ""),
        "at": int(row.get("received_at") or 0),
        "status": row.get("status", "queued"),
        "note": row.get("last_error", ""),
        "group_id": row.get("group_id", ""),
        "group_status": row.get("group_status", ""),
        "has_original": bool(row.get("receipt_key")),
        # The filename, so a claim carrying two documents can name them rather
        # than calling them "Document 1" and "Document 2". It is what finance
        # is looking for: "the invoice" and "the receipt" are the names on the
        # paper.
        "receipt_name": row.get("receipt_name", ""),
        "receipt_type": row.get("receipt_type", ""),
        "vendor": receipt.get("vendor", ""),
        "vendor_original": receipt.get("vendor_original", ""),
        # The date printed on the bill, which is not the date it was sent in.
        # A receipt from March forwarded in September is a different thing to
        # finance than one from last night, and only this field says so.
        # Empty when the bill was illegible on that point - the model is told
        # to leave it blank rather than guess, and a guessed date on an expense
        # claim is the kind of detail an audit picks up on.
        "receipt_date": receipt.get("date", ""),
        "language": receipt.get("language", ""),
        "handwritten": bool(receipt.get("handwritten")),
        # The grand total as printed on the bill. The engine works from the sum
        # of the lines; this is the other witness, and the console recomputes
        # against it when a reviewer edits a claim.
        "stated_total": receipt.get("stated_total", ""),
        # What the vendor called this bill. Shown on the claim because it is
        # what somebody quotes back to the supplier, and it is what decides
        # whether two claims are the same bill.
        "invoice_number": receipt.get("invoice_number", ""),
        # The seller's registration, shown on the claim. Distinct from the
        # buyer's, which is what attributed the receipt to a cost centre.
        "vendor_tax_id": receipt.get("vendor_tax_id", ""),
        "buyer_tax_id": receipt.get("buyer_tax_id", ""),
        # Shown to the reviewer verbatim. A headcount that came from the
        # claimant rather than from the bill is a claim, not a fact, and the
        # person deciding has to be able to see which they are looking at.
        "sender_note": row.get("sender_note", ""),
        # Which claim this one appears to repeat. Named rather than merely
        # flagged: "possibly a duplicate" with nothing to compare against is
        # an accusation the reviewer cannot act on.
        "duplicate_of": verdict.get("duplicate_of", ""),
        # What has actually been paid, so a reload rebuilds it rather than
        # showing a settled claim as still owing.
        "outcome": row.get("outcome", ""),
        "paid": row.get("outcome_paid", ""),
        "paid_by": row.get("outcome_by_name") or row.get("outcome_by", ""),
        "paid_at": int(row.get("outcome_at") or 0),
        "paid_mode": row.get("outcome_mode", ""),
        "paid_reference": row.get("outcome_reference", ""),
        "paid_on": row.get("outcome_paid_on", ""),
        # What a person quotes about this claim. Absent on anything submitted
        # before references existed, which is honest - there is no number that
        # was ever issued for those.
        "reference": row.get("reference", ""),
        "assured": verdict.get("assured_total"),
        # What this claim counts as against a budget kept in the organisation's
        # currency. Stamped at audit time with the rate of the day; computed
        # here for claims audited before that existed, and absent when no rate
        # could be had - which the console reports rather than treating as nil.
        "budget_value": _budget_value(row, budget_currency),
        # What this claim pays out as in the currency payouts are made in,
        # with the rate that produced it. Absent when the receipt is already
        # in that currency, and absent when no rate could be had - which the
        # console reports rather than inventing a figure for.
        "payout_value": row.get("payout_value") or {},
        "review_action": row.get("review_action", ""),
        "review_reason": row.get("review_reason", ""),
        "review_by": row.get("review_by_name", ""),
        "review_at": int(row.get("review_at") or 0),
        # When the question a reviewer asked was answered. A claim is only
        # waiting on its submitter until this catches up with `review_at`.
        "answered_at": int(row.get("answered_at") or 0),
        # A reviewer supplied something the agent was missing - an expense type,
        # a currency - so the claim must come back to a person rather than
        # release itself on the strength of their correction. See _claim_retype.
        "corrected_by": row.get("corrected_by", ""),
        "corrected_at": int(row.get("corrected_at") or 0),
        # Cleared by the agent, then disagreed with. The queue needs this to
        # know the claim is waiting on a human despite a verdict that would
        # otherwise have released it, and the reviewer needs the words.
        "pulled_back": bool(row.get("pulled_back")),
        "pulled_reason": row.get("pulled_reason", ""),
        "pulled_by": row.get("pulled_by_name") or row.get("pulled_by", ""),
        "pulled_at": int(row.get("pulled_at") or 0),
        "approved_total": row.get("approved_total", ""),
        "expense_type": verdict.get("expense_type", receipt.get("expense_type", "")),
        "currency": verdict.get("currency", ""),
        "currency_assumed": bool(verdict.get("currency_assumed")),
        "total": verdict.get("receipt_total"),
        "reimbursable": verdict.get("reimbursable_total"),
        "provisional": verdict.get("provisional_total"),
        "disallowed": verdict.get("disallowed_total"),
        "verdict": verdict.get("verdict", ""),
        "violations": [
            {"code": v.get("code"), "message": v.get("message"),
             "amount": v.get("amount"), "blocking": bool(v.get("blocks_automatic_decision"))}
            for v in (verdict.get("violations") or [])
        ],
        "line_items": verdict.get("line_items") or [],
        "rationale": row.get("rationale", ""),
        # The words the submitter was actually sent, most recent first and
        # only ever one. The console's "What the submitter is told" panel
        # showed `rationale` - the model's audit explanation, which is written
        # for the verdict and dispatched to nobody - so a reviewer read a
        # paragraph the person had never seen.
        "last_notice": row.get("last_notice") or {},
        # The claim this is a second document of, if it is one. An invoice and
        # its receipt arrive in one email; both become claims; only one of them
        # is a claim.
        "companion_of": row.get("companion_of", ""),
    }


def acting_name(org: dict[str, Any], actor: str) -> str:
    me = identity.resolve_by_email(actor, channel=None) or {}
    return me.get("name") or actor


def _people(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """The organisation's members, for the People tab and for naming a claimant."""
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    me = identity.resolve_by_email(actor, channel=None)
    if not me:
        return _reply(403, {"error": "No membership for this account."}, origin)
    if not runs_the_org(me):
        return _reply(403, {"error": "Only an owner, administrator or finance executive can see the team."}, origin)

    org_id = me.get("org_id", "")
    rows, kwargs = [], {
        "FilterExpression": "org_id = :o",
        "ExpressionAttributeValues": {":o": org_id},
    }
    while True:
        page = _users.scan(**kwargs)
        rows.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    open_by_person: dict[str, int] = {}
    settled_by_person: dict[str, int] = {}
    total_by_person: dict[str, int] = {}
    try:
        for row in _submissions_tbl_scan(org_id):
            who = str(row.get("submitted_by", "")).lower()
            total_by_person[who] = total_by_person.get(who, 0) + 1
            if row.get("outcome") == "settled":
                settled_by_person[who] = settled_by_person.get(who, 0) + 1
            elif (row.get("outcome") not in OPEN_OUTCOMES
                    and row.get("review_action") not in ("rejected", "withdrawn")):
                open_by_person[who] = open_by_person.get(who, 0) + 1
    except Exception:
        logger.exception("could not count claims")

    return _reply(200, {"people": [{
        # What removing this person would stop. Shown before the question is
        # asked, because "remove them?" and "remove them and cancel four claims
        # worth 11,000?" are different questions.
        "open_claims": open_by_person.get(str(r.get("email", "")).lower(), 0),
        "settled_claims": settled_by_person.get(str(r.get("email", "")).lower(), 0),
        "total_claims": total_by_person.get(str(r.get("email", "")).lower(), 0),
        # When the invitation last went out - the original, or the most recent
        # nudge. What "48 hours" is measured from.
        "invited_at": int(r.get("reinvited_at") or r.get("added_at") or 0),
        "reinvites": int(r.get("reinvites") or 0),
        "email": r.get("email", ""),
        "name": r.get("name", ""),
        "staff_id": r.get("staff_id", ""),
        "role": r.get("role", "staff"),
        "status": r.get("status", ""),
        "mobile": r.get("mobile", ""),
        "email_channel": r.get("email_channel", "active"),
        "whatsapp_channel": r.get("whatsapp_channel", "not_added"),
        "groups": list(r.get("groups") or []),
        "added_at": int(r.get("added_at") or 0),
        # When their access was last changed and by whom, for the people who
        # are no longer on the roll. Absent on everybody who has never been
        # suspended or removed, which is almost everybody.
        "status_changed_at": int(r.get("status_changed_at") or 0),
        "status_changed_by": r.get("status_changed_by", ""),
    } for r in sorted(rows, key=lambda x: int(x.get("added_at") or 0))]}, origin)


def _audit_log(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """The organisation's decisions, newest first.

    Owner and finance only, and not staff. Every other read in this console is
    scoped so an ordinary employee sees their own claims; there is no such
    scoping to apply here. The log is a list of who decided what about whose
    money, and half of it is about colleagues - what a manager was paid, whose
    claim was refused and in what words. Giving that to everybody would make
    people stop writing honest reasons, and the reasons are the point.

    Read-only, and that is the whole API. There is no endpoint that edits or
    deletes an entry, here or anywhere else - the function's IAM grant does not
    carry the permissions to write one.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    acting = identity.resolve_by_email(actor, channel=None)
    if not acting:
        return _reply(403, {"error": "No membership for this account."}, origin)
    if not runs_the_org(acting):
        return _reply(403, {
            "error": "Only an owner, administrator or finance executive can read the audit log."}, origin)

    # One claim's history, for the panel on the claim page. Scoped to the
    # organisation inside `audit.history`, and the claim is confirmed to be
    # this organisation's here as well - the index is keyed by submission id
    # alone, so neither check is redundant.
    claim = str(body.get("submission_id", "")).strip()[:80]
    if claim:
        item = _submissions.get_item(Key={"submission_id": claim}).get("Item")
        if not item or item.get("org_id") != acting.get("org_id"):
            return _reply(404, {"error": "No claim found."}, origin)
        return _reply(200, {
            "entries": audit.history(str(acting.get("org_id") or ""), claim),
            "next": "",
        }, origin)

    rows, nxt = audit.read(str(acting.get("org_id") or ""),
                           limit=int(body.get("limit") or 200),
                           before=str(body.get("before") or "") or None)
    return _reply(200, {"entries": rows, "next": nxt}, origin)


def _api_key(token: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Look at, or mint, the organisation's API key.

    Issuing is owner-only and returns the key exactly once. There is no
    endpoint that reads one back, because there is nothing stored to read: a
    key we could hand over on request would be a key an attacker could ask for
    with a stolen session.
    """
    actor = _identity_from_token(token)
    if not actor:
        return _reply(401, {"error": "Sign in to continue."}, origin)
    org = _org_of(actor)
    if not org:
        return _reply(403, {"error": "No organisation for this account."}, origin)

    if not body.get("issue"):
        return _reply(200, {"api_key": apikeys.describe(org["org_id"])}, origin)

    if org["_role"] != "owner":
        return _reply(403, {"error": "Only an administrator or the owner can issue an API key."}, origin)

    try:
        mode = payments.selected_mode()
    except payments.PaymentError:
        mode = "test"
    issued = apikeys.issue(org["org_id"], mode, actor)
    logger.info("%s issued an API key", actor)
    return _reply(200, {
        "api_key": apikeys.describe(org["org_id"]),
        # The only time this is ever returned. Said plainly so the UI can say
        # it too, rather than the customer discovering it by closing the panel.
        "secret": issued["key"],
        "replaced": issued["replaced"],
    }, origin)


def _identity_from_token(token: str) -> str | None:
    """Verify a session token and return the email it proves."""
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(_key(), body.encode(), hashlib.sha256).digest()
        pad = "=" * (-len(sig) % 4)
        if not hmac.compare_digest(expected, base64.urlsafe_b64decode(sig + pad)):
            return None
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    if int(claims.get("exp", 0)) < int(time.time()):
        return None
    return str(claims.get("email", "")).lower() or None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _cors(origin: str | None) -> dict[str, str]:
    allow = origin if origin in ALLOWED_ORIGINS else (ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else "*")
    return {
        "Access-Control-Allow-Origin": allow,
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "POST,OPTIONS",
        "Vary": "Origin",
    }


def _jsonable(value: Any) -> Any:
    """Whatever DynamoDB handed back, in something `json.dumps` will take.

    Every number that comes out of DynamoDB is a `Decimal`, and `json.dumps`
    refuses one. Each endpoint therefore wrapped its own numbers in `int()` on
    the way out - which works right up until a field is added that nobody
    wrapped, and then the whole response raises instead of that one value being
    wrong. `/org` returns the stored policy wholesale, so the first time an
    organisation saved a policy its `version` came back a Decimal and the
    endpoint stopped answering: the console loaded with no groups, no credits
    and no rules, and there was nothing on the screen to say why.

    Doing it here means the class of bug is closed rather than one instance of
    it. A whole number goes out as one; anything else keeps its fractional part
    rather than being silently truncated - money is carried as a string in this
    API precisely so it never reaches this path, and a float here would be a
    rounding error nobody asked for.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _reply(status: int, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **_cors(origin)},
        "body": json.dumps(body, default=_jsonable),
    }


def _is_admin(email: str) -> bool:
    if email and email == ROOT_ADMIN_EMAIL:
        return True
    item = _admins.get_item(Key={"email": email}).get("Item")
    return bool(item and item.get("status") == "active")


def _default_group(org_name: str) -> dict[str, Any]:
    label = (org_name.split() or ["General"])[0][:60]
    gid = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "general"
    return {"id": gid, "label": label, "default": True}


def _signup(org_name: str, email: str, body: dict[str, Any], origin: str | None) -> dict[str, Any]:
    """Create an organisation and its owner, then send the first code."""
    now = int(time.time())

    # If the address is already registered we do NOT create anything and do NOT
    # say so - otherwise signup becomes an account-enumeration oracle. The
    # caller sees the same response either way and simply gets a sign-in code.
    existing = identity.resolve_by_email(email, channel=None)
    if existing:
        return _request_code(email, origin)

    org_id = "org_" + secrets.token_hex(8)
    _orgs.put_item(
        Item={
            "org_id": org_id,
            "name": org_name[:120],
            # Pinned now and never recomputed. Every reference this account
            # ever issues carries the same prefix, so finance can search on
            # it - which a prefix derived live from a group name could not
            # promise past the first rename.
            "ref_prefix": reference.prefix_for(org_name),
            "root_email": email,
            "billing_email": email,
            # Captured at sign-up: an invoice needs an address, and asking for
            # it later means chasing every customer who already onboarded.
            "address": _address_from(body.get("address") or {}, {}),
            "tax_id": str(body.get("tax_id", "")).strip()[:40],
            # Left empty on purpose: it follows the country captured above
            # until somebody deliberately pins it to something else.
            "default_currency": "",
            # Every organisation starts with one group, named from the first
            # word of its name. Without it, a brand-new account has nowhere to
            # attribute spend and every receipt lands in "not attributed" -
            # which reads as a defect rather than a setting nobody has made yet.
            "groups": [_default_group(org_name)],
            "credits": TRIAL_CREDITS,
            "trial_granted": True,
            "created_at": now,
        }
    )
    _users.put_item(
        Item={
            "email": email,
            "org_id": org_id,
            "org_name": org_name[:120],
            # What every claim they decide is signed with, and what the rest of
            # the organisation sees them as. Falls back to the local part only
            # for a caller that predates the field.
            "name": str(body.get("full_name", "")).strip()[:MAX_NAME] or email.split("@")[0],
            "role": "owner",
            # The owner proved control of this mailbox by completing the OTP
            # that created the org, so email is on from the start. WhatsApp is
            # not - they still have to add and verify their own number.
            "status": "active",
            "email_channel": "active",
            "whatsapp_channel": "not_added",
            "groups": [_default_group(org_name)["id"]],
            "added_at": now,
            "created_at": now,
        },
        # Belt and braces against a race between the check above and this write.
        ConditionExpression="attribute_not_exists(email) AND attribute_not_exists(org_id)",
    )
    logger.info("created org %s with %s trial credits", org_id, TRIAL_CREDITS)
    return _request_code(email, origin)


def _request_code(email: str, origin: str | None) -> dict[str, Any]:
    now = int(time.time())

    # Only registered addresses get mail. Without this, an unauthenticated
    # endpoint will send email to any address anyone types - a spam relay
    # wearing our domain, burning a sending reputation and a quota shared with
    # every other product in this account.
    #
    # The response below is deliberately identical whether or not the account
    # exists. Returning "no such account" would turn this into a free
    # who-uses-Expenze lookup for anyone with a list of addresses.
    # Sign-in accepts an invited member as well as an active one: signing in is
    # how an invitation is accepted, so demanding `active` first locks every
    # invited person out of the product for ever.
    account = identity.resolve_for_signin(email)
    if not account:
        # BMS administrators are a separate identity set - they sign in to the
        # back office without being a customer of it.
        account = _is_admin(email) and {"status": "active"} or None
    if not account:
        logger.info("code requested for unregistered or inactive address; not sending")
        return _reply(200, {"sent": True, "expires_in": CODE_TTL_SECONDS}, origin)

    item = _table.get_item(Key={"email": email}).get("Item") or {}

    last_sent = int(item.get("last_sent_at", 0))
    if now - last_sent < RESEND_COOLDOWN_SECONDS:
        wait = RESEND_COOLDOWN_SECONDS - (now - last_sent)
        return _reply(429, {"error": f"Wait {wait}s before requesting another code."}, origin)

    window_start = int(item.get("window_start", 0))
    sent_in_window = int(item.get("sent_in_window", 0))
    if now - window_start > 3600:
        window_start, sent_in_window = now, 0
    if sent_in_window >= MAX_REQUESTS_PER_HOUR:
        return _reply(429, {"error": "Too many codes requested. Try again in an hour."}, origin)

    code = _new_code()
    salt = secrets.token_hex(8)

    _table.put_item(
        Item={
            "email": email,
            "code_hash": _hash_code(code, salt),
            "salt": salt,
            "attempts": 0,
            "last_sent_at": now,
            "window_start": window_start,
            "sent_in_window": sent_in_window + 1,
            "expires_at": now + CODE_TTL_SECONDS,
        }
    )

    try:
        _send_code(email, code)
    except ClientError:
        logger.exception("SES send failed")
        return _reply(502, {"error": "Could not send the code. Try again shortly."}, origin)

    # Deliberately identical whether or not the address is known to us.
    return _reply(200, {"sent": True, "expires_in": CODE_TTL_SECONDS}, origin)


def _verify_code(email: str, code: str, origin: str | None) -> dict[str, Any]:
    now = int(time.time())
    item = _table.get_item(Key={"email": email}).get("Item")

    invalid = _reply(401, {"error": "That code is not valid or has expired."}, origin)
    if not item or int(item.get("expires_at", 0)) < now:
        return invalid

    attempts = int(item.get("attempts", 0))
    if attempts >= MAX_ATTEMPTS:
        _table.delete_item(Key={"email": email})
        return _reply(429, {"error": "Too many attempts. Request a new code."}, origin)

    expected = str(item["code_hash"])
    if not hmac.compare_digest(expected, _hash_code(code, str(item["salt"]))):
        _table.update_item(
            Key={"email": email},
            UpdateExpression="SET attempts = :a",
            ExpressionAttributeValues={":a": attempts + 1},
        )
        return invalid

    # Single use: the code dies the moment it works.
    _table.delete_item(Key={"email": email})

    # Same looser resolver as the request side, or the acceptance below could
    # never see the invited row it exists to flip.
    account = identity.resolve_for_signin(email) or {}

    # Signing in is the acceptance. It proves control of the mailbox, which is
    # precisely what gates receipts sent from it - so a separate accept link
    # would add a token to leak and expire without proving anything more.
    if account and account.get("status") == "invited":
        _users.update_item(
            Key={"email": email, "org_id": account["org_id"]},
            UpdateExpression="SET #s = :a, email_channel = :c, accepted_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":a": "active", ":c": "active", ":t": int(time.time())},
        )
        account["status"] = "active"
        logger.info("%s accepted their invitation", email)
    return _reply(
        200,
        {
            "token": _sign_session(email),
            "email": email,
            "role": account.get("role", "staff"),
            "org_name": account.get("org_name", ""),
            "is_admin": _is_admin(email),
        },
        origin,
    )


def _public_pricing(origin: str | None) -> dict[str, Any]:
    """What credits cost, for the page that sells them. No session needed.

    The marketing page used to carry its own copy of the price list, which is
    how a public page ends up quoting a rate the product no longer charges.
    It now reads the same row the console and the back office read, so a price
    changed in BMS is the price a customer is shown.

    Public on purpose: this is the same list printed on the pricing page. It
    carries no organisation, no balance and no secret.
    """
    slabs, gst = _platform_pricing()
    trial = 50
    if _settings is not None:
        try:
            row = _settings.get_item(Key={"key": "platform"}).get("Item") or {}
            trial = int(row.get("trial_credits") or 50)
        except Exception:
            logger.exception("could not read the trial allowance")
    return _reply(200, {"pricing": slabs, "gst_percent": gst,
                        "trial_credits": trial}, origin)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    origin = headers.get("origin")
    method = event.get("httpMethod", "POST")

    if method == "OPTIONS":
        return _reply(204, {}, origin)

    try:
        body = json.loads(event.get("body") or "{}")
        path = event.get("path", "")

        # Before the email gate: nobody reading a price list has an account yet.
        if path.endswith("/pricing"):
            return _public_pricing(origin)

        if not any(path.endswith(p) for p in
                   ("/whatsapp/add", "/whatsapp/verify", "/me", "/org", "/org/groups",
                    "/member/groups", "/member/transfer", "/member/reinvite", "/member",
                    "/org/budgets", "/org/rules",
                    "/claim/outcome",
                    "/claim/review", "/claim/retype",
                    "/credits/order", "/credits/verify",
                    "/submissions", "/people", "/credits/ledger", "/apikey", "/audit",
                    "/advances", "/advances/record",
                    "/receipt/view", "/receipt/upload", "/receipt/submit")):
            email = str(body.get("email", "")).strip().lower()
            if not EMAIL_RE.match(email) or len(email) > 254:
                return _reply(400, {"error": "Enter a valid email address."}, origin)

        bearer = headers.get("authorization", "")
        tok = bearer[7:] if bearer.lower().startswith("bearer ") else ""

        if path.endswith("/org"):
            return _org_get(tok, origin) if not body else _org_put(tok, body, origin)
        if path.endswith("/org/groups"):
            return _groups_put(tok, body, origin)
        if path.endswith("/org/rules"):
            return _rules_put(tok, body, origin)
        if path.endswith("/org/budgets"):
            return _budgets_put(tok, body, origin)
        if path.endswith("/claim/retype"):
            return _claim_retype(tok, body, origin)
        if path.endswith("/claim/review"):
            return _claim_review(tok, body, origin)
        if path.endswith("/claim/outcome"):
            return _claim_outcome(tok, body, origin)
        if path.endswith("/apikey"):
            return _api_key(tok, body, origin)
        if path.endswith("/audit"):
            return _audit_log(tok, body, origin)
        if path.endswith("/advances/record"):
            return _advance_record(tok, body, origin)
        if path.endswith("/advances"):
            return _advances_view(tok, body, origin)
        if path.endswith("/submissions"):
            return _submissions_list(tok, body, origin)
        if path.endswith("/people"):
            return _people(tok, body, origin)
        if path.endswith("/credits/ledger"):
            return _credits_ledger(tok, body, origin)
        if path.endswith("/credits/order"):
            return _credits_order(tok, body, origin)
        if path.endswith("/credits/verify"):
            return _credits_verify(tok, body, origin)
        if path.endswith("/member/reinvite"):
            return _reinvite(tok, body, origin)
        if path.endswith("/member/transfer"):
            return _transfer_ownership(tok, body, origin)
        if path.endswith("/member/groups"):
            return _member_groups(tok, body, origin)
        if path.endswith("/member"):
            return _member_update(tok, body, origin)

        # Ahead of the /verify catch-all below, which would otherwise swallow
        # nothing here but will the moment someone adds /receipt/verify.
        if path.endswith("/receipt/view"):
            return _receipt_view(tok, body, origin)
        if path.endswith("/receipt/upload"):
            return _receipt_upload(tok, body, origin)
        if path.endswith("/receipt/submit"):
            return _receipt_submit(tok, body, origin)

        if path.endswith("/whatsapp/add"):
            return _wa_add(tok, body, origin)
        if path.endswith("/whatsapp/verify"):
            return _wa_verify(tok, body, origin)
        if path.endswith("/me"):
            return _me(tok, origin)

        if path.endswith("/invite"):
            auth_header = headers.get("authorization", "")
            token = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
            return _invite(token, body, origin)

        if path.endswith("/signup"):
            org_name = str(body.get("org_name", "")).strip()
            if not 2 <= len(org_name) <= 120:
                return _reply(400, {"error": "Enter your organisation name."}, origin)
            # Asked for here rather than derived from the address. A membership
            # with no name is displayed by the local part of its email, so the
            # person who created the organisation appeared throughout their own
            # console - and on every claim they decided - as "riyad".
            person = str(body.get("full_name", "")).strip()
            if not MIN_NAME <= len(person) <= MAX_NAME:
                return _reply(400, {
                    "error": "Enter your name."
                    if len(person) < MIN_NAME else
                    f"A name is between {MIN_NAME} and {MAX_NAME} characters."
                }, origin)
            return _signup(org_name, email, body, origin)

        if path.endswith("/verify"):
            code = re.sub(r"\D", "", str(body.get("code", "")))
            if len(code) != 6:
                return _reply(400, {"error": "Enter the six-digit code."}, origin)
            return _verify_code(email, code, origin)

        return _request_code(email, origin)
    except Exception:
        logger.exception("auth failed")
        return _reply(500, {"error": "Something went wrong. Try again."}, origin)
