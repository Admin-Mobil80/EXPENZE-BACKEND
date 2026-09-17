"""What credits cost. One definition, read the same way everywhere.

This exists because there were two. The back office merged the stored pricing
against a seeded default before showing it, and the customer console read the
same row raw - so BMS displayed a full INR column while every slab in the
customer's Credits tab said "no INR price set". Both were reading the same
table and neither was wrong on its own terms; they simply disagreed, and the
disagreement was invisible from either screen.

So the merge lives here and both callers use it:

**Per slab, per currency.** A row saved before a currency column existed has
nothing in it, and taking that literally hands the checkout an empty price. A
missing currency falls back to the seeded one for that slab rather than
blanking it, which means adding a third currency later cannot silently stop
sales to everyone who saved settings before it existed.

**Seeded prices are placeholders, and say so.** The USD column was a commercial
decision; the INR column still needs one.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

# Slab sizes are fixed; only the prices move. The INR column is a placeholder
# at roughly 84/USD, rounded for readability, and needs a commercial decision
# exactly as the USD column did.
DEFAULT_PRICING: list[dict[str, Any]] = [
    {"credits": 500, "usd": 50, "inr": 4200},
    {"credits": 1000, "usd": 90, "inr": 7600},
    {"credits": 2500, "usd": 210, "inr": 17600},
    {"credits": 5000, "usd": 380, "inr": 31900},
    {"credits": 10000, "usd": 695, "inr": 58400},
    {"credits": 25000, "usd": 1550, "inr": 130200},
    {"credits": 50000, "usd": 2725, "inr": 228900},
    {"credits": 100000, "usd": 4750, "inr": 399000},
]

# Added to INR sales at checkout, shown as its own line. USD sales are an
# export of services and are not taxed here.
DEFAULT_GST_PERCENT = 18

CURRENCIES = ("usd", "inr")


def _number(value: Any) -> Any:
    """DynamoDB Decimals as plain numbers; anything unusable as None."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (int, float)):
        return value
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return int(d) if d == d.to_integral_value() else float(d)


def merge(stored: Any) -> list[dict[str, Any]]:
    """Stored pricing, with any missing currency filled from the seed.

    Falls back slab by slab rather than all-or-nothing: a row that has a USD
    price and no INR one keeps its USD price and gains the seeded INR, instead
    of the whole table reverting or the currency coming back empty.
    """
    seeded = {int(r["credits"]): r for r in DEFAULT_PRICING}
    rows = list(stored or [])
    if not rows:
        return [dict(r) for r in DEFAULT_PRICING]

    merged = []
    for row in rows:
        credits = _number(row.get("credits"))
        if not credits:
            continue
        fallback = seeded.get(int(credits), {})
        slab: dict[str, Any] = {"credits": int(credits)}
        for ccy in CURRENCIES:
            value = _number(row.get(ccy))
            slab[ccy] = fallback.get(ccy) if value is None else value
        merged.append(slab)
    return sorted(merged, key=lambda r: r["credits"])


def gst_percent(stored: Any) -> Any:
    """The configured rate, or the seeded one when nothing is set."""
    value = _number(stored)
    return DEFAULT_GST_PERCENT if value is None else value
