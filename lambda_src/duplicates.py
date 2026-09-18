"""Catching the same receipt twice.

People resend. They photograph a bill, the reply is slow, they send it again.
They forward the hotel's emailed invoice on Monday having already photographed
the printed copy on Friday. Someone forwards a colleague's receipt to help and
the colleague has already sent it. And occasionally somebody claims the same
dinner twice on purpose, which is the one finance actually worries about.

Two different questions, answered differently, because the confidence is not
the same:

**Is this the same file?** A SHA-256 of the bytes settles it. Identical bytes
are the same photograph, and there is no innocent reading - so this is caught
at intake, before a credit is spent, and the sender is told rather than
charged.

**Is this the same receipt?** Asked two ways, because they catch different
things and neither catches both.

*Same person, same day, same money.* The common case by a distance: somebody
photographs a bill on WhatsApp and then forwards the emailed copy, or resends
because the reply was slow. Every part of this is a fact we hold rather than a
string a model produced, so it does not vary between two readings of one
receipt - which is exactly how the other one used to miss these.

*Same vendor, same invoice number.* Two people claiming one invoice, which the
first test cannot see because the submitters differ. Rarer, and the one finance
actually worries about. A vendor does not issue two invoices under one number,
so unlike everything else here this one is not a guess.

This used to be "same vendor, same day, same money", which was the best
available while no invoice number was read off the bill - and which was wrong
in the commonest case in business: two colleagues each holding a seat of the
same SaaS product, billed on the same day for the same price, are not
duplicates of each other, and that rule called them one every month.

Either is *evidence*, not a verdict: two people can buy the same coffee at the
same shop for the same price on the same morning, and one person can buy two.
The claim is flagged and a human decides. Refusing it outright would eventually
reject somebody's real lunch, and that costs more trust than it saves money.

Both use one small table and a conditional write. The first receipt to claim a
fingerprint owns it; a later one fails the condition and is told which
submission owns it - the same exactly-once pattern the idempotency keys use,
for the same reason: two Lambdas racing must not both conclude they are first.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from typing import Any, Optional

import boto3

logger = logging.getLogger()

FINGERPRINTS_TABLE = os.environ.get("FINGERPRINTS_TABLE", "")

# Long enough to cover a financial year and the audit that follows it. A
# duplicate submitted fourteen months apart is not what this is for, and rows
# that never expire are a table that only grows.
RETENTION_DAYS = 400

_table = boto3.resource("dynamodb").Table(FINGERPRINTS_TABLE) if FINGERPRINTS_TABLE else None

_PUNCT = re.compile(r"[^a-z0-9]+")


def content_hash(data: bytes) -> str:
    """The identity of the bytes themselves."""
    return hashlib.sha256(data or b"").hexdigest()


# Words that are part of a company's registration, not of what anybody calls
# it. One reading of a bill says "Cursor", the next says "Cursor, Anysphere
# Inc." - the same supplier, and a fingerprint that keeps both misses the pair.
_LEGAL = {
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "pvt", "private",
    "plc", "corp", "corporation", "co", "company", "gmbh", "bv", "nv", "sa",
    "srl", "ag", "oy", "ab", "as", "pte", "sdn", "bhd", "and", "the",
}


def _normalise_vendor(vendor: str) -> str:
    """The one word that names this supplier, as steadily as can be managed.

    Two photographs of one shopfront come back as "Nandhini Deluxe" and
    "NANDHINI DELUXE," - case, punctuation and spacing are noise, and keeping
    them makes the fingerprint miss exactly the case it exists for.

    A second reading of the *same* bill varies more than that, which is the
    harder problem and the one that matters: the same Cursor invoice read twice
    produced "Cursor", "Cursor Cursor" and "Cursor (Anysphere Inc.)", three
    different fingerprints, so the duplicate went unflagged. Nothing about the
    model guarantees the same string twice, and a fingerprint built on one is
    only as reliable as that.

    So: the first meaningful word, with legal forms dropped. Coarser on
    purpose. It groups "Cursor" with "Cursor, Anysphere Inc." and it will
    occasionally group two genuinely different suppliers that share a first
    word - and that is the right way round to be wrong. A duplicate is
    *flagged* here, never refused: a false flag costs a reviewer one glance at
    two receipts, and a missed one costs a company a second payment of the same
    invoice.
    """
    words = [w for w in _PUNCT.sub(" ", (vendor or "").strip().lower()).split()
             if w and w not in _LEGAL]
    return words[0] if words else ""


def _normalise_amount(total: Any) -> str:
    """A decimal string, so 5500 and 5500.00 and "5,500.00" agree."""
    raw = str(total or "").replace(",", "").strip()
    try:
        return f"{float(raw):.2f}"
    except (TypeError, ValueError):
        return ""


def file_key(org_id: str, sha256: str) -> str:
    return f"{org_id}#file#{sha256}"


# Characters that are the same shape on a printed bill, folded together before
# an invoice number becomes a fingerprint.
#
# One Gamma transaction arrived as two documents - the invoice and the payment
# receipt for it - and the number printed on both came back as `OCB07C05-0005`
# from one and `0CB07C05-0005` from the other. Capital O against digit zero, in
# the first character. Two fingerprints, no collision, and a company one
# approval away from paying the same bill twice.
#
# Nothing else could have caught that pair. The submitter fingerprint needs the
# same date and these legitimately differ - an invoice dated the 9th and its
# receipt dated the 10th is simply how being invoiced and then paying works.
# The file fingerprints need the same bytes or the same name and size, and
# these are two different documents. The invoice number is the only thing the
# two share, so it has to survive being read twice.
#
# Folded only inside the fingerprint. What a reviewer sees is always the number
# as it was read off their bill.
#
# Deliberately one-directional and small: letters fold to digits, never the
# reverse, so `O` and `0` land on the same key without `0` ever becoming a
# letter. Collisions cost a reviewer one glance at two receipts; a miss costs a
# second payment of the same invoice, which is the whole reason this exists.
_CONFUSABLE = str.maketrans({"o": "0", "i": "1", "l": "1", "s": "5",
                             "b": "8", "z": "2"})


def invoice_key(org_id: str, vendor: str, invoice_number: str) -> str:
    """The one duplicate signal that is not a guess.

    A vendor does not issue two different invoices under one number, so two
    claims naming the same one are the same bill - whoever sent them, by
    whatever channel, however differently the two readings spelled the shop.

    This replaced "same vendor, same day, same amount", which was the best
    available while no invoice number was extracted and which was wrong in the
    commonest case in business: two colleagues each holding a seat of the same
    SaaS product, billed on the same day for the same price, are not duplicates
    of each other - and that rule called them one every month. A control that
    cries wolf on every subscription renewal is a control people learn to
    dismiss, and then it is worth nothing on the day it is right.

    Empty when the bill prints no number, which is normal for a handwritten or
    small retail bill. Those are covered by `submitter_key`, which needs
    nothing printed on the paper at all.
    """
    vendor_part = _normalise_vendor(vendor)
    number = _PUNCT.sub("", str(invoice_number or "").strip().lower())
    # Checked before folding, not after. `o5o` folds to `050`, which would
    # otherwise pass a digit test it should have failed - the point of that
    # test is that a number with no digits on the paper identifies nothing.
    #
    # A number that is a single character, or that is only the word "invoice"
    # with the digits lost, identifies nothing and would collide with every
    # other unreadable one from that vendor.
    if not vendor_part or len(number) < 3 or not any(c.isdigit() for c in number):
        return ""
    return f"{org_id}#invoice#{vendor_part}#{number.translate(_CONFUSABLE)}"


def submitter_key(org_id: str, submitted_by: str, date: str,
                  total: Any, currency: str) -> str:
    """One person, one day, one amount - the sturdiest fingerprint there is.

    Everything in it is a fact we hold rather than a string a model produced.
    Who sent it is the membership that was resolved at intake; the date is off
    the bill; the total is arithmetic. None of it varies between two readings
    of the same receipt, which is the failure mode that made the vendor-based
    fingerprint miss real duplicates: the same Cursor invoice came back as
    "Cursor", "Cursor Cursor" and "Cursor (Anysphere Inc.)".

    It catches the case that actually happens - somebody photographs a bill on
    WhatsApp and then forwards the emailed copy, or resends because the reply
    was slow. Both arrive from the same person, for the same money, on the same
    day, whatever either reading called the shop.

    It will occasionally flag two genuine receipts: one person, two coffees,
    same price, same day. That is a glance at two receipts for a reviewer, and
    a duplicate is flagged here and never refused.
    """
    who = str(submitted_by or "").strip().lower()
    amount = _normalise_amount(total)
    day = str(date or "").strip()[:10]
    ccy = str(currency or "").strip().upper()
    if not (who and amount and day and ccy):
        return ""
    return f"{org_id}#sender#{who}#{day}#{ccy}#{amount}"


def file_shape_key(org_id: str, filename: str, size: Any) -> str:
    """The same attachment, arriving twice by different routes.

    Name and exact byte count. Weaker than the content hash and it exists for
    the case the hash cannot see: one PDF forwarded round an office and sent in
    by two people through two channels, re-encoded somewhere on the way so the
    bytes no longer match. Both copies still carry the vendor's own filename
    and, usually, the same length.

    The exact byte count is what makes this safe. `receipt.jpg` is the name of
    every photograph WhatsApp has ever sent, so the name alone would flag the
    entire channel against itself; two unrelated photographs agreeing to the
    byte are a different matter.

    A generic name with nothing behind it is still refused: a file called
    `image` or `scan` with a size and nothing else is not evidence of anything.
    """
    name = _PUNCT.sub("-", str(filename or "").strip().lower()).strip("-")
    try:
        length = int(size or 0)
    except (TypeError, ValueError):
        return ""
    if not name or length <= 0:
        return ""
    return f"{org_id}#shape#{name}#{length}"


def claim(key: str, submission_id: str) -> Optional[str]:
    """Take ownership of a fingerprint.

    Returns None when this submission is the first to claim it, or the id of
    the submission that already owns it.

    A conditional write rather than read-then-write: two receipts arriving
    together would otherwise both read "nothing here" and both conclude they
    were the original.
    """
    if _table is None or not key or not submission_id:
        return None
    now = int(time.time())
    try:
        _table.put_item(
            Item={"fingerprint": key, "submission_id": submission_id,
                  "created_at": now,
                  "expires_at": now + RETENTION_DAYS * 86400},
            ConditionExpression="attribute_not_exists(fingerprint)",
        )
        return None
    except _table.meta.client.exceptions.ConditionalCheckFailedException:
        row = _table.get_item(Key={"fingerprint": key}).get("Item") or {}
        owner = str(row.get("submission_id") or "")
        # A row with no owner is a half-written claim from a run that died. It
        # proves nothing, so let this one through rather than accuse it.
        if not owner or owner == submission_id:
            return None
        logger.info("fingerprint already held by %s", owner)
        return owner
    except Exception:
        # Never fail a receipt over duplicate bookkeeping. Missing a duplicate
        # costs one wrong payment that a human may still catch; refusing a
        # genuine receipt costs the customer's trust in the whole product.
        logger.exception("could not claim a fingerprint")
        return None


def release(key: str, submission_id: str) -> None:
    """Give a fingerprint back, if this submission is the one holding it.

    Used when a claim is rejected: the receipt was never paid, so the next
    honest attempt at the same bill must not be told it is a duplicate of
    something that went nowhere.
    """
    if _table is None or not key or not submission_id:
        return
    try:
        _table.delete_item(
            Key={"fingerprint": key},
            ConditionExpression="submission_id = :s",
            ExpressionAttributeValues={":s": submission_id},
        )
    except Exception:
        logger.info("fingerprint %s was not ours to release", key[:40])


def release_all(item: dict) -> int:
    """Give back every fingerprint a claim is holding. Returns how many went.

    A claim takes out **four** fingerprints, and letting go of two of them is
    the same as letting go of none.

    A DTDC bill for INR 1,400 was rejected, the submitter photographed it again
    and sent it, and the second one was flagged `possible_duplicate` of the
    rejected one - the exact failure the release exists to prevent, still
    happening because rejection released the invoice key and the content hash
    and left the other two held.

    It was the sender key that caught it, and that is not a coincidence: it is
    the same person, the same day, the same amount and the same currency by
    definition when somebody resends their own bill, so it is *always* the key
    that matches a resubmission. The two that were being released are the two
    that often do not match a second attempt at all - the content hash needs
    identical bytes, and a second photograph is never the same bytes, while the
    invoice number is read by the model and varies between readings. Here it
    varied by a dropped digit: `d350596726` against `d3505966726`. So the one
    release that mattered was missing, and the two that fired were the ones
    that had nothing to release.

    Reconstructed from the claim rather than stored, for the two keys that were
    never written down: the shape key is built from the filename and the byte
    count, both of which are on the row. `release` is conditional on ownership,
    so passing a key this claim never held, or one a later claim now owns, is
    safely nothing.
    """
    submission_id = str(item.get("submission_id") or "")
    if not submission_id:
        return 0
    org_id = str(item.get("org_id") or "")
    sha = str(item.get("receipt_sha256") or "")
    keys = [
        str(item.get("fingerprint") or ""),
        str(item.get("sender_fingerprint") or ""),
        file_key(org_id, sha) if (org_id and sha) else "",
        file_shape_key(org_id, str(item.get("receipt_name") or ""),
                       item.get("receipt_bytes")) if org_id else "",
    ]
    freed = 0
    for key in keys:
        if key:
            release(key, submission_id)
            freed += 1
    logger.info("released %d fingerprints held by %s", freed, submission_id)
    return freed
