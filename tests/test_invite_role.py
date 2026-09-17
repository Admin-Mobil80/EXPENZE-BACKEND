"""Choosing somebody's role when you invite them.

The server had always accepted a role on an invitation and validated it; the
console simply never asked, and sent "staff" every time. So an organisation
whose finance executive needed inviting had to invite them as staff, find them
in the list afterwards and promote them - and the second step is the one people
forget, which is how a finance executive spends a week unable to see the queue.

The same look also turned up an escalation. `_member_update` refuses to let a
finance executive make somebody an owner; `_invite` did not. Two doors to the
same privilege, and the quieter one was unlocked.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


class TheInvitationCarriesARole(unittest.TestCase):
    def setUp(self):
        for name, path in (("auth", "lambda_src/auth.py"), ("app", "../PORTAL/app.html")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())
        self.invite = self.auth.split("def _invite(", 1)[1].split("\ndef ", 1)[0]

    def test_the_form_offers_one(self):
        self.assertIn('id="p-role"', self.app)
        # Filled from `assignableBy`, so it offers only what the person
        # looking at it may actually grant - a fixed list showed a finance
        # executive roles the server would refuse.
        self.assertIn("assignableBy(currentUser.tier).forEach", self.app)
        self.assertIn('const assignableBy = (mine) =>', self.app)

    def test_it_is_sent_rather_than_hardcoded(self):
        self.assertIn('role: $("p-role").value', self.app)
        self.assertNotIn('role: "staff" })', self.app)

    def test_submitting_receipts_is_the_default(self):
        # Most people never need anything else, and a wrong default here hands
        # out access nobody asked for. `assignableBy` lists ascending, so
        # staff is first, and the fallback names it outright.
        self.assertIn('if (!roleSel.value) roleSel.value = "staff";', self.app)
        order = self.app.split("const assignableBy = (mine) =>", 1)[1].split(";", 1)[0]
        self.assertIn('["staff", "finance", "admin"]', order)

    def test_the_server_still_validates_it(self):
        self.assertIn("if role not in ROLES:", self.invite)

    def test_an_owner_cannot_be_created_by_invitation_at_all(self):
        # There is one owner, so there is nothing to invite somebody into.
        # Handing the organisation over is a transfer, with both sides written.
        self.assertIn('if role == "owner":', self.invite)
        self.assertIn("transfer ownership", self.invite)

    def test_and_you_may_only_invite_beneath_your_own_rank(self):
        # The invitation is the quieter of the two doors to a privilege, and
        # the one nobody thinks to lock: without this an administrator could
        # invite a second administrator.
        self.assertIn('if not may_manage(membership.get("role"), role):', self.invite)
        self.assertIn("403", self.invite)

    def test_the_confirmation_says_what_was_granted(self):
        # An invitation that quietly made somebody a finance executive is not
        # a thing to find out later.
        self.assertIn("as ${granted}", self.app)

    def test_changing_a_role_afterwards_still_works(self):
        # Owners could always do this and still can; the invitation is an
        # addition, not a replacement.
        self.assertIn('authCall("/member", { email: person.email, role: roleSel.value })', self.app)
        update = self.auth.split("def _member_update(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('if not may_manage(org["_role"], member.get("role")):', update)
        self.assertIn('if not may_manage(org["_role"], role):', update)


if __name__ == "__main__":
    unittest.main()


class TheInvitationComesFromAPerson(unittest.TestCase):
    """It read "riyad@mobil80.com added you to Mobil80".

    An address is how a system refers to somebody; it is not how a colleague
    does. The first thing a new person sees of this product should read like
    it came from the person who sent it.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as handle:
            self.auth = handle.read()
        self.invite = self.auth.split("def _invite(", 1)[1].split("\ndef ", 1)[0]
        self.send = self.auth.split("def _send_invite(", 1)[1].split("\ndef ", 1)[0]

    def test_the_inviter_is_named(self):
        self.assertIn('membership.get("name")', self.invite)
        self.assertIn("_send_invite(email, org_name,", self.invite)

    def test_it_falls_back_to_the_address_rather_than_to_nobody(self):
        # "Someone added you" is worse than an address: it reads as a system
        # that does not know who its own users are.
        call = self.invite.split("_send_invite(email, org_name,", 1)[1].split(
            ", role)", 1)[0]
        self.assertIn("or actor", call)

    def test_a_name_cannot_carry_markup_into_the_email(self):
        # Free text up to 120 characters, dropped straight into an HTML body.
        # An apostrophe in a company name would have survived that; an angle
        # bracket would not.
        self.assertIn("html.escape(inviter)", self.send)
        self.assertIn("html.escape(org_name)", self.send)
        body = self.send.split('html = f"""', 1)[1]
        self.assertIn("{inviter_html}", body)
        self.assertIn("{org_html}", body)
        self.assertNotIn("<strong>{inviter}</strong>", body)

    def test_the_plain_text_part_names_them_too(self):
        # Most mail clients show the HTML, but not all of them do.
        text = self.send.split("text = (", 1)[1].split(")\n", 1)[0]
        self.assertIn("{inviter} added you", text)


class ANameIsRequiredAndShort(unittest.TestCase):
    """A membership with no name is displayed by the local part of its email.

    That is how the account which created this organisation spent a fortnight
    being called "riyad". The console had always refused a blank name - but
    silently, and the API had never refused one at all, so any other caller
    could create a nameless member.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as handle:
            self.auth = handle.read()
        with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_there_is_one_limit_not_three(self):
        self.assertIn("MAX_NAME = 20", self.auth)
        # Invite, rename and sign-up all read the same constant.
        self.assertGreaterEqual(self.auth.count("MAX_NAME"), 5)

    def test_an_invitation_without_a_name_is_refused(self):
        invite = self.auth.split("def _invite(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("Enter their name.", invite)
        self.assertIn("MIN_NAME <= len(name) <= MAX_NAME", invite)

    def test_a_rename_cannot_blank_it(self):
        # Putting somebody back to being called by their inbox.
        update = self.auth.split("def _member_update(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("A name cannot be blank.", update)
        self.assertIn("MIN_NAME <= len(renamed) <= MAX_NAME", update)

    def test_sign_up_is_held_to_the_same_length(self):
        dispatch = self.auth.split('path.endswith("/signup")', 1)[1].split("if path", 1)[0]
        self.assertIn("MAX_NAME", dispatch)

    def test_the_fields_stop_at_the_limit_rather_than_failing_on_submit(self):
        self.assertIn('id="p-name" maxlength="20"', self.app)
        self.assertIn('if (key === "name") input.maxLength = 20;', self.app)

    def test_the_refusal_is_not_silent(self):
        # The cursor moved and nothing said why, which reads as a button that
        # does not work.
        handler = self.app.split('$("p-add").addEventListener', 1)[1].split("authCall", 1)[0]
        self.assertIn("peopleMsg(", handler)
        self.assertIn("Enter their name.", handler)


class TheInvitationExplainsHowToTurnOnWhatsApp(unittest.TestCase):
    """The one thing a new person has to do that nobody can do for them.

    Email starts working the moment they sign in - that is what signing in
    proves. WhatsApp does not: the number has to be added and verified by its
    owner, because an administrator typing a number is not consent, and one
    typed a digit wrong is a stranger who can spend an organisation's credits.

    The invitation mentioned this in a half sentence between two others, which
    is not an instruction. Somebody who never reads it simply never gets the
    channel the product is mostly used through.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as handle:
            self.send = handle.read().split("def _send_invite(", 1)[1].split("\ndef ", 1)[0]
        self.text = self.send.split("text = (", 1)[1].split('\n    body_html', 1)[0]
        self.html = self.send.split('body_html = f"""', 1)[1].split('"""', 1)[0]

    def test_both_parts_of_the_email_say_it(self):
        # Most clients show the HTML; some show the text. A step that exists
        # in one of them is a step half the readers never see.
        for part, label in ((self.text, "text"), (self.html, "html")):
            self.assertIn("sign in to the portal", part.lower(), f"missing from {label}")
            self.assertIn("verify it with the code", part, f"missing from {label}")

    def test_it_says_where_in_the_portal(self):
        # "Somewhere after signing in" is not a place.
        for part in (self.text, self.html):
            self.assertIn("My expenses", part)

    def test_it_says_why_only_they_can_do_it(self):
        # Otherwise it reads as a chore rather than as the thing protecting
        # them from somebody else claiming as them.
        for part in (self.text, self.html):
            self.assertIn("Nobody can add a number on your behalf", part)

    def test_it_says_what_they_get_for_it(self):
        for part in (self.text, self.html):
            self.assertIn("photograph a", part)
            self.assertIn("same chat", part)

    def test_it_does_not_promise_a_number_that_may_be_stale(self):
        # The number staff send to is a platform setting the console reads
        # live. Baking one into an email that outlives it would leave people
        # photographing receipts into a number nobody answers.
        for part in (self.text, self.html):
            self.assertNotIn("+91", part)


class EveryFieldOnTheInviteIsChecked(unittest.TestCase):
    """Two of the three were not checked at all.

    A staff ID of any length and an address with no domain both travelled to
    the server to be refused there, and the refusal arrived as a sentence above
    a table with no indication of which box it meant.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            self.auth = h.read()
        self.invite = self.auth.split("def _invite(", 1)[1].split("\ndef ", 1)[0]
        with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as h:
            self.app = h.read()
        self.handler = self.app.split('$("p-add").addEventListener', 1)[1].split(
            "await fetch", 1)[0]

    def test_the_limits_live_in_one_place(self):
        self.assertIn("MIN_NAME = 3", self.auth)
        self.assertIn("MAX_NAME = 20", self.auth)
        self.assertIn("MAX_STAFF_ID = 10", self.auth)

    def test_the_server_checks_all_three(self):
        self.assertIn("MIN_NAME <= len(name) <= MAX_NAME", self.invite)
        self.assertIn("len(staff_id) > MAX_STAFF_ID", self.invite)
        self.assertIn("EMAIL_RE.match(email)", self.invite)

    def test_a_staff_id_is_required(self):
        self.assertIn("Enter their staff ID", self.invite)

    def test_the_console_checks_the_same_three(self):
        self.assertIn("name.length < 3 || name.length > 20", self.handler)
        self.assertIn("!staff || staff.length > 10", self.handler)
        self.assertIn("@", self.handler.split("test(email)", 1)[0][-90:])

    def test_the_wording_matches_on_both_sides(self):
        # A refusal that reads differently depending on where it came from
        # makes somebody wonder which of the two is the real rule.
        for phrase in ("A name is between 3 and 20 characters.",
                       "A staff ID is at most 10 characters.",
                       "Enter their staff ID"):
            self.assertIn(phrase, self.app, f"the console never says {phrase!r}")

    def test_the_offending_field_is_marked_not_just_named(self):
        self.assertIn('$(id).classList.add("bad")', self.handler)
        self.assertIn(".people-add input.bad", self.app)

    def test_a_previous_refusal_is_cleared_before_rechecking(self):
        # Otherwise the first field you fixed stays outlined in red.
        self.assertIn('forEach(id => $(id).classList.remove("bad"))', self.handler)

    def test_the_fields_stop_at_the_limit_as_you_type(self):
        self.assertIn('id="p-name" maxlength="20"', self.app)
        self.assertIn('id="p-staff" maxlength="10"', self.app)


class TheInviteOutcomeIsVisible(unittest.TestCase):
    """It rendered as bare text flush against the panel edge.

    An invitation could be sent and look like nothing had happened, and a
    refusal could be mistaken for a heading.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as h:
            self.app = h.read()
        self.css = "\n".join(__import__("re").findall(
            r"<style[^>]*>(.*?)</style>", self.app, __import__("re").S))

    def test_it_is_a_box_not_a_line_of_text(self):
        for state in ("ok", "err"):
            rule = self.css.split(f"#people-msg.{state} {{", 1)[1].split("}", 1)[0]
            self.assertIn("background", rule)
            self.assertIn("border", rule)

    def test_it_lines_up_with_the_form_above_it(self):
        rule = self.css.split("#people-msg {", 1)[1].split("}", 1)[0]
        # The form's own horizontal padding, so the two edges agree.
        self.assertIn("margin:0 14px", rule)

    def test_showing_it_reveals_the_wrapper(self):
        fn = self.app.split("function peopleMsg(", 1)[1].split("\n}", 1)[0]
        self.assertIn("wrap.hidden = false", fn)
