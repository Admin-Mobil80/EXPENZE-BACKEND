"""Catching the same receipt twice.

People resend: the reply is slow so they send the photo again, or they forward
the hotel's emailed invoice having already photographed the printed copy. And
occasionally somebody claims one dinner twice on purpose, which is the case
finance actually worries about.

Two questions with different confidence, and the tests here are mostly about
keeping them apart. Identical bytes are certainly the same photograph. The same
vendor, date, total and currency are only *probably* the same bill - two people
can buy the same coffee at the same shop on the same morning - so that one is
flagged for a human rather than refused.

The rest guards the ways this could cost somebody money they are owed: a
fingerprint held by a rejected claim, a fingerprint built from a total nobody
could read, or a claim taken by a submission that was never created because the
charge failed.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")

from test_apikeys import FakeTable  # noqa: E402

import duplicates  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class Fingerprints(unittest.TestCase):
    """Two ways of asking whether this bill has been claimed before.

    `invoice_key` is the one that is not a guess: a vendor does not issue two
    different invoices under one number. `submitter_key` needs nothing printed
    on the paper at all, which is what covers a handwritten bill.

    What used to sit here was "same vendor, same day, same amount". It was the
    best available while no invoice number was read off the bill, and it was
    wrong in the commonest case in business: two colleagues each holding a seat
    of the same SaaS product, billed on the same day for the same price, are
    not duplicates of each other - and it called them one every month. A
    control that cries wolf on every subscription renewal is one people learn
    to dismiss, and then it is worth nothing on the day it is right.
    """

    def test_one_invoice_number_is_one_bill_however_the_shop_is_spelled(self):
        a = duplicates.invoice_key("org-1", "Cursor", "INV-2026-0041")
        b = duplicates.invoice_key("org-1", "Cursor (Anysphere Inc.)", "inv 2026 0041")
        self.assertEqual(a, b)
        self.assertTrue(a)

    def test_two_seats_of_one_subscription_are_not_duplicates(self):
        # The case the old rule got wrong: same vendor, same day, same price,
        # two people, two invoices.
        self.assertNotEqual(
            duplicates.invoice_key("org-1", "Cursor", "INV-1001"),
            duplicates.invoice_key("org-1", "Cursor", "INV-1002"))

    def test_a_different_supplier_with_the_same_number_does_not_collide(self):
        self.assertNotEqual(
            duplicates.invoice_key("org-1", "Cursor", "INV-1001"),
            duplicates.invoice_key("org-1", "Nandhini Deluxe", "INV-1001"))

    def test_another_organisation_never_collides(self):
        self.assertNotEqual(
            duplicates.invoice_key("org-1", "Cafe", "INV-1001"),
            duplicates.invoice_key("org-2", "Cafe", "INV-1001"))

    def test_a_bill_with_no_number_has_no_invoice_fingerprint(self):
        # Normal for a handwritten or small retail bill. Returning something
        # would collide every such bill from one vendor with every other.
        for missing in (
            duplicates.invoice_key("org-1", "Cafe", ""),
            duplicates.invoice_key("org-1", "Cafe", "   "),
            duplicates.invoice_key("org-1", "", "INV-1001"),
        ):
            self.assertEqual(missing, "")

    def test_something_that_is_not_a_number_is_not_a_number(self):
        # OCR reads the word where the number should be, and a "number" with no
        # digits in it identifies nothing.
        for junk in ("INVOICE", "-", "#", "n/a"):
            self.assertEqual("", duplicates.invoice_key("org-1", "Cafe", junk), junk)

    def test_one_person_one_bill_date_one_amount_is_the_other_way_in(self):
        a = duplicates.submitter_key("org-1", "rijo@x.com", "2026-09-01", "5500.00", "INR")
        b = duplicates.submitter_key("org-1", "RIJO@X.com ", "2026-09-01", "5500", "inr")
        self.assertEqual(a, b)
        self.assertTrue(a)

    def test_a_thousand_separator_does_not_change_the_amount(self):
        self.assertEqual(
            duplicates.submitter_key("org-1", "a@x.com", "2026-09-01", "5,500.00", "INR"),
            duplicates.submitter_key("org-1", "a@x.com", "2026-09-01", "5500.0", "INR"))

    def test_a_receipt_missing_a_fact_has_no_fingerprint(self):
        # The important one. An unreadable total would otherwise fingerprint as
        # "this person, nothing" and collide with every other unreadable bill
        # they ever sent - turning a failure to read into an accusation.
        for missing in (
            duplicates.submitter_key("org-1", "", "2026-09-01", "100.00", "INR"),
            duplicates.submitter_key("org-1", "a@x.com", "", "100.00", "INR"),
            duplicates.submitter_key("org-1", "a@x.com", "2026-09-01", "", "INR"),
            duplicates.submitter_key("org-1", "a@x.com", "2026-09-01", "100.00", ""),
            duplicates.submitter_key("org-1", "a@x.com", "2026-09-01", "nope", "INR"),
        ):
            self.assertEqual(missing, "")

    def test_identical_bytes_hash_the_same_and_different_bytes_do_not(self):
        self.assertEqual(duplicates.content_hash(b"abc"), duplicates.content_hash(b"abc"))
        self.assertNotEqual(duplicates.content_hash(b"abc"), duplicates.content_hash(b"abd"))


class Claiming(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable("fingerprint")
        patch = mock.patch.object(duplicates, "_table", self.table)
        patch.start()
        self.addCleanup(patch.stop)
        self.key = duplicates.invoice_key("org-1", "Cafe", "INV-1001")

    def test_the_first_claim_wins_and_the_second_is_told_who_holds_it(self):
        self.assertIsNone(duplicates.claim(self.key, "sub-1"))
        self.assertEqual(duplicates.claim(self.key, "sub-2"), "sub-1")

    def test_a_submission_re_claiming_its_own_fingerprint_is_not_a_duplicate(self):
        # A re-audit of the same row must not accuse it of duplicating itself.
        duplicates.claim(self.key, "sub-1")
        self.assertIsNone(duplicates.claim(self.key, "sub-1"))

    def test_an_empty_fingerprint_claims_nothing(self):
        self.assertIsNone(duplicates.claim("", "sub-1"))
        self.assertEqual(self.table.rows, {})

    def test_a_half_written_claim_does_not_accuse_anybody(self):
        # A run that died between writing the row and naming its owner.
        self.table.rows[self.key] = {"fingerprint": self.key}
        self.assertIsNone(duplicates.claim(self.key, "sub-2"))

    def test_a_failure_to_check_never_refuses_a_receipt(self):
        # Missing a duplicate costs one wrong payment a human may still catch.
        # Refusing a genuine receipt costs trust in the whole product.
        with mock.patch.object(self.table, "put_item", side_effect=RuntimeError("throttled")):
            self.assertIsNone(duplicates.claim(self.key, "sub-1"))

    def test_no_table_configured_means_no_false_accusations(self):
        with mock.patch.object(duplicates, "_table", None):
            self.assertIsNone(duplicates.claim(self.key, "sub-1"))

    def test_a_claim_expires_so_the_table_does_not_grow_forever(self):
        duplicates.claim(self.key, "sub-1")
        row = self.table.rows[self.key]
        self.assertEqual(row["expires_at"] - row["created_at"],
                         duplicates.RETENTION_DAYS * 86400)


class Releasing(unittest.TestCase):
    """A rejected claim was never paid, so it must not hold the bill hostage."""

    def setUp(self):
        self.table = FakeTable("fingerprint")
        patch = mock.patch.object(duplicates, "_table", self.table)
        patch.start()
        self.addCleanup(patch.stop)
        self.key = duplicates.invoice_key("org-1", "Cafe", "INV-1001")

    def test_releasing_lets_an_honest_resend_through(self):
        duplicates.claim(self.key, "sub-1")
        duplicates.release(self.key, "sub-1")
        self.assertIsNone(duplicates.claim(self.key, "sub-2"),
                          "a corrected resend must not be called a duplicate")

    def test_one_submission_cannot_release_another_s_claim(self):
        # Otherwise rejecting claim B frees the fingerprint claim A is using,
        # and the same bill can then be claimed a third time.
        duplicates.claim(self.key, "sub-1")
        duplicates.release(self.key, "sub-2")
        self.assertEqual(duplicates.claim(self.key, "sub-3"), "sub-1")



class EveryKeyWasAnExactMatchOnAStringAModelProduced(unittest.TestCase):
    """Mobil80-Exp-67 and Exp-68: one bill, two photographs, four misses.

    Reena sent the same ₹300 bill from Sinvie Book House twice, twenty-two
    seconds apart, and nothing caught it:

        bytes        70,314 against 51,986 - two photographs never match
        name + size  same name, different length
        invoice      `WL15089` against `WL16089`
        sender       `2028-06-23` against `2026-09-23`, off one printed date

    Four keys, four exact matches on a string read off a photograph, and the
    model does not read the same string twice. What survived both readings was
    who sent it, what it cost, and - through `_normalise_vendor`, which is
    coarse on purpose - which shop. Nothing was keyed on those three together.

    The cost of dropping the date is that a second genuine purchase looks the
    same, so the key is read through a window: run over this account's whole
    history it flags eleven pairs, every one of them a real duplicate, and
    leaves Manoj's monthly Cursor renewal alone.
    """

    def setUp(self):
        self.table = FakeTable("fingerprint")
        patch = mock.patch.object(duplicates, "_table", self.table)
        patch.start()
        self.addCleanup(patch.stop)

    def key(self, vendor="SINVIE BOOK HOUSE", total="300.00", who="reena@x.com"):
        return duplicates.repeat_key("org-1", who, vendor, total, "INR")

    def test_the_pair_that_got_through_now_collides(self):
        self.assertEqual(self.key("SINVIE BOOK HOUSE"), self.key("Sinvie Book House"))
        self.assertTrue(self.key())

    def test_it_holds_nothing_read_off_the_paper_as_an_identifier(self):
        # Neither the date nor the invoice number, which are the two that
        # differed between the readings.
        self.assertNotIn("2026", self.key())
        self.assertNotIn("15089", self.key())

    def test_a_different_person_shop_amount_or_currency_is_a_different_key(self):
        base = self.key()
        self.assertNotEqual(base, self.key(who="manoj@x.com"))
        self.assertNotEqual(base, self.key(vendor="Gamma"))
        self.assertNotEqual(base, self.key(total="301.00"))
        self.assertNotEqual(base, duplicates.repeat_key(
            "org-1", "reena@x.com", "SINVIE BOOK HOUSE", "300.00", "USD"))
        self.assertNotEqual(base, duplicates.repeat_key(
            "org-2", "reena@x.com", "SINVIE BOOK HOUSE", "300.00", "INR"))

    def test_a_claim_missing_any_of_the_four_has_no_key(self):
        self.assertEqual("", duplicates.repeat_key("org-1", "", "Sinvie", "300.00", "INR"))
        self.assertEqual("", duplicates.repeat_key("org-1", "a@x.com", "", "300.00", "INR"))
        self.assertEqual("", duplicates.repeat_key("org-1", "a@x.com", "Sinvie", "", "INR"))
        self.assertEqual("", duplicates.repeat_key("org-1", "a@x.com", "Sinvie", "300.00", ""))

    def test_a_resend_minutes_later_is_caught(self):
        k = self.key()
        self.assertIsNone(duplicates.claim(k, "sub-67", within=duplicates.REPEAT_WITHIN))
        self.assertEqual("sub-67",
                         duplicates.claim(k, "sub-68", within=duplicates.REPEAT_WITHIN))

    def test_a_subscription_renewing_a_month_later_is_not(self):
        # Manoj's Cursor invoices, `-0004` then `-0005`. One character apart,
        # exactly like the Sinvie pair - which is why no sharper key could
        # separate them and why the window has to.
        k = self.key(vendor="Cursor", total="20.00", who="manoj@x.com")
        duplicates.claim(k, "sub-1", within=duplicates.REPEAT_WITHIN)
        self.table.rows[k]["created_at"] -= duplicates.REPEAT_WITHIN + 60
        self.assertIsNone(duplicates.claim(k, "sub-16", within=duplicates.REPEAT_WITHIN))

    def test_and_the_later_one_takes_the_key_over(self):
        # Otherwise the window is measured from whichever claim got there
        # first, and a resend of *this* month's invoice would be compared
        # against last month's.
        k = self.key(vendor="Cursor", total="20.00", who="manoj@x.com")
        duplicates.claim(k, "sub-1", within=duplicates.REPEAT_WITHIN)
        self.table.rows[k]["created_at"] -= duplicates.REPEAT_WITHIN + 60
        duplicates.claim(k, "sub-16", within=duplicates.REPEAT_WITHIN)
        self.assertEqual("sub-16", self.table.rows[k]["submission_id"])
        self.assertEqual("sub-16",
                         duplicates.claim(k, "sub-17", within=duplicates.REPEAT_WITHIN))

    def test_the_window_is_only_asked_for_where_it_is_passed(self):
        # Every other key means the same thing a year later.
        k = duplicates.invoice_key("org-1", "Cafe", "INV-1001")
        duplicates.claim(k, "sub-1")
        self.table.rows[k]["created_at"] -= 400 * 86400
        self.assertEqual("sub-1", duplicates.claim(k, "sub-2"))

    def test_the_row_is_not_kept_longer_than_the_window_reads_it(self):
        k = self.key()
        duplicates.claim(k, "sub-67", days=duplicates.REPEAT_DAYS,
                         within=duplicates.REPEAT_WITHIN)
        row = self.table.rows[k]
        self.assertEqual(duplicates.REPEAT_DAYS * 86400,
                         row["expires_at"] - row["created_at"])
        self.assertGreater(duplicates.REPEAT_DAYS * 86400, duplicates.REPEAT_WITHIN)

    def test_it_is_claimed_last_of_the_five(self):
        # It is the coarsest, so where a sharper key has already matched, that
        # is the better answer to put in front of a reviewer.
        with open(os.path.join(ROOT, "lambda_src", "auditor_worker.py"),
                  encoding="utf-8") as handle:
            worker = handle.read()
        order = [worker.index(f"duplicates.claim({fp}") for fp in
                 ("sender_fp", "fingerprint", "shape_fp", "repeat_fp")]
        self.assertEqual(order, sorted(order))

    def test_a_rejected_claim_gives_it_back_like_the_others(self):
        # The bug the release exists to prevent, one key later: a claim that
        # was refused must not tell the honest resend it is a duplicate of
        # something that went nowhere.
        src = open(os.path.join(ROOT, "lambda_src", "duplicates.py"),
                   encoding="utf-8").read()
        body = src.split("def release_all(", 1)[1]
        self.assertIn('item.get("repeat_fingerprint")', body)
        worker = open(os.path.join(ROOT, "lambda_src", "auditor_worker.py"),
                      encoding="utf-8").read()
        self.assertIn("repeat_fingerprint = :rf", worker)
        self.assertIn('":rf": repeat_fp,', worker)


class OneNumberReadTwoWays(unittest.TestCase):
    """An invoice and its own payment receipt, linked by the only thing they share.

    One Gamma transaction arrived as two documents: the invoice, dated the 9th,
    and the payment receipt for it, dated the 10th. Claiming both would pay the
    same bill twice.

    Nothing could catch that pair. `submitter_key` needs the same date, and an
    invoice dated one day and its receipt the next is simply how being invoiced
    and then paying works - not a bug, and not something to relax, because the
    date is what stops two genuine coffees on one morning being called one. The
    file fingerprints need the same bytes, or the same name and size, and these
    are two different documents.

    The invoice number is the only thing both carry - and it came back as
    `OCB07C05-0005` from one and `0CB07C05-0005` from the other. Capital O
    against digit zero, in the first character.
    """

    def test_the_pair_that_got_through_now_collides(self):
        self.assertEqual(
            duplicates.invoice_key("org", "Gamma", "OCB07C05-0005"),
            duplicates.invoice_key("org", "Gamma", "0CB07C05-0005"))

    def test_the_usual_confusions_all_fold(self):
        for a, b in (("INV-O123", "INV-0123"), ("INV-I23", "INV-123"),
                     ("INV-L23", "INV-123"), ("INV-S12", "INV-512"),
                     ("INV-B12", "INV-812"), ("INV-Z12", "INV-212")):
            self.assertEqual(duplicates.invoice_key("org", "Gamma", a),
                             duplicates.invoice_key("org", "Gamma", b),
                             f"{a} and {b} should fingerprint alike")

    def test_two_genuinely_different_numbers_stay_apart(self):
        self.assertNotEqual(duplicates.invoice_key("org", "Gamma", "INV-1001"),
                            duplicates.invoice_key("org", "Gamma", "INV-1002"))

    def test_folding_does_not_manufacture_a_digit(self):
        # `o5o` folds to `050`. The digit test exists because a number with no
        # digits printed on it identifies nothing, so it has to be applied to
        # what was read, not to what folding made of it.
        self.assertEqual("", duplicates.invoice_key("org", "Gamma", "INVOICE"))
        self.assertEqual("", duplicates.invoice_key("org", "Gamma", "OIL"))

    def test_it_folds_one_way_only(self):
        # Letters become digits, never the reverse, so the key stays anchored
        # to a shape rather than drifting between two spellings of it.
        key = duplicates.invoice_key("org", "Gamma", "OCB07C05-0005")
        number = key.rsplit("#", 1)[1]
        for letter in "oilsbz":
            self.assertNotIn(letter, number)

    def test_the_displayed_number_is_never_folded(self):
        # Folding is for matching. What a reviewer reads is what was on their
        # bill, and a console showing them "0C807C05" for a bill that says
        # "OCB07C05" would be the product correcting the paper.
        source = open(os.path.join(ROOT, "lambda_src", "auth.py"),
                      encoding="utf-8").read()
        view = source.split("def _submission_view(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"invoice_number"', view)
        self.assertNotIn("_CONFUSABLE", source)


if __name__ == "__main__":
    unittest.main()


class ThePortalHadNoIdenticalFileCheckAtAll(unittest.TestCase):
    """Every other channel hashes the bytes; the portal never did.

    WhatsApp, email and the API all store the file themselves and hash it on
    the way past. The portal uploads straight to S3 with a presigned URL, so
    the only thing that ever looked at the object again was a `head_object` -
    which returns a size and a content type and no hash.

    So the portal was the one route with no identical-file check. Two
    colleagues uploading the same PDF - one invoice, forwarded round an office,
    submitted twice - were both charged a credit and both audited, and nothing
    anywhere said they were the same file. It surfaced only because the two
    rows happened to carry the same byte count and the same filename.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "receipts.py"), encoding="utf-8") as h:
            self.receipts = h.read()
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            self.auth = h.read()

    def test_describe_returns_a_hash(self):
        fn = self.receipts.split("def describe(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"receipt_sha256": hashlib.sha256(data).hexdigest()', fn)

    def test_it_reads_the_bytes_rather_than_trusting_a_header(self):
        # A `head_object` cannot hash anything, which is how this came to be
        # missing in the first place.
        fn = self.receipts.split("def describe(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_s3.get_object(", fn)
        # Past the docstring, which explains why it is no longer a head.
        body = fn.split('"""', 2)[-1]
        self.assertNotIn("head_object", body)

    def test_it_still_refuses_an_object_that_is_too_big(self):
        # And reads one byte past the limit so an oversized object is caught
        # rather than silently truncated to exactly the limit.
        fn = self.receipts.split("def describe(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("read(MAX_BYTES + 1)", fn)
        self.assertIn("size > MAX_BYTES", fn)

    def test_the_portal_forwards_what_it_gets(self):
        # `**stored` already carried everything; the hash simply was not in it.
        body = self.auth.split("def _receipt_submit(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("**stored", body)


class TheSameAttachmentTwiceByDifferentRoutes(unittest.TestCase):
    """Name and exact byte count, for what the content hash cannot see.

    One PDF forwarded round an office and sent in by two people through two
    channels, re-encoded somewhere on the way so the bytes no longer match.
    Both copies still carry the vendor's own filename and, usually, the same
    length.
    """

    def test_one_attachment_two_routes_is_one_key(self):
        self.assertEqual(
            duplicates.file_shape_key("org", "cursor-invoice-2026-08.pdf", 310818),
            duplicates.file_shape_key("org", "Cursor-Invoice-2026-08.PDF", "310818"))

    def test_a_different_length_is_a_different_file(self):
        self.assertNotEqual(
            duplicates.file_shape_key("org", "invoice.pdf", 310818),
            duplicates.file_shape_key("org", "invoice.pdf", 310819))

    def test_the_byte_count_is_what_makes_a_generic_name_safe(self):
        # `receipt.jpg` is the name of every photograph WhatsApp has ever sent,
        # so the name alone would flag the whole channel against itself. Two
        # unrelated photographs agreeing to the byte are a different matter.
        self.assertNotEqual(
            duplicates.file_shape_key("org", "receipt.jpg", 157773),
            duplicates.file_shape_key("org", "receipt.jpg", 114741))

    def test_a_nameless_or_sizeless_file_is_not_evidence(self):
        for missing in (duplicates.file_shape_key("org", "", 310818),
                        duplicates.file_shape_key("org", "invoice.pdf", 0),
                        duplicates.file_shape_key("org", "invoice.pdf", "nope"),
                        duplicates.file_shape_key("org", "   ", 310818)):
            self.assertEqual("", missing)

    def test_another_organisation_never_collides(self):
        self.assertNotEqual(
            duplicates.file_shape_key("org-1", "invoice.pdf", 310818),
            duplicates.file_shape_key("org-2", "invoice.pdf", 310818))

    def test_it_is_tried_last(self):
        # It is the weakest of the three, so a stronger match names the other
        # claim first.
        with open(os.path.join(ROOT, "lambda_src", "auditor_worker.py"),
                  encoding="utf-8") as h:
            worker = h.read()
        self.assertLess(worker.index("duplicates.claim(sender_fp"),
                        worker.index("duplicates.claim(shape_fp"))
        self.assertLess(worker.index("duplicates.claim(fingerprint"),
                        worker.index("duplicates.claim(shape_fp"))
