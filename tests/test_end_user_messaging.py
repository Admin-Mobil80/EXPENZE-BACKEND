"""Carrying the same WhatsApp number through AWS instead of through Meta.

The move replaces a bearer token in a secret with the Lambda's own IAM role,
which is worth having on its own. What it must not do is change a single thing
a person receives - the templates, the wording, the number it comes from and
the thread it lands in are all exactly as they were, because the Message object
is handed to AWS untouched.

Most of what is tested here is the ways that could quietly stop being true.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))

for _k, _v in (("AWS_DEFAULT_REGION", "ap-southeast-1"), ("INTAKE_TABLE", "t"),
               ("WA_SECRET_ARN", "arn:aws:secretsmanager:x:1:secret:y"),
               ("USERS_TABLE", "u"), ("ORGS_TABLE", "o"), ("INTAKE_BUCKET", "b"),
               ("RECEIPTS_BUCKET", "receipts"), ("EXPENSES_TABLE", "e")):
    os.environ.setdefault(_k, _v)

import eum  # noqa: E402


META_ID = "1364912593363578"
AWS_ID = "phone-number-id-186b71bed8f0445195b84f57d375f757"

LIVE = {
    "phoneNumberId": META_ID,
    "accessToken": "t",
    "transport": "aws",
    "aws": {"region": "us-east-1", "phoneNumberId": AWS_ID},
}


class WhichTransportIsLive(unittest.TestCase):
    """A deploy must not move anybody's traffic by itself."""

    def test_a_secret_that_has_never_heard_of_aws_still_uses_meta(self):
        # The secret in production the moment this ships. If `enabled` read
        # anything into its silence, the first deploy would be the cutover.
        self.assertFalse(eum.enabled({"phoneNumberId": META_ID, "accessToken": "t"}))

    def test_meta_is_the_default_even_with_an_aws_block_present(self):
        self.assertFalse(eum.enabled({**LIVE, "transport": "meta"}))

    def test_saying_aws_turns_it_on(self):
        self.assertTrue(eum.enabled(LIVE))

    def test_the_word_is_read_loosely(self):
        # Somebody editing a secret by hand types "AWS".
        self.assertTrue(eum.enabled({**LIVE, "transport": " AWS "}))

    def test_a_half_written_secret_keeps_sending_the_old_way(self):
        """`transport: aws` with no number to send from.

        The dangerous reading is "the operator asked for AWS, so refuse to use
        Meta" - which turns a half-finished edit into an outage. Messages keep
        going out the way they already were until the secret is complete.
        """
        self.assertFalse(eum.enabled({**LIVE, "aws": {"region": "us-east-1"}}))
        self.assertFalse(eum.enabled({**LIVE, "aws": "not even a block"}))


class TheNumberHasTwoNames(unittest.TestCase):
    """Meta's id and the AWS id name one number and look nothing alike."""

    def test_our_own_number_is_found_without_being_named(self):
        self.assertEqual(eum.number_for(LIVE), AWS_ID)

    def test_a_delivery_naming_the_meta_id_resolves_to_the_aws_id(self):
        self.assertEqual(eum.number_for(LIVE, META_ID), AWS_ID)

    def test_a_second_live_number_resolves_to_its_own_aws_id(self):
        """Answering from the wrong number splits the sender's thread.

        During a migration the WABA holds two numbers. This is the same
        failure `creds_for` guards against on the Meta path, in the other
        vocabulary - and it has to be guarded in both or the cutover
        reintroduces the bug the last migration fixed.
        """
        other = "1212181331972066"
        cfg = {**LIVE, "alsoAccept": [
            {"phoneNumberId": other, "aws": {"phoneNumberId": "phone-number-id-old"}}]}
        self.assertEqual(eum.number_for(cfg, other), "phone-number-id-old")
        self.assertEqual(eum.number_for(cfg, META_ID), AWS_ID)

    def test_a_number_aws_does_not_manage_yields_nothing(self):
        """Rather than falling back to ours, which would send from the wrong one.

        An `alsoAccept` entry with no `aws` block is a number still on the Meta
        path. Returning our id for it would answer that sender from a number
        they have never seen.
        """
        cfg = {**LIVE, "alsoAccept": [{"phoneNumberId": "999"}]}
        self.assertEqual(eum.number_for(cfg, "999"), "")

    def test_an_unknown_number_falls_back_to_ours(self):
        # Not a migration case: a delivery for a number the secret has never
        # mentioned. Our own number is the only sane sender.
        self.assertEqual(eum.number_for(LIVE, "nonsense"), AWS_ID)


class TheMessageIsNotTouched(unittest.TestCase):
    """Every template and every word survives the change of carrier."""

    def test_the_message_object_arrives_at_aws_byte_for_byte(self):
        sent = {}

        class Client:
            def send_whatsapp_message(self, **kw):
                sent.update(kw)
                return {"messageId": "wamid.1"}

        message = {
            "messaging_product": "whatsapp", "to": "919", "type": "template",
            "template": {"name": "expenze_claim_settled",
                         "language": {"code": "en"},
                         "components": [{"type": "body", "parameters": [
                             {"type": "text", "text": "The Copper Table"}]}]},
        }
        with mock.patch.object(eum, "_client", return_value=Client()):
            self.assertTrue(eum.send(LIVE, message))

        self.assertEqual(json.loads(sent["message"].decode()), message)
        self.assertEqual(sent["originationPhoneNumberId"], AWS_ID)
        self.assertTrue(sent["metaApiVersion"].startswith("v"))

    def test_a_refusal_is_reported_not_raised(self):
        """The settlement already happened; a bounced notice must not undo it.

        Every caller on this path treats delivery as something to report. A
        transport that raised would turn a missed message into a failed
        settlement.
        """
        class Client:
            def send_whatsapp_message(self, **kw):
                raise RuntimeError("throttled")

        with mock.patch.object(eum, "_client", return_value=Client()):
            self.assertFalse(eum.send(LIVE, {"type": "text"}))

    def test_nothing_is_sent_when_there_is_no_number_to_send_from(self):
        calls = []

        class Client:
            def send_whatsapp_message(self, **kw):
                calls.append(kw)

        cfg = {**LIVE, "alsoAccept": [{"phoneNumberId": "999"}]}
        with mock.patch.object(eum, "_client", return_value=Client()):
            self.assertFalse(eum.send(cfg, {"type": "text"}, "999"))
        self.assertEqual(calls, [])


class InboundArrivesWrapped(unittest.TestCase):
    """AWS does not invent a shape; it posts Meta's inside an SNS envelope."""

    @staticmethod
    def _sns(entry):
        return {"Records": [{"Sns": {"Message": json.dumps(
            {"context": {"MetaWabaIds": []},
             "whatsAppWebhookEntry": json.dumps(entry)})}}]}

    def test_the_meta_entry_comes_back_out_intact(self):
        entry = {"id": "waba", "changes": [{"value": {
            "metadata": {"phone_number_id": META_ID},
            "messages": [{"from": "919", "type": "image",
                          "image": {"id": "media-1"}}]}}]}
        self.assertEqual(eum.entries(self._sns(entry)), [entry])

    def test_an_sns_event_is_told_apart_from_an_api_gateway_one(self):
        self.assertTrue(eum.is_sns(self._sns({"id": "x"})))
        self.assertFalse(eum.is_sns({"httpMethod": "POST", "body": "{}"}))
        self.assertFalse(eum.is_sns({}))
        # S3 and SQS also deliver `Records`, and neither is ours.
        self.assertFalse(eum.is_sns({"Records": [{"s3": {}}]}))

    def test_one_unreadable_record_does_not_cost_the_rest_of_the_batch(self):
        """SNS redelivers whole batches.

        Raising on a malformed record would redeliver the good ones too, and
        every receipt among them would be charged a second credit.
        """
        good = {"id": "good", "changes": []}
        event = {"Records": [
            {"Sns": {"Message": "{not json"}},
            {"Sns": {"Message": json.dumps({"whatsAppWebhookEntry": "{also not json"})}},
            {"Sns": {"Message": json.dumps({"whatsAppWebhookEntry": json.dumps(good)})}},
        ]}
        self.assertEqual(eum.entries(event), [good])

    def test_an_entry_sent_as_a_list_is_accepted(self):
        a, b = {"id": "a"}, {"id": "b"}
        event = {"Records": [{"Sns": {"Message": json.dumps(
            {"whatsAppWebhookEntry": json.dumps([a, b])})}}]}
        self.assertEqual(eum.entries(event), [a, b])


class MediaNeverArrivesInline(unittest.TestCase):
    """AWS returns a size and a type and writes the bytes to S3."""

    def _client_and_s3(self, mime="image/jpeg", size_kb=12, body=b"x" * 100):
        staged = {}

        class Client:
            def get_whatsapp_message_media(self, **kw):
                if kw.get("metadataOnly"):
                    return {"mimeType": mime, "fileSize": size_kb}
                staged["key"] = kw["destinationS3File"]["key"]
                return {}

        class S3:
            def get_object(self, Bucket, Key):
                return {"Body": mock.Mock(read=lambda n: body[:n])}

            def delete_object(self, Bucket, Key):
                staged["deleted"] = Key

        return Client(), S3(), staged

    def test_the_bytes_and_the_type_come_back(self):
        client, s3, _ = self._client_and_s3()
        with mock.patch.object(eum, "_client", return_value=client), \
             mock.patch.object(eum, "_s3", return_value=s3):
            got = eum.fetch_media(LIVE, "media-1", META_ID, 10 * 1024 * 1024)
        self.assertEqual(got, (b"x" * 100, "image/jpeg"))

    def test_the_staged_copy_is_always_removed(self):
        """It exists only to carry the bytes across.

        The original is stored under its own fingerprinted key by
        `receipts.put`. Leaving the staging object behind would keep a second,
        unreferenced copy of somebody's receipt in the bucket for ever.
        """
        client, s3, staged = self._client_and_s3()
        with mock.patch.object(eum, "_client", return_value=client), \
             mock.patch.object(eum, "_s3", return_value=s3):
            eum.fetch_media(LIVE, "media-1", META_ID, 10 * 1024 * 1024)
        self.assertEqual(staged.get("deleted"), staged.get("key"))

    def test_it_is_removed_even_when_the_download_fails(self):
        class Client:
            def get_whatsapp_message_media(self, **kw):
                if kw.get("metadataOnly"):
                    return {"mimeType": "image/jpeg", "fileSize": 12}
                raise RuntimeError("gone")

        deleted = []

        class S3:
            def get_object(self, **kw):
                raise AssertionError("should not be reached")

            def delete_object(self, Bucket, Key):
                deleted.append(Key)

        with mock.patch.object(eum, "_client", return_value=Client()), \
             mock.patch.object(eum, "_s3", return_value=S3()):
            self.assertIsNone(
                eum.fetch_media(LIVE, "media-1", META_ID, 10 * 1024 * 1024))
        self.assertEqual(len(deleted), 1)

    def test_the_limit_is_enforced_on_the_bytes_that_arrived(self):
        """Not on the stated size, which is in different units on each path.

        Meta reports `file_size` in bytes and AWS reports `fileSize` in KB. A
        limit checked against the wrong unit is either a thousand times too
        strict or not a limit at all, so the stated size only rejects the
        obviously enormous and the real check is on what turned up.
        """
        client, s3, _ = self._client_and_s3(size_kb=1, body=b"y" * 5000)
        with mock.patch.object(eum, "_client", return_value=client), \
             mock.patch.object(eum, "_s3", return_value=s3):
            self.assertIsNone(eum.fetch_media(LIVE, "media-1", META_ID, 4000))

    def test_something_enormous_is_refused_before_it_is_written(self):
        downloaded = []

        class Client:
            def get_whatsapp_message_media(self, **kw):
                if kw.get("metadataOnly"):
                    return {"mimeType": "video/mp4", "fileSize": 500_000}
                downloaded.append(kw)
                return {}

        with mock.patch.object(eum, "_client", return_value=Client()), \
             mock.patch.object(eum, "_s3", return_value=mock.Mock()):
            self.assertIsNone(
                eum.fetch_media(LIVE, "media-1", META_ID, 10 * 1024 * 1024))
        self.assertEqual(downloaded, [])


if __name__ == "__main__":
    unittest.main()
