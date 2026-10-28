"""Sending and receiving through AWS End User Messaging Social.

The same WhatsApp number, reached a different way. Until now every message
went straight to Meta's Graph API with a bearer token kept in the secret;
this module puts AWS between us and Meta instead, so the credential becomes
the Lambda's own IAM role and there is no long-lived token to leak, rotate or
forget to rotate.

Nothing about the *messages* changes, and that is the point. `SendWhatsAppMessage`
takes the WhatsApp Message object verbatim - the same dict the notifier already
builds for an approved template, the same one the adapter builds for a line of
text. So this module carries no knowledge of templates, parameters or copy. It
is a transport and only a transport, which is why swapping it is survivable.

Three things genuinely differ, and each one is a trap:

**The number has two names.** Meta calls it `1364912593363578`; AWS calls the
same number `phone-number-id-186b71...`. Inbound deliveries name the Meta one,
every AWS call wants the AWS one, and nothing in either id hints at the other.
The mapping lives in the secret beside the number it belongs to, because a
lookup on every send would put `GetLinkedWhatsAppBusinessAccount` in the path
of every receipt.

**Media never arrives inline.** `GetWhatsAppMessageMedia` returns a mime type
and a size and writes the bytes to S3; there is no "give me the bytes" call.
So a receipt lands in a staging key, is read back once, and the staging copy is
dropped. That is one more round trip than Meta's two hops, in exchange for the
bytes never being held in Lambda memory twice.

**The region is not ours.** The WABA is linked in us-east-1 and everything else
Expenze owns is in ap-southeast-1. That is not a mistake to correct in code:
re-linking means redoing the Meta onboarding, and a cross-region API call costs
tens of milliseconds on a path that already waits on Meta.

Which transport is live is a secret key, not a deploy, for the same reason the
number itself is: a cutover that can only be undone by a release is a cutover
nobody dares make on a Friday. `transport` defaults to `meta`, so deploying
this module changes nothing until somebody decides otherwise.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

import boto3

logger = logging.getLogger()

# The API version the Message object is written against. Meta's shapes are
# versioned and AWS passes ours through untouched, so this tracks whatever the
# payload builders in notify.py and auth.py were written for.
META_API_VERSION = os.environ.get("EUM_META_API_VERSION", "v21.0")

# Where the WABA is linked, which is not where the rest of the stack lives.
DEFAULT_REGION = os.environ.get("EUM_REGION", "us-east-1")

# Media is staged here on its way in. Same bucket the original ends up in, so
# no second grant and no second lifecycle rule.
RECEIPTS_BUCKET = os.environ.get("RECEIPTS_BUCKET", "")

_lock = threading.Lock()
_clients: dict[str, Any] = {}


def _client(region: str):
    """One client per region, built once per container.

    boto3 clients are expensive to construct and safe to share; building one
    per message would add a credential resolution to every receipt.
    """
    with _lock:
        if region not in _clients:
            _clients[region] = boto3.client("socialmessaging", region_name=region)
        return _clients[region]


def _s3():
    with _lock:
        if "s3" not in _clients:
            _clients["s3"] = boto3.client("s3")
        return _clients["s3"]


# ---------------------------------------------------------------------------
# What the secret says
# ---------------------------------------------------------------------------


def settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """The `aws` block of the WhatsApp secret, or an empty one."""
    block = cfg.get("aws")
    return block if isinstance(block, dict) else {}


def enabled(cfg: dict[str, Any]) -> bool:
    """True when this transport is the live one.

    Defaults to False. A secret written before this module existed, or one
    mid-edit with the `aws` block half filled in, must keep sending the way it
    always did rather than start failing quietly.
    """
    if str(cfg.get("transport") or "meta").strip().lower() != "aws":
        return False
    if not settings(cfg).get("phoneNumberId"):
        logger.warning(
            "transport is 'aws' but the secret has no aws.phoneNumberId; "
            "falling back to Meta so messages keep arriving")
        return False
    return True


def region(cfg: dict[str, Any]) -> str:
    return str(settings(cfg).get("region") or DEFAULT_REGION)


def number_for(cfg: dict[str, Any], meta_phone_number_id: str = "") -> str:
    """The AWS id of the number a message arrived on, or of our own number.

    During a migration a WABA holds more than one number and inbound names
    whichever was messaged. Answering from the wrong one splits the thread on
    the sender's phone - the same failure the Meta path guards against with
    `creds_for`, and the same guard, expressed in the other vocabulary.

    `alsoAccept` entries may each carry their own `aws.phoneNumberId`. One that
    does not is a number AWS does not manage, and there is no id to return.
    """
    wanted = str(meta_phone_number_id or "")
    if wanted:
        for entry in [cfg] + list(cfg.get("alsoAccept") or []):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("phoneNumberId") or "") == wanted:
                # An `alsoAccept` entry with no aws block of its own is not
                # assumed to share ours: that would send from the wrong number.
                return str(settings(entry).get("phoneNumberId") or "")
    return str(settings(cfg).get("phoneNumberId") or "")


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send(cfg: dict[str, Any], message: dict[str, Any],
         meta_from_id: str = "") -> bool:
    """Send one WhatsApp Message object. True when AWS accepted it.

    `message` is exactly what the Graph path would have POSTed, `to` and
    `type` and template components and all. It is not inspected here.

    Raises nothing. Every caller is already reporting delivery rather than
    depending on it - the settlement has happened, the code has been generated -
    and a transport that throws would turn a missed message into a lost claim.
    """
    number = number_for(cfg, meta_from_id)
    if not number:
        logger.warning("no AWS phone number id for Meta number %s", meta_from_id or "(default)")
        return False
    try:
        _client(region(cfg)).send_whatsapp_message(
            originationPhoneNumberId=number,
            message=json.dumps(message).encode(),
            metaApiVersion=META_API_VERSION,
        )
        return True
    except Exception:
        logger.exception("End User Messaging rejected a %s message",
                         message.get("type") or "unknown")
        return False


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------


def fetch_media(cfg: dict[str, Any], media_id: str, meta_from_id: str,
                max_bytes: int) -> tuple[bytes, str] | None:
    """The bytes of an inbound photograph or PDF, and its mime type.

    Two calls where Meta needed two hops, for a different reason: the first
    asks only for the metadata, so an unsupported type or an oversized file is
    refused before anything is written anywhere. The second writes the bytes to
    a staging key, which is read back once and then deleted.

    `fileSize` from AWS is documented in KB while Meta's `file_size` is bytes,
    and a limit enforced against the wrong unit is a limit that is either a
    thousand times too strict or not there at all. So the metadata size is used
    only to reject the obviously enormous, and the real check is on the bytes
    that actually arrived.
    """
    number = number_for(cfg, meta_from_id)
    if not number:
        logger.warning("no AWS phone number id to fetch media against")
        return None

    client = _client(region(cfg))
    key = f"inbound/{media_id}"
    try:
        meta = client.get_whatsapp_message_media(
            mediaId=media_id, originationPhoneNumberId=number, metadataOnly=True)
        mime = str(meta.get("mimeType") or "")

        # Generous, and deliberately so: this only exists to avoid writing a
        # video to S3 before refusing it. `receipts.put` enforces the real one.
        stated_kb = int(meta.get("fileSize") or 0)
        if stated_kb and stated_kb * 1024 > max_bytes * 4:
            logger.info("media %s is about %d KB; refusing before download",
                        media_id, stated_kb)
            return None

        client.get_whatsapp_message_media(
            mediaId=media_id,
            originationPhoneNumberId=number,
            destinationS3File={"bucketName": RECEIPTS_BUCKET, "key": key},
        )
        body = _s3().get_object(Bucket=RECEIPTS_BUCKET, Key=key)["Body"]
        # One byte past the limit is enough to know it is over, and stops a
        # hostile or broken sender pulling an unbounded object into memory.
        data = body.read(max_bytes + 1)
        if len(data) > max_bytes:
            logger.info("media %s exceeds the size limit", media_id)
            return None
        return data, mime
    except Exception:
        logger.exception("could not fetch media %s", media_id)
        return None
    finally:
        # The original is stored under its own fingerprinted key by
        # `receipts.put`; this copy exists only to carry the bytes across.
        try:
            _s3().delete_object(Bucket=RECEIPTS_BUCKET, Key=key)
        except Exception:
            logger.warning("could not remove staged media %s", key)


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------


def entries(event: dict[str, Any]) -> list[dict[str, Any]]:
    """The Meta webhook entries carried by an SNS delivery.

    AWS does not invent a new inbound shape - it wraps Meta's. Each SNS record
    carries a JSON message whose `whatsAppWebhookEntry` is the same `entry`
    object the webhook would have received, as a *string*. Unwrapping it here
    means `_handle_message` cannot tell which transport delivered a receipt,
    which is the whole reason both can be live at once.

    A record that does not parse is logged and skipped rather than raised: one
    malformed delivery in a batch must not take the rest of the batch down,
    because SNS would then redeliver the ones that already succeeded.
    """
    out: list[dict[str, Any]] = []
    for record in event.get("Records") or []:
        raw = ((record.get("Sns") or {}).get("Message")) or ""
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            logger.exception("an SNS record carried something that was not JSON")
            continue
        if not isinstance(payload, dict):
            continue
        entry = payload.get("whatsAppWebhookEntry")
        if isinstance(entry, str):
            try:
                entry = json.loads(entry)
            except Exception:
                logger.exception("whatsAppWebhookEntry was not JSON")
                continue
        if isinstance(entry, dict):
            out.append(entry)
        elif isinstance(entry, list):
            out.extend(e for e in entry if isinstance(e, dict))
    return out


def is_sns(event: dict[str, Any]) -> bool:
    """Whether this invocation came from SNS rather than API Gateway.

    Both are live during the cutover and they arrive at the same handler.
    """
    records = event.get("Records")
    return bool(records) and isinstance(records, list) \
        and isinstance(records[0], dict) and "Sns" in records[0]
