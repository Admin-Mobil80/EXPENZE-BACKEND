"""Every WhatsApp message leaves by the transport the secret names.

eum.py can be perfect and still never run. Five places send or receive
WhatsApp - the adapter's reply, the adapter's media fetch, the notifier's
outcomes, the sign-in code, and the acknowledgement for a receipt emailed in -
and a migration that reaches four of them leaves the fifth quietly on a bearer
token that is about to be deleted.

The fifth is the one that was missed, and it was missed in the stack rather
than in the code: `mail_fn` had neither the secret nor the grant, so the
notice composed and was dropped before a call was made.

The sign-in code is the one worth stating plainly: it is the only sender here
allowed to fail loudly, because somebody watching a phone for a code they will
never receive is worse served by silence than by an error on screen.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
ROOT = os.path.join(os.path.dirname(__file__), "..")


def source(name: str) -> str:
    with open(os.path.join(ROOT, "lambda_src", name), encoding="utf-8") as h:
        return h.read()


class EverySenderAsks(unittest.TestCase):
    def test_the_adapters_reply_asks(self):
        self.assertIn("if eum.enabled(cfg):", source("whatsapp.py"))

    def test_the_notifier_asks(self):
        src = source("notify.py")
        self.assertIn("import eum", src)
        self.assertIn("return eum.send(cfg, message)", src)

    def test_the_sign_in_code_asks(self):
        src = source("auth.py")
        self.assertIn("import eum", src)
        self.assertIn("if eum.enabled(cfg):", src)

    def test_media_asks(self):
        self.assertIn("return eum.fetch_media(", source("whatsapp.py"))


class TheFailureModesDiffer(unittest.TestCase):
    def test_a_verification_code_that_cannot_be_sent_is_raised(self):
        """Not swallowed like an outcome notice.

        A settlement has already happened and the notice is a courtesy. A
        verification code *is* the thing the caller asked for, and reporting
        success for one that never went is how somebody ends up waiting on a
        phone for ever.
        """
        self.assertIn(
            'raise RuntimeError("End User Messaging would not send the code")',
            source("auth.py"))


class TheTokenStopsBeingRequired(unittest.TestCase):
    def test_a_missing_access_token_does_not_break_the_adapter(self):
        """Once AWS carries the traffic there is nothing to put in that field.

        The last step of the migration is deleting the bearer token from the
        secret. If anything still subscripted it, that deletion would take the
        adapter down on its very next message.
        """
        src = source("whatsapp.py")
        self.assertNotIn('token = cfg["accessToken"]', src)
        self.assertIn('token = str(cfg.get("accessToken") or "")', src)


class BothTransportsShareOneHandler(unittest.TestCase):
    def test_sns_and_the_webhook_reach_the_same_code(self):
        """A receipt is a receipt however it was delivered.

        Two inbound paths handled by two pieces of code is two chances for one
        of them to start behaving differently - a reply that follows the thread
        on one and not the other, a duplicate caught on one and not the other.
        """
        src = source("whatsapp.py")
        self.assertIn("def _dispatch(", src)
        self.assertIn("_dispatch(eum.entries(event), config())", src)
        self.assertIn('_dispatch(json.loads(raw or "{}").get("entry", []), cfg)', src)

    def test_an_sns_delivery_is_never_allowed_to_raise(self):
        """A raised handler tells SNS to redeliver the whole batch.

        Every receipt in it that already succeeded would be read, audited and
        charged a second credit.
        """
        src = source("whatsapp.py")
        head = src[src.index("def lambda_handler"):]
        self.assertIn("an End User Messaging delivery failed", head)


class TheInfrastructureExists(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "expensifyai", "stack.py"), encoding="utf-8") as h:
            self.src = h.read()
        with open(os.path.join(ROOT, "expensifyai", "whatsapp_inbound.py"),
                  encoding="utf-8") as h:
            self.inbound = h.read()

    def test_there_is_a_topic_for_inbound(self):
        self.assertIn("topic_name=TOPIC", self.inbound)
        self.assertIn('TOPIC = "expenze-whatsapp-inbound"', self.inbound)

    def test_the_topic_is_in_the_wabas_region_not_the_apis(self):
        """An event destination may only name a topic in the WABA's region.

        `PutWhatsAppBusinessAccountEventDestinations` refuses anything else
        with "Invalid EventDestination Region" - which is clear once you have
        seen it and says nothing in advance. The topic therefore lives in
        us-east-1 while the adapter that consumes it stays in ap-southeast-1.
        """
        self.assertIn('REGION = "us-east-1"', self.inbound)
        self.assertIn('CONSUMER_REGION = "ap-southeast-1"', self.inbound)

    def test_the_adapter_is_subscribed_across_regions(self):
        """With a raw subscription, not the convenience wrapper.

        `LambdaSubscription` also writes the invoke permission, and writes it
        in this stack's region, where Lambda cannot see a function that lives
        in another one. The permission is declared beside the function instead.
        """
        self.assertIn("sns.CfnSubscription(", self.inbound)
        self.assertIn('protocol="lambda"', self.inbound)
        self.assertIn(f'arn:aws:lambda:{{CONSUMER_REGION}}', self.inbound)

    def test_the_topic_is_allowed_to_invoke_the_adapter(self):
        """Declared beside the function, because the permission belongs to it.

        A subscription SNS accepts is not a subscription that can deliver: the
        function's own resource policy has to name the topic, and that policy
        lives in the other region's stack.
        """
        self.assertIn('principal="sns.amazonaws.com"', self.src)
        self.assertIn("source_arn=wa_inbound_arn", self.src)

    def test_the_senders_may_call_the_service(self):
        """Every sender, named. Not "the string appears somewhere".

        This asserted only that the action existed in the file, which it did
        while `mail_fn` - the function that acknowledges an emailed receipt on
        WhatsApp - was absent from the grant and from the secret entirely. A
        wiring test that cannot tell four functions from three is not a wiring
        test.
        """
        # The adapter gets its own statement: it fetches media as well as sends.
        self.assertIn("social-messaging:GetWhatsAppMessageMedia", self.src)
        self.assertIn("wa_fn.add_to_role_policy(", self.src)

        # Everybody else shares one, and the tuple is the list of senders.
        self.assertIn("for fn in (auth_fn, auditor_worker, intake_fn, mail_fn):",
                      self.src)
        shared = self.src.split(
            "for fn in (auth_fn, auditor_worker, intake_fn, mail_fn):", 1)[1][:400]
        self.assertIn("social-messaging:SendWhatsAppMessage", shared)

    def test_the_grant_is_not_a_wildcard_across_accounts(self):
        """Scoped to this account's resources.

        `*` would let the function send from any number the account ever
        links, including one nobody meant to expose to it.
        """
        self.assertIn('f"arn:aws:social-messaging:{eum_region}:{self.account}:*"',
                      self.src)


if __name__ == "__main__":
    unittest.main()
