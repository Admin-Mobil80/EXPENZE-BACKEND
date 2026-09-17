"""Tax registrations, compared the way a person would compare them.

A GSTIN is printed on a bill in whatever way the vendor's software felt like
printing it: `29AABCU9603R1ZM`, `29 AABCU9603R 1ZM`, `GSTIN: 29aabcu9603r1zm`.
They are the same registration. Comparing the strings as they arrive answers
"no" to every one of those pairs, which for this module's whole purpose - is
this receipt billed to us - is the wrong answer every time.

So a registration is reduced to the characters that identify it: letters and
digits, upper-cased. Nothing else about the format is assumed, because this has
to hold for a VAT number, an EIN, a TRN and whatever the next country calls it.

**Matching is exact after that, deliberately.** A partial or prefix match would
attribute a receipt to a cost centre on the strength of a shared state code,
and a wrongly attributed expense is worse than an unattributed one: the second
is visibly unfinished, the first looks like an answer.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Long enough for every registration format in use, short enough that a
# paragraph of OCR noise cannot become one.
MAX_LEN = 24
MIN_LEN = 5

_STRIP = re.compile(r"[^A-Za-z0-9]+")

# Words that travel with the number on a printed bill and are not part of it.
_LABELS = re.compile(
    r"^(GSTIN|GSTNO|GSTINNO|GST|VATNO|VAT|TAXID|TIN|TRN|EIN|PAN|ABN|UID)", re.I)


def normalise(value: Any) -> str:
    """A registration reduced to what identifies it, or empty.

    Empty is an ordinary answer, not a failure: most receipts print no
    registration at all, and everything read before this field existed has
    none. Whatever cannot be reduced to a plausible registration comes back
    empty rather than half-parsed, because a half-parsed one that happens to
    match something is the outcome this module exists to avoid.
    """
    if value is None:
        return ""
    cleaned = _STRIP.sub("", str(value)).upper()
    if not cleaned:
        return ""
    # `GSTIN: 29AABCU9603R1ZM` collapses to `GSTIN29AABCU9603R1ZM`; the label
    # is dropped, but only when something plausible is left underneath it.
    stripped = _LABELS.sub("", cleaned)
    if MIN_LEN <= len(stripped) <= MAX_LEN:
        cleaned = stripped
    if not (MIN_LEN <= len(cleaned) <= MAX_LEN):
        return ""
    # A registration always carries digits. A word does not, and OCR reads
    # plenty of words where a number should be.
    if not any(c.isdigit() for c in cleaned):
        return ""
    return cleaned


def same(left: Any, right: Any) -> bool:
    """Whether two registrations are the same one. Empty never matches."""
    a, b = normalise(left), normalise(right)
    return bool(a) and a == b


def group_for(groups: list[dict[str, Any]], receipt_tax_id: Any) -> Optional[str]:
    """The group a receipt billed to this registration belongs to.

    `None` when it matches nothing, which is the common case and not an error -
    most bills carry no registration, and an organisation may register none.

    Ambiguity is also `None`. Two groups sharing a registration cannot be told
    apart by it, and picking the first would make the attribution depend on the
    order somebody happened to add them in. Better to leave it to the person
    who knows.
    """
    wanted = normalise(receipt_tax_id)
    if not wanted:
        return None
    hits = [g["id"] for g in (groups or [])
            if g.get("id") and normalise(g.get("tax_id")) == wanted]
    return hits[0] if len(hits) == 1 else None
