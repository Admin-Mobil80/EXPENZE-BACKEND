"""WhatsApp intake: photograph a receipt, send it, get an answer back.

Meta's WhatsApp Cloud API, driven entirely by a secret so the number can be
swapped without a code change:

    GET  /whatsapp/webhook   Meta's subscription challenge
    POST /whatsapp/webhook   inbound messages

Three things carry the safety of this file:

**Every POST is signature-checked.** The webhook URL is public and spends
credits, so an unsigned request is refused before anything is read. Meta signs
the raw body with the app secret as `X-Hub-Signature-256`; we recompute it and
compare in constant time.

**Unknown senders get silence.** Not an error, not a "who are you?" - nothing.
A reply confirms to anyone probing the number that it is live and that their
message reached something. The resolver in identity.py additionally requires
the number to have been added *and verified by its owner*, so an administrator
typing a digit wrong never turns a stranger into a claimant.

**Replies only ever go to resolved senders**, and only about their own claim.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Optional

import boto3

import identity
import intake
import policy
import receipts
import wacfg

logger = logging.getLogger()
logger.setLevel(logging.INFO)

GRAPH = "https://graph.facebook.com/v21.0"
INTAKE_TABLE = os.environ["INTAKE_TABLE"]
WA_SECRET_ARN = os.environ["WA_SECRET_ARN"]

_intake_tbl = boto3.resource("dynamodb").Table(INTAKE_TABLE)
_secrets = boto3.client("secretsmanager")


def config() -> dict[str, str]:
    """Number, tokens and app secret. Cached per container.

    Everything that identifies the WhatsApp number lives here rather than in
    code, so moving from a shared number to a dedicated one is a secret update
    and a webhook re-registration - no deploy.
    """
    return wacfg.config(WA_SECRET_ARN)


# ---------------------------------------------------------------------------
# Graph API
# ---------------------------------------------------------------------------


def identities() -> list[dict[str, str]]:
    """Every WhatsApp number we currently answer on, credentials included.

    One entry normally. Two while a migration is in flight, and that is the
    whole reason this exists.

    A number belongs to a Meta app, and so does everything needed to work with
    it: the app secret that signs its deliveries, and the access token that
    downloads its media and sends its replies. A token issued for a new app
    cannot fetch a photograph sent to a number on the old one - so swapping the
    credentials wholesale does not merely change which number is preferred, it
    stops the old number working, and the person who sent the receipt is simply
    ignored.

    The top-level values are the identity used for messages *we* start - the
    sign-in code, the settlement notice - because those have no inbound
    delivery to take their credentials from. Anything under `alsoAccept` is a
    number we still answer on but no longer initiate from.
    """
    cfg = config()
    out = [cfg]
    for extra in (cfg.get("alsoAccept") or []):
        if isinstance(extra, dict) and extra.get("phoneNumberId"):
            # Inherit anything the entry does not override, so an old number
            # sharing a token with the new one need only name its own id.
            out.append({**cfg, **extra})
    # The flat form, kept working so a secret written against the earlier shape
    # does not silently stop verifying.
    if cfg.get("previousAppSecret") or cfg.get("previousWebhookVerifyToken"):
        out.append({**cfg,
                    "appSecret": cfg.get("previousAppSecret") or cfg["appSecret"],
                    "webhookVerifyToken": cfg.get("previousWebhookVerifyToken")
                                          or cfg.get("webhookVerifyToken")})
    return out


def creds_for(phone_number_id: str) -> dict[str, str]:
    """The credentials belonging to the number a message arrived on."""
    if phone_number_id:
        for identity_ in identities():
            if str(identity_.get("phoneNumberId", "")) == str(phone_number_id):
                return identity_
    return config()


def _graph(url: str, token: str, *, data: bytes | None = None) -> bytes:
    req = urllib.request.Request(url, data=data)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def _fetch_media(media_id: str, token: str) -> tuple[bytes, str] | None:
    """Meta hands over an id; the bytes need two authenticated hops."""
    try:
        meta = json.loads(_graph(f"{GRAPH}/{media_id}", token))
        mime = receipts.normalise_type(meta.get("mime_type") or "")
        if not receipts.is_supported(mime):
            logger.info("media %s is %s; not a receipt", media_id, mime)
            return None
        if int(meta.get("file_size", 0)) > receipts.MAX_BYTES:
            logger.info("media %s exceeds the size limit", media_id)
            return None
        return _graph(meta["url"], token), mime
    except Exception:
        logger.exception("could not fetch media %s", media_id)
        return None


def _received(outcome: dict[str, Any], stored: dict[str, Any]) -> str:
    """The acknowledgement, with enough in it to be worth reading.

    "Got it" alone leaves the sender wondering whether the right photograph
    arrived, against which company, and what happens next. A reference they can
    quote to finance costs nothing to include and saves the message that
    otherwise follows a week later.

    The reference, not a second one derived from the submission id. This used to
    print `EXP-7834875` - the last seven digits of the internal id - while every
    other message about the same claim called it `Mobil80-Exp-4`. Two names for
    one thing, and the one shown first was the one finance had never heard of:
    somebody quoting it back was quoting a number that appears nowhere in the
    console, on the invoice, or in any other message.

    """
    reference = str(outcome.get("reference") or "")

    lines = ["*Receipt received*", ""]
    if reference:
        lines.append(f"Claim: {reference}"
                     + (f"  ·  {outcome['org_name']}" if outcome.get("org_name") else ""))
    elif outcome.get("org_name"):
        lines.append(f"Claimed against {outcome['org_name']}.")

    size = int(stored.get("receipt_bytes") or 0)
    kind = "PDF" if stored.get("receipt_type") == "application/pdf" else "Photo"
    if size:
        lines.append(f"{kind} received, {max(1, round(size / 1024))} KB")

    # Where the spend will be attributed, said plainly. "Unset" is not a
    # failure - it is finance's job at settlement - but the sender should know
    # it is not tagged rather than assume it is.
    group = outcome.get("group_label") or ""
    if outcome.get("group_status") == "assigned" and group:
        lines.append(f"Group: {group}")
    elif outcome.get("group_status") == "unset":
        lines.append("Group: not set — your finance team will assign it")
    elif outcome.get("group_status") == "ask":
        # Belongs to several and the bill has not been read yet. Nobody is
        # asked: if the bill does not settle it, the reviewer does.
        lines.append("Group: your finance team will confirm it")

    lines += [
        "",
        # And nothing after it.
        #
        # There was a line here telling the sender what to do if they had sent
        # the wrong photograph. It is the exception, not the path: almost
        # everybody sends the right receipt, and everybody was being handed
        # instructions for a mistake they had not made - at the end of the one
        # message they actually read, which is the worst place to spend their
        # attention. Withdrawing is still there in the portal for the people
        # who need it, and the outcome message follows in a minute either way.
        "Reading it now. I check it against your expense policy and come back "
        "here with the outcome, usually within a minute.",
    ]
    return "\n".join(lines)


def _reply(to: str, text: str, from_id: str = "", token: str = "") -> None:
    """Only ever called for a sender we resolved.

    Answers from the number that was messaged, not from whichever one the
    secret happens to name. While a migration is in flight both numbers are
    live, and replying to the old number from the new one splits the
    conversation across two threads on the sender's phone - they see their
    receipt go one way and the verdict arrive from a stranger.

    The id comes from Meta's own payload, which has already been
    signature-checked, so it is as trustworthy as the message it arrived with.
    """
    cfg = config()
    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text[:4000]},
    }).encode()
    try:
        _graph(f"{GRAPH}/{from_id or cfg['phoneNumberId']}/messages",
               token or cfg["accessToken"], data=payload)
    except Exception:
        logger.exception("reply to %s failed", to)


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def _signature_ok(raw_body: str, header: str, *app_secrets: str) -> bool:
    """Signed by any app we are currently listening for.

    Normally that is one. During a move to a dedicated Meta app it is two: the
    old number's deliveries are signed by the old app's secret and the new
    one's by the new, and there is no instant at which both switch over.

    With a single secret the cutover has a cliff - the moment it is replaced,
    every message to the old number fails its signature check and is dropped,
    which the sender experiences as the service silently ignoring them. So the
    secret may carry `previousAppSecret` for as long as the old number is live,
    and it is removed once it is not.

    Each candidate is compared in constant time, and an empty one is skipped
    rather than treated as a secret that happens to be blank.
    """
    if not header.startswith("sha256="):
        return False
    offered = header[7:]
    return any(
        hmac.compare_digest(
            hmac.new(secret.encode(), raw_body.encode(), hashlib.sha256).hexdigest(),
            offered)
        for secret in app_secrets if secret)


def _verify(event: dict[str, Any]) -> dict[str, Any]:
    """Meta's one-time subscription handshake."""
    q = event.get("queryStringParameters") or {}
    cfg = config()
    # Either token, for the same reason two app secrets are accepted: the old
    # app may re-verify its subscription while the new one is being set up.
    accepted = [t for t in (i.get("webhookVerifyToken") for i in identities()) if t]
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") in accepted:
        logger.info("webhook verification succeeded")
        return {"statusCode": 200, "headers": {"Content-Type": "text/plain"},
                "body": q.get("hub.challenge", "")}
    logger.warning("webhook verification failed")
    return {"statusCode": 403, "body": "forbidden"}


def _handle_message(msg: dict[str, Any], cfg: dict[str, str], from_id: str = "") -> None:
    # The credentials belonging to the number this arrived on, which during a
    # migration is not necessarily the one the secret prefers. Downloading the
    # photograph and answering the sender both go through the app that owns
    # the number; the other app's token cannot see either.
    cfg = creds_for(from_id) or cfg
    token = cfg["accessToken"]
    sender = msg.get("from", "")
    kind = msg.get("type")

    membership = identity.resolve_by_mobile(sender)
    if not membership:
        # Deliberate silence to the sender. See the module docstring.
        #
        # The log, though, has to name enough of the number to answer "why did
        # my receipt vanish?" without a full number sitting in CloudWatch. The
        # last four digits identify it to whoever sent it and to nobody else.
        logger.info(
            "unresolved WhatsApp sender ending %s; ignoring without reply. "
            "The number must be added and verified by its owner in the console.",
            sender[-4:] if len(sender) >= 4 else "????")
        return

    media_id = (msg.get(kind) or {}).get("id") if kind in ("image", "document") else None
    if not media_id:
        # Nothing is ever asked of a submitter, so anything that is not a
        # receipt is answered the same way. The agent used to put questions
        # here - a headcount, which cost centre a bill belonged to - and every
        # one of them was a round trip asking somebody who photographed a bill
        # to do finance's arithmetic. What it could not settle now goes to a
        # reviewer instead.
        _reply(sender, "Send a photo of the receipt and I'll audit it against "
                       "your expense policy.", from_id, token)
        return

    fetched = _fetch_media(media_id, token)
    if not fetched:
        _reply(sender, "I couldn't read that attachment. A photo (JPEG or PNG) or a PDF works best.", from_id, token)
        return

    payload, mime = fetched
    filename = (msg.get(kind) or {}).get("filename", "") if kind == "document" else ""
    stored = receipts.put(payload, mime, filename) or {}

    # The caption sent with the photo. Nothing is required of it and nothing is
    # asked for it, but "client dinner, Bangalore office" is context a reviewer
    # reads and the paper often does not carry - and typing it while sending
    # costs the employee nothing.
    caption = str((msg.get(kind) or {}).get("caption", "") or "")[:600]

    # Same gate as email and the API - credits, membership, everything.
    response = intake.lambda_handler(
        {"path": "/intake/whatsapp",
         "body": json.dumps({"from_mobile": sender, "sender_note": caption, **stored})},
        None,
    )
    outcome = json.loads(response.get("body") or "{}")
    status = outcome.get("status")

    if status != "queued":
        # Nothing points at those bytes now, so nothing could ever read them
        # back. Keeping someone's receipt in that state is storage without a
        # purpose.
        receipts.discard(stored.get("receipt_key", ""))

    if status == "queued":
        # The acknowledgement, always, and nothing else. The group question
        # used to be asked here instead, which was wrong twice over.
        #
        # It was asked before the bill had been read, so a tax invoice made out
        # to the company - which prints the registration that names the group
        # outright - still interrupted the sender to ask a question the paper
        # had already answered.
        #
        # And it replaced the acknowledgement rather than following it. Someone
        # who photographs a receipt and is met with "Which group is it for?"
        # has not been told their receipt arrived, or against which company, or
        # what its reference is. Jayakumar answered that question correctly and
        # the thread still read as though the receipt itself had gone missing.
        #
        # So: acknowledge here, and nowhere ask. A sender who belongs to
        # several groups is told the finance team will confirm which one, and
        # the reviewer resolves it on the claim.
        _reply(sender, _received(outcome, stored), from_id, token)
    elif status == "no_credits":
        _reply(sender, "Your organisation is out of Expenze credits, so this receipt wasn't processed. "
                       "Ask your finance team to top up and send it again.", from_id, token)
    elif status == "duplicate":
        # The same bytes twice, caught before a credit was spent - and, until
        # now, answered with nothing at all. Somebody who photographs a bill
        # and hears silence concludes it did not arrive, and the obvious next
        # move is to send it a third time. That is the loop this detection
        # exists to end, and staying quiet was feeding it.
        ref = str(outcome.get("reference") or "")
        which = (f"This is the same receipt as {ref}, which arrived earlier."
                 if ref else "This exact receipt has already been sent in.")
        # No mention of credits. A credit is what the organisation is billed
        # in; the person who photographed a bill has no idea what one is, and
        # telling them none was spent answers a question they were not asking
        # in a vocabulary they do not have. What they might reasonably worry
        # about is having claimed the same thing twice - so that is what the
        # message settles.
        said = ("*Already received*\n\n" + which + "\n\n"
                "No second claim was made. The outcome of the first one still "
                "follows here.")
        _reply(sender, said, from_id, token)
    else:
        logger.info("intake returned %s for a resolved sender", status)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = event.get("httpMethod", "POST")
    if method == "GET":
        return _verify(event)

    try:
        cfg = config()
        raw = event.get("body") or ""
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

        if not _signature_ok(raw, headers.get("x-hub-signature-256", ""),
                             *[i["appSecret"] for i in identities() if i.get("appSecret")]):
            logger.warning("rejected an unsigned or mis-signed webhook delivery")
            return {"statusCode": 403, "body": "forbidden"}

        body = json.loads(raw or "{}")
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                # Which of our numbers this arrived on. A WABA can hold
                # several, and during a migration it holds two.
                from_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
                for msg in value.get("messages", []):
                    _handle_message(msg, cfg, from_id)

        # Always 200 once signed: a non-2xx makes Meta retry, and a retry storm
        # on a handler that spends credits is worse than a dropped message.
        return {"statusCode": 200, "body": "ok"}
    except Exception:
        logger.exception("whatsapp webhook failed")
        return {"statusCode": 200, "body": "ok"}
