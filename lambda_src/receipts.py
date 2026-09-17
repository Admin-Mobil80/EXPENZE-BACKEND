"""The original receipt: stored once, read back only under authorisation.

Every channel puts the bytes here before anything is extracted from them, and
the extracted figures always carry a pointer back. A verdict that cannot be
traced to the piece of paper it came from is not auditable, and "the model said
so" is not an answer a finance executive can give an auditor.

Three decisions shape this file:

**Keys are opaque.** Nothing in a key names a person, an organisation, or a
date of spend. Authorisation to read a receipt comes from the submission row
that points at it - never from the shape of the key - so a key that leaks names
nobody, and a key that is guessed finds nothing that the caller was not already
entitled to.

**Nothing is served directly.** The bucket blocks public access and every read
goes out as a presigned URL that expires in five minutes. The console shows a
receipt; it never gets a durable link it might paste somewhere.

**An original with no submission is deleted.** Authorisation runs through the
submission row, so an object with no row can never be read back by anybody -
which makes it personal data held with no purpose and no way to return it.
When intake declines a receipt (out of credits, sender not recognised), the
caller discards the bytes.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
from typing import Any

import boto3
from botocore.config import Config

logger = logging.getLogger()

BUCKET = os.environ.get("RECEIPTS_BUCKET", "")

# What counts as an original. Anything else is refused at the door rather than
# stored and found to be unopenable months later, when the receipt matters.
TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "application/pdf": ".pdf",
}

MAX_BYTES = 10 * 1024 * 1024
VIEW_TTL_SECONDS = 300
UPLOAD_TTL_SECONDS = 300

# Two settings, both load-bearing for the presigned URLs below.
#
# SigV4, because buckets in regions created after 2014 reject anything older.
#
# Virtual addressing, because the default resolves to the *global* S3 host
# (bucket.s3.amazonaws.com) while still signing for ap-southeast-1. S3 answers
# that with a redirect to the regional host, the redirect drops the signature,
# and every presigned link 403s. It is a silent failure - the URL looks
# perfectly well formed - so it is pinned here rather than left to a default.
_s3 = boto3.client(
    "s3",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)


def normalise_type(content_type: str) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def is_supported(content_type: str) -> bool:
    return normalise_type(content_type) in TYPES


def safe_name(name: str, content_type: str) -> str:
    """A filename safe to put in a Content-Disposition header.

    The name arrives from an email attachment or a phone, so it is attacker
    controlled: quotes and newlines in it would let the sender write their own
    response headers.
    """
    cleaned = re.sub(r"[^\w.\-]", "_", (name or "").strip())[:120].strip("._-")
    return cleaned or ("receipt" + TYPES.get(normalise_type(content_type), ""))


def new_key(content_type: str) -> str:
    """An opaque key, dated only coarsely so lifecycle rules have something to bite on."""
    return (time.strftime("originals/%Y/%m/")
            + secrets.token_hex(16)
            + TYPES.get(normalise_type(content_type), ""))


def put(data: bytes, content_type: str, filename: str = "") -> dict[str, Any] | None:
    """Store an original. Returns the fields to record on the submission, or None.

    None means "this was not a receipt" - an unsupported type, nothing at all,
    or something past the size limit. The caller treats that as no original
    rather than as a failure: a claim with a missing photo is still a claim.
    """
    ctype = normalise_type(content_type)
    if ctype not in TYPES or not data or len(data) > MAX_BYTES:
        logger.info("not storing an original: type=%s bytes=%d", ctype, len(data or b""))
        return None

    key = new_key(ctype)
    _s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=ctype)
    return {
        "receipt_key": key,
        "receipt_type": ctype,
        "receipt_name": safe_name(filename, ctype),
        "receipt_bytes": len(data),
        "receipt_sha256": hashlib.sha256(data).hexdigest(),
    }


def describe(key: str) -> dict[str, Any] | None:
    """Confirm an object a client claims to have uploaded is really there, and sane.

    Hashes it as well, which is the whole reason this reads the body rather
    than stopping at a `head_object`. Every other channel stores the bytes
    itself and hashes them on the way past (`put` above); the portal uploads
    straight to S3 with a presigned URL, so this is the only place that can.

    Without it the portal was the one route with no identical-file check at
    all. Two colleagues uploading the same PDF - the same invoice, forwarded
    round an office, submitted twice - were both charged a credit and both
    audited, and nothing anywhere said they were the same file. It only showed
    up because the two rows had the same byte count and the same filename.

    One extra read of at most `MAX_BYTES` at submit time. The auditor fetches
    the same object seconds later, so this costs a duplicate of a read we were
    always going to make.
    """
    if not key:
        return None
    try:
        obj = _s3.get_object(Bucket=BUCKET, Key=key)
        data = obj["Body"].read(MAX_BYTES + 1)
    except Exception:
        logger.info("no object at %s", key)
        return None
    ctype = normalise_type(obj.get("ContentType", ""))
    size = len(data)
    if ctype not in TYPES or size <= 0 or size > MAX_BYTES:
        return None
    return {"receipt_key": key, "receipt_type": ctype, "receipt_bytes": size,
            "receipt_sha256": hashlib.sha256(data).hexdigest()}


def discard(key: str) -> None:
    """Drop an original whose submission never happened. See the module docstring."""
    if not key:
        return
    try:
        _s3.delete_object(Bucket=BUCKET, Key=key)
        logger.info("discarded an unclaimed original")
    except Exception:
        logger.exception("could not discard %s", key)


def view_url(key: str, filename: str = "", content_type: str = "") -> str:
    """A five-minute, read-only link to one object.

    Served inline so the console can render it in place; the filename still
    rides along so that saving it produces something recognisable rather than
    32 hex characters.
    """
    ctype = normalise_type(content_type) or "application/octet-stream"
    return _s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET,
            "Key": key,
            "ResponseContentType": ctype,
            "ResponseContentDisposition": f'inline; filename="{safe_name(filename, ctype)}"',
        },
        ExpiresIn=VIEW_TTL_SECONDS,
    )


def upload_url(content_type: str) -> tuple[str, str] | None:
    """A five-minute, write-only link for one object of one exact type.

    The browser sends the bytes straight to S3. Routing a 10MB photograph
    through API Gateway would mean base64 (a third larger again), against a
    10MB request cap - so the file would have to be small enough to be a poor
    photograph of a receipt.

    The content type is signed into the URL, so the upload cannot arrive as
    something other than what was asked for.
    """
    ctype = normalise_type(content_type)
    if ctype not in TYPES:
        return None
    key = new_key(ctype)
    url = _s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key, "ContentType": ctype},
        ExpiresIn=UPLOAD_TTL_SECONDS,
    )
    return key, url
