"""Rule-driven corporate expense policy for Expenze.

This module is the only place a reimbursement decision gets made. The model
reads the receipt and classifies what kind of expense it is; it never decides
the verdict.

One level, deliberately:

* **Expense type** - what the receipt *is*. A meal, a software subscription, a
  canteen grocery run. This selects which rule applies, and it is the only
  classification that changes what anybody is paid.

There used to be a second one beneath it - a line category, configured per
type, with a handful of universal ones underneath. It was a taxonomy to choose
within, under the taxonomy that had already been chosen, and no category ever
moved a rupee. What it was reaching for is now a description on the type
itself: the words the organisation uses for what belongs under it, handed to
the model when it picks one. An organisation that wants finer reporting adds a
type rather than a sub-list.

What a receipt is worth is what it prints. Where a bill states its own grand
total, that is the figure claimed against - the vendor charged it and the card
was debited for it, and the line items are only our reading of the same piece
of paper. A reading that comes up short is a reason to doubt the reading, not
the bill; paying the lower of the two would short the employee by exactly our
own extraction error. The difference raises no finding at all: it is a note
about our own extraction, not a question for a reviewer.

The engine deducts for one reason only: a cap. It used to also strike out whole
classes of line - alcohol, tobacco - and apportion tax pro rata between what it
had struck out and what it had kept. That is a judgment about whether an expense is
allowable at all, which is a person's to make on the facts of the claim, not a
rule the agent applies to a word the model put on a line. The agent's job is to
classify the receipt, read its items, measure it against the caps and budgets,
and put it in front of the right human.

A rule set is data, not code, so an administrator can add an expense type
without a deploy. `DEFAULT_RULES` below is the seed; the live set is stored and
passed in.
"""
from __future__ import annotations

import re

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Optional

# What a brand-new organisation is judged by until somebody edits it.
#
# An expense type is four things: what it is called, a description of what
# belongs under it, whether it is in use, and one cap per currency. The
# description is not a caption - it is what the model is handed when it decides
# which type a receipt belongs to, so it names the near-misses too.
DEFAULT_RULES: dict[str, Any] = {
    "version": 1,
    "expense_types": [
        {
            "id": "meals",
            "label": "Meals & entertainment",
            "hint": ("Restaurants, cafes, food delivery and client entertainment. "
                     "Team lunches, client dinners, coffee with a candidate. Not "
                     "provisions bought for the office - those are canteen groceries."),
            "enabled": True,
            "caps": {"per_transaction": {"INR": "3000.00", "USD": "36.00"}},
        },
        {
            "id": "software_subscription",
            "label": "Software subscriptions",
            "hint": ("SaaS and developer tools billed per seat or per month, cloud "
                     "hosting, API and model usage, domain and certificate renewals, "
                     "software licences."),
            "enabled": True,
            "caps": {"per_transaction": {"INR": "40000.00", "USD": "500.00"}},
        },
        {
            "id": "canteen_groceries",
            "label": "Canteen groceries",
            "hint": ("Provisions bought for the office canteen or pantry - tea, "
                     "coffee, milk, snacks, water, cleaning supplies. Bought for the "
                     "office rather than eaten at a restaurant."),
            "enabled": True,
            "caps": {"per_transaction": {"INR": "25000.00", "USD": "300.00"}},
        },
        {
            "id": "travel",
            "label": "Travel",
            "hint": ("Getting somewhere for work: cabs and ride hire, fuel and "
                     "parking, tolls, rail and flights, and hotel accommodation."),
            "enabled": True,
            "caps": {"per_transaction": {"INR": "10000.00", "USD": "120.00"}},
        },
    ],
}


def blocks_on(verdict: Any, code: str) -> bool:
    """Whether a decided claim is held up by one particular finding.

    Read from the verdict rather than from a flag beside it. The flag was
    added later, so claims audited before it exists carry the finding and not
    the flag - and anything keying off the flag hides the answer control on
    exactly the claims that need it. The violations are the fact; a flag is a
    cache of them, and a cache that only some rows have is worse than none.
    """
    for v in ((verdict or {}).get("violations") or []):
        if v.get("code") == code and v.get("blocks_automatic_decision"):
            return True
    return False


def _stated(value: Any) -> Optional[Decimal]:
    """The printed grand total, or nothing.

    Absent on most receipts and on everything audited before this was read, so
    "no answer" has to be ordinary rather than an error. Anything unparseable
    is treated the same way: a total we cannot read is not a total that
    disagrees.
    """
    if value in (None, ""):
        return None
    try:
        total = Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return total if total > 0 else None


def _money(value: Any, field: str, *, signed: bool = False) -> Decimal:
    """Coerce to a 2dp Decimal, refusing floats-as-money surprises.

    `signed` where a minus is a real thing the paper can say. A line item is
    such a place: invoices carry discounts, credits, proration adjustments and
    returned items as negative lines, and they are part of what the bill comes
    to. Refusing them threw out the whole claim - a Claude subscription invoice
    with one discount line failed three audits and was parked for a human, for
    printing something entirely ordinary.

    A cap is not such a place. A rule saying somebody may spend minus fifty is
    not a rule anybody meant to write, and taking it at face value would
    approve every claim of that type in silence.

    `signed` also decides what happens to an amount that will not parse at
    all, and for the same reason: which side of the line the number came from.

    A line item's amount comes off a photograph. A McDonald's order summary
    from a delivery app lists "1 x 2 Iced Coffee" and "2 x McSpicy Chicken
    Burger" with no prices against them at all - only the order total, INR
    749.54, printed once at the bottom. The model read exactly that: two
    lines, no amounts. This raised, the audit failed, and the claim was
    retried twice more and parked in `needs_human` - three vision calls to
    refuse a receipt whose total was legible the whole time, and a claimant
    left looking at a bill worth nothing.

    So an unreadable line amount is nought and the line is kept: the
    description is still worth showing a reviewer, and the printed grand total
    decides the claim anyway. Nothing is silently lost by this - a receipt
    with no line amounts *and* no printed total raises `nothing_read`, which
    says so in as many words.

    A cap that will not parse is a different thing entirely. It came from our
    own console, not from a photograph, and reading it as nought would turn
    "capped at 1,500" into "capped at nothing" and refuse every claim of that
    type. That stays an error.
    """
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        if signed:
            return Decimal("0.00")
        raise PolicyInputError(f"{field}: {value!r} is not a valid amount")
    if amount.is_nan() or amount.is_infinite():
        raise PolicyInputError(f"{field}: {value!r} is not a finite amount")
    if amount < 0 and not signed:
        raise PolicyInputError(f"{field}: amount may not be negative")
    return amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


# What a rule set may contain, for a set arriving from the console. Kept here
# beside the engine that reads it rather than in the request handler: a shape
# validated in one place and consumed in another drifts, and the consumer is
# the one deciding what somebody is paid.
MAX_TYPES = 40
MAX_LABEL = 40
# A description, not a caption. It is what the model is given when it decides
# which type a receipt belongs to, so it has room to name the things that
# belong under the type and the near-misses that do not.
MAX_HINT = 600

# One kind of cap: per transaction. It was a choice, and the per-head arm is
# what made the engine need a headcount it could only get by asking somebody.
CAP_KIND = "per_transaction"

# Money is handled to the paisa, and every figure this module returns is
# quantized to it before it leaves.
TWO_PLACES = Decimal("0.01")

# A receipt that printed no currency at all. Kept as a distinct value rather
# than an empty string so a cap lookup against it misses rather than matching
# something by accident.
NO_CURRENCY = "XXX"


class PolicyShapeError(ValueError):
    """A rule set as submitted was not usable."""


class PolicyInputError(ValueError):
    """A receipt as extracted could not be measured - a malformed amount."""


def _clean_type(raw: Any, where: str) -> dict[str, Any]:
    """One expense type, checked hard enough to be paid against.

    Every field is bounded and every unknown one is dropped. A rule set is the
    thing that decides what a person is reimbursed, and it arrives from a
    browser - so nothing in it is taken on trust, including the shape.
    """
    if not isinstance(raw, dict):
        raise PolicyShapeError(f"{where}: expected an expense type")

    type_id = re.sub(r"[^a-z0-9_]", "", str(raw.get("id", "")).strip().lower())[:60]
    if not type_id:
        raise PolicyShapeError(f"{where}: an expense type needs an id")

    label = str(raw.get("label", "")).strip()[:MAX_LABEL] or type_id
    # A stored rule from before there was one kind of cap may still say
    # `per_head`; its amounts are read as per-transaction rather than refused,
    # because a saved policy that will not load is worse than one whose meaning
    # has been simplified under it.
    raw_caps = raw.get("caps") or {}
    if isinstance(raw_caps.get(CAP_KIND), dict) or isinstance(raw_caps.get("per_head"), dict):
        raw_caps = raw_caps.get(CAP_KIND) or raw_caps.get("per_head") or {}

    caps: dict[str, str] = {}
    for code, value in raw_caps.items():
        code = str(code).strip().upper()[:3]
        if len(code) != 3:
            continue
        caps[code] = str(_money(value, f"{where}.caps.{code}"))

    return {
        "id": type_id,
        "label": label,
        "hint": str(raw.get("hint", "")).strip()[:MAX_HINT],
        "enabled": bool(raw.get("enabled", True)),
        "caps": {CAP_KIND: caps} if caps else {},
    }


def normalise_rules(raw: Any) -> dict[str, Any]:
    """A whole rule set on its way into storage.

    Refuses an empty one outright. A set with no enabled type means every
    receipt an organisation ever sends resolves to "no rule covers this" and
    lands in the review queue - the product still runs, but it has stopped
    doing the thing it is for, and it would have happened by somebody
    disabling four checkboxes without being told what that meant.
    """
    raw = raw or {}
    if not isinstance(raw, dict):
        raise PolicyShapeError("expected a rule set")

    types_in = raw.get("expense_types") or []
    if not isinstance(types_in, list):
        raise PolicyShapeError("expense_types: expected a list")
    if len(types_in) > MAX_TYPES:
        raise PolicyShapeError(f"a policy may hold at most {MAX_TYPES} expense types")

    seen, types = set(), []
    for i, one in enumerate(types_in):
        cleaned = _clean_type(one, f"expense_types[{i}]")
        if cleaned["id"] in seen:
            raise PolicyShapeError(f"two expense types share the id {cleaned['id']!r}")
        seen.add(cleaned["id"])
        types.append(cleaned)

    if not any(t["enabled"] for t in types):
        raise PolicyShapeError(
            "Leave at least one expense type enabled, or every receipt goes to "
            "the review queue with nothing to judge it by.")

    try:
        version = int(raw.get("version") or 1)
    except (TypeError, ValueError):
        version = 1

    return {"version": max(1, version), "expense_types": types}


def rules_for(org: dict[str, Any]) -> dict[str, Any]:
    """The rule set an organisation is judged by.

    `DEFAULT_RULES` for anybody who has never saved one - which is every
    account that existed before rules were stored, and every account on its
    first day. Falling back rather than seeding on read keeps the defaults in
    one place: an organisation that has not changed anything follows whatever
    the built-in set says today.
    """
    stored = (org or {}).get("rules")
    if not (isinstance(stored, dict) and stored.get("expense_types")):
        return DEFAULT_RULES

    # And a set saved while caps came in two kinds. A stored `per_head` cap is
    # read as the per-transaction cap it is now.
    #
    # Migrated on read for the same reason as the categories above, and with a
    # much sharper edge: the engine looks for `caps["per_transaction"]`, so a
    # policy still saying `per_head` reads as *no cap at all* - and a type with
    # no cap approves everything. A stored limit silently becoming no limit is
    # the one migration failure that costs money rather than tidiness.
    if any("per_head" in (t.get("caps") or {}) for t in stored["expense_types"]):
        stored = dict(stored, expense_types=[
            dict(t, caps={"per_transaction": t["caps"]["per_head"]})
            if "per_head" in (t.get("caps") or {}) else t
            for t in stored["expense_types"]])
    return stored


def expense_type_ids(rules: dict[str, Any]) -> list[str]:
    return [t["id"] for t in rules.get("expense_types", []) if t.get("enabled", True)]


def find_type(rules: dict[str, Any], type_id: str) -> Optional[dict[str, Any]]:
    for t in rules.get("expense_types", []):
        if t["id"] == type_id and t.get("enabled", True):
            return t
    return None


def _cap_for(spec: dict[str, Any], currency: str) -> Optional[Decimal]:
    raw = (spec or {}).get(currency)
    return _money(raw, "cap") if raw is not None else None


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_policy(
    currency: str,
    line_items: list[dict[str, Any]],
    expense_type: str = "meals",
    rules: Optional[dict[str, Any]] = None,
    stated_total: Any = None,
) -> dict[str, Any]:
    """Decide one receipt: clear it, or hand it to a person. Nothing in between.

    This engine used to compute how much of a claim was allowed - a per-head
    cap multiplied by a headcount it asked the claimant for, an itemisation
    requirement, a disallowed figure, a provisional figure, an assured figure,
    and four verdicts to describe the combinations. Every one of those was
    defensible on its own and together they made a product nobody could hold in
    their head: a reviewer looking at "reimbursable 0.00, provisional 3,705.00,
    assured 1,500.00" has to reconstruct the rules to know what it means, and
    the person who sent the receipt in cannot follow it at all.

    So the model is now one question with two answers. A claim is worth what
    the receipt says it is worth. Either nothing is in doubt and the agent
    clears it, or something is, and a named human decides - seeing the whole
    amount, not a computed fraction of it.

    Five things put it in front of a person, and each one is a fact rather than
    a judgment:

    * the policy has no enabled rule for what this is;
    * the total is over the cap for that rule;
    * the cap is not set in the currency the receipt is in;
    * (added by the auditor) it looks like a claim already made;
    * (added by the auditor) the submitter belongs to several groups and
      nothing on the bill says which.

    Deliberately *not* a reason: anything about how the bill is itemised or
    how many people it covered. Nobody is asked anything.
    """
    rules = rules or DEFAULT_RULES
    currency = (currency or NO_CURRENCY).upper().strip()
    violations: list[dict[str, Any]] = []
    decided: list[dict[str, Any]] = []

    rule = find_type(rules, expense_type)
    if rule is None:
        # An expense the policy has nothing to say about is a decision for a
        # human, not a default to the nearest rule.
        violations.append({
            "code": "no_rule_for_expense_type",
            "message": (
                f"No enabled rule covers expense type {expense_type!r}. "
                "A reviewer can approve it anyway; it cannot be paid until "
                "somebody sets a type the policy covers, or an administrator "
                "adds a rule for this one."
            ),
            "amount": None,
            "blocks_automatic_decision": True,
        })
        rule = {"id": expense_type, "label": expense_type, "caps": {}}

    receipt_total = Decimal("0.00")
    for index, raw in enumerate(line_items):
        description = str(raw.get("description", "")).strip() or f"line {index + 1}"
        amount = _money(raw.get("amount", 0), f"line_items[{index}].amount",
                        signed=True)
        receipt_total += amount
        # A line is what it says it is and what it cost. Nothing else here
        # decides money, so nothing else here is kept.
        decided.append({"description": description, "amount": str(amount)})

    # What the bill charges is what the bill says it charges.
    #
    # Two witnesses to the total: the grand total printed on the receipt, and
    # the sum of the lines the model read off it. The printed one wins, always.
    # It is what the vendor charged and what the card was debited; the lines
    # are our reading of it, and a reading that comes up short is a reason to
    # doubt the reading, not the bill.
    #
    # It no longer raises a finding. Under the old model the gap mattered
    # because lines could be struck out individually; now that a claim is worth
    # its printed total, a difference between the two is a note about our own
    # extraction and not something to put in front of a reviewer.
    printed = _stated(stated_total)
    if printed is not None:
        receipt_total = printed

    # Nothing was read off the bill at all.
    #
    # A blurred photograph of a handwritten bill came back with no line items
    # and no printed total - the model said so in its own words, "the
    # handwritten item details are too blurred to establish any covered
    # expense category reliably" - and an empty list sums to zero, so the
    # claim presented as a receipt for INR 0.00 that happened to have no
    # expense type. The only finding on it was `no_rule_for_expense_type`,
    # which pointed a reviewer at the expense type dropdown when the actual
    # problem was that there were no figures on the claim to type anything
    # about. The submitter was told "your claim for INR 0.00 has gone to your
    # finance team", which is not a sentence anybody should receive.
    #
    # Zero is a reading, and it is one no real receipt produces: a bill for
    # nothing is not a bill. So an empty reading is named as what it is, and
    # named first, because every other finding about a claim with no figures
    # is noise on top of it.
    #
    # Checked after `printed` is applied, so a receipt whose lines were
    # unreadable but whose grand total was legible is a normal claim.
    # Zero, whether or not descriptions came back with it.
    #
    # This asked for *no lines at all*, which was true until a line with no
    # price stopped being an error and started being nought. A delivery
    # summary listing two items and no prices, with no grand total either,
    # then produced a claim for 0.00 carrying two line descriptions and no
    # finding whatsoever - the exact silence this exists to break, restored by
    # the fix above. A line with no amount on it is not an amount.
    if receipt_total == 0 and printed is None:
        # First in the list, not last. `no_rule_for_expense_type` was appended
        # before this point and is the one a reviewer sees at the top - and on
        # a claim with no figures it is advice about the wrong control.
        violations.insert(0, {
            "code": "nothing_read",
            "message": (
                "Nothing could be read off this receipt - no line items and no "
                "printed total. The photograph is likely too blurred, too dark "
                "or cropped. Somebody has to read the bill itself and decide."
            ),
            "amount": None,
            "blocks_automatic_decision": True,
        })

    # A bill cannot come to less than nothing.
    #
    # Lines may be negative one at a time; their sum may not. A total below
    # zero means the reading is wrong - a credit note read as an invoice, a
    # minus sign hallucinated onto the largest line - and paying out against
    # it, or capping against it, would both be arithmetic on a fiction. The
    # claim is worth nothing and goes to a person.
    if receipt_total < 0:
        violations.append({
            "code": "negative_total",
            "message": (
                f"The lines add up to {receipt_total}, which is less than "
                "nothing. The receipt has been misread, or it is a credit "
                "note rather than a bill. A reviewer should look at it."
            ),
            "amount": None,
            "blocks_automatic_decision": True,
        })
        receipt_total = Decimal("0.00")

    # ---- the cap ----------------------------------------------------------
    #
    # One cap per expense type, per transaction. Per-head caps are gone with
    # the headcount that fed them: a rule whose answer depends on a fact the
    # receipt does not print is a rule that has to interrogate somebody.
    caps = (rule.get("caps") or {}).get("per_transaction", {})
    cap = _cap_for(caps, currency) if caps else None

    if (rule.get("caps") or {}) and cap is None:
        violations.append({
            "code": "unsupported_currency",
            "message": (
                f"{rule['label']} has no cap defined in {currency}. "
                "Refusing to apply an invented exchange rate."
            ),
            "amount": None,
            "blocks_automatic_decision": True,
        })

    if cap is not None and receipt_total > cap:
        # Over the cap goes to a person whole, not part paid.
        #
        # It used to reimburse the cap and disallow the rest, which is the
        # "what is allowed and what is not" arithmetic this engine no longer
        # does. A bill over the limit is a judgment - there may be a good
        # reason for it - and quietly paying part of it makes that judgment on
        # the reviewer's behalf while telling the claimant they were docked.
        violations.append({
            "code": "cap_exceeded",
            "message": (
                f"{receipt_total} {currency} is over the {rule['label']} cap of "
                f"{cap} {currency} per transaction. A reviewer decides whether "
                "to approve it."
            ),
            "amount": str(receipt_total - cap),
            "blocks_automatic_decision": True,
        })

    blocked = any(v["blocks_automatic_decision"] for v in violations)
    verdict = "needs_review" if blocked else "approved"

    # One figure. A claim is worth what the receipt says, and the only question
    # is who says yes to it.
    total = str(receipt_total.quantize(TWO_PLACES))
    return {
        "verdict": verdict,
        "expense_type": rule["id"],
        "expense_type_label": rule.get("label", rule["id"]),
        "currency": currency,
        "receipt_total": total,
        "reimbursable_total": total,
        "cap_total": str(cap) if cap is not None else None,
        "violations": violations,
        "line_items": decided,
        "policy_version": f"v{rules.get('version', 1)}",
    }


def awaiting_payment(item: dict) -> bool:
    """Whether this claim has cleared and is waiting for the money.

    One rule, in one place, because three things ask it and they were drifting
    apart. The console's `payableClaims` builds the Pending settlement list
    from it; `digest._cleared_at` decides who finance is emailed about; and
    `_claim_review` uses it to tell a rejection *at settlement* - which is
    finance declining to pay - from a rejection *at review*, which is an
    owner's or an administrator's decision about what the company owes. Three
    implementations of one sentence is how a tab, an email and a permission
    come to disagree about the same claim.

    Two ways in and both count: a person approved it after review, or the agent
    cleared it against the policy and nobody had to. Out again: a second
    document of a claim is not a second thing to pay, a settled one is not
    waiting, and a rejected one is the opposite outcome rather than a payment
    of nothing.

    Deliberately says nothing about *when*. The callers that need a timestamp
    take it from the row themselves, because they take different ones - the
    review stamp for a claim a person approved, the audit stamp for one the
    agent released.
    """
    if str(item.get("companion_of") or ""):
        return False
    if str(item.get("outcome") or ""):
        return False

    action = str(item.get("review_action") or "")
    if action == "approved":
        return True
    if action:
        return False

    return agent_would_release(item)


def agent_would_release(item: dict) -> bool:
    """Whether the engine's own verdict clears this claim without a person.

    Pulled out of `awaiting_payment` because two callers need the question
    asked at different moments. `awaiting_payment` is about *now* - it stops
    at a claim that has already been paid, because such a claim is no longer
    waiting for money. The auto-clear rate is about what happened when the
    receipt was read, which does not stop being true once finance pays it.
    """
    verdict = item.get("verdict") or {}
    if str(verdict.get("verdict") or "") not in ("approved", "partially_approved"):
        return False
    return not any(v.get("blocks_automatic_decision")
                   for v in (verdict.get("violations") or []))


def cleared_by(item: dict) -> str:
    """Who cleared this claim: `"agent"`, `"human"`, or `""` if nobody has.

    The distinction the auto-clear rate is made of, and the reason that rate
    once read 69% in one tile and 20% in another: counting every claim that
    *reached* a cleared stage answers "how diligently is the queue worked",
    not "how much of the work does the agent take off a person". A person's
    approval is a person's approval however automatic the stage it leads to.

    Deliberately not asked through `awaiting_payment`, which was the first
    thing tried and was wrong in a way that flattered nobody: that predicate
    goes false the moment an `outcome` is written, so every claim the agent
    released and finance then paid came back as cleared by no one. On a live
    account it reported five agent clearances against twenty-nine human ones
    for October and an auto-clear rate of 10%, with fourteen settled claims
    counted as undecided - the same error as the 69% reading, in the other
    direction.
    """
    action = str(item.get("review_action") or "")
    if action == "approved":
        return "human"
    if action:
        # Rejected or withdrawn. A decision was made, but nothing was cleared.
        return ""
    if str(item.get("companion_of") or ""):
        return ""
    return "agent" if agent_would_release(item) else ""


def owed(item: dict) -> tuple[str, str]:
    """What a settlement would pay for this claim, in the currency it leaves in.

    A payout leaves the account in the organisation's own currency however many
    currencies the receipts were in, converted at the rate stamped on the claim
    when it was audited - never at today's, because an amount owed that moves
    between the day it was approved and the day it is paid is not an amount
    owed.

    What a reviewer released comes first and the engine's figure second, which
    is the order the console uses: a claim approved over a cap pays what the
    person approved, and reading the engine's figure instead would report nil
    for every overridden claim. Five approved claims worth Rs 9,004.63 were
    once invisible to finance because `approved_total` had been stored as zero.

    An empty amount is a foreign claim whose rate could not be had. The figure
    is unknown rather than zero, and every caller says so rather than
    inventing one.

    Lives here rather than in `digest`, which is where it was written, because
    the finance digest is no longer the only thing that has to agree with the
    console about what a claim is worth - `capabilities` now answers the same
    question out loud.
    """
    verdict = item.get("verdict") or {}
    payout = item.get("payout_value") or {}
    home = str(payout.get("currency") or "").upper()
    claim_ccy = str(verdict.get("currency") or "").upper()

    raw = item.get("approved_total")
    if raw in (None, ""):
        raw = verdict.get("reimbursable_total")
    try:
        amount = Decimal(str(raw or "0"))
    except (ArithmeticError, ValueError):
        return ("", claim_ccy or home)

    if not home or not claim_ccy or claim_ccy == home:
        return (f"{amount:.2f}", home or claim_ccy)
    try:
        rate = Decimal(str(payout.get("rate") or "0"))
    except (ArithmeticError, ValueError):
        rate = Decimal(0)
    if rate <= 0:
        return ("", claim_ccy)
    return (str((amount * rate).quantize(Decimal("0.01"))), home)


def stage(item: dict) -> str:
    """Where this claim has got to, in the console's own vocabulary.

    One of `companion`, `withdrawn`, `rejected`, `settled`, `part_settled`,
    `approved` or `in_review` - the same seven the console's `claimStage`
    returns, because a figure spoken aloud with no screen to check it against
    has to be the figure on the screen.

    Two field names carry the endings and they are easy to transpose:
    `withdrawn` is a **review action**, taken by the submitter before anybody
    judged anything, while `outcome` is only ever `settled` or `rejected` and
    belongs to the settlement stage. A rejection can therefore arrive from
    either - a reviewer deciding the company does not owe it, or finance
    declining to pay something already approved - and both end the claim.

    `approved` is deliberately `awaiting_payment` rather than a test of its
    own, so this cannot come to disagree with the Pending settlement list
    about which claims are waiting.
    """
    if str(item.get("companion_of") or ""):
        return "companion"

    action = str(item.get("review_action") or "")
    if action in ("withdrawn", "rejected"):
        return action

    if str(item.get("outcome") or "") == "rejected":
        return "rejected"

    if str(item.get("outcome") or "") == "settled":
        paid = _stated(item.get("outcome_paid")) or Decimal(0)
        due = _stated(owed(item)[0])
        # Finance recorded a payment, so the claim is closed whatever the
        # arithmetic says - the only open question is whether it was paid in
        # full. When the amount owed cannot be read there is nothing to
        # compare against, and the two wrong answers are both worse than
        # "settled": `part_settled` leaves it looking like finance still owes
        # money, and falling through to `in_review` would put a closed claim
        # back into the review queue.
        if due is None:
            return "settled"
        return "settled" if paid >= due else "part_settled"

    return "approved" if awaiting_payment(item) else "in_review"
