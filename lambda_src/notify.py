"""Telling the employee what happened to their claim.

Two moments matter to the person who spent the money, and neither of them is
the moment the agent formed an opinion:

* **Reimbursed.** How much, against which receipt, by what route, and with the
  reference they will see on their bank statement. Without the reference the
  notice is unreconcilable and they will ask finance anyway.

* **Rejected.** Why, in words, and who decided. A rejection with no reason is
  the single most corrosive thing an expense system can send: the employee is
  out of pocket, cannot tell whether it was a mistake, and has nothing to act
  on. So the reason is mandatory here - the API refuses a rejection without
  one rather than sending an empty explanation.

Both go by email and, where the person has verified a number themselves, by
WhatsApp. Email always: it is the channel they were invited on and the one that
survives someone changing phones. WhatsApp only when verified, because a number
somebody else typed in is not proof of anything - the same rule the receipt
intake applies in reverse.

Neither notice invents a figure. Everything quoted is passed in from the
settlement or rejection that was actually recorded.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any

import boto3

import wacfg

logger = logging.getLogger()

SES_REGION = os.environ.get("SES_REGION", "us-east-1")
SENDER = os.environ.get("OTP_SENDER", "noreply@expenze.ai")
INTAKE_ADDRESS = os.environ.get("INTAKE_ADDRESS", "receipts@expenze.ai")

# Mail clients show the display name, not the address, so a bare
# `noreply@expenze.ai` arrives from "noreply" - sitting in an inbox next to
# "OpenAI" and "Axis Bank Cards" looking like something that got past a filter.
# The address is unchanged; only what the reader sees is.
def _from(address: str, label: str = "Expenze") -> str:
    return address if "<" in address else f"{label} <{address}>"

WA_SECRET_ARN = os.environ.get("WA_SECRET_ARN", "")
GRAPH = "https://graph.facebook.com/v21.0"

_ses = boto3.client("sesv2", region_name=SES_REGION)
_secrets = boto3.client("secretsmanager")

MAX_REASON = 600


def money(amount: Any, currency: str) -> str:
    """`INR 3,631.00`. Codes, not symbols.

    A rupee sign that arrives as a box in someone's mail client is worse than
    useless on a figure they are meant to reconcile against a bank statement.
    """
    try:
        value = Decimal(str(amount or 0))
    except (InvalidOperation, TypeError):
        return f"{currency} {amount}"
    return f"{(currency or '').upper()} {value:,.2f}".strip()


# ---------------------------------------------------------------------------
# What the notices say
# ---------------------------------------------------------------------------


def settled_notice(claim: dict[str, Any]) -> dict[str, str]:
    """Composed from the settlement that was recorded, never recomputed."""
    vendor = str(claim.get("vendor") or "your claim")
    ccy = str(claim.get("currency") or "")
    paid = money(claim.get("paid"), ccy)
    approved = money(claim.get("approved"), ccy)
    outstanding = claim.get("outstanding")
    part = outstanding not in (None, "", 0, "0", "0.00") and Decimal(str(outstanding or 0)) > 0

    claim_ref = str(claim.get("claim_ref") or "").strip()
    tail = f" ({claim_ref})" if claim_ref else ""

    # Set against the float they already hold, rather than paid to them.
    #
    # No money moved: they spent the company's cash and this is the company
    # accounting for it. Telling them it "has been reimbursed" would have
    # them waiting for a transfer that is never coming, and quoting a mode of
    # payment and a bank reference for a transfer nobody made is worse - it
    # invites them to go looking for it on a statement.
    from_float = str(claim.get("source") or "") == "float"

    if from_float:
        subject = f"{paid} set against your float — {vendor}{tail}"
        lines = [
            f"{'Part of your' if part else 'Your'} expense claim for {vendor} "
            "has been set against the cash advance you hold.",
            "",
            "No payment has been made to you - you spent this from the float, "
            "and your advance has been reduced by it.",
            "",
            f"Set against:  {paid}",
            f"Approved:     {approved}",
        ]
    else:
        subject = (f"{paid} reimbursed — {vendor}{tail}" if not part
                   else f"{paid} part payment — {vendor}{tail}")
        lines = [
            f"{'Part of your' if part else 'Your'} expense claim for {vendor} has been reimbursed.",
            "",
            f"Paid:        {paid}",
            f"Approved:    {approved}",
        ]
    if part:
        lines.append(f"Still owed:  {money(outstanding, ccy)}")
    # No mode and no bank reference on a float settlement: there was no
    # transfer, so both would send somebody looking for one.
    fields = ((("Accounted on:", "paid_on"),) if from_float
              else (("Mode:", "mode"), ("Bank reference:", "reference"),
                    ("Paid on:", "paid_on")))
    for label, key in fields + (("Settled by:", "settled_by"),
                                ("Group:", "group"), ("Claim:", "claim_ref")):
        value = str(claim.get(key) or "").strip()
        if value:
            lines.append(f"{label:<12} {value}")
    note = str(claim.get("note") or "").strip()
    if note:
        lines += ["", f"Note: {note}"]
    lines += ["", "Reply to your finance team if anything here does not match your records.",
              "", "Expenze - expenze.ai"]

    ref = _ref_line(claim)
    short = ((f"{ref}. " if ref else "")
             + (f"{paid} for {vendor} has been set against your cash advance - "
                "no payment is coming to you, your float is reduced by it."
                if from_float
                else f"{paid} has been reimbursed for {vendor}."
                     + (f" {money(outstanding, ccy)} of this claim is still owed."
                        if part else "")
                     + (f" Reference: {claim['reference']}."
                        if claim.get("reference") else "")))

    return {"subject": subject, "text": "\n".join(lines), "whatsapp": short}


def rejected_notice(claim: dict[str, Any]) -> dict[str, str]:
    """A rejection is only useful with the reason attached."""
    vendor = str(claim.get("vendor") or "your claim")
    ccy = str(claim.get("currency") or "")
    amount = money(claim.get("approved"), ccy)
    reason = str(claim.get("reason") or "").strip()[:MAX_REASON]
    by = str(claim.get("rejected_by") or "your finance team").strip()

    claim_ref = str(claim.get("claim_ref") or "").strip()
    subject = (f"Expense claim not reimbursed — {claim_ref}" if claim_ref
               else f"Expense claim not reimbursed — {vendor} {amount}")
    lines = [
        f"Your expense claim for {vendor} ({amount}) will not be reimbursed.",
        "",
        "Reason given:",
        reason,
        "",
        f"Decided by:  {by}",
    ]
    if claim_ref:
        lines.append(f"Claim:       {claim_ref}")
    when = str(claim.get("rejected_on") or "").strip()
    if when:
        lines.append(f"On:          {when}")
    lines += [
        "",
        "If this looks wrong, reply to your finance team - a rejection can be "
        "reversed. Sending the receipt again on its own will not change the "
        "outcome unless something about the claim changes.",
        "",
        "Expenze - expenze.ai",
    ]

    short = ((f"{_ref_line(claim)}. " if claim_ref else "")
             + f"Your claim for {vendor} ({amount}) was not reimbursed. "
             f"Reason: {reason} — decided by {by}. "
             "Reply to your finance team if this looks wrong.")

    return {"subject": subject, "text": "\n".join(lines), "whatsapp": short}


def disputed_notice(claim: dict[str, Any]) -> dict[str, str]:
    """An approval the agent gave, taken back for a human to look at.

    This person has already been told they are owed money and to expect it
    from finance. Saying nothing while the claim goes quietly back in the
    queue means they stop chasing a payment that is no longer coming.

    Not a rejection and not a question: nothing is being asked of them and
    nothing has been decided against them. Both of those would send them
    looking for something to do about it.
    """
    vendor = str(claim.get("vendor") or "your claim")
    ccy = str(claim.get("currency") or "")
    amount = money(claim.get("approved"), ccy)
    reason = str(claim.get("reason") or "").strip()[:MAX_REASON]
    by = str(claim.get("disputed_by") or "your finance team").strip()

    subject = f"Your claim is being reviewed again — {vendor} {amount}"
    lines = [
        f"Your expense claim for {vendor} ({amount}) was cleared automatically, "
        "and it is now going to a person for a second look. It has not been "
        "rejected, and it is not waiting on you.",
        "",
        "What finance noted:",
        reason,
        "",
        f"Sent back by: {by}",
    ]
    ref = _ref_line(claim)
    if ref:
        lines.append(ref)
    lines += [
        "",
        "You do not need to do anything or send the receipt again. You will "
        "hear the outcome here once it has been decided.",
        "",
        "Expenze - expenze.ai",
    ]
    short = ((f"{ref}. " if ref else "")
             + f"Your claim for {vendor} ({amount}) is going to a person for a "
             f"second look — sent back by {by}: {reason}. Nothing is needed "
             "from you; I'll tell you the outcome.")
    return {"subject": subject, "text": "\n".join(lines), "whatsapp": short}


def approved_notice(claim: dict[str, Any]) -> dict[str, str]:
    """A person approved a claim the agent would not clear on its own.

    The gap this closes: a claim that goes to review is told "sent to your
    finance team, nothing is needed from you" and then says nothing at all
    until it is paid. That silence can run for days, and it is the one stretch
    where the claimant has been told to expect something with no sign that
    anything moved. Approval is also the moment the answer stops being in
    doubt - up to here the claim could still have been rejected.

    Named, because a person decided it. The agent's own approvals say "by the
    agent" for the same reason: who decided is part of what happened, and a
    claimant who needs to ask about the money should know whose desk it left.

    Not a payment, and careful not to read like one. `PENDING` does that work
    here exactly as it does on an automatic approval - same sentence, because
    it is the same fact about the same next step.
    """
    vendor = str(claim.get("vendor") or "your claim")
    ccy = str(claim.get("currency") or "")
    amount = money(claim.get("approved"), ccy)
    by = str(claim.get("approved_by") or "your finance team").strip()

    lines = [
        f"Your expense claim for {vendor} ({amount}) has been approved by {by}.",
        "",
        PENDING,
    ]
    ref = _ref_line(claim)
    if ref:
        lines += ["", ref]
    lines += ["", "Expenze - expenze.ai"]

    short = ((f"{ref}. " if ref else "")
             + f"Your claim for {vendor} ({amount}) was approved by {by}. "
             + PENDING)
    return {"subject": f"Approved — {vendor} {amount}",
            "text": "\n".join(lines), "whatsapp": short}


# Approval is a policy decision; settlement is a payment, made by a different
# person at a different time. "Approved" alone reads as "paid" - and somebody
# who believes the money is on its way does not chase it, then discovers weeks
# later that nobody ever released it.
PENDING = ("Pending settlement — your finance team reimburses it from here, "
           "and I'll message you when it is paid.")


def _ref_line(claim: dict[str, Any]) -> str:
    """`Claim: Mobil80-Exp-41`, or nothing for a receipt that predates them."""
    ref = str(claim.get("claim_ref") or "").strip()
    return f"Claim: {ref}" if ref else ""


def outcome_notice(claim: dict[str, Any]) -> dict[str, str]:
    """What happened to a receipt: one of two things, told to whoever sent it.

    The acknowledgement promises this - "I check it against your expense policy
    and come back here with the outcome" - and there are now exactly two
    outcomes it can report.

    **Cleared.** Nothing was in doubt, the claim is worth what the receipt says,
    and it is with finance to pay.

    **With a person.** Something needs a human decision. Which thing is
    deliberately not spelled out here: the reasons are a reviewer's business -
    over a cap, a type nothing covers, a possible duplicate - and telling a
    claimant "your bill is 205 over the meals cap" invites them to argue a case
    to the wrong audience, or to feel accused when the answer is usually yes.

    Nothing is ever asked. This used to carry a question when the claim was
    blocked on a fact only the claimant held; there are no such facts now.
    """
    vendor = str(claim.get("vendor") or "your receipt")
    ccy = str(claim.get("currency") or "")
    total = money(claim.get("total"), ccy)
    verdict = str(claim.get("verdict") or "")

    # The one case where the amount must not be quoted.
    #
    # A blurred photograph of a handwritten bill came back with no lines and
    # no printed total, so the claim was worth zero - and this message duly
    # told the person who sent it "*D. Velusamy* — INR 0.00. Sent to your
    # finance team to look at." Quoting a figure we did not read as though we
    # had read it is the one thing a receipt-reading product must never do,
    # and INR 0.00 is not a figure any receipt carries.
    #
    # It is also the one blocked case worth naming to them. Everything else
    # that stops a claim is a reviewer's business - a cap, a duplicate, a type
    # nothing covers - and telling a claimant their bill is over the meals cap
    # invites them to argue to the wrong audience. This is not that: it is a
    # fact about their photograph, it is theirs to fix, and a better one sent
    # now saves the reviewer squinting at the same image.
    nothing_read = any(
        str((v or {}).get("code") or "") == "nothing_read"
        for v in (claim.get("violations") or []))

    if verdict == "approved":
        head = (f"*{vendor}* — {total}\n\n"
                f"✅ Approved by the agent. {total} is going to your finance team "
                "for payment.")
        tail = PENDING
    elif nothing_read:
        head = (f"*{vendor}*\n\n"
                "We could not read this one — the photograph is too blurred or "
                "dark to make out the amounts. It has gone to your finance team "
                "anyway, so you do not need to do anything; a clearer photo of "
                "the same bill would help them.")
        tail = ""
    else:
        head = (f"*{vendor}* — {total}\n\n"
                "Sent to your finance team to look at. Nothing is needed from you; "
                "you will hear when it is decided.")
        tail = ""

    lines = [head]
    if tail:
        lines += ["", tail]
    # The first message about this claim, so the first chance to hand them the
    # number they would quote if they ever asked about it.
    ref = _ref_line(claim)
    if ref:
        lines += ["", ref]
    lines += ["", "Expenze"]
    text = "\n".join(lines)
    state = "approved" if verdict == "approved" else "with your finance team"
    return {"subject": f"{vendor} {total} — {state}",
            "text": text, "whatsapp": text}

def low_credits_notice(claim: dict[str, Any]) -> dict[str, str]:
    """Tell the people who can top up, before it stops mattering that they can.

    Running out is not a loud failure: receipts keep arriving and queue
    unaudited, the sender is told nothing was processed, and the first anybody
    notices is a colleague asking why their claim vanished. This is the message
    that arrives while it is still a purchase rather than a backlog.
    """
    left = claim.get("balance")
    org = str(claim.get("org_name") or "your organisation")
    days = claim.get("days_left")

    when = f" — about {days} days at the rate so far" if days else ""
    headline = (f"*{org}* is out of Expenze credits." if not left
                else f"*{org}* has {left} Expenze credits left{when}.")
    consequence = ("Receipts are still arriving and are being queued, but "
                   "nothing is audited until you top up. Nobody's claim is "
                   "lost; they are simply waiting.") if not left else (
                   "Once they run out, receipts are queued but not audited "
                   "until you top up.")

    lines = [headline, "", consequence, "",
             "Top up under Credits in the console.", "", "Expenze"]
    text = "\n".join(lines)
    return {"subject": (f"{org}: out of Expenze credits" if not left
                        else f"{org}: {left} Expenze credits left"),
            "text": text, "whatsapp": text}


def submissions_digest(claims: list[dict[str, Any]], org_name: str) -> dict[str, str]:
    """What arrived since the last time finance was told.

    One message for a window rather than one per receipt. A team that sends
    forty bills on a Friday would otherwise send forty emails, and the fortieth
    is read by nobody - which makes the first thirty-nine worthless too, since
    the habit it teaches is to filter the lot.

    The state of each claim is in the line, because "a receipt arrived" is not
    by itself something finance can act on. What they need to know is which of
    them are sitting in the queue waiting for a person.
    """
    n = len(claims)
    waiting = [c for c in claims if c.get("needs_review")]
    subject = (f"{n} new expense claim{'' if n == 1 else 's'}"
               + (f" · {len(waiting)} to review" if waiting else ""))

    lines = [f"{n} receipt{'' if n == 1 else 's'} came in to {org_name}.", ""]
    for c in claims:
        ref = c.get("reference") or c.get("submission_id", "")
        money = f"{c.get('currency', '')} {c.get('total', '')}".strip()
        state = "needs review" if c.get("needs_review") else (c.get("state") or "")
        lines.append(" · ".join(p for p in (
            ref, c.get("who", ""), c.get("vendor", ""), money, state) if p))
    lines += ["", "Open Expenze to review them: https://expenze.ai"]
    return {"subject": subject, "text": "\n".join(lines)}


# No "queried". Nothing in the product asks a submitter anything any more:
# what the agent cannot settle goes to a reviewer, and what a reviewer cannot
# settle they decide. A claimant hears twice - when it is cleared, and when it
# is paid or refused.
NOTICES = {"settled": settled_notice, "rejected": rejected_notice,
           "outcome": outcome_notice, "approved": approved_notice,
           "low_credits": low_credits_notice, "disputed": disputed_notice}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _wa() -> dict[str, str]:
    """The identity notices are sent from - see wacfg.py for why it expires."""
    return wacfg.config(WA_SECRET_ARN)


def _send_email(to: str, subject: str, text: str) -> str:
    """Send one notice. Returns SES's message id, or "" if it did not go.

    The id rather than a bare success flag, because a question asked by email
    has to be findable again when the answer comes back: SES puts this id
    inside the `Message-ID` header it generates, so the reply's `In-Reply-To`
    carries it back to us. Callers that only care whether it went can still
    treat the result as a boolean - an id is truthy and "" is not.
    """
    try:
        sent = _ses.send_email(
            FromEmailAddress=_from(SENDER),
            # Answers come back to the intake mailbox, which reads them. The
            # sending address is a no-reply, so without this a reply to "just
            # reply with the number" goes nowhere at all.
            ReplyToAddresses=[INTAKE_ADDRESS],
            Destination={"ToAddresses": [to]},
            Content={"Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": text, "Charset": "UTF-8"}},
            }},
        )
        return str(sent.get("MessageId") or "")
    except Exception:
        logger.exception("could not email the claim outcome")
        return ""


# The approved template behind each notice. A claim outcome is business
# initiated - the employee has usually not messaged the number in the last 24
# hours, and outside that window WhatsApp delivers templates and nothing else.
# Free-form text is accepted by Meta with a message id and then silently never
# arrives, which is the worst possible failure for "you have been paid".
#
# Names live in the secret so a dedicated Expenze number with its own approved
# templates is a secret update, like the number itself.
TEMPLATES = {
    "settled": ("settledTemplate", "expenze_claim_settled"),
    "rejected": ("rejectedTemplate", "expenze_claim_rejected"),
}

# Notices with no approved template of their own. An outcome answers a message
# sent minutes ago, so the 24-hour customer-service window is open and ordinary
# text is both permitted and the right shape. A question and a low-credit alert
# have no template either - and sending one under a template approved for
# something else is how a number loses its quality rating, so they go as text
# and reach WhatsApp only inside the window. Email always carries them.
# `approved` and `disputed` are the two that usually fall outside the window:
# both are a person acting on a claim hours or days after the receipt arrived,
# by which time the 24-hour session has closed and WhatsApp will not take free
# text. Email carries them; WhatsApp gets them only when the reviewer happened
# to be quick. Giving either one a template is a Meta approval away and would
# make WhatsApp reliable for both.
NO_TEMPLATE = {"outcome", "low_credits", "disputed", "approved"}


def _template_parameters(kind: str, claim: dict[str, Any]) -> list[str]:
    """The variables the approved template expects, in order.

    Every one must be non-empty and single-line: WhatsApp rejects a template
    parameter containing a newline or a tab, and drops empty ones.
    """
    ccy = str(claim.get("currency") or "")
    if kind == "settled":
        values = [claim.get("vendor"), money(claim.get("paid"), ccy),
                  claim.get("reference") or "not recorded",
                  claim.get("paid_on") or "today",
                  claim.get("settled_by") or "your finance team"]
    else:
        values = [claim.get("vendor"), money(claim.get("approved"), ccy),
                  claim.get("reason"), claim.get("rejected_by") or "your finance team"]
    return [" ".join(str(v or "-").split())[:900] for v in values]


def _send_whatsapp(to: str, kind: str, claim: dict[str, Any],
                   notice: dict[str, str] | None = None) -> bool:
    if not WA_SECRET_ARN:
        return False
    try:
        cfg = _wa()

        # An outcome answers a message the person sent minutes ago, so the
        # 24-hour customer-service window is open and ordinary text is both
        # permitted and the right shape - a template would turn a sentence
        # about their dinner into a form letter.
        if kind in NO_TEMPLATE:
            body = (notice or {}).get("whatsapp") or (notice or {}).get("text") or ""
            if not body:
                return False
            plain = json.dumps({
                "messaging_product": "whatsapp", "to": to, "type": "text",
                "text": {"preview_url": False, "body": body[:4000]},
            }).encode()
            req = urllib.request.Request(
                f"{GRAPH}/{cfg['phoneNumberId']}/messages", data=plain)
            req.add_header("Authorization", f"Bearer {cfg['accessToken']}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=20):
                return True

        key, fallback = TEMPLATES[kind]
        payload = json.dumps({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": cfg.get(key, fallback),
                "language": {"code": cfg.get("templateLanguage", "en")},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": v}
                                   for v in _template_parameters(kind, claim)],
                }],
            },
        }).encode()
        req = urllib.request.Request(
            f"{GRAPH}/{cfg['phoneNumberId']}/messages", data=payload)
        req.add_header("Authorization", f"Bearer {cfg['accessToken']}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20):
            return True
    except Exception:
        # Email has already gone, or is about to. A WhatsApp template that is
        # still pending approval must not take the whole notice down with it.
        logger.exception("could not WhatsApp the %s notice", kind)
        return False

def send(kind: str, member: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
    """Tell one person what happened to one claim.

    Delivery on either channel can fail without the other being affected, and
    the outcome of each is reported rather than raised: the settlement or
    rejection has already happened, and losing it because a message bounced
    would be the worse failure.
    """
    compose = NOTICES.get(kind)
    if compose is None:
        raise ValueError(f"unknown notice {kind!r}")
    notice = compose(claim)

    email = str(member.get("email") or "").strip().lower()
    sent = {"email": False, "whatsapp": False}
    message_id = ""
    if email:
        message_id = _send_email(email, notice["subject"], notice["text"])
        sent["email"] = bool(message_id)

    # Only a number its owner verified. One somebody else typed in proves
    # nothing, and a claim outcome names an amount and a vendor.
    mobile = str(member.get("mobile") or "").strip()
    if mobile and member.get("whatsapp_channel") == "active":
        sent["whatsapp"] = _send_whatsapp(mobile, kind, claim, notice)

    logger.info("%s notice: email=%s whatsapp=%s", kind, sent["email"], sent["whatsapp"])
    # The words themselves come back with the result, so a caller can store
    # what was actually sent against the claim.
    #
    # The console has a panel headed "What the submitter is told". It showed
    # the model's audit rationale, which is written for the verdict and
    # dispatched nowhere - so a reviewer read a paragraph the submitter had
    # never seen, next to a claim whose real message said something else in
    # different words. The settlement half of that panel was worse: a second
    # implementation of `settled_notice` written in JavaScript, free to drift
    # from this one and with nothing to catch it if it did.
    #
    # Storing the sent text removes both. There is one author of these words
    # and the console quotes it.
    return {"sent": sent, "subject": notice["subject"],
            "email_message_id": message_id,
            "notice": {"kind": kind, "subject": notice["subject"],
                       "text": notice["text"], "whatsapp": notice["whatsapp"],
                       "email": sent["email"], "wa": sent["whatsapp"]}}


def record(table: Any, submission_id: str, result: dict[str, Any]) -> None:
    """Store the words a submitter was sent, against the claim they are about.

    Best effort, always. The message has already gone by the time this runs,
    and the decision it reports was written down before that - losing the copy
    because a write failed must not fail either of them.

    One notice is kept, the latest. A claim's history is the audit log's job;
    what this answers is the question a reviewer actually asks, which is "what
    does this person currently believe?" - and that is the last thing they
    heard, not the first.
    """
    notice = (result or {}).get("notice")
    if not table or not submission_id or not notice:
        return
    if not (notice.get("email") or notice.get("wa")):
        # Nothing reached them, so there is nothing they were told. Recording
        # it anyway would show a reviewer a message that never arrived.
        return
    try:
        table.update_item(
            Key={"submission_id": submission_id},
            UpdateExpression="SET last_notice = :n",
            ExpressionAttributeValues={
                ":n": {**notice, "at": int(time.time())}},
        )
    except Exception:
        logger.exception("could not record the %s notice for %s",
                         notice.get("kind"), submission_id)


def email_digest(to: str, claims: list[dict[str, Any]], org_name: str) -> bool:
    """Send one digest. Email only - this is a working list, not an alert."""
    if not to or not claims:
        return False
    notice = submissions_digest(claims, org_name)
    return bool(_send_email(to, notice["subject"], notice["text"]))
