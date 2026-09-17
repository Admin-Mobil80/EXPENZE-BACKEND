"""Which currency a receipt is in, when the receipt does not say.

Plenty of real receipts never print a currency. A handwritten bill from a
kirana shop or a dhaba is a pad of paper with figures on it and no ISO code
anywhere - and the figures are the whole claim. Rejecting those is not an
option, so something has to supply the missing currency.

The answer is the organisation's own currency, taken from the country it
registered in. That is right far more often than any guess from the numbers,
and unlike a guess it is a setting a customer can see and change.

Two rules keep the substitution honest:

**A printed currency always wins.** If the receipt states INR, or carries a
symbol only one currency uses, that is the currency - the organisation default
never overrides what the document actually says.

**An assumed currency is labelled as assumed.** The reviewer sees that nothing
on the paper established it. An unlabelled assumption is the dangerous kind:
reading a $180 software invoice as ₹180 puts it three orders of magnitude under
the cap and clears it silently.

Ambiguous symbols are the interesting middle. "Rs." is India, Pakistan, Sri
Lanka and Nepal; "$" is a dozen countries. There the organisation's own
currency is the tie-breaker, but only when it is actually one of the
candidates - an Indian company's receipt marked "$" is not INR, it is someone
who travelled, and saying otherwise would be worse than admitting the doubt.
"""
from __future__ import annotations

from typing import Any, Optional

# Where a customer can register. Kept small and explicit: free-text countries
# are unusable for tax logic later, and every entry here needs a currency.
COUNTRIES = {
    "IN": "India", "SG": "Singapore", "AE": "United Arab Emirates",
    "US": "United States", "GB": "United Kingdom", "AU": "Australia",
    "CA": "Canada", "DE": "Germany", "FR": "France", "NL": "Netherlands",
    "IE": "Ireland", "MY": "Malaysia", "PH": "Philippines", "ID": "Indonesia",
    "LK": "Sri Lanka", "BD": "Bangladesh", "SA": "Saudi Arabia", "QA": "Qatar",
    "ZA": "South Africa", "NZ": "New Zealand", "JP": "Japan", "OTHER": "Elsewhere",
}

COUNTRY_CURRENCY = {
    "IN": "INR", "SG": "SGD", "AE": "AED", "US": "USD", "GB": "GBP",
    "AU": "AUD", "CA": "CAD", "DE": "EUR", "FR": "EUR", "NL": "EUR",
    "IE": "EUR", "MY": "MYR", "PH": "PHP", "ID": "IDR", "LK": "LKR",
    "BD": "BDT", "SA": "SAR", "QA": "QAR", "ZA": "ZAR", "NZ": "NZD",
    "JP": "JPY",
    # "Elsewhere" is the one country entry with no currency of its own. USD is
    # the least surprising neutral answer, and it is visible and changeable.
    "OTHER": "USD",
}

# `decimals` is not decoration: a yen amount written 3800 is 3800 yen, and
# dividing it by 100 the way a minor-unit assumption would is a 100x error.
CURRENCIES: dict[str, dict[str, Any]] = {
    "INR": {"name": "Indian rupee",        "symbol": "₹",  "decimals": 2},
    "USD": {"name": "US dollar",           "symbol": "$",       "decimals": 2},
    "EUR": {"name": "Euro",                "symbol": "€",  "decimals": 2},
    "GBP": {"name": "Pound sterling",      "symbol": "£",  "decimals": 2},
    "SGD": {"name": "Singapore dollar",    "symbol": "S$",      "decimals": 2},
    "AED": {"name": "UAE dirham",          "symbol": "AED",     "decimals": 2},
    "AUD": {"name": "Australian dollar",   "symbol": "A$",      "decimals": 2},
    "CAD": {"name": "Canadian dollar",     "symbol": "C$",      "decimals": 2},
    "NZD": {"name": "New Zealand dollar",  "symbol": "NZ$",     "decimals": 2},
    "HKD": {"name": "Hong Kong dollar",    "symbol": "HK$",     "decimals": 2},
    "MYR": {"name": "Malaysian ringgit",   "symbol": "RM",      "decimals": 2},
    "PHP": {"name": "Philippine peso",     "symbol": "₱",  "decimals": 2},
    "IDR": {"name": "Indonesian rupiah",   "symbol": "Rp",      "decimals": 2},
    "LKR": {"name": "Sri Lankan rupee",    "symbol": "Rs",      "decimals": 2},
    "NPR": {"name": "Nepalese rupee",      "symbol": "Rs",      "decimals": 2},
    "PKR": {"name": "Pakistani rupee",     "symbol": "Rs",      "decimals": 2},
    "BDT": {"name": "Bangladeshi taka",    "symbol": "৳",  "decimals": 2},
    "SAR": {"name": "Saudi riyal",         "symbol": "SAR",     "decimals": 2},
    "QAR": {"name": "Qatari riyal",        "symbol": "QAR",     "decimals": 2},
    "ZAR": {"name": "South African rand",  "symbol": "R",       "decimals": 2},
    "JPY": {"name": "Japanese yen",        "symbol": "¥",  "decimals": 0},
    "CNY": {"name": "Chinese yuan",        "symbol": "¥",  "decimals": 2},
}

# What a mark on the paper could mean. Order matters: the first candidate is
# the one used when nothing else can break the tie.
SYMBOL_CANDIDATES: dict[str, list[str]] = {
    "₹": ["INR"],
    "rs": ["INR", "PKR", "LKR", "NPR"],
    "rs.": ["INR", "PKR", "LKR", "NPR"],
    # The rupee abbreviation as it is actually written across India.
    "रु": ["INR", "NPR"],           # Devanagari
    "रू": ["INR", "NPR"],
    "ரூ": ["INR", "LKR"],           # Tamil
    "రూ": ["INR"],                  # Telugu
    "ರೂ": ["INR"],                  # Kannada
    "രൂ": ["INR"],                  # Malayalam
    "રૂ": ["INR"],                  # Gujarati
    "টা": ["BDT", "INR"],           # Bengali taka/tanka
    "৳": ["BDT"],
    "$": ["USD", "SGD", "AUD", "CAD", "NZD", "HKD"],
    "us$": ["USD"], "s$": ["SGD"], "a$": ["AUD"], "c$": ["CAD"],
    "nz$": ["NZD"], "hk$": ["HKD"],
    "£": ["GBP"],
    "€": ["EUR"],
    "¥": ["JPY", "CNY"],
    "rm": ["MYR"],
    "₱": ["PHP"],
    "rp": ["IDR"],
    "د.إ": ["AED"],
    "﷼": ["SAR", "QAR"],                # ﷼
    "r": ["ZAR"],
}

# Deliberately empty. An organisation that has set neither a currency nor a
# country has told us nothing, and inventing one for it is an 80x error waiting
# to happen - a 10,000 rupee bill read as 10,000 dollars clears nothing and
# rejects everything. No currency routes the receipt to a human instead, which
# is the honest outcome when nobody has said what money this company counts in.
FALLBACK = ""


def normalise(code: Any) -> str:
    return str(code or "").strip().upper()


def is_currency(code: Any) -> bool:
    return normalise(code) in CURRENCIES


def for_country(country: Any) -> str:
    """The currency an organisation registered in this country most likely uses.

    Empty for a country we have no entry for, and for no country at all.
    """
    return COUNTRY_CURRENCY.get(str(country or "").strip().upper(), "")


def default_for_org(org: dict[str, Any]) -> str:
    """An organisation's own currency.

    An explicit setting wins over the country it registered in - a company can
    be incorporated in Singapore and account in US dollars, and having chosen
    that once they should not have to keep choosing it.
    """
    chosen = normalise(org.get("default_currency"))
    if chosen in CURRENCIES:
        return chosen
    return for_country((org.get("address") or {}).get("country"))


def candidates_for(evidence: Any) -> list[str]:
    """Which currencies a mark seen on the receipt could denote."""
    mark = str(evidence or "").strip().lower().rstrip(".").strip()
    if not mark:
        return []
    for key in (mark, mark + "."):
        if key in SYMBOL_CANDIDATES:
            return SYMBOL_CANDIDATES[key]
    # A bare code written into the evidence field, e.g. "INR".
    if normalise(mark) in CURRENCIES:
        return [normalise(mark)]
    return []


def label(code: Any) -> str:
    code = normalise(code)
    meta = CURRENCIES.get(code)
    return f"{code} — {meta['name']}" if meta else code


def resolve(
    code: Any,
    source: Any,
    evidence: Any,
    org_default: Any,
) -> dict[str, Any]:
    """Settle the currency of one receipt.

    `source` is what the model reports about where the currency came from:
    `printed_code`, `unambiguous_symbol`, `ambiguous_symbol` or `absent`.

    Returns the currency actually used, whether it was assumed rather than
    read, and a sentence a reviewer can act on.
    """
    org_default = normalise(org_default)
    if org_default not in CURRENCIES:
        org_default = ""

    code = normalise(code)
    source = str(source or "").strip().lower()
    evidence_text = str(evidence or "").strip()

    # Older payloads carry no source at all; infer one rather than refuse.
    if not source:
        source = "printed_code" if code in CURRENCIES else "absent"

    if source in ("printed_code", "unambiguous_symbol") and code in CURRENCIES:
        return {
            "currency": code,
            "assumed": False,
            "source": source,
            "evidence": evidence_text,
            "note": "",
        }

    if source == "ambiguous_symbol":
        candidates = candidates_for(evidence_text) or ([code] if code in CURRENCIES else [])

        # A mark only one currency uses is not ambiguous, whatever the model
        # called it. "S$" is Singapore and nowhere else; flagging that as an
        # assumption trains reviewers to click past the flag that matters.
        if len(candidates) == 1:
            return {
                "currency": candidates[0],
                "assumed": False,
                "source": "unambiguous_symbol",
                "evidence": evidence_text,
                "note": "",
            }

        if candidates:
            # The organisation's own currency breaks the tie, but only when it
            # is genuinely one of the possibilities.
            if org_default in candidates:
                chosen, why = org_default, "your organisation's own currency"
            elif code in candidates:
                chosen, why = code, "the closest match on the receipt"
            else:
                chosen, why = candidates[0], "the most common currency using that symbol"
            return {
                "currency": chosen,
                "assumed": True,
                "source": source,
                "evidence": evidence_text,
                "note": (f"{evidence_text or 'That symbol'} is used by several currencies. "
                         f"Read as {chosen} — {why}. Change it if the receipt was in another."),
            }

    # Nothing on the paper settles it. Common on handwritten bills, and exactly
    # what the organisation default exists for - when there is one.
    if not org_default:
        return {
            "currency": "",
            "assumed": True,
            "source": "absent",
            "evidence": evidence_text,
            "note": ("No currency is printed on this receipt, and your organisation "
                     "has not set one. Set a country or a default currency under "
                     "Organisation, or set the currency on this claim."),
        }
    return {
        "currency": org_default,
        "assumed": True,
        "source": "absent",
        "evidence": evidence_text,
        "note": (f"No currency is printed on this receipt. Read as {org_default}, "
                 "your organisation's default. Change it if that is wrong."),
    }
