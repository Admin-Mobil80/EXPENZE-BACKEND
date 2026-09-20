"""The thing that actually reads what people send in.

Every channel - WhatsApp, email, the portal, the API - ends at a row in
`Expenze-Submissions` marked `queued`. Until this existed, that is where a
receipt stopped: stored, charged for, and never read. The console showed
nothing because there was nothing to show.

Triggered by the table's own stream rather than called by each channel. Three
reasons, and the third is the one that matters:

* The channel adapters answer a webhook. Auditing takes two model passes and
  tens of seconds; Meta and SES both want an answer in a few.
* One trigger, one code path, whatever the receipt arrived on.
* A failure is retried by the stream instead of being lost. A receipt that was
  charged for and never read is the worst outcome this system has, because the
  customer paid a credit and nobody knows the claim exists.

**A receipt is audited once.** The row is claimed with a conditional update
from `queued` to `auditing` before any model call. A stream retry - and
DynamoDB streams retry - finds the row already claimed and does nothing, so a
retry costs nothing rather than a second model call against the same
photograph. If the audit then fails, the row is put back to `queued` and the
next delivery picks it up.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer

import duplicates
import handler
import identity
import notify
import pdfpages
import grouping

logger = logging.getLogger()
logger.setLevel(logging.INFO)

INTAKE_TABLE = os.environ["INTAKE_TABLE"]
RECEIPTS_BUCKET = os.environ["RECEIPTS_BUCKET"]

# Anything larger is not a photograph of a receipt, and the model has its own
# limit well below this.
MAX_AUDIT_BYTES = 5 * 1024 * 1024

ORGS_TABLE = os.environ.get("ORGS_TABLE", "")

_s3 = boto3.client("s3")
_ddb = boto3.resource("dynamodb")
_intake = _ddb.Table(INTAKE_TABLE)
# For matching a receipt to a cost centre by the registration printed on it.
_orgs = _ddb.Table(ORGS_TABLE) if ORGS_TABLE else None


def _plain(image: dict[str, Any]) -> dict[str, Any]:
    """A DynamoDB stream image, as ordinary Python."""
    de = TypeDeserializer()
    return {k: de.deserialize(v) for k, v in (image or {}).items()}


# How long a receipt may sit in `auditing` before another invocation is
# entitled to conclude that whoever had it is not coming back. Comfortably
# longer than this function's own timeout, so a slow audit is never stolen from
# itself; short enough that a stranded claim is picked up on the next delivery
# rather than sitting there until somebody notices.
STALE_AUDIT_SECONDS = 15 * 60


def _claim(submission_id: str) -> bool:
    """Take the receipt, or discover somebody already has.

    The whole of once-only, in one conditional write: a row that has left
    `queued` cannot be claimed again, so a retried stream record costs nothing
    instead of a second pass over the same photograph.

    Except when the last attempt never finished. A Lambda that times out is
    killed where it stands - no exception is raised, nothing runs, and nothing
    releases the claim. The row is left in `auditing` and stays there: no
    retry can take it because it is no longer `queued`, so the receipt reads
    "Being read..." for ever and nobody is ever told why. That is exactly what
    happened to a 23-line grocery bill the moment the extraction schema grew
    enough to push it past two minutes.

    So a claim older than `STALE_AUDIT_SECONDS` can be taken again. The window
    is what keeps this safe: a slow audit still holds its own claim, and only
    one that has outlived any possible run is reclaimed.
    """
    now = int(time.time())
    try:
        _intake.update_item(
            Key={"submission_id": submission_id},
            UpdateExpression="SET #s = :auditing, audit_started_at = :now",
            ConditionExpression=(
                "#s = :queued"
                " OR (#s = :auditing AND attribute_not_exists(audit_started_at))"
                " OR (#s = :auditing AND audit_started_at < :stale)"),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":auditing": "auditing", ":queued": "queued", ":now": now,
                ":stale": now - STALE_AUDIT_SECONDS},
        )
        return True
    except _intake.meta.client.exceptions.ConditionalCheckFailedException:
        return False


# How many times one receipt may be attempted before it is parked for a person.
#
# Three, because the failures worth retrying are transient - a timeout, a model
# hiccup, a throttle - and they do not recur three times in a row. Anything
# that does is a fault in the code or the data, and retrying it is just paying
# to fail again.
MAX_AUDIT_ATTEMPTS = 3


def _release(submission_id: str, reason: str) -> None:
    """Put a receipt back for another attempt - or stop, having had enough.

    Releasing writes to the table this worker's stream reads, so the release is
    itself a new event: the claim is queued again, picked up again, and fails
    again. On a transient fault that is exactly right and the second attempt
    succeeds. On a repeatable one it is a loop with no exit that spends two
    model calls - one of them a vision call over the full receipt image - on
    every pass.

    That is not a hypothetical. A `NameError` in the write below - code that
    runs *after* the model has been called and paid for - turned one receipt
    into 3,435 failed audits and roughly 6,900 model calls in a day. Lambda's
    own `retry_attempts` cannot help: every cycle is a fresh event rather than
    a retry of the last one.

    So attempts are counted on the row. Past the limit the claim is parked in
    `needs_human`, which this worker will not claim - so the loop ends - and
    which the console already shows as a receipt the agent could not read.
    """
    try:
        row = _intake.update_item(
            Key={"submission_id": submission_id},
            UpdateExpression=("SET #s = :queued, last_error = :e, "
                              "audit_attempts = if_not_exists(audit_attempts, :zero) + :one"),
            ConditionExpression="#s = :auditing",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":queued": "queued", ":auditing": "auditing",
                                       ":e": reason[:300], ":zero": 0, ":one": 1},
            ReturnValues="UPDATED_NEW",
        )
    except Exception:
        logger.exception("could not release %s", submission_id)
        return

    attempts = int((row.get("Attributes") or {}).get("audit_attempts") or 0)
    if attempts < MAX_AUDIT_ATTEMPTS:
        return

    # Enough. Parked where nothing will pick it up again, with the reason kept
    # so somebody can see why rather than finding a receipt that simply stopped.
    try:
        _intake.update_item(
            Key={"submission_id": submission_id},
            UpdateExpression="SET #s = :parked, last_error = :e",
            ConditionExpression="#s = :queued",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":parked": "needs_human", ":queued": "queued",
                ":e": f"gave up after {attempts} attempts: {reason}"[:300]},
        )
        logger.error("%s parked after %d failed audits: %s",
                     submission_id, attempts, reason[:200])
    except Exception:
        logger.exception("could not park %s", submission_id)


def _other_claim(submission_id: str) -> dict[str, Any]:
    """The claim a duplicate points at, for saying something useful about it."""
    if not submission_id:
        return {}
    try:
        return _intake.get_item(Key={"submission_id": submission_id}).get("Item") or {}
    except Exception:
        logger.exception("could not read %s", submission_id)
        return {}


def _reference_of(submission_id: str) -> str:
    return str(_other_claim(submission_id).get("reference") or "")


def taxlike(value: Any) -> str:
    """An invoice number reduced to what identifies it.

    `4000 - 438350` and `4000-438350` are one number; so are `AD2NTG1F-0004`
    and `ad2ntg1f 0004`. Empty stays empty, and empty never equals empty: a
    bill with no number proves nothing about another bill with no number.
    """
    return "".join(ch for ch in str(value or "") if ch.isalnum()).upper()


def _invoice_of(submission_id: str) -> str:
    """The invoice number a claim was read as carrying, normalised."""
    return taxlike((_other_claim(submission_id).get("receipt") or {}).get("invoice_number"))


def _source_ref_of(submission_id: str) -> str:
    """Which delivery a claim arrived on - one email is one `mail://` key.

    Empty for anything that arrived without siblings, and empty must never
    compare equal to empty: two claims with no source are not companions.
    """
    return str(_other_claim(submission_id).get("source_ref") or "")


def _refund_one_credit(org_id: str, submission_id: str, companion_of: str) -> None:
    """Give back the credit a second document of one claim cost.

    One receipt, one credit - and two attachments describing one purchase are
    one receipt. Never raises: the claim is audited and correct by the time
    this runs, and losing that because a counter would not increment is the
    worse trade.
    """
    if not (_orgs and org_id):
        return
    try:
        _orgs.update_item(
            Key={"org_id": org_id},
            UpdateExpression="SET credits = if_not_exists(credits, :z) + :one",
            ExpressionAttributeValues={":z": 0, ":one": 1},
        )
        logger.info("refunded the credit for %s, a second document of %s",
                    submission_id, companion_of)
    except Exception:
        logger.exception("could not refund the credit for %s", submission_id)


def _describe(submission_id: str) -> str:
    """`Mobil80-Exp-1, sent by manoj@mobil80.com` - something to go and find.

    Falls back to the raw id for a claim from before references existed, which
    is not useful but is at least honest: there is no other string for those.
    """
    row = _other_claim(submission_id)
    ref = str(row.get("reference") or "") or submission_id
    who = str(row.get("submitted_by") or "")
    return f"{ref}, sent by {who}" if who else ref


def _org_groups(org_id: str) -> list[dict[str, Any]]:
    """This organisation's groups, for matching a receipt to one.

    Read here rather than carried on the submission: the list is edited while
    claims are in flight, and a copy taken at intake would match against a set
    of groups that no longer exists.
    """
    if not (_orgs and org_id):
        return []
    try:
        org = _orgs.get_item(Key={"org_id": org_id}).get("Item") or {}
    except Exception:
        logger.exception("could not read groups for %s", org_id)
        return []
    return list(org.get("groups") or [])


def _audit_one(row: dict[str, Any]) -> None:
    submission_id = str(row.get("submission_id", ""))
    key = str(row.get("receipt_key", ""))
    org_id = str(row.get("org_id", ""))
    # Which delivery this arrived on. Every attachment of one email carries the
    # same key, which is what tells two documents of one purchase apart from
    # two people claiming the same bill.
    source_ref = str(row.get("source_ref", "") or "")

    if not submission_id:
        return
    if row.get("status") != "queued":
        return
    if not key:
        # An email whose attachment we could not store, or a submission from
        # before originals were kept. There is nothing to read; say so on the
        # row rather than leaving it queued for ever.
        _intake.update_item(
            Key={"submission_id": submission_id},
            UpdateExpression="SET #s = :s, last_error = :e",
            ConditionExpression="#s = :queued",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "no_original", ":queued": "queued",
                ":e": "No stored original to read."},
        )
        logger.info("%s has no original; nothing to audit", submission_id)
        return

    if not _claim(submission_id):
        logger.info("%s is already being audited or is done", submission_id)
        return

    # There is no re-audit branch here any more.
    #
    # A reviewer correcting a claim used to send it back round this worker,
    # which re-ran the whole policy under their type and rewrote the verdict.
    # That is the wrong shape for a claim that has already reached a person:
    # once it is in front of one, they decide it. They read the bill, the
    # findings and the figures, and they approve or reject - nothing in
    # between, and nothing that re-decides it on their behalf between their
    # answer and their decision.
    #
    # `_claim_retype` now writes the type, currency and group and leaves the
    # claim where it is, so nothing arrives here with `answered_expense_type`
    # on it. That also closes the loop this branch lived at the edge of: a
    # write that woke the stream that wrote the row that woke the stream,
    # which cost 576 attempts in twenty minutes the first time it went wrong.
    try:
        obj = _s3.get_object(Bucket=RECEIPTS_BUCKET, Key=key)
        data = obj["Body"].read()
        media = str(row.get("receipt_type") or obj.get("ContentType") or "")

        if len(data) > MAX_AUDIT_BYTES:
            raise ValueError(f"original is {len(data)} bytes")

        # A PDF carries no pixels, so it is rendered to images first. Decided
        # by the bytes rather than the declared type: a phone that names a JPEG
        # `.pdf` should not change how it is read.
        if pdfpages.is_pdf(data, media):
            pages = pdfpages.render(data)
            if not pages:
                _intake.update_item(
                    Key={"submission_id": submission_id},
                    UpdateExpression="SET #s = :s, last_error = :e",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":s": "needs_human",
                        ":e": "This PDF could not be opened. Send a photo instead."},
                )
                logger.info("%s is a PDF that would not render", submission_id)
                return
            receipt_input = {"kind": "images", "pages": pages}
        else:
            receipt_input = {"kind": "image_base64",
                             "data": base64.b64encode(data).decode(),
                             "media_type": media or "image/jpeg"}

        outcome = handler.audit(receipt_input, org_id=org_id,
                                sender_note=str(row.get("sender_note", "") or ""))

        # Now that the bill has been read, ask whether we have seen this
        # *receipt* before - not this file. A re-photographed bill and the
        # hotel's emailed copy of one already sent on WhatsApp are both
        # invisible to a byte hash and are the common real-world case.
        #
        # Flagged, never refused. Two people can buy the same coffee at the
        # same shop for the same price on the same morning, so this is evidence
        # for a human, not a verdict. It blocks automatic settlement, which is
        # the only thing it should do on its own.
        receipt = outcome["receipt"]
        verdict = outcome["verdict"]
        # Two fingerprints, because they catch different things. The sender one
        # first: it is built entirely from facts we hold rather than from a
        # string the model produced, so it is the one that does not quietly
        # stop working when a vendor name comes back slightly different.
        sender_fp = duplicates.submitter_key(
            org_id, row.get("submitted_by", ""), receipt.get("date", ""),
            verdict.get("receipt_total"), verdict.get("currency", ""))
        fingerprint = duplicates.invoice_key(
            org_id, receipt.get("vendor", ""), receipt.get("invoice_number", ""))
        # The same attachment arriving twice by different routes, re-encoded on
        # the way so the content hash at intake could not see it.
        shape_fp = duplicates.file_shape_key(
            org_id, row.get("receipt_name", ""), row.get("receipt_bytes"))

        held = (duplicates.claim(sender_fp, submission_id) if sender_fp else None)
        if not held and fingerprint:
            held = duplicates.claim(fingerprint, submission_id)
        if not held and shape_fp:
            held = duplicates.claim(shape_fp, submission_id)
        # Two documents of one purchase, or two claims?
        #
        # An invoice and its receipt arrive in one email all the time - the
        # vendor sends both, the employee forwards the lot. Each attachment
        # becomes a claim, so one subscription charge became two: one approved,
        # one flagged as a possible duplicate and put in front of a person to
        # confirm what the paper already proves.
        #
        # Arriving in the same message is what settles it. `possible_duplicate`
        # is evidence for a human because two people can buy the same coffee at
        # the same shop for the same price on the same morning - but not in one
        # email, from one sender, carrying one invoice number. There is no
        # judgment left to make, so nobody is asked to make it.
        #
        # The companion is not rejected: nothing about it is wrong. It stays as
        # the second document of the claim it belongs to, out of the queue and
        # out of the payment run, and the credit it cost goes back.
        # Arriving together is necessary and nowhere near sufficient.
        #
        # Ten receipts in one email is an ordinary thing to send, and two of
        # them can honestly be the same amount at the same shop on the same
        # day. That is all `submitter_key` knows - and it is tried first, so
        # asking *which* fingerprint matched would answer "the weak one" even
        # when the invoice numbers agree. Absorbing a second claim on that
        # evidence would destroy a real one and hand back a credit for it with
        # nobody asked.
        #
        # So the documents are compared directly. One invoice number is one
        # document, whichever key happened to catch it: a vendor does not
        # issue two invoices under one number, and two separate purchases do
        # not share one. A bill with no invoice number proves nothing and is
        # left to a person.
        this_invoice = taxlike(receipt.get("invoice_number"))
        same_message = bool(source_ref) and bool(this_invoice) and held and (
            _source_ref_of(held) == source_ref
            and _invoice_of(held) == this_invoice)
        if same_message:
            companion_of = _reference_of(held) or held
            verdict["companion_of"] = companion_of
            logger.info("%s is a second document of %s, from the same message",
                        submission_id, companion_of)
            # Once, not once per audit. A claim can be read again - a
            # reviewer corrects its type, a fault is fixed and it is re-driven
            # - and a refund that fires on every pass mints credits out of a
            # retry. Already being a companion is the record that it was paid
            # back the first time.
            if not str(row.get("companion_of") or ""):
                _refund_one_credit(org_id, submission_id, companion_of)
        elif held:
            # Named the way a person can act on. This quoted the raw
            # `sub_1789477019786_595007`, which is the identifier the whole
            # reference scheme exists because nobody can use: a reviewer told
            # their claim matches one of those has nothing to type into a
            # search box, and reasonably concludes there is no other claim.
            #
            # Who sent it matters as much as which one it is. "The same bill
            # twice" and "two people expensed one invoice" are different
            # problems with different answers, and the second is invisible
            # unless the other person is named.
            other = _describe(held)
            verdict.setdefault("violations", []).insert(0, {
                "code": "possible_duplicate",
                "message": (
                    f"The same vendor, date, total and currency were already "
                    f"claimed on {other}. If this is a second receipt that "
                    f"happens to match, approve it; if it is the same bill "
                    f"twice, reject it."
                ),
                "amount": None,
                "blocks_automatic_decision": True,
            })
            verdict["verdict"] = "needs_review"
            # The reference, not the id: this is what the console prints, and
            # it has to be the string somebody can look up.
            verdict["duplicate_of"] = _reference_of(held) or held
            verdict["reimbursable_total"] = "0.00"
            logger.info("%s looks like a duplicate of %s", submission_id, held)

        # Which cost centre this receipt belongs to, settled by the bill
        # itself where it says so.
        #
        # A tax invoice made out to the company prints the buyer's
        # registration, and often the buyer's name beside it; a group carrying
        # either is a full answer to a question we would otherwise have to put
        # to a person. Only consulted when the question is actually open: a
        # receipt already tagged - because the sender belongs to one group, or
        # because a reviewer set it - is not overruled by what is on the paper.
        #
        # Everything the bill does not settle stays open and goes to a
        # reviewer. See grouping.py for why nothing here guesses.
        group_id, group_status = str(row.get("group_id") or ""), str(row.get("group_status") or "")
        if group_status in ("ask", "unset") and not group_id:
            matched, how = grouping.group_for(_org_groups(org_id), receipt)
            if matched:
                group_id = matched
                group_status = ("assigned_by_tax_id" if how == "tax_id"
                                else "assigned_by_buyer_name")
                logger.info("%s matched group %s on the buyer's %s",
                            submission_id, matched, how)

        # Still nothing, and the submitter belongs to more than one group.
        #
        # This used to message them a list of cost centres to tap. Asking the
        # person who spent the money which budget it comes out of puts an
        # accounting question to somebody who took a photograph of a bill - and
        # it is finance's question, answerable in one click by the reviewer who
        # is going to approve the claim anyway.
        if group_status == "ask" and not group_id:
            verdict.setdefault("violations", []).append({
                "code": "group_not_set",
                "message": ("This submitter belongs to more than one group and "
                            "the bill does not say which. Set it before approving."),
                "amount": None,
                "blocks_automatic_decision": True,
            })
            verdict["verdict"] = "needs_review"

        # Written onto the submission itself, so the console reads one row per
        # receipt rather than joining two tables to show a line.
        _intake.update_item(
            Key={"submission_id": submission_id},
            UpdateExpression=("SET #s = :s, verdict = :v, receipt = :r, "
                              "rationale = :n, currency_resolution = :c, "
                              "audited_at = :t, model = :m, fingerprint = :f, "
                              "budget_value = :bv, sender_fingerprint = :sf, "
                              "payout_value = :pv, companion_of = :co, "
                              "group_id = :g, group_status = :gs REMOVE last_error"),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "audited",
                # The claim this is a second document of, or empty. Read by the
                # console to keep it out of the queue and the payment run.
                ":co": verdict.get("companion_of", ""),
                ":g": group_id,
                ":gs": group_status,
                # What this claim counts as against a budget kept in the
                # organisation's currency, with the rate that produced it.
                # Written once, here, so the figure does not move under
                # somebody every time the rupee does. Empty when the
                # currencies already agree or no rate could be had.
                ":bv": json.loads(json.dumps(outcome.get("budget") or {}),
                                  parse_float=Decimal),
                # What this claim pays out as, in the organisation's own
                # currency, with the rate that produced it and when that rate
                # was taken. Stamped once so an amount somebody is owed does
                # not move between the day it was approved and the day it is
                # paid.
                ":pv": json.loads(json.dumps(outcome.get("payout") or {}),
                                  parse_float=Decimal),
                # Round-tripped through JSON with every float turned into a
                # Decimal on the way back. The round trip alone was not enough:
                # DynamoDB refuses a float, `json` happily preserves one, and
                # the model returns one whenever it answers a quantity or an
                # amount as a number rather than a string. The write then threw
                # `Float types are not supported`, the receipt was left in
                # `auditing`, and the sender watched "Being read…" for ever
                # while their credit had already been spent.
                ":v": json.loads(json.dumps(verdict), parse_float=Decimal),
                ":r": json.loads(json.dumps(receipt), parse_float=Decimal),
                ":f": fingerprint,
                ":sf": sender_fp,
                ":n": outcome["rationale"],
                ":c": json.loads(json.dumps(outcome["currency_resolution"]), parse_float=Decimal),
                ":t": int(time.time()),
                ":m": outcome["model"],
            },
        )
        logger.info("%s audited: %s", submission_id, outcome["verdict"]["verdict"])
        # With the receipt that was just read, not the one on the row. `row`
        # is the stream's image of the claim *before* this audit, so it has no
        # receipt at all - and the first message a submitter ever gets about a
        # claim was therefore addressed to "your receipt" rather than naming
        # the vendor printed on it.
        if verdict.get("companion_of"):
            logger.info("%s is a second document of %s; its outcome was already sent",
                        submission_id, verdict["companion_of"])
        else:
            _tell_sender({**row, "receipt": receipt}, verdict)
    except Exception as exc:
        logger.exception("audit of %s failed", submission_id)
        _release(submission_id, f"{type(exc).__name__}: {exc}")
        raise


# Only where the person is waiting on an answer in a thread they opened. A
# portal upload is watched on screen, and an API submission answers the caller
# over HTTP; messaging those would be unsolicited.
TELL_THE_SENDER = ("whatsapp", "email")


def _tell_sender(row: dict[str, Any], verdict: dict[str, Any]) -> None:
    """Keep the promise the acknowledgement made.

    "I check it against your expense policy and come back here with the
    outcome, usually within a minute." Nothing kept it: the verdict was
    computed, written to the row and shown in the console, and the person who
    photographed the bill heard nothing further, ever.

    Never allowed to fail the audit. The claim is already decided and recorded
    by the time this runs - losing the verdict because a message bounced would
    be the far worse outcome.
    """
    try:
        if str(row.get("channel", "")) not in TELL_THE_SENDER:
            return

        # Once per claim, whatever the stream does.
        #
        # A failed audit leaves its stream record to be retried, and a record
        # can be delivered more than once by design - so a claim that crashed
        # and was then fixed had two attempts succeed in a row and sent two
        # outcome messages a minute apart, saying the same thing about the same
        # receipt. The decision is idempotent; the message was not.
        #
        # Keyed off the notice already recorded against the claim, which is the
        # same record the console reads - so there is one answer to "has this
        # person been told", not two that can disagree.
        told = (row.get("last_notice") or {})
        if str(told.get("kind") or "") == "outcome":
            logger.info("%s: the sender has already been told the outcome",
                        row.get("submission_id"))
            return

        member = identity.resolve_by_email(str(row.get("submitted_by", "")), channel=None)
        if not member:
            return

        # One message with one of two things in it: the agent cleared this and
        # finance will pay it, or a person is going to look at it. Nothing is
        # ever asked of the sender - see notify.outcome_notice.
        result = notify.send("outcome", member, {
            "vendor": (row.get("receipt") or {}).get("vendor", ""),
            # The first message about this claim, so the first chance to give
            # them the number they will quote if they ever ask about it.
            "claim_ref": str(row.get("reference", "")),
            "currency": verdict.get("currency", ""),
            "total": verdict.get("receipt_total"),
            "reimbursable": verdict.get("reimbursable_total"),
            "verdict": verdict.get("verdict", ""),
            # Not for the notice to recite - which finding blocked a claim is
            # a reviewer's business, not the claimant's. `nothing_read` is the
            # one exception, because it is a fact about their photograph and
            # theirs to fix. See `notify.outcome_notice`.
            "violations": verdict.get("violations") or [],
        })
        # Kept against the claim, so the console can show a reviewer the words
        # that actually went rather than the model's audit rationale, which is
        # written for the verdict and sent nowhere.
        notify.record(_intake, str(row.get("submission_id") or ""), result)

    except Exception:
        logger.exception("could not tell %s the outcome", row.get("submission_id"))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    done, failed = 0, 0
    for record in event.get("Records", []):
        if record.get("eventName") not in ("INSERT", "MODIFY"):
            continue
        row = _plain((record.get("dynamodb") or {}).get("NewImage"))
        try:
            _audit_one(row)
            done += 1
        except Exception:
            # Already logged and released. Counted so the batch reports it.
            failed += 1
    return {"audited": done, "failed": failed}
