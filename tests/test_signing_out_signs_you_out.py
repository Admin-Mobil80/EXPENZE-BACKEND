"""Sign out did not sign anybody out.

It was an anchor: `<a href="/login.html">Sign out</a>`. Clicking it navigated
to the sign-in page and did nothing else. The token stayed in sessionStorage
and app.html stayed in history, so the browser's back button came back to a
live console - signed in, with real data, for whoever was sitting there next.

A user reported exactly that. It is not a caching subtlety: the session was
never ended, so even a full reload of the console would have signed them
straight back in.

Three layers, because there are three ways back to a page whose session has
gone: the button ends the session and replaces the history entry, `pageshow`
re-asks on every restore including the back-forward cache (which does not
re-run the script, so the boot guard never fires), and the sign-in page
clears any token it finds, so arriving there means what it looks like it
means however somebody got there.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class TheButtonEndsTheSession(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_it_is_a_button_and_not_a_link(self):
        # An anchor navigates. That is all it does.
        self.assertIn('<button type="button" class="btn back" id="signout">Sign out</button>',
                      self.app)
        self.assertNotIn('<a href="/login.html" class="btn back"', self.app)

    def test_it_clears_the_token(self):
        fn = self.app.split('$("signout").addEventListener("click", () => {', 1)[1] \
                     .split("});", 1)[0]
        self.assertIn('sessionStorage.removeItem("expenze_token")', fn)

    def test_and_replaces_rather_than_pushes(self):
        # So the console is not left in history for the back button to reach.
        fn = self.app.split('$("signout").addEventListener("click", () => {', 1)[1] \
                     .split("});", 1)[0]
        self.assertIn('location.replace("/login.html?bye=1")', fn)
        self.assertNotIn("location.href", fn)

    def test_both_halves_are_needed(self):
        # Clearing without replacing leaves a page that reloads into the
        # redirect - which works, and flashes the console's chrome on the way.
        # Replacing without clearing leaves a live session one keystroke away.
        fn = self.app.split('$("signout").addEventListener("click", () => {', 1)[1] \
                     .split("});", 1)[0]
        self.assertIn("removeItem", fn)
        self.assertIn("replace", fn)


class EveryWayBackAsksAgain(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_one_guard_that_both_callers_use(self):
        self.assertIn("function requireSession() {", self.app)
        fn = self.app.split("function requireSession() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (sessionToken()) return true;", fn)
        self.assertIn('location.replace("/login.html")', fn)

    def test_it_redirects_only_once(self):
        # The boot guard and `pageshow` both ask, and two `replace` calls to
        # one URL is the kind of thing that works until it does not.
        fn = self.app.split("function requireSession() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (!leaving) {", fn)

    def test_a_restored_page_is_checked(self):
        # The back-forward cache does not re-run the script, so the boot guard
        # never fires: the console reappears exactly as it was left, rendered,
        # with somebody else's figures on it.
        fn = self.app.split('window.addEventListener("pageshow", (e) => {', 1)[1] \
                     .split("});", 1)[0]
        self.assertIn("if (!requireSession()) return;", fn)

    def test_and_refreshed_if_it_was(self):
        # A restored page is showing figures from whenever it was last open.
        fn = self.app.split('window.addEventListener("pageshow", (e) => {', 1)[1] \
                     .split("});", 1)[0]
        self.assertIn("if (e.persisted) refreshNow();", fn)

    def test_the_boot_guard_uses_it_too(self):
        self.assertIn("if (requireSession()) {", self.app)


class TheSignInPageMeansWhatItSays(unittest.TestCase):

    def test_it_clears_any_token_it_finds(self):
        # Reached by signing out, by an expired session, by the back button
        # and by somebody typing the address. In each of those a token left in
        # sessionStorage is a console one navigation away.
        login = read("../PORTAL/login.html")
        self.assertIn('sessionStorage.removeItem("expenze_token")', login)


if __name__ == "__main__":
    unittest.main()
