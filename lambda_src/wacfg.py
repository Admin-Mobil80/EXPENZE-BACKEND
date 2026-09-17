"""The WhatsApp secret, cached for minutes rather than for ever.

Three modules read this secret - the adapter that answers inbound messages,
the notifier that sends outcomes, and the auth endpoint that sends the code
somebody uses to claim their number. Each kept its own copy in a module global
set on first use and never refreshed, which is correct right up to the moment
the secret changes.

It changed. Moving to the dedicated Expenze number is a secret update and
nothing else - no deploy, by design - and the numbers duly swapped over. Then
a warm container that had loaded the secret before the swap went on sending
from the old number, and the person waiting for a verification code got it
from an address the product had already stopped claiming to use. No error, no
log line, nothing to notice: the code arrived, from the wrong number, for as
long as that container lived.

A per-container cache is still right. Reading a secret on every call would add
a round trip to every message and a Secrets Manager bill to every receipt.
What was wrong is the absence of an upper bound, so this one has a short life
instead: long enough that a burst of messages reads it once, short enough that
a credential rotation or a number change takes effect while somebody is still
watching to see whether it worked.

Kept in one module because three copies of a cache is three chances for one of
them to be the stale one, and the failure is silent in every case.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import boto3

# Five minutes. A migration is watched for longer than that, and a fleet under
# load reads the secret a handful of times an hour rather than once a message.
TTL_SECONDS = int(os.environ.get("WA_SECRET_TTL", "300"))

_secrets = boto3.client("secretsmanager")
_lock = threading.Lock()
_cached: dict[str, Any] | None = None
_loaded_at = 0.0


def config(secret_id: str, *, force: bool = False) -> dict[str, Any]:
    """The secret, from the cache when it is fresh enough.

    Never raises past a first successful read: if Secrets Manager is briefly
    unavailable, the copy already in hand is better than failing to answer
    somebody's receipt. A stale secret still points at a number that works;
    no secret at all points at nothing.
    """
    global _cached, _loaded_at
    now = time.time()
    if not force and _cached is not None and now - _loaded_at < TTL_SECONDS:
        return _cached

    with _lock:
        # Another thread may have refreshed it while this one waited.
        if not force and _cached is not None and time.time() - _loaded_at < TTL_SECONDS:
            return _cached
        try:
            fresh = json.loads(
                _secrets.get_secret_value(SecretId=secret_id)["SecretString"])
        except Exception:
            if _cached is None:
                raise
            return _cached
        _cached = fresh
        _loaded_at = time.time()
        return _cached


def forget() -> None:
    """Drop the cache. For tests, and for a caller that knows it is stale."""
    global _cached, _loaded_at
    _cached, _loaded_at = None, 0.0
