"""Razorpay's own notification that a payment succeeded.

    POST /razorpay/webhook

This is the authoritative path, not the fallback. The browser callback after
checkout is the *fast* one and the one that lets us show a balance immediately,
but it only fires if the customer is still looking at the page - and people
close the tab on the confirmation screen, lose signal in a lift, and pay from a
phone that goes to sleep. The webhook arrives regardless, and retries if we are
down. A credit system that only worked when the buyer stayed on the page would
lose real money for real customers.

Its own function, deliberately. The endpoint is public and unauthenticated, so
it does not get the session signing key that the console API holds: nothing
reachable without a session should be able to mint one.

**Nothing in the body is trusted.** A signing secret is optional here: when one
is configured a bad signature is refused, and when none is the delivery is read
as a hint rather than a fact. Either way the figures come from asking Razorpay
directly, with our own key, before a single credit moves - which also settles
which mode a payment belongs to, since a live payment is invisible to a test
key and a test payment to a live one. That matters when one URL serves both.

So forging a delivery gains nothing: crediting still requires a real captured
payment, against a real order of ours, for the exact amount - which is
indistinguishable from having actually paid.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import payments

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Events that mean money arrived. `order.paid` fires once the order is fully
# paid; `payment.captured` once the payment itself is captured. Razorpay sends
# both for a normal purchase, which is harmless - crediting is exactly-once.
CREDITING_EVENTS = {"payment.captured", "order.paid"}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    raw = event.get("body") or ""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    signature = headers.get("x-razorpay-signature", "")

    try:
        # A secret is optional. When one is set, a bad signature is refused
        # outright. When none is, the delivery is accepted as a hint and
        # nothing in it is believed - every figure that matters is confirmed
        # against Razorpay's own API before a credit moves. See
        # payments.confirm_payment.
        if payments.webhook_secret():
            if not payments.webhook_signature_ok(raw, signature):
                logger.warning("rejected a mis-signed Razorpay webhook")
                return {"statusCode": 401, "body": "forbidden"}
        elif signature:
            logger.info("delivery is signed but no secret is configured; "
                        "verifying against the API instead")
    except payments.PaymentError:
        logger.error("payments are not configured; cannot handle the webhook")
        return {"statusCode": 503, "body": "not configured"}

    try:
        body = json.loads(raw or "{}")
        kind = str(body.get("event", ""))
        if kind not in CREDITING_EVENTS:
            logger.info("ignoring Razorpay event %s", kind)
            return _ok()

        entities = (body.get("payload") or {})
        payment = ((entities.get("payment") or {}).get("entity") or {})
        order = ((entities.get("order") or {}).get("entity") or {})

        order_id = str(payment.get("order_id") or order.get("id") or "")
        payment_id = str(payment.get("id") or "")
        if not order_id:
            logger.warning("%s carried no order id", kind)
            return _ok()

        # `order.paid` arrives without a payment id of its own on some plans;
        # the order id alone is enough to claim the row exactly once.
        result = payments.credit_purchase(order_id, payment_id or f"order:{order_id}", "webhook")
        logger.info("%s -> credited=%s already=%s", kind, result["credited"], result["already"])
        return _ok()
    except payments.PaymentError as exc:
        # A payment for an order we do not know about. Nothing to credit, and
        # nothing Razorpay can usefully do by retrying.
        logger.error("webhook could not be applied: %s", exc)
        return _ok()
    except Exception:
        # Anything else may be transient - a throttled table, a cold dependency.
        # A non-2xx tells Razorpay to retry, which is what we want, because the
        # alternative is a customer who paid and never got their credits.
        logger.exception("razorpay webhook failed")
        return {"statusCode": 500, "body": "retry"}


def _ok() -> dict[str, Any]:
    return {"statusCode": 200, "body": "ok"}
