"""The number a person quotes when they ring up about a receipt.

`sub_1789455641368_806298` is an identifier, not a reference. Nobody reads it
down a phone, nobody types it into a search box, and nobody recognises it on a
bank statement. So every receipt also gets something short and sequential:

    Mobil80-Exp-1, Mobil80-Exp-2, ...

Two decisions in that shape, and both are about what happens later.

**The prefix is pinned to the organisation, once.** It is derived from the
name at sign-up and then never recomputed. Deriving it live from the default
group would have been fewer moving parts until the day somebody renamed the
group, at which point every reference issued after it has a different shape
from every reference issued before - and a finance team cannot search on a
prefix that changed halfway through the year.

**The number comes from the same write that spends the credit.** One receipt,
one credit, one reference: they cannot drift, because there is no second write
to fail. Two receipts arriving in the same millisecond get consecutive numbers
from DynamoDB's own `ADD`, not from a read-then-write that would hand them
both the same one.

Gaps are possible and are not a problem. A receipt refused for want of credit
never reaches the counter; nothing else consumes a number. What matters is
that two receipts never share one, and `ADD` guarantees that.
"""
from __future__ import annotations

import re
from typing import Any

# Long enough for a company name somebody recognises, short enough that the
# whole reference stays quotable.
MAX_PREFIX = 12
FALLBACK_PREFIX = "Exp"

# What sits between the prefix and the number. Fixed rather than configurable:
# it is there to make the string recognisable as ours at a glance, and a
# per-customer separator would defeat that for no gain.
INFIX = "Exp"


def prefix_for(org_name: str) -> str:
    """A short tag from the organisation's name.

    The first word, because that is what people call the company: "Mobil80
    Solutions and Services Pvt Ltd" is Mobil80 to everybody who works there.
    Letters and digits only - a reference travels through spreadsheets, bank
    narration fields and URLs, and an ampersand or a space in it will be
    mangled by at least one of them.
    """
    words = re.findall(r"[A-Za-z0-9]+", org_name or "")
    if not words:
        return FALLBACK_PREFIX
    first = words[0][:MAX_PREFIX]
    # A leading word that is purely a digit reads as part of the number that
    # follows it, so take the next word too where there is one.
    if first.isdigit() and len(words) > 1:
        first = (first + words[1])[:MAX_PREFIX]
    return first


def build(prefix: str, seq: Any) -> str:
    """`Mobil80-Exp-41`, or nothing if there is no number to build it from."""
    try:
        number = int(seq)
    except (TypeError, ValueError):
        return ""
    if number < 1:
        return ""
    return f"{(prefix or FALLBACK_PREFIX).strip()}-{INFIX}-{number}"


def of(org: dict[str, Any], seq: Any) -> str:
    """The reference for one receipt, from the organisation row and a number.

    Falls back to deriving the prefix from the name for an organisation that
    predates this - which is every organisation that existed before today, and
    is why the derivation has to stay deterministic.
    """
    prefix = str((org or {}).get("ref_prefix") or "").strip()
    if not prefix:
        prefix = prefix_for(str((org or {}).get("name") or ""))
    return build(prefix, seq)
