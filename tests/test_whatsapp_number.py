"""Answering on the thread the message arrived on.

Expenze has been borrowing Yandle's WhatsApp number. Moving to a dedicated one
means a period with both numbers live on the same WhatsApp Business Account,
and the replies have to follow the conversation rather than the configuration.

Until this, every reply went out from whichever number the secret named. A
person who photographed a receipt to the old number would have had the verdict
arrive from a different number entirely - a second thread, from what looks to
them like a stranger who somehow knows about their dinner.

The receiving number is in Meta's own payload, which has already been
signature-checked by the time it is read, so it is exactly as trustworthy as
the message it came with.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
ROOT = os.path.join(os.path.dirname(__file__), "..")

# whatsapp.py pulls in identity.py and intake.py, which read their table names
# at import. Nothing here touches a table; they just have to be nameable.
for _k, _v in (("AWS_DEFAULT_REGION", "ap-southeast-1"), ("INTAKE_TABLE", "t"),
               ("WA_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:y"),
               ("USERS_TABLE", "u"), ("ORGS_TABLE", "o"), ("INTAKE_BUCKET", "b"),
               ("EXPENSES_TABLE", "e")):
    os.environ.setdefault(_k, _v)


class RepliesFollowTheThread(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "whatsapp.py"), encoding="utf-8") as h:
            self.src = h.read()

    def test_the_receiving_number_is_read_from_the_delivery(self):
        self.assertIn('(value.get("metadata") or {}).get("phone_number_id")', self.src)

    def test_it_is_passed_to_the_handler(self):
        self.assertIn("_handle_message(msg, cfg, from_id)", self.src)

    def test_every_reply_carries_it(self):
        # Matched by balancing brackets rather than by a non-greedy regex. The
        # regex stopped at the first ")" that ended a line, so a reply whose
        # message was assembled across several lines looked like a call with
        # no `from_id` - the test failing on formatting rather than on the
        # thing it is here to protect.
        calls = []
        for m in re.finditer(r"_reply\(sender,", self.src):
            depth, i = 1, m.end()
            while depth and i < len(self.src):
                depth += {"(": 1, ")": -1}.get(self.src[i], 0)
                i += 1
            calls.append(self.src[m.start():i])
        self.assertTrue(calls)
        for call in calls:
            self.assertIn("from_id", call, call[:120])

    def test_it_falls_back_to_the_configured_number(self):
        # notify.py sends unsolicited template messages with no thread to
        # follow, and a malformed delivery should not stop a reply either.
        self.assertIn("from_id or cfg['phoneNumberId']", self.src)


class NothingIdentifyingTheNumberIsInCode(unittest.TestCase):
    """Swapping the number must stay a secret update, not a deploy.

    The module docstring promises exactly that. A phone number or a numeric id
    hardcoded anywhere here would make it a lie, and the person migrating would
    find out at the worst moment.
    """

    FILES = ("whatsapp.py", "notify.py", "auth.py")

    def test_no_phone_number_id_is_hardcoded(self):
        for name in self.FILES:
            with open(os.path.join(ROOT, "lambda_src", name), encoding="utf-8") as h:
                body = h.read()
            # Meta ids are 15-16 digits; a WhatsApp number in E.164 is 10-15.
            for match in re.findall(r"(?<![\w.])\d{10,}(?![\w.])", body):
                self.fail(f"{name} carries a bare long number: {match}")

    def test_the_number_shown_to_users_comes_from_settings(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            auth = h.read()
        # BMS owns the display number; the console reads it rather than
        # carrying its own copy to drift.
        self.assertIn('row.get("whatsapp_number", "")', auth)


if __name__ == "__main__":
    unittest.main()


class TheOldNumberKeepsWorking(unittest.TestCase):
    """The instruction was: do not break inbound while we move.

    Accepting the old app's signature is necessary and nowhere near enough. A
    number belongs to a Meta app, and so does the access token that downloads
    its media and sends its replies - a token issued for a new app cannot
    fetch a photograph sent to a number on the old one. Swap the credentials
    wholesale and every receipt to the current number comes back "I couldn't
    read that attachment", which the sender reads as the product being broken.

    So credentials are chosen by the number a message arrived on, not by which
    entry the secret happens to list first.
    """

    OLD = {"phoneNumberId": "111", "accessToken": "old-token", "appSecret": "old-secret",
           "webhookVerifyToken": "old-verify"}
    NEW = {"phoneNumberId": "222", "accessToken": "new-token", "appSecret": "new-secret",
           "webhookVerifyToken": "new-verify", "alsoAccept": [OLD]}

    def setUp(self):
        os.environ.setdefault("INTAKE_TABLE", "t")
        os.environ.setdefault("WA_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:y")
        os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
        import whatsapp  # noqa: E402
        self.wa = whatsapp
        # The secret now comes through the shared expiring cache rather than
        # a module global of whatsapp.py's own, so `config` is the seam.
        patch = mock.patch.object(whatsapp, "config", return_value=dict(self.NEW))
        patch.start()
        self.addCleanup(patch.stop)

    def test_both_numbers_are_answered_on(self):
        ids = [i["phoneNumberId"] for i in self.wa.identities()]
        self.assertIn("222", ids)
        self.assertIn("111", ids, "the current number must still be served")

    def test_a_message_to_the_old_number_uses_the_old_token(self):
        # The one that actually breaks: media download and replies both go
        # through the app that owns the number.
        self.assertEqual(self.wa.creds_for("111")["accessToken"], "old-token")

    def test_a_message_to_the_new_number_uses_the_new_token(self):
        self.assertEqual(self.wa.creds_for("222")["accessToken"], "new-token")

    def test_an_unknown_number_falls_back_rather_than_failing(self):
        self.assertEqual(self.wa.creds_for("999")["accessToken"], "new-token")
        self.assertEqual(self.wa.creds_for("")["accessToken"], "new-token")

    def test_an_entry_inherits_what_it_does_not_override(self):
        # An old number sharing a token with the new one need only name its id.
        with mock.patch.object(self.wa, "config",
                               return_value={**self.NEW, "alsoAccept": [{"phoneNumberId": "333"}]}):
            self.assertEqual(self.wa.creds_for("333")["accessToken"], "new-token")
            self.assertEqual(self.wa.creds_for("333")["appSecret"], "new-secret")

    def test_either_app_may_sign_a_delivery(self):
        secrets = [i["appSecret"] for i in self.wa.identities()]
        self.assertIn("old-secret", secrets)
        self.assertIn("new-secret", secrets)

    def test_either_verify_token_completes_the_handshake(self):
        tokens = [i.get("webhookVerifyToken") for i in self.wa.identities()]
        self.assertIn("old-verify", tokens)
        self.assertIn("new-verify", tokens)

    def test_an_entry_with_no_number_is_ignored(self):
        # A half-filled block must not silently become a second identity that
        # shadows the real one.
        with mock.patch.object(self.wa, "config",
                               return_value={**self.NEW, "alsoAccept": [{"accessToken": "stray"}]}):
            self.assertEqual([i["phoneNumberId"] for i in self.wa.identities()], ["222"])

    def test_one_number_configured_is_still_one_identity(self):
        with mock.patch.object(self.wa, "config",
                               return_value={k: v for k, v in self.NEW.items() if k != "alsoAccept"}):
            self.assertEqual(len(self.wa.identities()), 1)


class TwoAppsDuringACutover(unittest.TestCase):
    """The old number keeps working while the new one is brought up.

    Moving to a dedicated Meta app means a new app secret, and Meta signs each
    delivery with the secret of the app that owns the number. With one secret
    configured there is a cliff: the instant it is replaced, every message to
    the old number fails its signature check and is dropped - which the sender
    experiences as the service ignoring them, with nothing to see anywhere.

    So `previousAppSecret` is accepted for as long as the old number is live,
    and removed once it is not.
    """

    def setUp(self):
        import importlib, types
        os.environ.setdefault("INTAKE_TABLE", "t")
        os.environ.setdefault("WA_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:y")
        os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
        os.environ.setdefault("ORGS_TABLE", "o")
        os.environ.setdefault("USERS_TABLE", "u")
        import whatsapp  # noqa: E402
        self.wa = whatsapp

    def _sign(self, body: str, secret: str) -> str:
        import hashlib, hmac
        return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()

    BODY = '{"entry":[{"changes":[]}]}'

    def test_the_current_secret_is_accepted(self):
        self.assertTrue(self.wa._signature_ok(self.BODY, self._sign(self.BODY, "new"), "new", "old"))

    def test_the_previous_secret_is_accepted_too(self):
        self.assertTrue(self.wa._signature_ok(self.BODY, self._sign(self.BODY, "old"), "new", "old"))

    def test_anything_else_is_refused(self):
        self.assertFalse(self.wa._signature_ok(self.BODY, self._sign(self.BODY, "other"), "new", "old"))

    def test_an_absent_previous_secret_is_not_a_blank_one(self):
        # "" must not become a secret that an empty-keyed HMAC would match.
        forged = self._sign(self.BODY, "")
        self.assertFalse(self.wa._signature_ok(self.BODY, forged, "new", ""))

    def test_an_unsigned_delivery_is_still_refused(self):
        for header in ("", "sha1=abc", "abc", "sha256="):
            self.assertFalse(self.wa._signature_ok(self.BODY, header, "new", "old"), header)

    def test_a_tampered_body_fails_even_with_the_right_secret(self):
        good = self._sign(self.BODY, "new")
        self.assertFalse(self.wa._signature_ok(self.BODY + " ", good, "new", "old"))
