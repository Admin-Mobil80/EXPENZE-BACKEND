"""Exchange rates, for budgets and for nothing else.

A budget is a number in one currency. Spend is not: somebody on the same team
buys lunch in rupees, a conference ticket in dollars and a hotel in dirhams,
and all three came out of the same monthly allowance. Until now the console
simply left foreign-currency claims out of the budget arithmetic and showed a
count of them beside it, so a team could go well past its limit entirely in
dollars and appear comfortably inside it.

Three rules keep this honest.

**It never decides anything.** A policy cap is still compared against a cap set
in the receipt's own currency - `policy.py` does not convert, and converting
there would mean an exchange rate deciding whether a receipt passes. What is
converted is the figure that gets paid, after the verdict is already settled,
because the money leaves the company's account in one currency however many the
receipts were in. An arithmetic step at the end, not a judgment in the middle.

**The rate is recorded, not recomputed.** The converted figure and the rate
that produced it are written onto the claim when it is audited. A budget that
moved because the rupee moved overnight - on claims settled weeks ago - would
be unexplainable to the person looking at it, and two people opening the same
report on different days would see different totals. It matters more again for
a payout: an amount owed that quietly changes between the day it was approved
and the day it is paid is not an amount owed, and the person being reimbursed
has every right to be told which rate was used and when it was taken.

**A missing rate is not a zero.** If no rate can be had, the claim carries no
converted figure and the console says so. Counting it as nothing would quietly
under-report spend, which is the failure that matters here: a budget that
under-reports is worse than one that admits it cannot see part of the picture.

Rates come from two free, key-less services, tried in order, and are cached in
the settings table so a warm fleet asks once or twice a day rather than once a
receipt.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import boto3

logger = logging.getLogger()

SETTINGS_TABLE = os.environ.get("SETTINGS_TABLE", "")
_settings = boto3.resource("dynamodb").Table(SETTINGS_TABLE) if SETTINGS_TABLE else None

# One row, one base. Every pair is derived from it, so A→B and B→A always agree
# with each other and nothing depends on which order somebody asked.
CACHE_KEY = "fx_rates"
BASE = "USD"

# Published daily. Refreshing every six hours costs two requests a day and
# keeps a fleet that scales out from hammering a free service.
MAX_AGE = 6 * 60 * 60

# Both are free and need no key, which matters: a rate source behind a secret
# is one more credential to rotate, and a budget check is not worth that. The
# second is the fallback, not a cross-check - the first answer wins.
SOURCES = (
    "https://open.er-api.com/v6/latest/USD",
    "https://api.frankfurter.app/latest?from=USD",
)

TIMEOUT = 6

# Per-container memo on top of the table, so one invocation reading forty
# claims does one DynamoDB read rather than forty.
TWO_PLACES = Decimal("0.01")

_memo: Optional[dict[str, Any]] = None


def _parse(payload: dict[str, Any]) -> dict[str, str]:
    """Rates against USD, as strings, from either service's shape."""
    raw = payload.get("rates") or payload.get("conversion_rates") or {}
    out: dict[str, str] = {}
    for code, value in raw.items():
        code = str(code).strip().upper()
        if len(code) != 3:
            continue
        try:
            rate = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if rate > 0:
            out[code] = str(rate)
    out[BASE] = "1"
    return out


def _fetch() -> Optional[dict[str, Any]]:
    """Ask each source in turn. Returns nothing if none of them answers."""
    for url in SOURCES:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "expenze/1.0"})
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            logger.warning("could not read rates from %s", url, exc_info=True)
            continue
        rates = _parse(payload)
        # One currency back is a malformed answer, not a rate table.
        if len(rates) < 5:
            logger.warning("%s returned %d rates; ignoring", url, len(rates))
            continue
        return {
            "base": BASE,
            "rates": rates,
            "as_of": str(payload.get("time_last_update_utc") or payload.get("date") or ""),
            "fetched_at": int(time.time()),
            "source": url,
        }
    return None


def _stored() -> Optional[dict[str, Any]]:
    if _settings is None:
        return None
    try:
        return _settings.get_item(Key={"key": CACHE_KEY}).get("Item")
    except Exception:
        logger.warning("could not read the cached rates", exc_info=True)
        return None


def _store(table: dict[str, Any]) -> None:
    if _settings is None:
        return
    try:
        _settings.put_item(Item={"key": CACHE_KEY,
                                 **json.loads(json.dumps(table), parse_float=Decimal)})
    except Exception:
        logger.warning("could not cache the rates", exc_info=True)


def rates(force: bool = False) -> dict[str, Any]:
    """The current table, from memory, storage or the network in that order.

    A stale table beats no table. If every source is down, yesterday's rates
    still answer "roughly how much of the budget has this eaten" correctly
    enough to raise a hand, and the date it was fetched travels with it so
    nobody is misled about how current it is.
    """
    global _memo
    now = int(time.time())

    if not force and _memo and now - int(_memo.get("fetched_at") or 0) < MAX_AGE:
        return _memo

    stored = _stored()
    if not force and stored and now - int(stored.get("fetched_at") or 0) < MAX_AGE:
        _memo = stored
        return stored

    fresh = _fetch()
    if fresh:
        _store(fresh)
        _memo = fresh
        return fresh

    # Nothing fresh. Whatever is on hand, however old.
    _memo = stored or _memo or {"base": BASE, "rates": {}, "fetched_at": 0, "as_of": ""}
    return _memo


def _rate(table: dict[str, Any], code: str) -> Optional[Decimal]:
    try:
        value = Decimal(str((table.get("rates") or {}).get(code.upper())))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value > 0 else None


def convert(amount: Any, frm: str, to: str) -> Optional[dict[str, Any]]:
    """`amount` of `frm` expressed in `to`, with the rate that did it.

    Nothing back means the conversion could not be made honestly - an unknown
    currency, an unreadable amount, or no rates at all. The caller shows the
    claim as unconverted rather than counting it as zero.
    """
    frm, to = str(frm or "").strip().upper(), str(to or "").strip().upper()
    if not frm or not to:
        return None
    try:
        value = Decimal(str(amount or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return None

    if frm == to:
        return {"amount": str(value.quantize(Decimal("0.01"))), "currency": to,
                "rate": "1", "as_of": "", "from": frm}

    table = rates()
    per_from, per_to = _rate(table, frm), _rate(table, to)
    if per_from is None or per_to is None:
        logger.info("no rate for %s->%s", frm, to)
        return None

    # Both legs are quoted against the same base, so the cross rate is their
    # ratio. Kept at full precision until the final rounding: quantising the
    # rate first and multiplying by it puts a visible error on large amounts.
    rate = per_to / per_from
    return {
        "amount": str((value * rate).quantize(Decimal("0.01"))),
        "currency": to,
        "rate": str(rate.quantize(Decimal("0.00000001"))),
        "as_of": str(table.get("as_of") or ""),
        "from": frm,
    }


def for_budget(verdict: dict[str, Any], budget_currency: str) -> dict[str, Any]:
    """What one audited claim counts as against a budget, or nothing.

    The receipt total, not the reimbursable figure: a budget answers "how much
    has been committed", and money spent is committed whether or not policy
    agrees to reimburse all of it.
    """
    verdict = verdict or {}
    frm = str(verdict.get("currency") or "")
    to = str(budget_currency or "")
    if not frm or not to or frm.upper() == to.upper():
        return {}
    converted = convert(verdict.get("receipt_total"), frm, to)
    return converted or {}


def for_payout(verdict: dict[str, Any], base_currency: str,
               receipt: Any = None) -> dict[str, Any]:
    """The rate this claim pays out at, and what that is on today's figures.

    **The rate is the part that matters.** What is owed on a claim is not
    settled when it is audited: anything blocked reimburses nothing until a
    person approves it, and what they approve is theirs to set. Stamping only a
    converted amount would have fixed every blocked claim at a payout of zero
    and left it there after approval - the figure would have been wrong for
    exactly the claims a human had looked at.

    So what is fixed here is the rate, taken when the receipt arrived, and the
    day it was taken. Whatever ends up owed is converted at that rate, so the
    amount cannot drift between the day a claim was decided and the day it is
    paid, and the employee can be told which rate they were paid at.

    `amount` is that conversion applied to the engine's own figure - right for
    the common case where nothing was blocked, and recomputed from `rate`
    wherever a person has since set a different one. Empty when the receipt is
    already in the base currency, and empty when no rate could be had, which is
    reported rather than papered over with a figure nothing stands behind.
    """
    verdict = verdict or {}
    frm = str(verdict.get("currency") or "")
    to = str(base_currency or "")
    if not frm or not to or frm.upper() == to.upper():
        return {}

    # The bill's own conversion wins, where it made one.
    printed = printed_rate(receipt, frm, to)
    if printed is not None:
        amount = _decimal(verdict.get("reimbursable_total"))
        return {
            "amount": ("" if amount is None
                       else str((amount * printed).quantize(TWO_PLACES))),
            "currency": to.upper(),
            "rate": str(printed.quantize(Decimal("0.00000001"))),
            "as_of": "as printed on the bill",
            "from": frm.upper(),
            # So the console can say where the figure came from, and so a
            # reader who knows today's rate is not left thinking we got it
            # wrong by four per cent.
            "source": "receipt",
        }

    converted = convert(verdict.get("reimbursable_total"), frm, to)
    if converted:
        converted["source"] = "market"
    return converted or {}


def printed_rate(receipt: Any, frm: str, to: str) -> Optional[Decimal]:
    """The rate this bill printed for itself, or None if it printed none.

    Taken in preference to any rate we can look up, and the reason is not that
    it is fresher. It is that it is not a rate at all in the sense a table
    means: it is the transaction. A foreign invoice settled by an Indian card
    prints "$20.00" as the price and "Charged 1,995.62 INR using 1 USD =
    99.7812 INR (includes 4% conversion fee)" as what actually left the
    account, issuer's margin and all. Mobil80-Exp-16 was reimbursed at the
    market rate, 96.0216, for 1,920.43 - leaving the employee 75.19 short on a
    bill that said in print exactly what he had paid.

    No public rate reproduces that number, because no public rate includes
    somebody's card issuer's margin. Reimbursing at one means quietly making
    the employee carry the fee for spending the company's money.

    The printed rate is preferred over dividing the charge by the total: it is
    the figure the bank quoted, at full precision, and the division inherits
    the rounding of both amounts. The division is the fallback for a bill that
    states the charge and not the rate, which is common.

    A printed rate of zero is a misread rather than a discount, so it falls
    through to the division like a missing one. None only when the bill did not
    do this at all, when what it did does not match the pair being converted,
    or when neither the rate nor the two amounts can be made sense of.
    """
    if not isinstance(receipt, dict):
        return None
    if str(receipt.get("charged_currency") or "").strip().upper() != str(to or "").upper():
        return None
    # And the price it converted has to be the currency we are converting from,
    # or this is two unrelated numbers being divided by each other.
    if str(receipt.get("currency") or "").strip().upper() != str(frm or "").upper():
        return None

    rate = _decimal(receipt.get("charged_rate"))
    if rate is None or rate <= 0:
        charged = _decimal(receipt.get("charged_total"))
        total = _decimal(receipt.get("stated_total"))
        if charged is None or total is None or total <= 0 or charged <= 0:
            return None
        rate = charged / total
    return rate if rate > 0 else None


def _decimal(value: Any) -> Optional[Decimal]:
    """A printed figure as a number, or None. Never an exception, never a zero
    standing in for one - a total nobody could read is not a total of nil."""
    try:
        text = str(value if value is not None else "").strip().replace(",", "")
    except (TypeError, ValueError):
        return None
    if not text:
        return None
    try:
        return Decimal(text)
    except (ArithmeticError, ValueError):
        return None
