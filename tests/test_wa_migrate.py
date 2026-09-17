"""Moving to a dedicated WhatsApp number without dropping a receipt.

The dangerous step is not the swap, it is the moment between: a number whose
credentials have been replaced stops being able to fetch the photograph that
was just sent to it, and the sender experiences that as the service ignoring
them. So the secret holds every number we still answer on, and the migration
is three reversible steps rather than one edit.

What these check is the shape of what gets written, because the shape is what
`whatsapp.identities()` reads at runtime - and the failure mode of getting it
wrong is silence, not an error.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lambda_src"))

_spec = importlib.util.spec_from_file_location(
    "wa_migrate", os.path.join(ROOT, "tools", "wa_migrate.py"))
wa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wa)


OLD = {"accessToken": "old-token", "appSecret": "old-secret",
       "phoneNumberId": "111", "wabaId": "aaa",
       "webhookVerifyToken": "old-verify"}


class Adding(unittest.TestCase):
    def run_add(self, cfg, answers, waba="bbb", pid="222"):
        written = {}
        with mock.patch.object(wa, "read", return_value=dict(cfg)), \
             mock.patch.object(wa, "write", side_effect=lambda c: written.update(c)), \
             mock.patch.object(wa, "_ask", return_value=answers["token"]), \
             mock.patch.object(wa.getpass, "getpass",
                               side_effect=[answers.get("secret", ""),
                                            answers.get("verify", "")]):
            wa.cmd_add(mock.Mock(phone_number_id=pid, waba_id=waba))
        return written

    def test_the_old_number_stays_in_charge(self):
        out = self.run_add(OLD, {"token": "new-token"})
        self.assertEqual("111", out["phoneNumberId"])
        self.assertEqual("222", out["alsoAccept"][0]["phoneNumberId"])

    def test_the_old_credentials_are_never_retyped(self):
        # Reading them out and putting them back is the difference between a
        # migration and a chance to mistype a 200-character token.
        out = self.run_add(OLD, {"token": "new-token"})
        self.assertEqual("old-token", out["accessToken"])
        self.assertEqual("old-secret", out["appSecret"])

    def test_a_number_on_a_new_app_carries_its_own_secret(self):
        out = self.run_add(OLD, {"token": "new-token", "secret": "new-secret",
                                 "verify": "new-verify"})
        entry = out["alsoAccept"][0]
        self.assertEqual("new-secret", entry["appSecret"])
        self.assertEqual("new-verify", entry["webhookVerifyToken"])

    def test_a_number_on_the_same_app_inherits_rather_than_duplicating(self):
        # Pressing Enter at both prompts. The adapter merges the entry over the
        # top-level config, so an absent key means "the same as the primary".
        out = self.run_add(OLD, {"token": "new-token"})
        entry = out["alsoAccept"][0]
        self.assertNotIn("appSecret", entry)
        self.assertNotIn("webhookVerifyToken", entry)

    def test_adding_the_same_number_twice_is_refused(self):
        with mock.patch.object(wa, "read", return_value=dict(OLD)):
            with self.assertRaises(SystemExit):
                wa.cmd_add(mock.Mock(phone_number_id="111", waba_id="aaa"))


class Promoting(unittest.TestCase):
    def setUp(self):
        self.staged = {**OLD, "alsoAccept": [
            {"phoneNumberId": "222", "wabaId": "bbb",
             "accessToken": "new-token", "appSecret": "new-secret",
             "webhookVerifyToken": "new-verify"}]}

    def promote(self, cfg, pid="222"):
        written = {}
        with mock.patch.object(wa, "read", return_value=dict(cfg)), \
             mock.patch.object(wa, "write", side_effect=lambda c: written.update(c)):
            wa.cmd_promote(mock.Mock(phone_number_id=pid))
        return written

    def test_the_new_number_takes_over_sending(self):
        out = self.promote(self.staged)
        self.assertEqual("222", out["phoneNumberId"])
        self.assertEqual("new-token", out["accessToken"])
        self.assertEqual("new-secret", out["appSecret"])

    def test_the_old_number_keeps_everything_it_needs_to_receive(self):
        # Its own token and app secret, or the photograph sent to it cannot be
        # downloaded and the reply cannot be sent.
        out = self.promote(self.staged)
        demoted = out["alsoAccept"][0]
        self.assertEqual("111", demoted["phoneNumberId"])
        self.assertEqual("old-token", demoted["accessToken"])
        self.assertEqual("old-secret", demoted["appSecret"])
        self.assertEqual("old-verify", demoted["webhookVerifyToken"])

    def test_a_shared_app_does_not_duplicate_what_is_already_inherited(self):
        shared = {**OLD, "alsoAccept": [{"phoneNumberId": "222", "wabaId": "bbb",
                                         "accessToken": "old-token"}]}
        out = self.promote(shared)
        demoted = out["alsoAccept"][0]
        self.assertEqual("111", demoted["phoneNumberId"])
        self.assertNotIn("accessToken", demoted)

    def test_promoting_an_unknown_number_is_refused(self):
        with mock.patch.object(wa, "read", return_value=dict(self.staged)):
            with self.assertRaises(SystemExit):
                wa.cmd_promote(mock.Mock(phone_number_id="999"))

    def test_the_result_is_what_the_adapter_reads(self):
        # The whole point of the shape: whatsapp.py has to find both numbers,
        # each with the credentials of the app that owns it. Imported here
        # rather than at module scope because it reads its table names from
        # the environment the moment it loads.
        for var in ("INTAKE_TABLE", "USERS_TABLE", "ORGS_TABLE", "WA_SECRET_ARN",
                    "RECEIPTS_BUCKET", "SUBMISSIONS_TABLE", "FINGERPRINTS_TABLE",
                    "IDEMPOTENCY_TABLE", "SETTINGS_TABLE"):
            os.environ.setdefault(var, "test-" + var.lower())
        os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
        import whatsapp

        out = self.promote(self.staged)
        with mock.patch.object(whatsapp, "config", return_value=out):
            found = {i["phoneNumberId"]: i for i in whatsapp.identities()}
            self.assertEqual({"111", "222"}, set(found))
            self.assertEqual("old-token", found["111"]["accessToken"])
            self.assertEqual("old-secret", found["111"]["appSecret"])
            self.assertEqual("new-token", found["222"]["accessToken"])
            # And the adapter picks the right one from the arriving number.
            self.assertEqual("old-token", whatsapp.creds_for("111")["accessToken"])
            self.assertEqual("new-token", whatsapp.creds_for("222")["accessToken"])


class Finishing(unittest.TestCase):
    def test_dropping_needs_the_word_typed(self):
        staged = {**OLD, "alsoAccept": [{"phoneNumberId": "222"}]}
        with mock.patch.object(wa, "read", return_value=dict(staged)), \
             mock.patch("builtins.input", return_value="yes"):
            with self.assertRaises(SystemExit):
                wa.cmd_finish(None)

    def test_it_clears_the_older_flat_form_too(self):
        staged = {**OLD, "alsoAccept": [{"phoneNumberId": "222"}],
                  "previousAppSecret": "x", "previousWebhookVerifyToken": "y"}
        written = {}
        with mock.patch.object(wa, "read", return_value=dict(staged)), \
             mock.patch.object(wa, "write", side_effect=lambda c: written.update(c)), \
             mock.patch("builtins.input", return_value="drop"):
            wa.cmd_finish(None)
        for gone in ("alsoAccept", "previousAppSecret", "previousWebhookVerifyToken"):
            self.assertNotIn(gone, written)

    def test_finishing_with_one_number_is_refused(self):
        with mock.patch.object(wa, "read", return_value=dict(OLD)):
            with self.assertRaises(SystemExit):
                wa.cmd_finish(None)


class NothingSecretIsPrintedOrPassed(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "tools", "wa_migrate.py"), encoding="utf-8") as h:
            self.source = h.read()

    def test_credentials_are_typed_not_argued(self):
        # An argument lands in shell history and in the process list.
        parser = self.source.split("def main(", 1)[1]
        for leak in ("--access-token", "--app-secret", "--verify-token"):
            self.assertNotIn(leak, parser)
        self.assertIn("getpass.getpass", self.source)

    def test_the_report_names_numbers_and_never_tokens(self):
        report = self.source.split("def write(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("phoneNumberId", report)
        for secret in ("accessToken", "appSecret", "webhookVerifyToken"):
            self.assertNotIn(f"entry.get('{secret}')", report)
            self.assertNotIn(f'entry["{secret}"]', report)


if __name__ == "__main__":
    unittest.main()


class TheWebhookStep(unittest.TestCase):
    """Registering a callback is where the order bites.

    Meta verifies a callback URL by calling it immediately with the verify
    token, so the token has to be in our secret before the URL is registered -
    an endpoint that does not know it yet answers 403 and the registration
    fails with a message about the URL rather than about the token.
    """

    def setUp(self):
        self.staged = {**OLD, "alsoAccept": [
            {"phoneNumberId": "222", "wabaId": "bbb",
             "accessToken": "new-token", "appSecret": "new-secret"}]}

    def run_webhook(self, cfg):
        put = {}
        posts = []

        class FakeClient:
            def put_secret_value(self, SecretId, SecretString):  # noqa: N803
                put.update(json.loads(SecretString))

        with mock.patch.object(wa, "read", return_value=json.loads(json.dumps(cfg))), \
             mock.patch.object(wa, "_client", return_value=FakeClient()), \
             mock.patch.object(wa, "_graph", return_value={
                 "data": {"app_id": "999", "application": "Expenze"}}), \
             mock.patch.object(wa, "_post",
                               side_effect=lambda path, tok, body: (
                                   posts.append((path, body)) or {"success": True})):
            wa.cmd_webhook(mock.Mock(phone_number_id="222"))
        return put, posts

    def test_a_number_with_no_verify_token_of_its_own_gets_one(self):
        # The merged view inherits the old app's token, so asking it always
        # said yes - and the new app was registered under the old one's token,
        # which is the coupling this migration exists to remove.
        put, _ = self.run_webhook(self.staged)
        entry = put["alsoAccept"][0]
        self.assertIn("webhookVerifyToken", entry)
        self.assertNotEqual("old-verify", entry["webhookVerifyToken"])
        self.assertGreater(len(entry["webhookVerifyToken"]), 20)

    def test_an_existing_token_is_reused_rather_than_churned(self):
        cfg = json.loads(json.dumps(self.staged))
        cfg["alsoAccept"][0]["webhookVerifyToken"] = "already-set"
        put, posts = self.run_webhook(cfg)
        self.assertEqual({}, put, "the secret was rewritten for no reason")
        self.assertEqual("already-set", posts[0][1]["verify_token"])

    def test_the_token_is_stored_before_the_callback_is_registered(self):
        # Ordering is the whole point; a test that only checks both happened
        # would pass on the broken version.
        order = []

        class FakeClient:
            def put_secret_value(self, SecretId, SecretString):  # noqa: N803
                order.append("stored")

        with mock.patch.object(wa, "read", return_value=json.loads(json.dumps(self.staged))), \
             mock.patch.object(wa, "_client", return_value=FakeClient()), \
             mock.patch.object(wa, "_graph", return_value={
                 "data": {"app_id": "999", "application": "Expenze"}}), \
             mock.patch.object(wa, "_post",
                               side_effect=lambda path, tok, body: (
                                   order.append("registered") or {"ok": True})):
            wa.cmd_webhook(mock.Mock(phone_number_id="222"))
        self.assertEqual("stored", order[0])

    def test_it_subscribes_the_waba_after_registering(self):
        _, posts = self.run_webhook(self.staged)
        self.assertIn("subscriptions", posts[0][0])
        self.assertEqual("messages", posts[0][1]["fields"])
        self.assertIn("bbb/subscribed_apps", posts[1][0])

    def test_the_callback_is_the_deployed_endpoint(self):
        _, posts = self.run_webhook(self.staged)
        self.assertEqual(wa.WEBHOOK_URL, posts[0][1]["callback_url"])
        self.assertIn("/whatsapp/webhook", wa.WEBHOOK_URL)


class TheSignInCodeNeedsItsOwnTemplate(unittest.TestCase):
    """Promoting broke adding a WhatsApp number, and only that.

    The code that verifies a mobile is sent as an AUTHENTICATION template -
    it has to be, because Meta delivers free-form text only to somebody who
    wrote to the business in the last 24 hours, and a person adding their
    number for the first time never has. The template name lives in the secret
    so a dedicated number can have its own; until it did, the new number went
    on naming the *old* WABA's template, which its own WABA has never heard of:

        HTTP Error 404: Not Found

    Receipts, replies and outcome notices were all fine. Only the one path
    that starts a conversation with a stranger broke.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "tools", "wa_migrate.py"), encoding="utf-8") as h:
            self.source = h.read()
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            self.auth = h.read()

    def test_the_tool_knows_about_the_authentication_template(self):
        self.assertIn('AUTH_TEMPLATE = "expenze_verify_code"', self.source)
        body = self.source.split("AUTH_TEMPLATE: {", 1)[1].split("},\n    \"", 1)[0]
        self.assertIn('"category": "AUTHENTICATION"', body)
        # Rejected without any of these.
        self.assertIn("add_security_recommendation", body)
        self.assertIn("code_expiration_minutes", body)
        self.assertIn("COPY_CODE", body)

    def test_submitting_it_also_points_the_secret_at_it(self):
        # Creating the template and not naming it is the same outage with an
        # extra step: the number still asks for a name its WABA does not know.
        fn = self.source.split("def cmd_templates(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('raw["otpTemplate"] = AUTH_TEMPLATE', fn)
        self.assertIn("put_secret_value", fn)

    def test_the_server_reads_the_name_rather_than_hard_coding_it(self):
        send = self.auth.split("def _send_wa_code(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('cfg.get("otpTemplate"', send)
        self.assertIn('cfg.get("otpTemplateLanguage"', send)

    def test_a_receive_only_number_is_not_asked_for_one(self):
        # It never starts a conversation, so it cannot need an authentication
        # template - reporting one as missing there is noise.
        check = self.source.split("def cmd_check(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if i == 0:", check)
        self.assertIn('entry.get("otpTemplate"', check)
