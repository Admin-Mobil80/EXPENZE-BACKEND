"""Fetch and cache a secret value for the container's lifetime.

Cached deliberately: Secrets Manager charges per API call and the key never
changes within a container. Never logged, never returned in an API response.
"""
from __future__ import annotations

import json

import boto3

_client = boto3.client("secretsmanager")
_cache: dict[str, str] = {}


def get_secret_value(secret_id: str, json_key: str = "api_key") -> str:
    """Return the secret string, unwrapping ``{"api_key": "..."}`` if present.

    Accepts either a bare string secret or a JSON object holding ``json_key``,
    since the console's "key/value" editor produces the latter and the CLI's
    ``--secret-string`` commonly produces the former.
    """
    if secret_id in _cache:
        return _cache[secret_id]

    raw = _client.get_secret_value(SecretId=secret_id).get("SecretString", "")
    value = raw.strip()
    if value.startswith("{"):
        try:
            value = str(json.loads(value).get(json_key, "")).strip()
        except json.JSONDecodeError:
            pass

    _cache[secret_id] = value
    return value
