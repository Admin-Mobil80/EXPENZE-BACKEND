"""Every inline script on every page has to parse.

This shipped: a `const scopeList` I added to `renderBudgets` was called `list`,
and `renderBudgets` already had a `const list` a few lines up. One duplicate
declaration, and the browser refused the *entire* script - so the console came
up with a masthead, no tabs, no name, no data, and nothing in the log that a
person signing in would ever see. The whole product, dark, from a variable
name.

Nothing else here could catch it. Six hundred structural tests read the file
as text and every one of them passed: the markup was correct, the CSS was
correct, and the strings they assert on were all present. A syntax error is
invisible to a test that greps.

So this one actually parses the scripts, the way the browser does, and it runs
on every page rather than only the one being worked on - a broken sign-in page
is worse than a broken console, because nobody gets far enough to report it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")

PAGES = [
    "../PORTAL/app.html",
    "../PORTAL/index.html",
    "../PORTAL/login.html",
    "../BMS/home.html",
    "../BMS/index.html",
]

# Inline only. A `src=` script is somebody else's file and is not ours to
# parse; there are none of those on these pages today.
INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)

NODE = shutil.which("node")


@unittest.skipIf(NODE is None, "node is not installed on this machine")
class EveryPageScriptParses(unittest.TestCase):
    def check(self, page: str) -> None:
        with open(os.path.join(ROOT, page), encoding="utf-8") as handle:
            blocks = INLINE.findall(handle.read())
        self.assertTrue(blocks, f"{page}: no inline script found - has it moved?")

        # Concatenated, because that is how the browser sees them: one shared
        # top-level scope, where a name declared in the first block collides
        # with the same name in the second.
        source = "\n".join(blocks)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as tmp:
            tmp.write(source)
            path = tmp.name
        try:
            done = subprocess.run([NODE, "--check", path],
                                  capture_output=True, text=True)
        finally:
            os.unlink(path)

        if done.returncode:
            # The line number is the concatenated script's, so quote the line
            # itself - that is what makes it findable in the page.
            detail = done.stderr.strip()
            line = re.search(r":(\d+)\n", done.stderr)
            if line:
                numbered = source.splitlines()
                index = int(line.group(1)) - 1
                if 0 <= index < len(numbered):
                    detail += f"\n\n  offending line: {numbered[index].strip()}"
            self.fail(f"{page} does not parse:\n{detail}")

    def test_the_console_parses(self):
        self.check("../PORTAL/app.html")

    def test_the_marketing_page_parses(self):
        self.check("../PORTAL/index.html")

    def test_the_sign_in_page_parses(self):
        self.check("../PORTAL/login.html")

    def test_the_back_office_parses(self):
        self.check("../BMS/home.html")

    def test_the_back_office_sign_in_parses(self):
        self.check("../BMS/index.html")


class TheCheckerItselfWorks(unittest.TestCase):
    """A test that cannot fail is worse than no test.

    `node --check` is doing the work here, so this proves it is actually
    running and actually rejecting - otherwise a missing node, a changed flag
    or a regex that stopped matching would leave five tests passing on nothing.
    """

    @unittest.skipIf(NODE is None, "node is not installed on this machine")
    def test_a_duplicate_declaration_is_rejected(self):
        # The exact shape of the bug: two `const` of the same name in one
        # function scope.
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as tmp:
            tmp.write("function f() { const list = 1; const list = 2; return list; }")
            path = tmp.name
        try:
            done = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
        finally:
            os.unlink(path)
        self.assertNotEqual(0, done.returncode, "node --check accepted a duplicate const")
        self.assertIn("already been declared", done.stderr)

    def test_the_pattern_finds_the_console_script(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            blocks = INLINE.findall(handle.read())
        self.assertTrue(any("renderBudgets" in b for b in blocks),
                        "the inline-script pattern no longer matches the console")


if __name__ == "__main__":
    unittest.main()
