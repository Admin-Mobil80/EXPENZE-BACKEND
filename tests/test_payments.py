"""Buying credits: pricing, signatures, and the arithmetic of money.

Everything here guards against a specific way of losing money or giving it
away. The slab lookup stops a client naming its own price; the minor-unit
conversion stops a hundredfold undercharge; the signature checks stop an
unauthenticated endpoint crediting an account on request.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")

import payments  # noqa: E402


PRICING = [
    {"credits": 500, "usd": 50, "inr": 4200},
    {"credits": 1000, "usd": 90, "inr": 7600},
    {"credits": 5000, "usd": 380},
]

TEST_CFG = {"key_id": "rzp_test_ABC123", "key_secret": "secret-key"}

# The secret as it is actually stored: both key sets side by side, and which
# one is switched on.
STORED = {"mode": "test",
          "test": {"key_id": "rzp_test_ABC123", "key_secret": "secret-key"},
          "live": {"key_id": "rzp_live_XYZ789", "key_secret": "live-key"},
          "webhook_secret": "hook-secret"}


def as_configured(stored=None, config=None):
    """Patch both caches together - they are read by different functions."""
    return (mock.patch.object(payments, "_stored_cache", stored or STORED),
            mock.patch.object(payments, "_config", config or TEST_CFG))


class Slabs(unittest.TestCase):
    def test_a_published_slab_is_found(self):
        self.assertEqual(payments.find_slab(PRICING, 1000)["usd"], 90)

    def test_strings_from_a_form_still_match(self):
        self.assertEqual(payments.find_slab(PRICING, "500")["credits"], 500)

    def test_an_unpublished_quantity_is_refused(self):
        # Rounding 4,999 up to a slab would charge a price nobody was shown.
        for wanted in (4999, 0, -100, "lots", None, 999999):
            self.assertIsNone(payments.find_slab(PRICING, wanted), wanted)

    def test_an_empty_price_list_sells_nothing(self):
        self.assertIsNone(payments.find_slab([], 500))
        self.assertIsNone(payments.find_slab(None, 500))


class Prices(unittest.TestCase):
    def test_the_currency_asked_for_is_the_one_charged(self):
        slab = payments.find_slab(PRICING, 500)
        self.assertEqual(payments.price_of(slab, "INR"), 4200)
        self.assertEqual(payments.price_of(slab, "USD"), 50)

    def test_a_missing_price_is_refused_not_converted(self):
        # An exchange rate invented at checkout is a price the customer never
        # agreed to and a figure finance cannot reconcile.
        slab = payments.find_slab(PRICING, 5000)
        with self.assertRaises(payments.PaymentError) as caught:
            payments.price_of(slab, "INR")
        self.assertIn("no price in INR", str(caught.exception))


class MinorUnits(unittest.TestCase):
    """Razorpay charges in paise and cents. A factor-of-100 slip here is the
    difference between ₹4,200 and ₹42."""

    def test_rupees_become_paise(self):
        self.assertEqual(payments.minor_units(4200, "INR"), 420000)

    def test_decimals_survive(self):
        self.assertEqual(payments.minor_units("50.25", "USD"), 5025)

    def test_fractions_of_a_paisa_do_not_round_away_silently(self):
        self.assertEqual(payments.minor_units("0.01", "INR"), 1)

    def test_nothing_free_or_negative_gets_through(self):
        for bad in (0, -1, "0.00"):
            with self.assertRaises(payments.PaymentError, msg=str(bad)):
                payments.minor_units(bad, "INR")

    def test_nonsense_is_refused(self):
        with self.assertRaises(payments.PaymentError):
            payments.minor_units("free", "INR")


class CheckoutSignature(unittest.TestCase):
    def setUp(self):
        for patch in as_configured():
            patch.start()
            self.addCleanup(patch.stop)

    def _sign(self, order, payment, secret="secret-key"):
        return hmac.new(secret.encode(), f"{order}|{payment}".encode(),
                        hashlib.sha256).hexdigest()

    def test_a_genuine_signature_passes(self):
        sig = self._sign("order_1", "pay_1")
        self.assertTrue(payments.checkout_signature_ok("order_1", "pay_1", sig))

    def test_a_signature_for_another_order_is_refused(self):
        # Replaying a real signature against a different, larger order is the
        # obvious attack, and the order id is inside the signed string.
        sig = self._sign("order_1", "pay_1")
        self.assertFalse(payments.checkout_signature_ok("order_2", "pay_1", sig))

    def test_a_signature_from_the_wrong_secret_is_refused(self):
        sig = self._sign("order_1", "pay_1", secret="not-the-key")
        self.assertFalse(payments.checkout_signature_ok("order_1", "pay_1", sig))

    def test_nothing_missing_is_ever_treated_as_valid(self):
        for args in (("", "pay_1", "sig"), ("order_1", "", "sig"),
                     ("order_1", "pay_1", ""), ("", "", "")):
            self.assertFalse(payments.checkout_signature_ok(*args), args)


class WebhookSignature(unittest.TestCase):
    def setUp(self):
        for patch in as_configured():
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_genuine_webhook_passes(self):
        body = '{"event":"payment.captured"}'
        sig = hmac.new(b"hook-secret", body.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(payments.webhook_signature_ok(body, sig))

    def test_a_changed_body_is_refused(self):
        body = '{"event":"payment.captured"}'
        sig = hmac.new(b"hook-secret", body.encode(), hashlib.sha256).hexdigest()
        self.assertFalse(payments.webhook_signature_ok(body + " ", sig))

    def test_the_api_secret_does_not_verify_a_webhook(self):
        # Separate secrets on purpose: knowing how to call the API must not be
        # enough to forge a payment notification into a public endpoint.
        body = '{"event":"payment.captured"}'
        sig = hmac.new(b"secret-key", body.encode(), hashlib.sha256).hexdigest()
        self.assertFalse(payments.webhook_signature_ok(body, sig))

    def test_an_empty_body_or_signature_never_passes(self):
        self.assertFalse(payments.webhook_signature_ok("", "sig"))
        self.assertFalse(payments.webhook_signature_ok("{}", ""))


class Mode(unittest.TestCase):
    def test_the_key_itself_says_which_mode_this_is(self):
        # A stored flag can disagree with the key beside it; a key cannot
        # disagree with itself, so the key is what is reported.
        with mock.patch.object(payments, "_config", TEST_CFG):
            self.assertEqual(payments.mode(), "test")
        with mock.patch.object(payments, "_config", {"key_id": "rzp_live_XYZ"}):
            self.assertEqual(payments.mode(), "live")

    def test_an_unrecognisable_key_is_never_called_live(self):
        with mock.patch.object(payments, "_config", {"key_id": ""}):
            self.assertEqual(payments.mode(), "test")

    def test_the_switch_chooses_which_key_set_is_used(self):
        with mock.patch.object(payments, "_stored_cache", STORED), \
             mock.patch.object(payments, "_config", None):
            self.assertEqual(payments.selected_mode(), "test")
            self.assertEqual(payments.config()["key_id"], "rzp_test_ABC123")
        live = {**STORED, "mode": "live"}
        with mock.patch.object(payments, "_stored_cache", live), \
             mock.patch.object(payments, "_config", None):
            self.assertEqual(payments.selected_mode(), "live")
            self.assertEqual(payments.config()["key_id"], "rzp_live_XYZ789")

    def test_switching_to_a_mode_with_no_keys_says_so(self):
        with mock.patch.object(payments, "_stored_cache", {"mode": "live", "test": STORED["test"]}), \
             mock.patch.object(payments, "_config", None):
            with self.assertRaises(payments.PaymentError) as caught:
                payments.config()
        self.assertIn("live", str(caught.exception))

    def test_an_older_flat_secret_still_works(self):
        # One key pair, no mode, as the secret was first written. Read as
        # whichever mode its key id says, so an upgrade needs no edit.
        flat = {"key_id": "rzp_test_OLD", "key_secret": "old-key"}
        with mock.patch.object(payments, "_stored_cache", None), \
             mock.patch.object(payments, "_config", None), \
             mock.patch.object(payments, "RZP_SECRET_ARN", "arn:secret"), \
             mock.patch.object(payments, "_secrets") as sm:
            import json as _json
            sm.get_secret_value.return_value = {"SecretString": _json.dumps(flat)}
            self.assertEqual(payments.selected_mode(), "test")
            self.assertEqual(payments.config()["key_id"], "rzp_test_OLD")


class PurchaseRow(unittest.TestCase):
    def test_it_records_what_was_sold_and_for_how_much(self):
        with mock.patch.object(payments, "_config", TEST_CFG):
            row = payments.purchase_row(
                {"id": "order_9", "amount": 420000}, "org_1", 500, "INR",
                4200, "riyad@mobil80.com")
        self.assertEqual(row["order_id"], "order_9")
        self.assertEqual(row["credits"], 500)
        self.assertEqual(row["amount_minor"], 420000)
        self.assertEqual(row["status"], "created")
        self.assertEqual(row["mode"], "test")
        self.assertEqual(row["started_by"], "riyad@mobil80.com")



class CheckoutCurrency(unittest.TestCase):
    """Charged in the same money the organisation is reported to in."""

    def test_an_indian_organisation_pays_in_rupees(self):
        self.assertEqual(
            payments.checkout_currency({"address": {"country": "IN"}}), "INR")

    def test_everyone_else_pays_in_dollars(self):
        for country in ("SG", "GB", "AE", "US", "OTHER"):
            self.assertEqual(
                payments.checkout_currency({"address": {"country": country}}),
                "USD", country)

    def test_an_organisation_with_no_country_pays_in_dollars(self):
        # The published pricing is USD, so that is the safe assumption when
        # nobody has said where the company is.
        self.assertEqual(payments.checkout_currency({}), "USD")
        self.assertEqual(payments.checkout_currency({"address": {}}), "USD")

    def test_the_case_of_the_stored_country_does_not_matter(self):
        self.assertEqual(
            payments.checkout_currency({"address": {"country": "in"}}), "INR")


class GstBreakdown(unittest.TestCase):
    """India is charged the published INR price plus GST on top. The tax is its
    own line: an accountant reclaiming input credit needs the base and the tax
    separately, and so does the invoice."""

    SLAB = {"credits": 500, "usd": 50, "inr": 4200}

    def test_indian_sales_carry_gst_on_top(self):
        got = payments.price_breakdown(self.SLAB, "INR", 18)
        self.assertEqual(got["base"], "4200.00")
        self.assertEqual(got["tax"], "756.00")
        self.assertEqual(got["total"], "4956.00")
        self.assertEqual(got["tax_label"], "GST 18%")

    def test_the_charged_amount_is_the_total_including_tax(self):
        got = payments.price_breakdown(self.SLAB, "INR", 18)
        self.assertEqual(got["total_minor"], 495600)

    def test_base_plus_tax_always_equals_the_total_charged(self):
        # Rounding once, on the tax alone, is what keeps this true - an invoice
        # whose lines do not add up to what was taken is unusable.
        from decimal import Decimal
        for price in (4200, 7600, 17600, 31900, 58400, 130200, 228900, 399000, 1, 333):
            got = payments.price_breakdown({"inr": price}, "INR", 18)
            self.assertEqual(Decimal(got["base"]) + Decimal(got["tax"]),
                             Decimal(got["total"]), price)
            self.assertEqual(payments.minor_units(got["total"], "INR"),
                             got["total_minor"], price)

    def test_a_usd_sale_is_never_taxed_at_checkout(self):
        # An export of services. Adding GST here would be wrong twice over.
        got = payments.price_breakdown(self.SLAB, "USD", 18)
        self.assertEqual(got["base"], "50.00")
        self.assertEqual(got["tax"], "0.00")
        self.assertEqual(got["total"], "50.00")
        self.assertEqual(got["tax_label"], "")

    def test_a_changed_rate_is_one_number(self):
        self.assertEqual(payments.price_breakdown(self.SLAB, "INR", 5)["tax"], "210.00")
        self.assertEqual(payments.price_breakdown(self.SLAB, "INR", 0)["tax"], "0.00")

    def test_a_nonsense_rate_is_refused_not_ignored(self):
        for bad in ("eighteen", -1):
            with self.assertRaises(payments.PaymentError, msg=str(bad)):
                payments.price_breakdown(self.SLAB, "INR", bad)

    def test_a_slab_with_no_indian_price_cannot_be_sold_in_india(self):
        with self.assertRaises(payments.PaymentError):
            payments.price_breakdown({"credits": 5000, "usd": 380}, "INR", 18)


class Unconfigured(unittest.TestCase):
    """The window between `cdk deploy` creating the secret and somebody putting
    keys in it. Anything reaching the API in that window should say so, not
    return a stack trace as a 502."""

    def test_an_empty_secret_is_a_clear_message(self):
        with mock.patch.object(payments, "_stored_cache", None), \
             mock.patch.object(payments, "_config", None), \
             mock.patch.object(payments, "RZP_SECRET_ARN", "arn:secret"), \
             mock.patch.object(payments, "_secrets") as sm:
            sm.get_secret_value.return_value = {"SecretString": ""}
            with self.assertRaises(payments.PaymentError) as caught:
                payments.config()
        # Says where to fix it, not which JSON key is missing.
        self.assertIn("BMS", str(caught.exception))

    def test_a_key_set_with_no_secret_half_is_refused(self):
        with mock.patch.object(payments, "_stored_cache",
                               {"mode": "test", "test": {"key_id": "rzp_test_x"}}), \
             mock.patch.object(payments, "_config", None):
            with self.assertRaises(payments.PaymentError):
                payments.config()

    def test_no_webhook_secret_means_no_signature_can_verify(self):
        # And that is fine: with no secret the delivery is a hint, and
        # confirm_payment is what actually decides. Nothing throws.
        with mock.patch.object(payments, "_stored_cache", {"mode": "test", "test": TEST_CFG}):
            self.assertEqual(payments.webhook_secret(), "")
            self.assertFalse(payments.webhook_signature_ok("{}", "deadbeef"))


class ConfirmPayment(unittest.TestCase):
    """A valid signature proves the message came from Razorpay. It does not
    prove which Razorpay, nor what the payment was for - and with one webhook
    URL serving both test and live, both of those matter."""

    def setUp(self):
        for patch in as_configured():
            patch.start()
            self.addCleanup(patch.stop)

    def _payment(self, **over):
        return {"id": "pay_1", "order_id": "order_1", "status": "captured",
                "amount": 495600, **over}

    def test_a_matching_payment_is_accepted(self):
        with mock.patch.object(payments, "fetch_payment", return_value=self._payment()):
            self.assertEqual(payments.confirm_payment("order_1", "pay_1", 495600), "")

    def test_a_payment_from_the_other_mode_cannot_be_fetched_and_is_refused(self):
        # A live payment is invisible to a test key and vice versa, so this is
        # what a cross-mode webhook looks like from here.
        with mock.patch.object(payments, "fetch_payment",
                               side_effect=payments.PaymentError("id does not exist")):
            why = payments.confirm_payment("order_1", "pay_1", 495600)
        self.assertIn("wrong mode", why)

    def test_a_cheaper_payment_cannot_credit_an_expensive_order(self):
        # Without this a one-rupee test payment credits a slab sold for
        # four hundred thousand.
        with mock.patch.object(payments, "fetch_payment",
                               return_value=self._payment(amount=100)):
            why = payments.confirm_payment("order_1", "pay_1", 47082000)
        self.assertIn("but the order was for", why)

    def test_a_payment_for_another_order_is_refused(self):
        with mock.patch.object(payments, "fetch_payment",
                               return_value=self._payment(order_id="order_9")):
            self.assertIn("different order",
                          payments.confirm_payment("order_1", "pay_1", 495600))

    def test_an_uncaptured_payment_is_refused(self):
        for status in ("failed", "created", "refunded"):
            with mock.patch.object(payments, "fetch_payment",
                                   return_value=self._payment(status=status)):
                self.assertIn("not captured",
                              payments.confirm_payment("order_1", "pay_1", 495600), status)

    def test_order_paid_without_a_payment_id_still_passes(self):
        # Some plans send order.paid carrying no payment of its own. The
        # signature is the only proof available, and the order is still
        # claimed exactly once.
        with mock.patch.object(payments, "fetch_payment") as fetch:
            self.assertEqual(payments.confirm_payment("order_1", "order:order_1", 495600), "")
            fetch.assert_not_called()

    def test_crediting_refuses_a_payment_that_does_not_confirm(self):
        with mock.patch.object(payments, "_purchases") as purchases, \
             mock.patch.object(payments, "fetch_payment",
                               return_value=self._payment(amount=100)):
            purchases.get_item.return_value = {"Item": {
                "order_id": "order_1", "org_id": "org_1", "credits": 100000,
                "amount_minor": 47082000}}
            with self.assertRaises(payments.PaymentError):
                payments.credit_purchase("order_1", "pay_1", "webhook")
            # Nothing was claimed, so nothing was credited.
            purchases.update_item.assert_not_called()

if __name__ == "__main__":
    unittest.main()
