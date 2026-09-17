"""API keys for the third-party intake endpoint.

Until this existed `/intake/api` took anything: a POST carrying only a
colleague's work address created a claim and spent a credit. Work addresses are
guessable, so that was a way to drain an organisation's balance from outside.

Four decisions:

**The key is never stored.** Only a SHA-256 of it, so a dump of the table lets
nobody call the API. It is shown to the person who created it exactly once and
cannot be recovered afterwards - if they lose it they issue another, which is a
smaller problem than a key we could hand back to whoever asked.

**Plain SHA-256, not a slow KDF.** Passwords need one because people choose
`summer2024`; this is 32 bytes from a CSPRNG. Stretching it would protect
against nothing and cost a Lambda's time on every receipt.

**The hash is the lookup.** The table is keyed by it, so verification is one
read of the exact item rather than a scan comparing candidates - which also
means there is no timing signal to learn from.

**A key names an organisation, not a person.** It is a machine credential for a
travel system or an ERP. The employee the receipt belongs to is still resolved
the normal way, and still has to be an active member of the organisation the
key belongs to - so a key cannot claim on behalf of somebody else's company.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from typing import Any, Optional

import boto3

logger = logging.getLogger()

API_KEYS_TABLE = os.environ.get("API_KEYS_TABLE", "")

# `exp_` then mode then 32 random bytes in base32-ish hex. Prefixed so a key
# found in a log or a commit is recognisable as one, which is what makes
# automated secret scanning able to catch it.
PREFIX = "exp"
KEY_BYTES = 24

_keys = boto3.resource("dynamodb").Table(API_KEYS_TABLE) if API_KEYS_TABLE else None


def hash_key(key: str) -> str:
    return hashlib.sha256((key or "").strip().encode()).hexdigest()


def issue(org_id: str, mode: str, actor: str) -> dict[str, Any]:
    """Mint a key for an organisation. The plaintext is returned once, here.

    Any existing key for the organisation is revoked in the same breath: two
    live keys with no way to tell which system is using which is worse than
    making somebody re-paste one.
    """
    revoked = revoke_all(org_id, actor)

    secret = secrets.token_hex(KEY_BYTES)
    key = f"{PREFIX}_{'live' if mode == 'live' else 'test'}_{secret}"
    now = int(time.time())
    _keys.put_item(Item={
        "key_hash": hash_key(key),
        "org_id": org_id,
        "tail": key[-4:],
        "mode": mode,
        "status": "active",
        "created_at": now,
        "created_by": actor,
    })
    logger.info("issued an API key for %s (replacing %d)", org_id, revoked)
    return {"key": key, "tail": key[-4:], "created_at": now, "replaced": revoked}


def revoke_all(org_id: str, actor: str) -> int:
    """Turn off every key an organisation has. Returns how many."""
    if _keys is None:
        return 0
    rows = _keys.scan(
        FilterExpression="org_id = :o AND #s = :a",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":o": org_id, ":a": "active"},
    ).get("Items", [])
    for row in rows:
        _keys.update_item(
            Key={"key_hash": row["key_hash"]},
            UpdateExpression="SET #s = :r, revoked_at = :t, revoked_by = :b",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":r": "revoked", ":t": int(time.time()), ":b": actor},
        )
    return len(rows)


def describe(org_id: str) -> dict[str, Any]:
    """What the console shows: that a key exists, not what it is."""
    if _keys is None:
        return {"configured": False}
    rows = _keys.scan(
        FilterExpression="org_id = :o AND #s = :a",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":o": org_id, ":a": "active"},
    ).get("Items", [])
    if not rows:
        return {"configured": False}
    row = max(rows, key=lambda r: int(r.get("created_at") or 0))
    return {
        "configured": True,
        "tail": row.get("tail", ""),
        "mode": row.get("mode", "test"),
        "created_at": int(row.get("created_at") or 0),
        "created_by": row.get("created_by", ""),
        "last_used_at": int(row.get("last_used_at") or 0),
    }


def resolve(key: str) -> Optional[str]:
    """The organisation this key belongs to, or nothing.

    One read of the exact hash. A revoked key resolves to nothing, which is the
    whole point of keeping the row rather than deleting it: an integration that
    keeps calling with a retired key is visible instead of silent.
    """
    if _keys is None or not key:
        return None
    row = _keys.get_item(Key={"key_hash": hash_key(key)}).get("Item")
    if not row or row.get("status") != "active":
        return None

    # Best effort: knowing a key is in use is what tells you it is safe to
    # retire, and a failed write here must never fail the receipt.
    try:
        _keys.update_item(
            Key={"key_hash": row["key_hash"]},
            UpdateExpression="SET last_used_at = :t",
            ExpressionAttributeValues={":t": int(time.time())},
        )
    except Exception:
        logger.exception("could not stamp last use on an API key")
    return str(row.get("org_id") or "") or None


def bearer(headers: dict[str, Any]) -> str:
    """The key from an Authorization header, however it was cased."""
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    raw = str(lowered.get("authorization", "") or "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    # Some HTTP clients make a bare key easier than a header scheme; accepted
    # rather than making integrators fight their own library.
    return str(lowered.get("x-api-key", "") or "").strip()
