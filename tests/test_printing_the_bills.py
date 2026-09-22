"""Finance files a printed copy of every receipt they pay against.

Until this existed there was no way to get one. The browser's own print prints
the console around the receipt - the nav, the tiles, the queue - and the only
full-size view of a bill was the lightbox, which is drawn over the page and
prints as part of it. The other route, opening the presigned URL in a window
and printing from there, is the thing this console has gone out of its way
never to do: that link authorises itself, and a window puts it in an address
bar, in history, and one copy-paste from somebody with no account.

So it prints from this page. A sheet that is hidden on screen is the only thing
visible on paper, and the receipt reaches the printer without its URL reaching
anywhere.

Both kinds of receipt become images, which is the decision worth knowing about.
A cross-origin PDF in an iframe prints blank in every browser that matters, and
a blob iframe prints the PDF *instead of* the sheet - fine for one bill and
impossible for twenty-two. Rendering each PDF page to a canvas gives one code
path and one appearance whether it is one bill or a whole payment run.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def console() -> str:
    with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as h:
        return h.read()


def strip_comments(js: str) -> str:
    """Code only. A comment explaining a rule is not a breach of it."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"<!--.*?-->", "", js, flags=re.S)
    return re.sub(r"(^|\s)//[^\n]*", " ", js)


class ThePresignedUrlStaysOnThePage(unittest.TestCase):

    def setUp(self):
        self.app = console()
        self.code = strip_comments(self.app)

    def test_nothing_opens_a_window_at_the_receipt(self):
        # The whole reason the lightbox exists rather than a link.
        self.assertNotIn("window.open(", self.code)

    def test_the_sheet_is_part_of_this_document(self):
        self.assertIn('<div id="printsheet" aria-hidden="true"></div>', self.app)
        self.assertIn('const sheet = $("printsheet");', self.code)

    def test_and_the_printer_sees_nothing_else(self):
        rules = self.app.split("@media print {", 1)[1].split("\n}", 1)[0]
        self.assertIn("body > * { display:none !important; }", rules)
        self.assertIn("body > #printsheet { display:block !important; }", rules)

    def test_hidden_by_display_rather_than_visibility(self):
        # `visibility` leaves the space behind and prints twenty blank pages
        # before the first receipt.
        rules = self.app.split("@media print {", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("visibility:hidden", rules)

    def test_a_pdf_is_fetched_rather_than_handed_over_as_a_url(self):
        # So the link is used once, here, and never becomes a document
        # location that pdf.js or anything else could surface.
        fn = self.app.split("async function billImages(", 1)[1].split("\n}", 1)[0]
        self.assertIn("await (await fetch(original.url)).arrayBuffer()", fn)
        self.assertIn("getDocument({ data: bytes })", fn)


class TheReaderIsPinnedAndChecked(unittest.TestCase):

    def setUp(self):
        self.app = console()

    def test_it_is_a_fixed_version(self):
        block = self.app.split("const PDFJS = {", 1)[1].split("};", 1)[0]
        self.assertIn("pdf.js/3.11.174/pdf.min.js", block)
        self.assertIn("pdf.js/3.11.174/pdf.worker.min.js", block)

    def test_both_files_carry_a_hash(self):
        block = self.app.split("const PDFJS = {", 1)[1].split("};", 1)[0]
        self.assertEqual(2, len(re.findall(r'"sha384-[A-Za-z0-9+/=]{64}"', block)))

    def test_the_bytes_are_checked_before_anything_runs(self):
        fn = self.app.split("async function pinnedScript(", 1)[1].split("\n}", 1)[0]
        self.assertIn('crypto.subtle.digest("SHA-384", bytes)', fn)
        self.assertIn("did not match its pinned hash", fn)

    def test_the_worker_is_a_real_one(self):
        # A cross-origin URL cannot start a worker, so the alternative was
        # pdf.js's "fake worker", which renders on the main thread - slow in
        # exactly the case this feature exists for, a tab rendering twenty-two
        # invoices with the interface frozen behind it. A verified blob is
        # same-origin and starts properly.
        fn = self.app.split("function loadPdfJs() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("GlobalWorkerOptions.workerSrc = worker", fn)
        self.assertIn("URL.createObjectURL",
                      self.app.split("async function pinnedScript(", 1)[1])

    def test_it_loads_on_the_first_print_and_not_before(self):
        # Nobody who never prints pays for 1.4MB of it.
        self.assertIn("if (pdfjsReady) return pdfjsReady;", self.app)
        fn = self.app.split("async function billImages(", 1)[1].split("\n}", 1)[0]
        self.assertLess(fn.index('original.type !== "application/pdf"'),
                        fn.index("await loadPdfJs()"))

    def test_a_failed_load_does_not_poison_the_next_attempt(self):
        fn = self.app.split("function loadPdfJs() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("catch(err => { pdfjsReady = null; throw err; })", fn)


class OneBillOneSheetOfPaper(unittest.TestCase):

    def setUp(self):
        self.app = console()
        self.rules = self.app.split("@media print {", 1)[1].split("\n}", 1)[0]

    def test_each_claim_breaks_to_a_new_page(self):
        # Finance files these against invoices, so two receipts sharing a page
        # is two documents to cut apart.
        self.assertIn("#printsheet .bill { break-after:page;", self.rules)
        self.assertIn("page-break-after:always;", self.rules)

    def test_but_not_a_blank_one_after_the_last(self):
        self.assertIn("#printsheet .bill:last-child { break-after:auto;", self.rules)

    def test_a_multi_page_invoice_gets_a_page_each(self):
        self.assertIn("#printsheet .page + .page { break-before:page;", self.rules)

    def test_a_tall_receipt_is_held_to_one_page(self):
        # A till roll printed at full width runs onto a second sheet, and the
        # next bill's header lands under the tail of this one.
        self.assertIn("max-height:238mm", self.rules)
        self.assertIn("max-width:100%", self.rules)

    def test_the_header_says_which_claim_it_is(self):
        # A bare photograph of a bill in a folder is a bill, not a claim.
        fn = self.app.split("function billBlock(sub, images) {", 1)[1].split(
            "\n}", 1)[0]
        for field in ("sub.reference", "sub.who", "sub.vendor",
                      "groupLabel(groupOf(sub).id)", "sub.invoiceNumber"):
            self.assertIn(field, fn)

    def test_and_what_it_came_to_in_both_currencies(self):
        fn = self.app.split("function billBlock(sub, images) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("fmt(res.receiptTotal, res.currency)", fn)
        self.assertIn("fmt(Math.round(res.receiptTotal * rate), pv.currency)", fn)
        self.assertIn("paid ${fmt(paid, orgCurrency())}", fn)

    def test_the_identifying_line_is_spaced(self):
        # Without this they run together as one unreadable string, and it is
        # the line somebody reads to file the page.
        self.assertIn("#printsheet .billhead > span:not(.figs) { display:inline-block;",
                      self.rules)

    def test_a_claim_with_no_file_still_prints_a_page(self):
        # A gap in a stack of bills is something finance has to go and account
        # for; a page naming the claim is the answer they would go looking for.
        fn = self.app.split("function billBlock(sub, images) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("No original is stored for this claim.", fn)


class PrintingAWholeRun(unittest.TestCase):

    def setUp(self):
        self.app = console()
        self.fn = self.app.split("async function printBills(subs, say) {", 1)[1] \
                          .split("\n}", 1)[0]

    def test_the_button_prints_what_is_still_owed(self):
        # Not everything ever cleared: the stack has to match the run being
        # paid.
        block = self.app.split("const printAll = $(\"pay-print\");", 1)[1].split(
            "\n  }", 1)[0]
        self.assertIn("printBills(awaiting.map(c => c.sub), payMsg)", block)

    def test_it_counts_the_bills_in_its_own_label(self):
        block = self.app.split("const printAll = $(\"pay-print\");", 1)[1].split(
            "\n  }", 1)[0]
        self.assertIn("Print all ${awaiting.length} bills", block)
        self.assertIn('awaiting.length === 1 ? "Print the bill"', block)

    def test_and_is_not_offered_on_an_empty_list(self):
        block = self.app.split("const printAll = $(\"pay-print\");", 1)[1].split(
            "\n  }", 1)[0]
        self.assertIn("printAll.hidden = !awaiting.length || !can.seeQueue();", block)

    def test_progress_is_reported_on_the_button_that_started_it(self):
        # Twenty-two PDFs is long enough that a silent button reads as broken.
        #
        # Not through `payMsg`. That is the settlement confirmation line and it
        # persists on purpose - "INR 6,632.00 recorded as paid" is worth
        # leaving up. A progress message is the opposite: true for four seconds
        # and misleading after. "14 bills ready." sat over a list that had
        # since become ten claims and read as a statement about the list.
        self.assertIn("printProgress = subs.length > 1 ? `${++done} of ${subs.length}`",
                      self.fn)
        self.assertIn("`Preparing${printProgress ? \" \" + printProgress : \"\"}",
                      console())

    def test_and_it_cannot_outlive_the_job(self):
        tail = self.fn.split("} finally {", 1)[1]
        self.assertIn('printProgress = "";', tail)

    def test_nothing_is_left_behind_saying_it_finished(self):
        # The print dialog opening is the confirmation. A sentence after it is
        # about a job that has ended.
        self.assertNotIn("bills ready", self.fn)
        self.assertIn("window.print();", self.fn)

    def test_only_failures_are_left_on_screen(self):
        # Those are worth persisting: the reader needs to know it did not
        # happen, and why.
        for call in re.findall(r"say\(([^;]+)\);", self.fn, re.S):
            self.assertIn("false", call, call[:60])

    def test_one_unreadable_receipt_does_not_cost_the_others(self):
        self.assertIn("catch (err) {", self.fn)
        self.assertIn("could not render", self.fn)

    def test_every_image_is_decoded_before_the_dialog_opens(self):
        # Otherwise the browser prints the frames it happens to have and the
        # rest come out blank.
        self.assertIn("img.decode()", self.fn)
        self.assertLess(self.fn.index("img.decode()"), self.fn.index("window.print()"))

    def test_the_sheet_is_emptied_whether_it_printed_or_was_cancelled(self):
        # The browser gives no reliable signal for either, and a sheet left
        # full is the next print job silently doubled.
        tail = self.fn.split("} finally {", 1)[1]
        self.assertIn('sheet.textContent = "";', tail)
        self.assertIn("printing = false;", tail)

    def test_two_print_jobs_cannot_overlap(self):
        self.assertIn("if (printing) return;", self.fn)

    def test_an_impossible_batch_is_refused_rather_than_attempted(self):
        # Past this the browser is the thing that fails, and half a stack with
        # no way to tell where it stopped is worse than a sentence.
        self.assertIn("subs.length > PRINT_MAX_BILLS", self.fn)
        self.assertIn("PRINT_MAX_BILLS = 60", self.app)


class TheButtonOnOneClaim(unittest.TestCase):

    def setUp(self):
        self.app = console()

    def test_it_exists_and_is_explicit(self):
        self.assertIn('id="r-print"', self.app)
        self.assertIn('printOne.textContent = printing ? "Preparing\\u2026" : "Print";',
                      self.app)

    def test_it_prints_the_claim_that_is_open(self):
        self.assertIn("printOne.onclick = () => printBills([sub], payMsg);", self.app)

    def test_it_is_rewired_on_every_paint(self):
        # The button outlives the claim; a listener added once would reach for
        # whichever claim was open when the page loaded. Same reason as zoom.
        self.assertIn("printOne.onclick =", self.app)
        self.assertNotIn('$("r-print").addEventListener', self.app)

    def test_it_shows_on_the_original_and_not_on_the_reading(self):
        # "Print" beside a transcript would reasonably be taken to print the
        # transcript.
        fn = self.app.split("function applyReceiptTab() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("printBtn.hidden = !(orig && currentOriginal)", fn)


if __name__ == "__main__":
    unittest.main()
