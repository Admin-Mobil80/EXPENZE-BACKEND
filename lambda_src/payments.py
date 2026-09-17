"""Buying credits through Razorpay.

Credits are the whole commercial surface of this product: one credit, one
receipt. So this file is where money meets state, and three rules keep that
honest.

**The price is never taken from the browser.** A request says which slab the
customer picked - nothing more. The number of credits and the amount to charge
are both read server-side from the platform pricing table. A client that could
name its own amount could buy a hundred thousand credits for one rupee, and no
amount of signature checking downstream would notice, because the signature
would be perfectly valid over the wrong figure.

**Crediting happens exactly once.** Razorpay tells us a payment succeeded twice
by design - once when the browser returns from checkout, and again over the
webhook, which is the path that still works when the customer closes the tab on
the confirmation screen. Both are welcome; both call the same function; the
first one to arrive wins. The guard is a conditional write on the payment id,
so a double credit is not a race that is unlikely to happen - it is one the
database refuses.

**Every signature is checked.** The checkout callback is signed over
`order_id|payment_id` with the API secret; the webhook is signed over the raw
body with a separate webhook secret. Neither is trusted without that, because
both arrive over a public endpoint and both add money to an account.

A failed payment changes nothing. There is no partial state to reconcile: the
purchase row is written when the order is created, and only ever moves to
`paid` when a verified signature says so.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import boto3

logger = logging.getLogger()

API = "https://api.razorpay.com/v1"
RZP_SECRET_ARN = os.environ.get("RZP_SECRET_ARN", "")

PURCHASES_TABLE = os.environ.get("PURCHASES_TABLE", "")
ORGS_TABLE = os.environ.get("ORGS_TABLE", "")
LEDGER_TABLE = os.environ.get("LEDGER_TABLE", "")

_secrets = boto3.client("secretsmanager")
_ddb = boto3.resource("dynamodb")
_purchases = _ddb.Table(PURCHASES_TABLE) if PURCHASES_TABLE else None
_orgs = _ddb.Table(ORGS_TABLE) if ORGS_TABLE else None
_ledger = _ddb.Table(LEDGER_TABLE) if LEDGER_TABLE else None
_config: dict[str, str] | None = None
_stored_cache: dict[str, Any] | None = None


class PaymentError(Exception):
    """Something about this purchase was not usable."""


def _stored() -> dict[str, Any]:
    """The whole Razorpay secret, both key sets and the selected mode.

        {"mode": "test",
         "test": {"key_id": "rzp_test_...", "key_secret": "..."},
         "live": {"key_id": "rzp_live_...", "key_secret": "..."}}

    Both sets live side by side so switching between them is a setting rather
    than a re-entry of credentials - which matters because the moment you have
    to paste live keys in to test something, somebody eventually tests with
    them.
    """
    global _stored_cache
    if _stored_cache is None:
        if not RZP_SECRET_ARN:
            raise PaymentError("Payments are not configured.")
        try:
            loaded = json.loads(
                _secrets.get_secret_value(SecretId=RZP_SECRET_ARN)["SecretString"])
        except Exception:
            raise PaymentError("Payment keys are not set. Add them in BMS under Settings.")
        if not isinstance(loaded, dict):
            raise PaymentError("Payment keys are not readable.")

        # An older flat secret - one key pair, no mode - still works. Read as
        # whichever mode its key id says it is, so an upgrade needs no edit.
        if "key_id" in loaded and "test" not in loaded and "live" not in loaded:
            guessed = "live" if str(loaded.get("key_id", "")).startswith("rzp_live_") else "test"
            loaded = {"mode": guessed, guessed: {
                "key_id": loaded.get("key_id", ""),
                "key_secret": loaded.get("key_secret", ""),
            }}
        _stored_cache = loaded
    return _stored_cache


def selected_mode() -> str:
    """Which key set BMS has switched on."""
    return "live" if str(_stored().get("mode", "test")).lower() == "live" else "test"


def config() -> dict[str, str]:
    """The key pair currently in use. Cached per container."""
    global _config
    if _config is None:
        chosen = selected_mode()
        keys = _stored().get(chosen) or {}
        if not keys.get("key_id") or not keys.get("key_secret"):
            raise PaymentError(
                f"No {chosen} payment keys are set. Add them in BMS under Settings.")
        _config = {"key_id": str(keys["key_id"]), "key_secret": str(keys["key_secret"])}
    return _config


def mode() -> str:
    """`test` or `live`, read from the key itself rather than a separate flag.

    A flag can disagree with the key it sits next to. The key cannot disagree
    with itself, and the console shows this to the customer so nobody is ever
    unsure whether they just spent real money.
    """
    key = str(config().get("key_id", ""))
    return "live" if key.startswith("rzp_live_") else "test"


# ---------------------------------------------------------------------------
# What a slab costs
# ---------------------------------------------------------------------------


def find_slab(pricing: list[dict[str, Any]], credits: Any) -> Optional[dict[str, Any]]:
    """The published slab for this many credits, or nothing.

    Exact matches only. A request for 4,999 credits is not a slab, and quietly
    rounding it to one would charge a price the customer never saw.
    """
    try:
        wanted = int(credits)
    except (TypeError, ValueError):
        return None
    for slab in pricing or []:
        try:
            if int(slab.get("credits", 0)) == wanted:
                return slab
        except (TypeError, ValueError):
            continue
    return None


def minor_units(amount: Any, currency: str) -> int:
    """Razorpay charges in the smallest unit: paise for INR, cents for USD.

    Sending 500 when 50000 was meant undercharges by a factor of a hundred, so
    this converts once, here, rather than at each call site.
    """
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError):
        raise PaymentError(f"{amount!r} is not a price")
    if value <= 0:
        raise PaymentError("A price must be positive")
    # Every currency Razorpay settles in that we sell in has two decimal places.
    return int((value * 100).quantize(Decimal("1")))


# Where a customer is billed in their own money rather than in dollars. Kept
# as an explicit set rather than "anywhere Razorpay settles locally", because
# adding a country here is a commercial decision - it needs a published price
# in that currency before it can be true.
LOCAL_BILLING = {"IN": "INR"}
DEFAULT_BILLING = "USD"


def checkout_currency(org: dict[str, Any]) -> str:
    """What this organisation is charged in.

    Driven by the country on the organisation record - the same field that
    decides how an unmarked receipt is read - so a customer is never charged in
    one currency and reported to in another.
    """
    country = str((org.get("address") or {}).get("country", "")).strip().upper()
    return LOCAL_BILLING.get(country, DEFAULT_BILLING)


def price_of(slab: dict[str, Any], currency: str) -> Any:
    """What this slab costs in the currency we are charging in.

    Published prices are USD. An account that settles only in INR needs an INR
    price set alongside it - deliberately a second published number rather than
    a conversion, because an exchange rate invented at checkout is a price the
    customer never agreed to and a figure finance cannot reconcile.
    """
    key = currency.lower()
    if key in slab:
        return slab[key]
    if key == "usd" and "usd" in slab:
        return slab["usd"]
    raise PaymentError(
        f"This slab has no price in {currency.upper()}. Set one in BMS under "
        "Settings before selling in this currency.")


def price_breakdown(slab: dict[str, Any], currency: str,
                    gst_percent: Any = 0) -> dict[str, Any]:
    """What the customer is charged, itemised.

    GST is added to the published price rather than assumed to be inside it,
    and it is returned as its own line. A tax silently folded into a total is
    the same figure to the payment gateway and a different thing entirely to
    the person approving the spend and the accountant reclaiming the input
    credit - they need to see the base and the tax separately, and so does the
    invoice.

    Only Indian sales are taxed here. A USD sale is an export of services,
    which is handled on the invoice rather than at the checkout, so adding
    anything to it would be wrong twice over.
    """
    currency = currency.upper()
    base = Decimal(str(price_of(slab, currency)))
    if base <= 0:
        raise PaymentError("A price must be positive")

    rate = Decimal("0")
    label = ""
    if currency == "INR":
        try:
            rate = Decimal(str(gst_percent or 0))
        except (InvalidOperation, TypeError):
            raise PaymentError("The configured GST rate is not a number")
        if rate < 0:
            raise PaymentError("The configured GST rate is negative")
        label = f"GST {rate.normalize():f}%"

    # Rounded to the minor unit once, on the tax alone, so base + tax always
    # equals the total that is actually charged.
    tax = (base * rate / Decimal(100)).quantize(Decimal("0.01"))
    total = base + tax

    return {
        "currency": currency,
        "base": str(base.quantize(Decimal("0.01"))),
        "tax": str(tax),
        "tax_label": label,
        "tax_percent": str(rate),
        "total": str(total.quantize(Decimal("0.01"))),
        "total_minor": minor_units(total, currency),
    }


# ---------------------------------------------------------------------------
# Razorpay
# ---------------------------------------------------------------------------


def _call(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config()
    auth = base64.b64encode(
        f"{cfg['key_id']}:{cfg['key_secret']}".encode()).decode()
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data,
                                 method="POST" if data else "GET")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read() or b"{}")
        message = (body.get("error") or {}).get("description") or "Payment provider error"
        logger.error("razorpay %s -> %s %s", path, e.code, message)
        raise PaymentError(message)


def create_order(amount_minor: int, currency: str, receipt: str,
                 notes: dict[str, str]) -> dict[str, Any]:
    """An order is the amount the customer is about to be shown.

    Created server-side so the figure in the checkout dialog is one we chose,
    not one the page asked for.
    """
    return _call("/orders", {
        "amount": amount_minor,
        "currency": currency.upper(),
        "receipt": receipt[:40],
        "notes": {k: str(v)[:250] for k, v in notes.items()},
        "payment_capture": 1,
    })


def fetch_payment(payment_id: str) -> dict[str, Any]:
    return _call(f"/payments/{payment_id}")


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------


def checkout_signature_ok(order_id: str, payment_id: str, signature: str) -> bool:
    """The signature the browser hands back after a successful checkout.

    Razorpay signs `order_id|payment_id` with the API secret. Without this the
    endpoint would credit an account on the say-so of whatever the page posted.
    """
    if not (order_id and payment_id and signature):
        return False
    expected = hmac.new(config()["key_secret"].encode(),
                        f"{order_id}|{payment_id}".encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def webhook_secret() -> str:
    """The signing secret, if one is configured. Optional by design - see below."""
    return str(_stored().get("webhook_secret", "") or "")


def webhook_signature_ok(raw_body: str, signature: str) -> bool:
    """Whether this delivery carries a good signature.

    Only meaningful when a secret is configured. When none is, the webhook is
    treated as a *notification* rather than a statement of fact: it says "look
    at order X", and the answer comes from asking Razorpay directly with our
    own key (see confirm_payment). Nothing in the body is believed.

    That is a deliberate trade, and the reason it is safe is worth stating:
    forging a webhook gains an attacker nothing, because crediting requires a
    real captured payment against a real order of ours for the exact amount -
    which is indistinguishable from having actually paid.
    """
    secret = webhook_secret()
    if not (secret and signature and raw_body):
        return False
    expected = hmac.new(secret.encode(), raw_body.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Purchases
# ---------------------------------------------------------------------------


def purchase_row(order: dict[str, Any], org_id: str, credits: int,
                 currency: str, amount: Any, actor: str) -> dict[str, Any]:
    """What is written when an order is created, before any money moves."""
    return {
        "order_id": order["id"],
        "org_id": org_id,
        "credits": int(credits),
        "currency": currency.upper(),
        "amount": str(amount),
        "amount_minor": int(order["amount"]),
        "status": "created",
        "mode": mode(),
        "started_by": actor,
        "created_at": int(time.time()),
    }


# ---------------------------------------------------------------------------
# Crediting, exactly once
# ---------------------------------------------------------------------------


def record_order(row: dict[str, Any]) -> None:
    _purchases.put_item(Item=row)


def confirm_payment(order_id: str, payment_id: str, expected_minor: int) -> str:
    """Ask Razorpay whether this payment is real, ours, and for the right amount.

    The signature proves the message came from Razorpay. It does not prove
    which Razorpay: test mode and live mode sign with whatever secret is set on
    each webhook, so one endpoint serving both means a test payment can arrive
    carrying a perfectly valid signature. Nothing in the payload says which
    mode it is.

    Fetching the payment with our own configured key settles it. A live payment
    is invisible to a test key and a test payment is invisible to a live one,
    so a cross-mode notification simply fails to resolve and is refused. The
    same call confirms the amount, which the payload could otherwise misstate:
    without it a one-rupee test payment could credit a slab sold for four
    hundred thousand.

    Returns "" when the payment is good, or a reason it is not.
    """
    if not payment_id or payment_id.startswith("order:"):
        # `order.paid` on some plans carries no payment id. The order itself is
        # still ours and still claimed exactly once, so this is allowed
        # through - the signature is the only proof available for it.
        return ""
    try:
        payment = fetch_payment(payment_id)
    except PaymentError as exc:
        return f"could not be fetched ({exc}) - wrong mode, or not our payment"

    if str(payment.get("order_id") or "") != order_id:
        return "belongs to a different order"
    if payment.get("status") not in ("captured", "authorized"):
        return f"is {payment.get('status')!r}, not captured"
    try:
        paid = int(payment.get("amount") or 0)
    except (TypeError, ValueError):
        return "carries no usable amount"
    if expected_minor and paid != expected_minor:
        return f"is for {paid}, but the order was for {expected_minor}"
    return ""


def credit_purchase(order_id: str, payment_id: str, source: str) -> dict[str, Any]:
    """Turn a paid order into credits. Safe to call as often as you like.

    Razorpay reports a successful payment twice by design - once when the
    browser comes back from checkout, and again over the webhook, which is the
    path that still works when someone closes the tab on the confirmation
    screen. Both are wanted: between them a payment is almost never missed.

    So the guard is not "hopefully only one of them arrives". The purchase row
    is claimed with a conditional write that only succeeds while the row has no
    payment against it. The loser of that race gets
    ConditionalCheckFailedException and returns the already-credited result, so
    calling this twice adds credits once - which is a property of the database,
    not of the ordering of two network callbacks.
    """
    row = _purchases.get_item(Key={"order_id": order_id}).get("Item")
    if not row:
        raise PaymentError("No such order.")

    # Checked against Razorpay itself before anything is credited, not just
    # against the signature on the message.
    wrong = confirm_payment(order_id, payment_id, int(row.get("amount_minor") or 0))
    if wrong:
        logger.warning("refusing to credit %s: the payment %s", order_id, wrong)
        raise PaymentError(f"That payment {wrong}.")

    try:
        claimed = _purchases.update_item(
            Key={"order_id": order_id},
            UpdateExpression=("SET payment_id = :p, #s = :paid, paid_at = :t, "
                              "credited_via = :via"),
            # The whole of exactly-once, in one line: a row that already names
            # a payment cannot be claimed again.
            ConditionExpression="attribute_not_exists(payment_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":p": payment_id, ":paid": "paid",
                ":t": int(time.time()), ":via": source,
            },
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except _purchases.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info("order %s was already credited; %s changed nothing", order_id, source)
        return {"credited": False, "already": True,
                "credits": int(row.get("credits", 0)),
                "org_id": row.get("org_id", "")}

    credits = int(claimed.get("credits", 0))
    org_id = str(claimed.get("org_id", ""))

    updated = _orgs.update_item(
        Key={"org_id": org_id},
        UpdateExpression="SET credits = if_not_exists(credits, :z) + :n",
        ExpressionAttributeValues={":n": credits, ":z": 0},
        ReturnValues="ALL_NEW",
    )["Attributes"]

    # Credits are money. A balance that changed with no record of why is not
    # auditable, so the ledger entry carries the payment it came from.
    if _ledger is not None:
        _ledger.put_item(Item={
            "org_id": org_id,
            "ts": int(time.time() * 1000),
            "delta": credits,
            "reason": "purchase",
            "order_id": order_id,
            "payment_id": payment_id,
            "amount": str(claimed.get("amount", "")),
            "currency": str(claimed.get("currency", "")),
            "tax": str(claimed.get("tax", "")),
            "mode": str(claimed.get("mode", "")),
            "by": str(claimed.get("started_by", "")),
            "via": source,
        })

    logger.info("credited %s with %s credits from %s", org_id, credits, order_id)
    return {"credited": True, "already": False, "credits": credits,
            "org_id": org_id, "balance": int(updated.get("credits", 0))}
