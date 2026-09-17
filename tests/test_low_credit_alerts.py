"""Warning the people who can top up, while topping up is still the fix.

Running out of credits is not a loud failure. Receipts keep arriving and queue
unaudited, each sender is told their receipt was not processed, and the first
anyone in finance hears of it is a colleague asking why their claim vanished.
By then it is a backlog rather than a purchase.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
for _k, _v in (("AWS_DEFAULT_REGION", "ap-southeast-1"), ("ORGS_TABLE", "o"),
               ("USERS_TABLE", "u"), ("SETTINGS_TABLE", "s"), ("INTAKE_TABLE", "i")):
    os.environ.setdefault(_k, _v)

ROOT = os.path.join(os.path.dirname(__file__), "..")

import alerts  # noqa: E402


class WhenItFires(unittest.TestCase):
    ORG = {"org_id": "org-1", "name": "Mobil80", "receipts_processed": 300,
           "created_at": 0}

    def setUp(self):
        self.sent = []
        patches = [
            mock.patch.object(alerts, "_thresholds",
                              return_value={"on": True, "credits": 50, "days": 7}),
            mock.patch.object(alerts, "_recipients",
                              return_value=[{"email": "owner@x.com", "role": "owner"}]),
            mock.patch.object(alerts, "_stamp"),
            mock.patch.object(alerts, "_clear"),
            mock.patch.object(alerts.notify, "send",
                              side_effect=lambda k, m, c: self.sent.append((k, m, c))),
        ]
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        self.stamp = patches[2].new if hasattr(patches[2], "new") else None

    def test_a_healthy_balance_says_nothing(self):
        alerts.check_low_credits({**self.ORG, "created_at": 1}, 5000)
        self.assertEqual(self.sent, [])

    def test_crossing_the_credit_threshold_alerts(self):
        alerts.check_low_credits({**self.ORG, "created_at": 1}, 40)
        self.assertEqual(len(self.sent), 1)
        kind, member, claim = self.sent[0]
        self.assertEqual(kind, "low_credits")
        self.assertEqual(claim["balance"], 40)

    def test_zero_alerts_too(self):
        alerts.check_low_credits({**self.ORG, "created_at": 1}, 0)
        self.assertEqual(len(self.sent), 1)

    def test_it_only_fires_once_per_crossing(self):
        # The threshold is passed on one receipt and every receipt after it. An
        # organisation filing forty a day would otherwise get forty warnings.
        alerts.check_low_credits(
            {**self.ORG, "created_at": 1, "low_credit_alerted_at": 123}, 10)
        self.assertEqual(self.sent, [])

    def test_recovering_re_arms_it(self):
        with mock.patch.object(alerts, "_clear") as clear:
            alerts.check_low_credits(
                {**self.ORG, "created_at": 1, "low_credit_alerted_at": 123}, 9000)
        clear.assert_called_once()

    def test_turning_it_off_turns_it_off(self):
        with mock.patch.object(alerts, "_thresholds", return_value={"on": False}):
            alerts.check_low_credits({**self.ORG, "created_at": 1}, 0)
        self.assertEqual(self.sent, [])

    def test_a_failure_never_touches_the_receipt(self):
        # The claim is charged and recorded by the time this runs.
        with mock.patch.object(alerts, "_recipients", side_effect=RuntimeError("boom")):
            alerts.check_low_credits({**self.ORG, "created_at": 1}, 0)


class Runway(unittest.TestCase):
    def test_no_usage_means_no_projection(self):
        # A new customer has a balance, not a runway; inventing a figure would
        # put a number on the alert that means nothing.
        self.assertEqual(alerts._runway_days({"receipts_processed": 0, "created_at": 1}, 500), 0)

    def test_it_is_measured_from_what_was_actually_used(self):
        import time
        org = {"receipts_processed": 100, "created_at": int(time.time()) - 100 * 86400}
        self.assertEqual(alerts._runway_days(org, 50), 50)   # 1/day


class OnlyPeopleWhoCanAct(unittest.TestCase):
    def test_the_roles_that_can_buy_credits(self):
        self.assertEqual(set(alerts.CAN_TOP_UP), {"owner", "finance"})


class TheSettingIsInTheBackOffice(unittest.TestCase):
    def setUp(self):
        for name, path in (("admin", "lambda_src/admin.py"), ("bms", "../BMS/home.html")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_both_thresholds_are_settable(self):
        for field in ("low_credit_credits", "low_credit_days", "low_credit_alerts"):
            self.assertIn(field, self.admin, field)
            self.assertIn(field, self.bms, field)

    def test_they_are_validated(self):
        self.assertIn("Low-credit thresholds must be whole numbers.", self.admin)
        self.assertIn("The credit threshold must be between 0 and 100,000.", self.admin)

    def test_the_back_office_explains_the_two_of_them(self):
        self.assertIn("Whichever is crossed first", self.bms)


class TheNoticeSaysWhatHappensNext(unittest.TestCase):
    def setUp(self):
        import notify  # noqa: E402
        self.notify = notify

    def test_it_is_a_notice_like_any_other(self):
        self.assertIn("low_credits", self.notify.NOTICES)

    def test_running_out_explains_the_consequence(self):
        said = self.notify.low_credits_notice(
            {"balance": 0, "org_name": "Mobil80"})["text"]
        self.assertIn("queued", said)
        self.assertIn("Nobody's claim is lost", said)

    def test_a_low_balance_quotes_the_runway(self):
        said = self.notify.low_credits_notice(
            {"balance": 40, "org_name": "Mobil80", "days_left": 6})["text"]
        self.assertIn("40", said)
        self.assertIn("6 days", said)

    def test_it_does_not_go_out_on_a_borrowed_template(self):
        self.assertIn("low_credits", self.notify.NO_TEMPLATE)
        self.assertNotIn("low_credits", self.notify.TEMPLATES)


if __name__ == "__main__":
    unittest.main()
