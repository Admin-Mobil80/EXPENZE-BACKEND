"""Nothing the console calls may have been deleted out from under it.

`site/app.html` holds 7,000 lines of hand-written JavaScript, and a syntax
check says nothing about whether the names in it still exist. Removing a
feature means removing declarations, and a slice that runs from one anchor to
the next takes whatever happens to sit between them.

That happened twice in one afternoon, both silently:

* `decide` - the function every Approve, Reject, Reopen, Send back and Withdraw
  goes through - was removed along with the headcount control that happened to
  precede it. `node --check` passed. Every decision button was dead.
* `COUNTRY_CURRENCY` and `CURRENCY_NAME` went with the old policy port, because
  the replacement ran to the next `function` keyword and the tables sat in
  between. `orgCurrency()` threw a ReferenceError, that threw out of
  `renderAll()`, and so `loadRecords()` never ran - a console showing a
  signed-in user, every tab, and no data at all.

Deliberately not clever. An earlier version of this file tried to strip string
and template literals so it could reason about the whole source, and got it
wrong in the direction that matters: the stripper swallowed real declarations
and the test reported fifteen things missing that were all present. A check
that cries wolf is one people delete. So: comments out, and two narrow
questions asked precisely.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def console_js():
    src = open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8").read()
    return "\n".join(re.findall(r"<script>([\s\S]*?)</script>", src))


def without_comments(js):
    js = re.sub(r"/\*[\s\S]*?\*/", " ", js)
    return re.sub(r"(?m)//[^\n]*", " ", js)


# Names the page gets from the browser rather than declaring itself.
HOST = set("""
window document location localStorage sessionStorage navigator console fetch
setTimeout setInterval clearTimeout clearInterval requestAnimationFrame alert
confirm prompt escape unescape parseInt parseFloat isNaN isFinite structuredClone
encodeURIComponent decodeURIComponent encodeURI decodeURI queueMicrotask atob btoa
if for while switch catch return typeof function do else new delete void await
async of in instanceof throw try
""".split())


class EveryFunctionTheConsoleCallsIsDefined(unittest.TestCase):
    """The `decide` case."""

    def setUp(self):
        self.js = without_comments(console_js())

    def test_no_call_has_lost_its_target(self):
        called = set(re.findall(r"(?<![.\w$'\"])([a-z_$][\w$]*)\s*\(", self.js))
        defined = set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", self.js))
        defined |= set(re.findall(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>)",
            self.js))
        # Locally bound: parameters, arrow bindings, catch clauses, methods.
        local = set(re.findall(r"([A-Za-z_$][\w$]*)\s*=>", self.js))
        local |= set(re.findall(r"function[^(]*\(([^()]*)\)", self.js)) and set(
            n for params in re.findall(r"function[^(]*\(([^()]*)\)", self.js)
            for n in re.findall(r"[A-Za-z_$][\w$]*", params))
        local |= set(n for params in re.findall(r"\(([^()]*)\)\s*=>", self.js)
                     for n in re.findall(r"[A-Za-z_$][\w$]*", params))
        local |= set(re.findall(r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)", self.js))
        # Anything reached through an object is that object's business.
        local |= set(re.findall(r"\.([a-z_$][\w$]*)\s*\(", self.js))
        # Words inside string and template literals read as calls; a name that
        # appears nowhere but a literal is prose, not a reference.
        literal = set(re.findall(r"[`\"'][^`\"'\n]*?\b([a-z_$][\w$]*)\s*\(", self.js))

        missing = sorted(called - defined - local - literal - HOST)
        self.assertEqual([], missing,
                         "called but never defined: " + ", ".join(missing))


class TheTablesAndPathsThatBrokeAreNamed(unittest.TestCase):
    """Narrow and explicit, because these are the ones that actually went.

    A general "is every module-level constant declared" check needs to reason
    about literals to avoid false positives, and getting that wrong is worse
    than not having it. These are the declarations whose loss took the console
    down; each is asserted by name and each is cheap to keep true.
    """

    TABLES = ["COUNTRY_CURRENCY", "CURRENCY_NAME", "CHANNELS", "CHANNEL_MARK",
              "MONTHS", "PAY_MODES", "STAGE_LABEL", "TIER_NAMES",
              "REPORT_SECTIONS", "REVIEW_ACTION", "AUTO_RELEASED"]

    STATE = ["SUBMISSIONS", "PEOPLE", "ORG_PROFILE", "ORG"]

    def setUp(self):
        self.js = console_js()

    def test_every_lookup_table_is_declared(self):
        for name in self.TABLES:
            self.assertRegex(self.js, rf"\b(?:const|let)\s+{name}\b",
                             f"{name} is read but no longer declared")

    def test_every_piece_of_module_state_is_declared(self):
        for name in self.STATE:
            self.assertRegex(self.js, rf"\b(?:const|let)\s+{name}\b",
                             f"{name} is read but no longer declared")

    def test_the_decision_path_is_whole(self):
        # The one that pays people.
        self.assertIn("async function decide(", self.js)
        self.assertIn('decide(sub, "Withdrawn"', self.js, "nothing triggers Withdrawn")
        # Every name the console can send has to be one the server takes.
        sendable = set(re.findall(r"const REVIEW_ACTION = \{(.*?)\};", self.js, re.S))
        for name in re.findall(r"decide\(sub, \"([^\"]+)\"", self.js):
            self.assertTrue(any(f"{name}:" in b or f'"{name}":' in b for b in sendable),
                            f"{name} is sent but REVIEW_ACTION does not map it")
        # Approve and Reject arrive through the button row's choices array.
        self.assertIn('["Approve as reviewed","primary","Approved"]', self.js)
        self.assertIn('["Reject","danger","Rejected"]', self.js)
        self.assertIn("decide(sub, action, b)", self.js)

    def test_the_currency_lookup_still_has_both_halves(self):
        self.assertIn("CURRENCY_NAME[pinned]", self.js)
        self.assertIn("COUNTRY_CURRENCY[(ORG_PROFILE.address || {}).country]", self.js)


if __name__ == "__main__":
    unittest.main()
