"""A receipt emailed in is acknowledged on WhatsApp too.

Somebody who sends a photograph to the intake address gets a reply on their
own mail thread, with the claim reference in it. That is the complete answer
and it is in the right place. It is also not where most people on this account
live: they send receipts on WhatsApp and hear back there, and an emailed one
went quiet on the channel they watch.

So the same "it arrived" goes to WhatsApp, for the people whose number we hold
and who verified it.

**Why it is a template.** An emailed receipt brings no inbound WhatsApp
message with it, so Meta's 24-hour customer-service window is shut for
everybody except the handful who happened to message the number earlier that
day - free text would reach almost nobody. `expenze_receipt_received` is
approved on the Expenze WABA and takes the claim reference and the
organisation, so this lands whatever the window is doing.

**What it must never do.** Turn a queued receipt into a failed one. The claim
is stored, charged and queued before any of this runs.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(name):
    with open(os.path.join(ROOT, "lambda_src", name), encoding="utf-8") as fh:
        return fh.read()


def read_at(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TheNoticeItself(unittest.TestCase):

    def setUp(self):
        self.notify = read("notify.py")
        self.fn = self.notify.split("def received_notice(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_it_is_registered_so_send_can_find_it(self):
        self.assertIn('"received": received_notice,', self.notify)

    def test_one_receipt_carries_the_reference_worth_quoting(self):
        self.assertIn('lines.append(f"Claim: {reference}"', self.fn)

    def test_but_three_are_answered_by_the_count(self):
        # Naming the first of three hands somebody a number covering a third
        # of what they sent; the email that went with it carries the list.
        self.assertIn("if count == 1 and reference:", self.fn)
        self.assertIn('f"*{count} receipts received*"', self.fn)

    def test_it_is_never_emailed(self):
        # `mail.py` composes the email acknowledgement against the sender's
        # own thread, with the message id to reply in place. A second email
        # saying the same thing in worse words is the product talking over
        # itself.
        self.assertIn('return {"subject": "", "text": text, "whatsapp": text}',
                      self.fn)

    def test_and_says_the_outcome_is_still_coming(self):
        self.assertIn("the outcome follows in a separate message", self.fn)


class ItGoesOutWithTheEmailAcknowledgement(unittest.TestCase):

    def setUp(self):
        self.mail = read("mail.py")
        self.fn = self.mail.split("def _also_on_whatsapp(", 1)[1].split(
            "\ndef ", 1)[0]

    def test_it_runs_after_the_email_has_gone(self):
        ack = self.mail.split("def _acknowledge(", 1)[1].split("\ndef ", 1)[0]
        self.assertLess(ack.index("_reply_on_thread(msg, sender"),
                        ack.index("_also_on_whatsapp(sender, queued)"))

    def test_and_only_when_something_was_actually_queued(self):
        # "Received" is not the answer to an email with no receipt in it, or
        # to one refused for want of credits.
        ack = self.mail.split("def _acknowledge(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if queued:\n        _also_on_whatsapp(sender, queued)", ack)

    def test_only_a_number_its_owner_verified(self):
        self.assertIn('if member.get("whatsapp_channel") != "active":', self.fn)
        self.assertIn('if not str(member.get("mobile") or "").strip():', self.fn)

    def test_a_sender_we_cannot_resolve_is_left_alone(self):
        self.assertIn("if not member:", self.fn)

    def test_it_sends_on_one_channel_only(self):
        # The email is already written and already threaded.
        self.assertIn('only="whatsapp"', self.fn)

    def test_a_failure_here_never_fails_the_receipt(self):
        # The claim is stored, charged and queued before any of this runs.
        self.assertIn("except Exception:", self.fn)
        self.assertIn("logger.exception(", self.fn)


class ItGoesAsAnApprovedTemplate(unittest.TestCase):
    """Because free text would reach almost nobody.

    A receipt emailed in brings no inbound WhatsApp message with it, so Meta's
    24-hour customer-service window is shut for everybody except the handful
    who happened to message the number earlier that day. Every other notice
    here answers something the person sent minutes ago and can go as text;
    this one cannot.

    `expenze_receipt_received` is the template, approved on the Expenze WABA,
    taking the claim reference and the organisation.
    """

    def setUp(self):
        self.notify = read("notify.py")

    def test_it_is_registered_as_a_template_not_as_free_text(self):
        self.assertIn('"received": ("receivedTemplate", "expenze_receipt_received"),',
                      self.notify)
        self.assertIn('NO_TEMPLATE = {"outcome", "low_credits", "disputed", '
                      '"approved"}', self.notify)

    def test_the_name_is_overridable_from_the_secret(self):
        # Same shape as the other two, so a rename is a secret update rather
        # than a deploy.
        self.assertIn('cfg.get(key, fallback)', self.notify)

    def test_one_receipt_passes_its_own_reference(self):
        fn = self.notify.split("def _template_parameters(", 1)[1].split(
            "\ndef ", 1)[0]
        self.assertIn('if kind == "received":', fn)
        self.assertIn('claim.get("org_name") or "your organisation"', fn)

    def test_and_several_are_named_without_pretending_to_be_one(self):
        # Naming the first of three hands somebody a number covering a third
        # of what they sent.
        fn = self.notify.split("def _template_parameters(", 1)[1].split(
            "\ndef ", 1)[0]
        self.assertIn('f"{ref} and {count - 1} more"', fn)
        self.assertIn('f"{count} receipts"', fn)

    def test_every_parameter_is_non_empty(self):
        # WhatsApp drops an empty template parameter and the message arrives
        # malformed, so each one has a fallback.
        fn = self.notify.split("def _template_parameters(", 1)[1].split(
            "\ndef ", 1)[0]
        self.assertIn('or "your organisation"', fn)
        self.assertIn('str(v or "-").split()', fn)

    def test_a_refused_send_is_swallowed_not_raised(self):
        fn = self.notify.split("def _send_whatsapp(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("return False", fn)
        self.assertIn("logger.exception(", fn)


class TheMailFunctionCanActuallySend(unittest.TestCase):
    """The notice composes, and then never left the building.

    `notify._send_whatsapp` reads `WA_SECRET_ARN` at import and returns False
    before making a single call when it is absent. The mail function was the
    one sender in the stack that never had it - so every receipt emailed in
    got its threaded email reply and nothing on the phone, with no exception
    and no error log, only an INFO line reading `whatsapp=False`.

    Four functions send WhatsApp and the adapter is only one of them. These
    assert the wiring per function rather than that the strings exist
    somewhere in a 1,400-line stack, which is what let this through.
    """

    def setUp(self):
        self.stack = read_at("expensifyai", "stack.py")

    def test_the_guard_that_made_it_silent_is_still_there(self):
        # If this ever stops being the shape of it, the test below stops
        # testing anything.
        notify = read("notify.py")
        self.assertIn('WA_SECRET_ARN = os.environ.get("WA_SECRET_ARN", "")', notify)
        fn = notify.split("def _send_whatsapp(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if not WA_SECRET_ARN:\n        return False", fn)

    def test_the_mail_function_is_told_where_the_secret_is(self):
        self.assertIn(
            'mail_fn.add_environment("WA_SECRET_ARN", wa_secret.secret_arn)',
            self.stack)

    def test_and_is_allowed_to_read_it(self):
        self.assertIn("wa_secret.grant_read(mail_fn)", self.stack)

    def test_and_may_call_the_messaging_service(self):
        senders = self.stack.split(
            "for fn in (auth_fn, auditor_worker, intake_fn, mail_fn):", 1)
        self.assertEqual(len(senders), 2,
                         "mail_fn is not in the social-messaging grant")
        self.assertIn("social-messaging:SendWhatsAppMessage", senders[1][:400])

    def test_every_sender_names_the_region_the_grant_is_scoped_to(self):
        # The grant is written against `eum_region`; a function left to fall
        # back to the default in eum.py is calling whichever region that names.
        self.assertIn(
            "for fn in (wa_fn, auth_fn, auditor_worker, intake_fn, mail_fn):\n"
            '            fn.add_environment("EUM_REGION", eum_region)',
            self.stack)


class TheTemplateIsCreatedOnTheWABA(unittest.TestCase):
    """Naming a template is not the same as having one.

    `notify.py` has sent this notice as `expenze_receipt_received` while
    `wa_migrate.py` knew nothing about it: `templates` never submitted it, and
    `check` printed a number as ready with the template that carries "Receipt
    received" absent from its WABA. A send against a template the WABA has
    never heard of is refused by Meta, logged, and swallowed - the same
    silence from the other end.
    """

    def setUp(self):
        self.tool = read_at("tools", "wa_migrate.py")

    def test_check_looks_for_it(self):
        self.assertIn("expenze_receipt_received", self.tool.split(
            "NEEDED_TEMPLATES = ", 1)[1].split(")", 1)[0])

    def test_templates_submits_it(self):
        bodies = self.tool.split("TEMPLATE_BODIES = {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('"expenze_receipt_received": {', bodies)

    def test_under_the_name_notify_sends_it_as(self):
        # One name, in two files that never see each other.
        self.assertIn('"received": ("receivedTemplate", "expenze_receipt_received"),',
                      read("notify.py"))

    def test_with_the_two_parameters_notify_fills(self):
        """{{1}} the reference, {{2}} the organisation, in that order.

        A template approved with a different number of placeholders is
        rejected at send time, and nobody sees the error until somebody is
        waiting to hear their receipt arrived.
        """
        body = self.tool.split('"expenze_receipt_received": {', 1)[1].split(
            "\n    },", 1)[0]
        self.assertIn("{{1}}", body)
        self.assertIn("{{2}}", body)
        self.assertNotIn("{{3}}", body)
        # The example Meta reviews against carries one value per placeholder.
        self.assertIn('"example": ["Mobil80-Exp-41", "Mobil80"]', body)

    def test_it_is_a_utility_template_not_an_authentication_one(self):
        # Only the sign-in code is AUTHENTICATION, and it is the one entry
        # here that carries its own category.
        body = self.tool.split('"expenze_receipt_received": {', 1)[1].split(
            "\n    },", 1)[0]
        self.assertNotIn("category", body)


if __name__ == "__main__":
    unittest.main()
