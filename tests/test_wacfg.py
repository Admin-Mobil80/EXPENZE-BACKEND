"""A cached secret that never expires is a change that never lands.

Moving to the dedicated WhatsApp number is a secret update and nothing else -
no deploy, by design. The swap went through, `check` reported the new number as
primary, and the next person to ask for a verification code got it from the old
number anyway: a warm Lambda container had read the secret once at start-up and
kept that copy for the life of the container.

Nothing failed. No error, no log line, no retry. The code arrived, from the
number the product had already stopped claiming to use, for as long as that
container happened to live - which on a quiet account is hours.

Three modules held their own copy of that cache. These cover the one that
replaced them.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")

import wacfg  # noqa: E402

ARN = "arn:aws:secretsmanager:ap-southeast-1:1:secret:expenze/whatsapp"


def _value(payload):
    return {"SecretString": json.dumps(payload)}


class TheCacheExpires(unittest.TestCase):
    def setUp(self):
        wacfg.forget()
        self.addCleanup(wacfg.forget)

    def test_a_burst_of_calls_reads_the_secret_once(self):
        # The reason a cache exists at all: a receipt can send several
        # messages, and a round trip on each is a bill and a delay.
        client = mock.Mock(get_secret_value=mock.Mock(return_value=_value({"a": 1})))
        with mock.patch.object(wacfg, "_secrets", client):
            for _ in range(10):
                wacfg.config(ARN)
        self.assertEqual(1, client.get_secret_value.call_count)

    def test_a_change_lands_once_the_life_is_up(self):
        # The whole defect: without this the new number never arrives.
        client = mock.Mock(get_secret_value=mock.Mock(
            side_effect=[_value({"phoneNumberId": "old"}),
                         _value({"phoneNumberId": "new"})]))
        with mock.patch.object(wacfg, "_secrets", client):
            self.assertEqual("old", wacfg.config(ARN)["phoneNumberId"])
            wacfg._loaded_at -= wacfg.TTL_SECONDS + 1
            self.assertEqual("new", wacfg.config(ARN)["phoneNumberId"])

    def test_the_life_is_short_enough_to_watch(self):
        # A migration is watched for a few minutes. A cache measured in hours
        # is indistinguishable from one that never expires.
        self.assertLessEqual(wacfg.TTL_SECONDS, 600)
        self.assertGreaterEqual(wacfg.TTL_SECONDS, 30)

    def test_force_skips_the_cache(self):
        client = mock.Mock(get_secret_value=mock.Mock(return_value=_value({"a": 1})))
        with mock.patch.object(wacfg, "_secrets", client):
            wacfg.config(ARN)
            wacfg.config(ARN, force=True)
        self.assertEqual(2, client.get_secret_value.call_count)


class AFailedRefreshKeepsWhatItHas(unittest.TestCase):
    """A stale number still works. No number at all does not."""

    def setUp(self):
        wacfg.forget()
        self.addCleanup(wacfg.forget)

    def test_a_refresh_that_fails_falls_back_to_the_copy_in_hand(self):
        client = mock.Mock(get_secret_value=mock.Mock(
            side_effect=[_value({"phoneNumberId": "old"}), RuntimeError("throttled")]))
        with mock.patch.object(wacfg, "_secrets", client):
            wacfg.config(ARN)
            wacfg._loaded_at -= wacfg.TTL_SECONDS + 1
            self.assertEqual("old", wacfg.config(ARN)["phoneNumberId"])

    def test_a_first_read_that_fails_is_not_swallowed(self):
        # There is nothing to fall back to, and pretending otherwise would
        # send a message to nowhere.
        client = mock.Mock(get_secret_value=mock.Mock(side_effect=RuntimeError("no")))
        with mock.patch.object(wacfg, "_secrets", client):
            with self.assertRaises(RuntimeError):
                wacfg.config(ARN)


class NobodyKeepsTheirOwnCopy(unittest.TestCase):
    """Three caches is three chances for one of them to be the stale one."""

    def setUp(self):
        self.src = os.path.join(os.path.dirname(__file__), "..", "lambda_src")

    def read(self, name):
        with open(os.path.join(self.src, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_three_senders_share_one_cache(self):
        for module in ("auth.py", "notify.py", "whatsapp.py"):
            source = self.read(module)
            self.assertIn("wacfg.config(WA_SECRET_ARN)", source,
                          f"{module} does not use the shared cache")
            self.assertNotIn("_wa_config = json.loads", source,
                             f"{module} still keeps its own copy")

    def test_none_of_them_reads_the_secret_directly(self):
        for module in ("auth.py", "notify.py", "whatsapp.py"):
            source = self.read(module)
            body = source.split("def _wa(", 1)[-1].split("\ndef ", 1)[0]
            self.assertNotIn("get_secret_value(SecretId=WA_SECRET_ARN)", body,
                             f"{module} fetches the WhatsApp secret itself")


if __name__ == "__main__":
    unittest.main()
