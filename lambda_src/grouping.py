"""Which group a bill is made out to, read off the bill itself.

A group is a cost centre - a subsidiary, a branch, a brand. Most people belong
to exactly one and their receipts are tagged without anybody thinking about it.
The question only becomes a question for somebody who belongs to several, and
it used to be answered by messaging them a list to tap: an accounting question
put to the person who photographed a bill, about a budget that is not theirs to
choose from. Nothing asks them now. The bill is read, and whatever the bill does
not settle goes to the reviewer who is going to approve the claim anyway.

Two things on a tax invoice answer it outright:

* **The buyer's tax registration.** Exact, after normalisation - see taxid.py.
  A registration identifies one entity and nothing else does it as well.
* **The buyer's name.** Weaker, so it is held to the same standard: reduced to
  what identifies it and then matched exactly. "Mobil80 Solutions & Services
  Pvt. Ltd." and "MOBIL80 SOLUTIONS AND SERVICES PRIVATE LIMITED" are the same
  company; "Mobil80" and "Mobil80 Logistics" are not, and are not treated as
  though they were.

**Ambiguity is never resolved by guessing.** Two groups that match equally leave
the answer open, exactly as no match does. The principle is taxid.py's and it
is worth repeating here, because the temptation is stronger with names: a
wrongly attributed expense is worse than an unattributed one, because the
second is visibly unfinished and the first looks like an answer. An unset group
stops a claim and puts it in front of a person; a confidently wrong one is paid
out of the wrong budget and nobody ever looks again.

No fuzzy matching, no edit distance, no "closest" group. Everything this cannot
settle is a question for the reviewer, and the reviewer has one dropdown.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import taxid

# Dropped before comparing, because they are how a name is written rather than
# which company it is. A bill saying "Pvt Ltd" and a group recorded as "Private
# Limited" are the same entity written twice.
_SUFFIXES = {
    "pvt", "private", "ltd", "limited", "llp", "llc", "inc", "incorporated",
    "corp", "corporation", "co", "company", "plc", "gmbh", "bv", "sa", "srl",
    "pte", "sdn", "bhd", "fzco", "fze", "lda", "ag", "nv", "oy", "ab", "as",
}

# "&" and "and" are the same word on a printed bill.
_AND = re.compile(r"\s*&\s*")
_NOISE = re.compile(r"[^a-z0-9]+")

# Short enough to be a person's initials or an OCR fragment rather than a
# company. Matching on two characters attributes a receipt on a coincidence.
MIN_NAME = 4


def normalise_name(value: Any) -> str:
    """A company name reduced to what identifies it, or empty.

    Empty is an ordinary answer. Most receipts name no buyer, everything read
    before the field existed has none, and a group may be recorded without a
    name to match against.
    """
    if value is None:
        return ""
    text = _AND.sub(" and ", str(value).lower())
    words = [w for w in _NOISE.sub(" ", text).split() if w]
    # The suffix set only applies at the end. "Company Kitchens" is a name;
    # "Kitchens Company" is the same company as "Kitchens".
    while words and words[-1] in _SUFFIXES:
        words.pop()
    joined = "".join(words)
    return joined if len(joined) >= MIN_NAME else ""


def group_for(groups: list[dict[str, Any]], receipt: dict[str, Any]) -> tuple[str, str]:
    """The group this bill is billed to, and what said so.

    Returns `(group_id, how)`, or `("", "")` when the bill does not say.
    `how` is one of `tax_id` or `buyer_name`, and it is carried onto the claim
    so the console can tell a reviewer *why* it is tagged - an attribution
    nobody can account for is one nobody can correct with any confidence.

    The registration is tried first and wins outright. It identifies an entity;
    a name is how somebody typed one, and where the two disagree the number is
    the one to believe.
    """
    matched = taxid.group_for(groups, (receipt or {}).get("buyer_tax_id"))
    if matched:
        return matched, "tax_id"

    wanted = normalise_name((receipt or {}).get("buyer_name"))
    if not wanted:
        return "", ""

    hits = [g["id"] for g in (groups or [])
            if g.get("id") and normalise_name(g.get("label")) == wanted]
    # One, or nothing. Two groups whose names reduce to the same string cannot
    # be told apart by the name, and picking either would make the attribution
    # depend on the order somebody happened to add them in.
    return (hits[0], "buyer_name") if len(hits) == 1 else ("", "")
