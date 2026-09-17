"""Asking people to verify their WhatsApp number, once, where they will see it.

The panel that does this has always existed. It lives at the top of My
expenses - which an owner or a finance executive never lands on, and which
everyone else scrolls past on the way to the upload box - so people were not
declining the step, they were never shown it.

Two things have to hold. It must be asked somewhere unmissable and answered
once, because a prompt on every sign-in is one people learn to click past. And
it must say where the code goes: "Send verification code" reads as SMS to
nearly everyone, and somebody waiting on a text that is sitting unread in
WhatsApp concludes the feature is broken.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def console() -> str:
    with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as fh:
        return fh.read()


class ItIsAskedWhereItCannotBeMissed(unittest.TestCase):

    def setUp(self):
        self.app = console()

    def test_it_sits_above_the_tabs_like_the_other_page_wide_notice(self):
        # Above the nav, because it is about the person rather than whichever
        # screen is open - and an admin's screen is never My expenses.
        head = self.app.split('<nav class="nav" id="nav">', 1)[0]
        self.assertIn('id="wa-prompt"', head)

    def test_it_is_painted_on_every_render(self):
        self.assertIn("renderSession(); renderNav(); paintWaPrompt();", self.app)

    def test_it_takes_them_to_the_field_rather_than_the_tab(self):
        # Landing somebody on the tab and leaving them to find the panel is
        # most of why the step was being missed.
        fn = self.app.split('$("wa-prompt-go").addEventListener', 1)[1] \
                     .split("\n});", 1)[0]
        self.assertIn('goTo("mine")', fn)
        self.assertIn("num.focus()", fn)


class ItIsAskedOnceAndOnlyWhenItApplies(unittest.TestCase):

    def setUp(self):
        self.app = console()
        self.fn = self.app.split("function paintWaPrompt() {", 1)[1].split("\n}", 1)[0]

    def test_never_to_somebody_who_already_has_a_number(self):
        self.assertIn('status === "not_added"', self.fn)

    def test_and_not_before_the_server_has_said_who_they_are(self):
        # `myProfile` falls back to "not_added" for everybody until LIVE
        # lands, which would flash the bar at people who verified months ago.
        self.assertIn("const known = !!LIVE;", self.fn)
        self.assertIn("known &&", self.fn)

    def test_both_answers_are_remembered(self):
        for button in ("wa-prompt-go", "wa-prompt-skip"):
            fn = self.app.split(f'$("{button}").addEventListener', 1)[1].split(";", 2)[0]
            self.assertIn("answerWaPrompt", fn)

    def test_the_memory_is_per_person(self):
        # Two people on one laptop must not inherit each other's answer.
        key = self.app.split("function waPromptKey() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("LIVE && LIVE.email", key)

    def test_a_browser_that_refuses_storage_still_shows_the_route(self):
        # Better a dismissible prompt every time than a hidden feature.
        fn = self.app.split("function waPromptAnswered() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("catch (_) { return false; }", fn)


class ItSaysTheCodeComesOnWhatsApp(unittest.TestCase):
    """The single most common way this step fails: waiting for an SMS."""

    def setUp(self):
        self.app = console()

    def test_the_prompt_says_so(self):
        bar = self.app.split('id="wa-prompt"', 1)[1].split("</div>", 1)[0]
        self.assertIn("not as an\n      SMS", bar)

    def test_the_button_does_not_read_as_a_text_message(self):
        # The rendered button, not the comment above it - which quotes the old
        # label to say why it changed.
        button = self.app.split('id="wa-add"', 1)[0].rsplit("<button", 1)[1] \
               + self.app.split('id="wa-add"', 1)[1].split("</button>", 1)[0]
        self.assertIn("Send code on WhatsApp", button)
        self.assertNotIn("Send verification code", button)

    def test_and_it_is_repeated_where_they_sit_waiting_for_it(self):
        self.assertIn("Check WhatsApp, not your text messages.", self.app)

    def test_skipping_is_offered_rather_than_hidden(self):
        # Not mandatory: email is always there, so a number nobody verifies
        # costs them one route, not the product.
        self.assertIn("I&rsquo;ll use email", self.app)
        self.assertIn("You can skip this and email receipts instead.", self.app)


if __name__ == "__main__":
    unittest.main()
