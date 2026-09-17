"""Budgets: what a team meant to spend, next to what it actually did.

A budget here never blocks anything. A receipt that would breach one is still
accepted, still audited, still queued or cleared exactly as it would have been.
Refusing an employee's dinner because a cost centre is 4% over would be a
product that punishes the wrong person for a decision they did not make - and
finance would simply stop using it. What a breach does is raise its hand, to
the finance executive and owner/management, who are the people who can act.

Three levels, most specific first:

    person  ->  group  ->  organisation

Inheritance is **per field, not per record**. Somebody given their own travel
budget still inherits the organisation's meal budget and its overall total.
Replacing the whole record instead would silently drop every limit that was
not restated, which is how a budget quietly stops applying to the person most
likely to have had one set for them.

Spend is measured against **everything submitted**, not what has been paid.
A budget answers "how much has been committed", and a receipt that is sitting
in the review queue has already been spent - the money left the employee's
pocket days ago. Measuring settled claims instead would show a team comfortably
inside budget right up to the moment finance runs the payments.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

PERIODS = ("week", "month")
DEFAULT_PERIOD = "month"

# Where "getting close" starts. Not configurable yet, and deliberately not 100%:
# a warning that arrives only once the budget is gone is not a warning.
NEAR_THRESHOLD = Decimal("0.80")

MAX_TYPES = 40


class BudgetInputError(ValueError):
    """A budget as submitted was not usable."""


def _amount(value: Any, field: str) -> Optional[str]:
    """A limit as a 2dp decimal string, or None for 'no limit set'."""
    if value in (None, "", "-"):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, TypeError):
        raise BudgetInputError(f"{field}: {value!r} is not an amount")
    if amount.is_nan() or amount.is_infinite() or amount < 0:
        raise BudgetInputError(f"{field}: a budget cannot be negative")
    return str(amount.quantize(Decimal("0.01")))


def _limits(raw: Any, field: str) -> dict[str, Any]:
    """One budget record: an overall total, and any per-expense-type limits."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise BudgetInputError(f"{field}: expected a budget object")

    types_in = raw.get("types") or {}
    if not isinstance(types_in, dict):
        raise BudgetInputError(f"{field}.types: expected an object")
    if len(types_in) > MAX_TYPES:
        raise BudgetInputError(f"{field}.types: too many expense types")

    types: dict[str, str] = {}
    for type_id, value in types_in.items():
        key = str(type_id).strip()[:60]
        if not key:
            continue
        amount = _amount(value, f"{field}.types.{key}")
        if amount is not None:
            types[key] = amount

    record: dict[str, Any] = {}
    total = _amount(raw.get("total"), f"{field}.total")
    if total is not None:
        record["total"] = total
    if types:
        record["types"] = types
    # A period on a person's own record lets someone on a weekly allowance sit
    # inside an organisation that budgets monthly.
    period = str(raw.get("period", "")).strip().lower()
    if period in PERIODS:
        record["period"] = period
    return record


def normalise(raw: Any, currency: str) -> dict[str, Any]:
    """Validate a whole budget configuration on its way into storage."""
    raw = raw or {}
    period = str(raw.get("period", DEFAULT_PERIOD)).strip().lower()
    if period not in PERIODS:
        raise BudgetInputError("A budget period is either a week or a month.")

    def scoped(key: str) -> dict[str, Any]:
        section = raw.get(key) or {}
        if not isinstance(section, dict):
            raise BudgetInputError(f"{key}: expected an object")
        out = {}
        for name, record in section.items():
            cleaned = _limits(record, f"{key}.{name}")
            if cleaned:
                out[str(name).strip().lower()[:254]] = cleaned
        return out

    return {
        "period": period,
        "currency": str(currency or "").upper()[:3],
        "org": _limits(raw.get("org"), "org"),
        "groups": scoped("groups"),
        "people": scoped("people"),
    }


def empty(currency: str = "") -> dict[str, Any]:
    return {"period": DEFAULT_PERIOD, "currency": str(currency or "").upper()[:3],
            "org": {}, "groups": {}, "people": {}}


# ---------------------------------------------------------------------------
# Which budget applies to whom
# ---------------------------------------------------------------------------


def effective(budgets: dict[str, Any], email: str = "", group_id: str = "") -> dict[str, Any]:
    """The budget one person is actually held to, and where each limit came from.

    Merged field by field: a person with only a travel limit of their own keeps
    the organisation's total and its other per-type limits.
    """
    budgets = budgets or empty()
    org = budgets.get("org") or {}
    group = (budgets.get("groups") or {}).get(str(group_id or "").lower(), {})
    person = (budgets.get("people") or {}).get(str(email or "").lower(), {})

    total, total_from = None, ""
    for record, source in ((person, "person"), (group, "group"), (org, "organisation")):
        if record.get("total") is not None:
            total, total_from = record["total"], source
            break

    types: dict[str, dict[str, str]] = {}
    # Least specific first, so a more specific level overwrites it.
    for record, source in ((org, "organisation"), (group, "group"), (person, "person")):
        for type_id, amount in (record.get("types") or {}).items():
            types[type_id] = {"limit": amount, "from": source}

    period = (person.get("period") or budgets.get("period") or DEFAULT_PERIOD)
    return {
        "period": period,
        "currency": budgets.get("currency", ""),
        "total": total,
        "total_from": total_from,
        "types": types,
        "has_any": bool(total or types),
    }


# ---------------------------------------------------------------------------
# The period being measured
# ---------------------------------------------------------------------------


def period_bounds(period: str, when: date) -> tuple[date, date]:
    """First and last day of the week or month containing `when`, inclusive.

    Weeks run Monday to Sunday: the alternative is a week that starts on a
    different day depending on who is looking, and two people reconciling the
    same overspend against different seven-day windows.
    """
    if period == "week":
        start = when - timedelta(days=when.weekday())
        return start, start + timedelta(days=6)
    start = when.replace(day=1)
    next_month = (start + timedelta(days=32)).replace(day=1)
    return start, next_month - timedelta(days=1)


def status(spent: Any, limit: Any) -> str:
    """`none`, `under`, `near` or `over` for one line of a budget."""
    if limit in (None, ""):
        return "none"
    try:
        spent_d, limit_d = Decimal(str(spent or 0)), Decimal(str(limit))
    except (InvalidOperation, TypeError):
        return "none"
    if limit_d <= 0:
        return "over" if spent_d > 0 else "under"
    if spent_d > limit_d:
        return "over"
    if spent_d >= limit_d * NEAR_THRESHOLD:
        return "near"
    return "under"
