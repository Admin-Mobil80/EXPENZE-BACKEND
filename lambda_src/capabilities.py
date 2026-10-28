"""The questions a CxO asks out loud, answered once.

This is the read surface behind the MCP server and, through it, the Alexa
skill. It exists as its own module for one reason: **a figure spoken aloud has
no screen beside it.** Every other reader of this data can be checked - the
Pending settlement tab shows its rows, the Reports tab shows the counts that
produce its percentages, the finance digest lists the claims it totals. A
voice answer is a number and nothing else, so the number has to be the one
already on the screen, and the only way to guarantee that is to go through the
same predicates the screen goes through rather than write a second set.

That is not a hypothetical in this codebase. The auto-clear rate was shown
twice and read 69% and 20% about the same seventy-one receipts, because three
readers asked the question three ways. Pending settlement and Reports
disagreed by Rs 8,819.23 because one of them counted a companion document.
Five approved claims worth Rs 9,004.63 were invisible to finance because one
caller read `approved_total` and another read the verdict. Each of those was
caught because somebody could see both numbers at once. Spoken, they would
not have been caught at all.

So: `policy.stage` decides where a claim has got to, `policy.owed` decides
what it is worth, `policy.awaiting_payment` decides whether finance has it,
and nothing here re-derives any of the three.

**Three readings of "spend", and the answer names which one it is.**
"What was the total expense for October" has three true answers that differ by
a lot - what was claimed, what was approved, and what has actually left the
bank - and the gap between them is the working capital question. Answering
with one unlabelled figure is how a reviewer and a finance executive come away
from the same sentence with different numbers in their heads.

**And two readings of "October".** The console measures a claim's period by
`received_at` - when the receipt arrived - not by the date printed on the
bill. A dinner on 28 September forwarded on 2 October is October's on the
screen. That is a defensible choice for a budget, which is about when money
left, and a surprising one for a question phrased "the month of October", so
both are computed and the answer says which it led with.
"""
from __future__ import annotations

import calendar
import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import boto3

import policy

INTAKE_TABLE = os.environ.get("INTAKE_TABLE", "")
ORGS_TABLE = os.environ.get("ORGS_TABLE", "")

_ddb = boto3.resource("dynamodb") if INTAKE_TABLE else None

# Out of every money figure and every count. A rejected claim is the company
# deciding it is not spending that; a withdrawn one is the submitter saying it
# was never a claim; a companion is the second photograph of one purchase,
# which is evidence and not a second thing to pay. The console calls this
# trio out in `LIVE_STAGES_OUT` and `claimsOnly`.
NOT_SPEND = ("rejected", "withdrawn", "companion")

# The endings. Nothing further happens to a claim in one of these.
CLOSED = ("settled", "rejected", "withdrawn")


def _ms(value: Any) -> int:
    """Whatever unit it was written in, in milliseconds.

    `received_at` is milliseconds while `review_at`, `audited_at` and
    `outcome_at` are seconds, and one row carries all four. Mixing them has
    produced a confident wrong answer here before - "last movement: 1m ago"
    against every float holder, because a seconds helper was handed
    milliseconds and a `Math.max(1, ...)` swallowed the negative.

    The boundary is 10^11, which is 1973 read as milliseconds and the year
    5138 read as seconds. Nothing in an expense system sits either side of it
    by accident.
    """
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 10 ** 11 else number * 1000


def _dec(value: Any) -> Optional[Decimal]:
    """A figure, or None if there isn't one. Never zero as a stand-in.

    Zero is a real amount and an unreadable one is not. Substituting it
    under-reports spend, which is the one direction a figure put in front of
    somebody who approves money must never fail in.
    """
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def rows(org_id: str) -> list[dict[str, Any]]:
    """Every claim this organisation has. One scan, no projection.

    A CxO question is almost always about a period rather than a claim, and
    the periods overlap - this month, last month, the quarter - so the whole
    table is cheaper to hold once than to query four times. Expenze accounts
    are in the low thousands of rows; when one is not, this is the thing to
    replace with a query against an index on `org_id`.
    """
    if not _ddb or not org_id:
        return []
    table = _ddb.Table(INTAKE_TABLE)
    found, kwargs = [], {
        "FilterExpression": "org_id = :o",
        "ExpressionAttributeValues": {":o": org_id},
    }
    while True:
        page = table.scan(**kwargs)
        found.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return found
        kwargs["ExclusiveStartKey"] = start


def period_bounds(spec: str, now: Optional[datetime] = None) -> tuple[date, date, str]:
    """A spoken period as two dates and the words to say back.

    Saying the period back is not decoration. "October" asked in October means
    the month so far, and asked in December means a closed month; a figure for
    the first of those is not comparable with one for the second, and the only
    cue a listener has that they got a part-month is being told so.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    spec = str(spec or "month").strip().lower()

    def whole_month(year: int, month: int) -> tuple[date, date, str]:
        last = calendar.monthrange(year, month)[1]
        first, final = date(year, month, 1), date(year, month, last)
        name = calendar.month_name[month]
        if (year, month) == (today.year, today.month):
            return (first, today, f"{name} so far")
        return (first, final, f"{name} {year}")

    if spec in ("month", "this_month"):
        return whole_month(today.year, today.month)
    if spec in ("last_month", "previous_month"):
        edge = date(today.year, today.month, 1) - timedelta(days=1)
        return whole_month(edge.year, edge.month)
    if spec in ("week", "this_week"):
        first = today - timedelta(days=today.weekday())
        return (first, today, "this week")
    if spec in ("year", "this_year"):
        return (date(today.year, 1, 1), today, f"{today.year} so far")
    if spec in ("quarter", "this_quarter"):
        start = 3 * ((today.month - 1) // 3) + 1
        return (date(today.year, start, 1), today, "this quarter")

    # An explicit month, "2026-10", and a bare month name, "october".
    if len(spec) == 7 and spec[4] == "-":
        try:
            return whole_month(int(spec[:4]), int(spec[5:]))
        except ValueError:
            pass
    for number in range(1, 13):
        if spec in (calendar.month_name[number].lower(),
                    calendar.month_abbr[number].lower()):
            year = today.year if number <= today.month else today.year - 1
            return whole_month(year, number)

    return whole_month(today.year, today.month)


def _day(row: dict[str, Any], basis: str) -> str:
    """The claim's day as `YYYY-MM-DD`, on one of the two bases.

    `received` is `received_at`, which is what the console measures a period
    by. `incurred` is the date printed on the bill, which is what somebody
    asking about "the month of October" usually has in mind.

    The printed date is inside `receipt`, not on the row. Reading a
    non-existent top-level `date` made the two bases identical on every claim
    and the second figure a copy of the first - which is worse than not
    offering it, because it says the two readings agree.
    """
    if basis == "incurred":
        printed = str((row.get("receipt") or {}).get("date") or "").strip()[:10]
        if printed:
            return printed
        return ""
    stamp = _ms(row.get("received_at"))
    if not stamp:
        return ""
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).date().isoformat()


def _type_of(row: dict[str, Any]) -> str:
    """Which expense type this claim is filed under, as the type's **id**.

    The same three fields in the same order as the console's own row builder:
    what a person answered beats what the engine read, which beats what was
    printed on the receipt. Reading only a top-level `expense_type`, which no
    row has, filed every claim on the account under "Not set".

    The id and not the label, which is the subtler half. A rule set gives each
    type both - `software_subscription` and "Software subscriptions" - and the
    two are stored in different places: a person's own answer is written as an
    id, while the engine's reading carries a label beside it. Preferring the
    label where there was one and the id where there was not split every type
    on the live account in two, so "Software subscriptions" appeared twice at
    Rs 1,46,247.03 and Rs 41,249.60 and neither was the real figure.
    """
    receipt = row.get("receipt") or {}
    verdict = row.get("verdict") or {}
    return (str(row.get("answered_expense_type") or "").strip()
            or str(verdict.get("expense_type") or "").strip()
            or str(receipt.get("expense_type") or "").strip())


def _pretty(type_id: str) -> str:
    """An id as something worth saying aloud, when the rules do not name it.

    A type that has since been renamed or switched off still has claims filed
    under it, and `software_subscription` read out as written is worse than a
    tidied version of itself.
    """
    return type_id.replace("_", " ").strip().capitalize() or "Not set"


def type_labels(org_id: str) -> dict[str, str]:
    """Every expense type this organisation has, as `{id: label}`.

    Taken from the organisation's own saved rules through `policy.rules_for`,
    which is the same function the engine judges a claim with - so a type
    renamed in the console is renamed here, and an account that has never
    saved a rule set gets the built-in defaults rather than nothing.
    """
    if not _ddb or not org_id or not ORGS_TABLE:
        return {}
    try:
        org = _ddb.Table(ORGS_TABLE).get_item(
            Key={"org_id": org_id}).get("Item") or {}
    except Exception:                                      # noqa: BLE001
        # A report is a read. Losing the pretty names is a worse answer than
        # the ids; failing to answer at all is worse than both.
        return {}
    rules = policy.rules_for(org)
    return {t["id"]: (t.get("label") or t["id"])
            for t in rules.get("expense_types", []) if t.get("id")}


def _claimed(row: dict[str, Any]) -> Optional[Decimal]:
    """What this claim is worth before anybody has decided on it.

    The figure frozen onto the row when it was audited, converted into the
    organisation's currency at that day's rate, exactly as the console's
    `budgetValue` reads it - never recomputed, because a figure that moves
    when the market does is not last month's figure.
    """
    stored = row.get("budget_value") or {}
    amount = _dec(stored.get("amount"))
    if amount is not None:
        return amount
    verdict = row.get("verdict") or {}
    return _dec(verdict.get("receipt_total")) or _dec(verdict.get("stated_total"))


def _currency(claims: list[dict[str, Any]]) -> str:
    """The currency these figures are in: the one payouts leave in.

    `payout_value` and `budget_value` are both empty on plenty of real rows -
    anything audited before those were written, and anything on an account
    billing in one currency where there was nothing to convert. The claim's
    own currency is the fallback, and an account with no currency anywhere is
    reported as having none rather than as rupees by assumption.
    """
    for field in ("payout_value", "budget_value"):
        for row in claims:
            found = str((row.get(field) or {}).get("currency") or "").upper()
            if found:
                return found
    for row in claims:
        found = (str(row.get("answered_currency") or "").strip()
                 or str((row.get("verdict") or {}).get("currency") or "").strip())
        if found:
            return found.upper()
    return ""


def _live(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The claims that count: neither refused, nor retracted, nor a duplicate."""
    return [r for r in claims if policy.stage(r) not in NOT_SPEND]


def spend(claims: list[dict[str, Any]], period: str = "month",
          now: Optional[datetime] = None,
          types: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """What a period cost, in the three readings that are all true.

    `claimed` is what people asked for, `approved` is what the company has
    agreed it owes, and `settled` is what has actually left the bank. They
    differ by the queue and the payment run, and the two gaps are the only
    numbers in here a CxO can act on this afternoon.

    `unreadable` is the count of claims whose worth could not be established -
    a foreign receipt whose rate could not be had, usually. They are named
    rather than counted as nothing, because a claim silently worth zero makes
    a period look cheaper than it was.
    """
    first, last, label = period_bounds(period, now)
    window = (first.isoformat(), last.isoformat())

    def inside(row: dict[str, Any], basis: str) -> bool:
        day = _day(row, basis)
        return bool(day) and window[0] <= day <= window[1]

    counted = [r for r in _live(claims) if inside(r, "received")]
    by_bill = [r for r in _live(claims) if inside(r, "incurred")]

    claimed, unreadable, categories, people = Decimal(0), 0, {}, {}
    for row in counted:
        amount = _claimed(row)
        if amount is None:
            unreadable += 1
            continue
        claimed += amount
        kind = _type_of(row)
        categories[kind] = categories.get(kind, Decimal(0)) + amount
        who = str(row.get("submitted_by") or "").strip().lower() or "unknown"
        people[who] = people.get(who, Decimal(0)) + amount

    # The two gaps are summed from the claims that are actually in each state,
    # not subtracted from one another. Subtracting looked equivalent and was
    # wrong twice over: `claimed` is each claim's own worth while `approved`
    # is what a reviewer released, so a claim approved above its receipt total
    # made `claimed - approved` **negative** - September 2026 reported minus
    # Rs 45.88 "awaiting decision" on live data. And within a closed month the
    # subtraction answers a question nobody asked: a claim submitted in
    # September and paid in October would make September look as though it had
    # been settled at the time.
    approved, settled = Decimal(0), Decimal(0)
    to_decide, to_pay = Decimal(0), Decimal(0)
    for row in counted:
        stage = policy.stage(row)
        if stage in ("approved", "part_settled", "settled"):
            due = _dec(policy.owed(row)[0])
            if due is not None:
                approved += due
        if stage == "in_review":
            waiting = _claimed(row)
            if waiting is not None:
                to_decide += waiting
        if policy.awaiting_payment(row):
            due = _dec(policy.owed(row)[0])
            if due is not None:
                to_pay += due
        paid = _dec(row.get("outcome_paid"))
        if paid is not None and str(row.get("outcome") or "") == "settled":
            settled += paid

    on_bill = sum((a for a in (_claimed(r) for r in by_bill) if a is not None),
                  Decimal(0))

    def share(part: Decimal) -> str:
        if not claimed or not part:
            return "0%"
        pct = part / claimed * 100
        return "<1%" if pct < 1 else f"{pct.quantize(Decimal('1'))}%"

    ccy = _currency(counted) or _currency(claims)
    named = types or {}
    ranked = sorted(categories.items(), key=lambda kv: kv[1], reverse=True)
    top = sorted(people.items(), key=lambda kv: kv[1], reverse=True)

    return {
        "period": label,
        "from": window[0],
        "to": window[1],
        "currency": ccy,
        "claims": len(counted),
        "claimed": f"{claimed:.2f}",
        "approved": f"{approved:.2f}",
        "settled": f"{settled:.2f}",
        "awaiting_decision": f"{to_decide:.2f}",
        "awaiting_payment": f"{to_pay:.2f}",
        "unreadable": unreadable,
        "by_category": [{"name": named.get(k) or _pretty(k), "id": k,
                         "amount": f"{v:.2f}", "share": share(v)}
                        for k, v in ranked],
        "by_person": [{"person": k, "amount": f"{v:.2f}", "share": share(v)}
                      for k, v in top],
        # The same period measured on the date printed on the bill. Carried so
        # the gap can be mentioned when it is material and ignored when it is
        # not, rather than quietly choosing one of the two.
        "on_bill_date": f"{on_bill:.2f}",
        "basis": "received",
    }


def queue(claims: list[dict[str, Any]]) -> dict[str, Any]:
    """What is waiting for a person to decide, and how long it has waited.

    Age is the number worth hearing. A queue of four is fine; a queue of four
    whose oldest has sat eleven days is somebody waiting on their own money.
    """
    waiting = [r for r in claims if policy.stage(r) == "in_review"]
    now_ms = int(time.time() * 1000)
    ages = sorted((now_ms - _ms(r.get("received_at"))) // 86_400_000
                  for r in waiting if _ms(r.get("received_at")))
    return {
        "waiting": len(waiting),
        "oldest_days": ages[-1] if ages else 0,
        "over_a_week": sum(1 for a in ages if a >= 7),
        "refs": [str(r.get("reference") or r.get("submission_id") or "")
                 for r in waiting][:20],
    }


def pending(claims: list[dict[str, Any]]) -> dict[str, Any]:
    """What finance owes and to whom - the payment run, totalled.

    Goes through `policy.awaiting_payment`, which is the same predicate the
    Pending settlement tab builds its rows from, so a spoken total and a tab
    badge cannot come apart.
    """
    due = [r for r in claims if policy.awaiting_payment(r)]
    total, unknown, owed_to = Decimal(0), 0, {}
    for row in due:
        amount = _dec(policy.owed(row)[0])
        if amount is None:
            unknown += 1
            continue
        total += amount
        who = str(row.get("submitted_by") or "").strip().lower() or "unknown"
        owed_to[who] = owed_to.get(who, Decimal(0)) + amount
    ranked = sorted(owed_to.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "claims": len(due),
        "currency": _currency(due) or _currency(claims),
        "total": f"{total:.2f}",
        "unreadable": unknown,
        "people": len(owed_to),
        "owed_to": [{"person": k, "amount": f"{v:.2f}"} for k, v in ranked],
    }


def claim(claims: list[dict[str, Any]], ref: str) -> dict[str, Any]:
    """One claim, in the few facts somebody asking after it wants.

    Reference matching is deliberately loose. A claim is `Exp-134` on screen
    and "expense one three four" through a microphone, and refusing to answer
    because the punctuation differs would make the capability useless in the
    channel it exists for.
    """
    wanted = "".join(c for c in str(ref or "").lower() if c.isalnum())
    if not wanted:
        return {"found": False, "ref": str(ref or "")}
    digits = "".join(c for c in wanted if c.isdigit())

    for row in claims:
        on_row = str(row.get("reference") or row.get("submission_id") or "")
        flat = "".join(c for c in on_row.lower() if c.isalnum())
        if not flat:
            continue
        if flat != wanted and not (digits and flat.endswith(digits)):
            continue
        stage = policy.stage(row)
        amount, ccy = policy.owed(row)
        return {
            "found": True,
            "ref": on_row,
            "stage": stage,
            "closed": stage in CLOSED,
            "cleared_by": policy.cleared_by(row),
            "vendor": str(row.get("vendor") or ""),
            "submitted_by": str(row.get("submitted_by") or ""),
            "expense_type": str(row.get("expense_type") or ""),
            "amount": amount,
            "currency": ccy,
            "received": _day(row, "received"),
            "bill_date": _day(row, "incurred"),
            "with_finance": policy.awaiting_payment(row),
        }
    return {"found": False, "ref": str(ref or "")}


def auto_clear(claims: list[dict[str, Any]], period: str = "month",
               now: Optional[datetime] = None) -> dict[str, Any]:
    """How much of the real work the agent took off a person.

    The denominator is the claims that turned out to be real - a claim nobody
    is being paid for was not work - and the numerator asks
    `policy.cleared_by`, so this cannot drift from the console's figure the
    way two readings of it once drifted to 69% and 20%.
    """
    first, last, label = period_bounds(period, now)
    counted = [r for r in _live(claims)
               if first.isoformat() <= _day(r, "received") <= last.isoformat()]
    agent = sum(1 for r in counted if policy.cleared_by(r) == "agent")
    human = sum(1 for r in counted if policy.cleared_by(r) == "human")
    pct = round(agent / len(counted) * 100) if counted else 0
    return {"period": label, "claims": len(counted), "by_agent": agent,
            "by_person": human, "undecided": len(counted) - agent - human,
            "rate": pct}


def brief(claims: list[dict[str, Any]], now: Optional[datetime] = None,
          types: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """The whole dashboard in one object: what a morning briefing reads from.

    Materialised rather than queried, because the voice channel has about
    eight seconds and a model turn that calls tools does not reliably fit in
    them. Everything here is a figure a scheduled job can compute hourly and a
    spoken answer can then read without touching the claims table at all.
    """
    this_month = spend(claims, "month", now, types)
    last_month = spend(claims, "last_month", now, types)

    def movement() -> Optional[str]:
        before, after = _dec(last_month["claimed"]), _dec(this_month["claimed"])
        if not before or after is None or before <= 0:
            return None
        return f"{(after - before) / before * 100:+.0f}%"

    return {
        "as_of": int((now or datetime.now(timezone.utc)).timestamp()),
        "this_month": this_month,
        "last_month": last_month,
        "month_on_month": movement(),
        "queue": queue(claims),
        "pending_settlement": pending(claims),
        "auto_clear": auto_clear(claims, "month", now),
    }
