#!/usr/bin/env python3
"""Empty the tenant data out of Expenze, leaving the platform standing.

Run before a real go-live, when the account holds nothing but test tenants and
the intent is to onboard for the first time properly. It is not a migration and
not a backup: everything it removes is gone.

What survives, and why
----------------------
**The back office.** ``ROOT_ADMIN_EMAIL`` is stack configuration, not a row -
``admin.py`` treats it as root whatever ``Expenze-Admins`` says - so BMS access
cannot be purged away. ``Expenze-Settings`` holds the platform itself: pricing
slabs, GST, trial credits, the intake address and the WhatsApp number shown to
customers. None of it belongs to a tenant.

**The WhatsApp integration.** It lives in Secrets Manager (``expenze/whatsapp``:
WABA id, phone number id, tokens, verify token, templates) and in the
``platform`` settings row. This script touches neither. What it does remove is
each person's ``mobile`` and ``whatsapp_verified_at``, which sit on their
membership - so numbers are re-verified at onboarding, which is the point.

What goes
---------
Every row of every tenant table and every stored receipt. There is one tenant
today and the tables are small; even so this deletes by scanning rather than by
filtering on an org, because "everything" is the instruction and a filter that
misses a row leaves a fresh account with somebody else's history in it.

Usage
-----
    python tools/purge_customer_data.py            # counts only, deletes nothing
    python tools/purge_customer_data.py --yes      # actually deletes
"""
from __future__ import annotations

import argparse
import sys

import boto3

REGION = "ap-southeast-1"

# Tenant data. Emptied completely.
TABLES: list[tuple[str, tuple[str, ...]]] = [
    ("Expenze-Orgs", ("org_id",)),
    ("Expenze-Memberships", ("email", "org_id")),
    ("Expenze-Submissions", ("submission_id",)),
    ("Expenze-Fingerprints", ("fingerprint",)),
    ("Expenze-CreditLedger", ("org_id", "ts")),
    ("Expenze-Purchases", ("order_id",)),
    ("Expenze-ApiKeys", ("key_hash",)),
    ("Expenze-AuthCodes", ("email",)),
    ("Expenze-Idempotency", ("idem_id",)),
    # Superseded by Expenze-Submissions and empty for months. Cleared with the
    # rest so a later reader does not find one stale row and trust it.
    ("ExpensifyAI-Expenses", ("expense_id",)),
]

# Stored receipts, and the raw mail they arrived in.
BUCKETS = ["expenze-receipts", "expenze-inbound-mail"]

# Named so the report can state what it is leaving alone, rather than leaving
# the reader to infer it from an absence.
KEPT = [
    ("Expenze-Settings", "pricing, GST, trial credits, intake address, WhatsApp number"),
    ("Expenze-Admins", "back-office accounts (root comes from stack config regardless)"),
    ("secret expenze/whatsapp", "WABA id, phone number id, tokens, templates"),
    ("secret expenze/razorpay", "payment keys"),
    ("secret expenze/session-signing-key", "session signing"),
    ("secret expensifyai/openai-api-key", "model access"),
    ("s3 expenze-site / expenze-bms", "the console and back office themselves"),
]


def purge_table(ddb, name: str, key_names: tuple[str, ...], commit: bool) -> int:
    table = ddb.Table(name)
    keys, start = [], None
    while True:
        kw = {"ProjectionExpression": ", ".join(f"#k{i}" for i in range(len(key_names))),
              "ExpressionAttributeNames": {f"#k{i}": k for i, k in enumerate(key_names)}}
        if start:
            kw["ExclusiveStartKey"] = start
        page = table.scan(**kw)
        keys.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            break

    if commit and keys:
        with table.batch_writer() as batch:
            for k in keys:
                batch.delete_item(Key=k)
    return len(keys)


def purge_bucket(s3, name: str, commit: bool) -> int:
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=name):
        objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if not objects:
            continue
        n += len(objects)
        if commit:
            s3.delete_objects(Bucket=name, Delete={"Objects": objects})
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true",
                    help="actually delete; without it nothing is written")
    args = ap.parse_args()
    commit = args.yes

    session = boto3.Session(region_name=REGION)
    ddb = session.resource("dynamodb")
    s3 = session.client("s3")
    account = session.client("sts").get_caller_identity()["Account"]

    print(f"Account {account}, region {REGION}")
    print("DELETING\n" if commit else "DRY RUN - nothing will be written\n")

    total = 0
    for name, key_names in TABLES:
        try:
            n = purge_table(ddb, name, key_names, commit)
        except ddb.meta.client.exceptions.ResourceNotFoundException:
            print(f"  {name:<26} (no such table)")
            continue
        total += n
        print(f"  {name:<26} {n:>5} rows{' deleted' if commit and n else ''}")

    for prefix in BUCKETS:
        bucket = f"{prefix}-{account}"
        try:
            n = purge_bucket(s3, bucket, commit)
        except s3.exceptions.NoSuchBucket:
            print(f"  {bucket:<26} (no such bucket)")
            continue
        total += n
        print(f"  {bucket:<26} {n:>5} objects{' deleted' if commit and n else ''}")

    print("\nUntouched:")
    for what, why in KEPT:
        print(f"  {what:<34} {why}")

    if commit:
        print(f"\n{total} records removed. Sign up fresh at expenze.ai; the new "
              f"organisation gets a new id and the trial credits set in BMS.")
    else:
        print(f"\n{total} records would be removed. Re-run with --yes to do it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
