"""Turn an inbound email into a receipt submission.

SES writes the raw message to S3; this runs on the resulting ObjectCreated
event, pulls out the sender and any attached receipts, and hands them to the
same intake gate every other channel goes through.

Going via S3 rather than SES's direct Lambda action is deliberate: the direct
action caps the message it can pass, and receipts are photographs. The raw
message also stays in the bucket, which is what you want when someone asks why
a claim was or was not created.

The sender is taken from the envelope/header address only. Anything the body
claims about who sent it is ignored - a body is attacker-controlled text, and
letting it name an employee would let anyone spend another organisation's
credits by writing an address into an email.
"""
from __future__ import annotations

import email
import json
import logging
import re
import os
from email.message import EmailMessage, Message
from email.utils import parseaddr
from typing import Any

import boto3

import identity
import intake
import receipts

logger = logging.getLogger()
logger.setLevel(logging.INFO)

INTAKE_ADDRESS = os.environ.get("INTAKE_ADDRESS", "receipts@expenze.ai").lower()
SES_REGION = os.environ.get("SES_REGION", "us-east-1")

_s3 = boto3.client("s3")
_ses = boto3.client("sesv2", region_name=SES_REGION)


def _sender(msg: Message) -> str:
    """The address the mail actually came from, header only."""
    # `From` first, then Return-Path. Return-Path carries the SMTP envelope
    # sender, which relays and forwarders routinely rewrite - anything sent
    # through SES arrives with a bounce address like
    # 0100...@mail.expenze.ai there, which identifies nobody. `From` is the
    # address the employee actually sent from and the one finance would
    # recognise. Spoofing it is the risk, and the answer to that is DMARC
    # enforcement on inbound plus SES's spam/virus verdicts, not preferring a
    # header that names the wrong party.
    for header in ("From", "Return-Path"):
        raw = msg.get(header, "")
        addr = parseaddr(raw)[1].strip().lower()
        if addr:
            return addr
    return ""


def _attachments(msg: Message) -> list[tuple[str, str, bytes]]:
    """(filename, content_type, payload) for every part that looks like a receipt."""
    found = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = receipts.normalise_type(part.get_content_type() or "")
        if not receipts.is_supported(ctype):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            continue
        if not payload or len(payload) > receipts.MAX_BYTES:
            logger.info("skipping %s: empty or over size limit", ctype)
            continue
        found.append((receipts.safe_name(part.get_filename() or "", ctype), ctype, payload))
    return found


# Long enough for "Dinner for 4 with the Wipro team, client meeting", short
# enough that a forwarded thread with six replies underneath does not become
# the prompt. Anything past this is quoted history, not a note to us.
NOTE_LIMIT = 600

# Where the sender's own words stop and the thread beneath them begins. Checked
# against the joined text rather than line by line, because Gmail wraps its
# attribution - "On Fri, 11 Sep at 6:01 PM Madhu <m@x.com>\nwrote:" - and a
# line-wise test for "wrote:" misses it, which is how a whole quoted thread
# ended up in the first real note this saw.
QUOTE_CUTS = [
    re.compile(r"-{2,}\s*Original Message\s*-{2,}", re.I),
    re.compile(r"-{2,}\s*Forwarded message\s*-{2,}", re.I),
    re.compile(r"Begin forwarded message:", re.I),
    re.compile(r"\bOn\b.{5,200}?\bwrote:", re.S),
    re.compile(r"(?m)^\s*From:\s", re.I),
    re.compile(r"(?m)^\s*Sent from my\b", re.I),
    re.compile(r"_{5,}"),
]

# Gmail writes this where an inline image sits. It is a placeholder, not
# something the sender typed, and the filename is noise in a prompt.
INLINE_IMAGE = re.compile(r"\[image:[^\]]*\]", re.I)


def _body_text(msg: Message) -> str:
    """What the sender actually typed, as distinct from what they forwarded.

    Read for context the bill cannot give: "client dinner, Wipro" tells the
    model what kind of expense this is where the receipt alone is ambiguous.

    Plain text only. An HTML-only mail yields nothing rather than being
    tag-scraped: a half-parsed body is a worse input than an absent one.
    """
    for part in msg.walk():
        if part.is_multipart() or part.get_content_type() != "text/plain":
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            text = raw.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue

        text = INLINE_IMAGE.sub(" ", text)
        # Quoted lines go first: a ">" line can otherwise sit above the marker
        # that would have cut the rest away.
        text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))

        cut = len(text)
        for marker in QUOTE_CUTS:
            found = marker.search(text)
            if found:
                cut = min(cut, found.start())
        note = " ".join(text[:cut].split())
        if note:
            return note[:NOTE_LIMIT]
    return ""


def _is_automated(msg: Message) -> bool:
    """Whether replying to this would start a loop.

    Our own acknowledgement carries `Auto-Submitted: auto-replied`. So does a
    holiday responder. Two systems each politely answering the other is a mail
    loop that runs until somebody notices the bill, and the standing convention
    for avoiding it is simply not to auto-reply to anything already marked as
    automatic.
    """
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return True
    if (msg.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk", "auto_reply"):
        return True
    return bool(msg.get("X-Auto-Response-Suppress") or msg.get("List-Id"))


def _acknowledge(msg: Message, sender: str, outcomes: list[dict[str, Any]]) -> None:
    """Tell the sender their receipt arrived, on the thread they sent it on.

    The same reasoning as the WhatsApp acknowledgement: someone who emails a
    photograph into an address and hears nothing cannot tell whether it worked,
    and writes to finance instead. A claim reference in their own mail thread
    is the cheapest possible answer to that.
    """
    if _is_automated(msg):
        logger.info("inbound mail is itself automated; not acknowledging")
        return

    queued = [o for o in outcomes if o.get("status") == "queued"]
    broke = [o for o in outcomes if o.get("status") == "no_credits"]
    # The same bill twice. Answered explicitly, because the branch it used to
    # fall into told the sender "no receipt attached - send it again", which is
    # both untrue and an instruction to do the one thing that repeats the
    # problem.
    again = [o for o in outcomes if o.get("status") == "duplicate"]

    if queued:
        lines = [f"{'Receipts' if len(queued) > 1 else 'Receipt'} received.", ""]
        # The claim's own reference, the one every other message about it uses.
        # This printed `EXP-2460070` - the tail of the internal submission id -
        # while the console, the outcome message and the invoice all said
        # `Mobil80-Exp-4`. Somebody quoting the number we showed them first was
        # quoting one that exists nowhere else, and a reply carrying it back
        # could only be matched because the inbound parser had been taught this
        # second shape specially.
        for o in queued:
            reference = str(o.get("reference") or "")
            name = o.get("filename") or "attachment"
            lines.append(f"  {reference}   {name}".rstrip() if reference
                         else f"  {name}")
        org = queued[0].get("org_name") or ""
        if org:
            lines += ["", f"Claimed against {org}."]
        if queued[0].get("group_status") == "unset":
            lines.append("Not yet tagged to a group - your finance team will assign it.")
        elif queued[0].get("group_label"):
            lines.append(f"Group: {queued[0]['group_label']}")
        lines += [
            "",
            "Each one is being read and checked against your expense policy. "
            "The outcome follows in a separate message.",
        ]
        subject_line = f"Received - {queued[0].get('filename') or 'your receipt'}"
    elif broke:
        lines = [
            "Your receipt could not be processed: your organisation is out of "
            "Expenze credits.",
            "",
            "Nothing was charged and nothing was lost. Ask your finance team to "
            "top up, then send the receipt again.",
        ]
        subject_line = "Not processed - out of credits"
    elif again:
        refs = [str(o.get("reference") or "") for o in again]
        named = ", ".join(r for r in refs if r)
        lines = [
            "This receipt has already been submitted"
            + (f" as {named}." if named else "."),
            "",
            "No second claim was created. The outcome of the original follows "
            "separately, if it has not already.",
        ]
        subject_line = "Already received" + (f" - {named}" if named else "")
    else:
        lines = [
            "We received your email, but there was no receipt attached that we "
            "could read.",
            "",
            "Attach the receipt as a photo (JPEG, PNG, WebP or HEIC) or a PDF, "
            "under 10MB, and send it again. An image pasted into the body of a "
            "message often does not arrive as an attachment.",
        ]
        subject_line = "No receipt attached"

    # Through the shared sender, so every automatic reply from this file
    # threads identically. Two copies of the In-Reply-To/References handling
    # is two chances for one of them to stop threading, and a reply that opens
    # a new thread is exactly the problem the reference matching exists to
    # work around.
    _reply_on_thread(msg, sender, subject_line, lines)
    logger.info("acknowledged %d receipt(s) to the sender", len(queued))


def _reply_on_thread(msg: Message, sender: str, subject_line: str,
                     lines: list[str]) -> None:
    """Send one message back on the sender's own thread. Never raises.

    The shared half of every automatic reply here. Threading matters more than
    it looks: a person who asked a question and got an answer in a *new* mail
    thread has to work out which of their claims it refers to, which is the
    exact problem the rest of this change exists to remove.
    """
    if _is_automated(msg):
        logger.info("inbound mail is itself automated; not replying")
        return

    # Everything, not just the send. "Never raises" was only true of the SES
    # call: building the message could throw on a header the sender's client
    # wrote, and did - taking down a handler that had already created and
    # charged for the claims. A courtesy message must never be able to do that.
    try:
        _compose_and_send(msg, sender, subject_line, lines)
    except Exception:
        logger.exception("could not reply to the inbound mail")


def _compose_and_send(msg: Message, sender: str, subject_line: str,
                      lines: list[str]) -> None:
    note = EmailMessage()
    note["From"] = f"Expenze Receipts <{INTAKE_ADDRESS}>"
    note["To"] = sender
    note["Reply-To"] = INTAKE_ADDRESS
    original = " ".join((msg.get("Subject") or "").split())
    note["Subject"] = (f"Re: {original}"[:200] if original else subject_line)
    if msg.get("Message-ID"):
        # Unfolded before they are set. A References header on a thread with a
        # few replies in it is folded across lines by the sending client, and
        # Python refuses outright to set a header containing a newline:
        #
        #   ValueError: Header values may not contain linefeed or carriage
        #   return characters
        #
        # That killed the whole acknowledgement - and, because it raised out of
        # the handler, the inbound message was left looking unprocessed. The
        # claims had already been created by then, so a retry would have made
        # a second set of them.
        note["In-Reply-To"] = " ".join(str(msg["Message-ID"]).split())[:900]
        note["References"] = " ".join(
            " ".join(filter(None, [msg.get("References"), msg["Message-ID"]])).split()
        )[:900]
    note["Auto-Submitted"] = "auto-replied"
    note.set_content("\n".join(lines + ["", "Expenze - expenze.ai"]))

    _ses.send_email(
        FromEmailAddress=f"Expenze Receipts <{INTAKE_ADDRESS}>",
        Destination={"ToAddresses": [sender]},
        Content={"Raw": {"Data": note.as_bytes()}},
    )
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    results = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        try:
            raw = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            msg = email.message_from_bytes(raw)
        except Exception:
            logger.exception("could not read %s/%s", bucket, key)
            continue

        sender = _sender(msg)
        subject = (msg.get("Subject") or "")[:200]

        if not sender:
            logger.info("no usable sender on %s; ignoring", key)
            results.append({"key": key, "status": "ignored"})
            continue

        # Resolved once, before anything is charged, purely to decide whether
        # this sender may be spoken to at all. The address is public: an
        # informative reply to a stranger confirms the service is live and
        # reading their mail, which is what somebody probing it wants.
        known = identity.resolve_sender(email=sender, mobile="", staff_id="",
                                        channel="email")
        if not known:
            logger.info("unresolved email sender; ignoring without reply")
            results.append({"key": key, "status": "ignored"})
            continue

        body_note = _body_text(msg)
        attachments = _attachments(msg)

        if not attachments:
            # A known sender who attached nothing is a person to help. Telling
            # them costs nothing and saves the message that otherwise arrives a
            # day later asking why nothing happened.
            logger.info("no receipt attachment from a known sender")
            _acknowledge(msg, sender, [])
            results.append({"key": key, "status": "no_attachment"})
            continue

        outcomes = []
        for name, ctype, payload in attachments:
            stored = receipts.put(payload, ctype, name) or {}

            # Straight through the same gate as WhatsApp and the API. Unknown
            # senders are dropped there, silently, before anything is charged.
            response = intake.lambda_handler(
                {
                    "path": "/intake/email",
                    "body": json.dumps({
                        "from_email": sender,
                        "subject": subject,
                        # The subject carries the note as often as the body
                        # does - "Fwd: lunch, 6 of us" - so both are offered
                        # and neither is trusted for anything but context.
                        "sender_note": " ".join(x for x in (subject, body_note) if x)[:NOTE_LIMIT],
                        "source_ref": f"mail://{key}",
                        **stored,
                    }),
                },
                context,
            )
            outcome = json.loads(response.get("body") or "{}")
            status = outcome.get("status")

            # The address is public, so most of what arrives is from nobody we
            # know. Their attachment is stored before we find that out - it has
            # to be, the gate runs on the sender - so it goes straight back out
            # again the moment the gate says no.
            if status != "queued":
                receipts.discard(stored.get("receipt_key", ""))

            logger.info("%s -> %s", sender, status)
            outcome["filename"] = stored.get("receipt_name", name)
            outcomes.append(outcome)
            results.append({"key": stored.get("receipt_key", ""), "status": status})

        # One acknowledgement for the message, not one per attachment: three
        # receipts in an email is one thing the sender did, and three near
        # identical replies to it reads as a fault.
        _acknowledge(msg, sender, outcomes)

    return {"processed": len(results), "results": results}
