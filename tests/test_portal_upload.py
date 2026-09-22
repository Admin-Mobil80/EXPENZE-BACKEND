"""Submitting a receipt from the console.

The portal's own upload routed through `/intake/api`, and when that endpoint
grew an API key requirement the console's Submit button started being refused
for want of a credential the console has no business holding. What the reader
saw was "That receipt couldn't be accepted." - a sentence general enough to
hide the cause completely, rendered as plain text below the whole panel where
it read as a caption rather than a failure.

It also meant every receipt uploaded in the console was recorded as having
arrived through a third-party integration.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda_src"))
ROOT = os.path.join(os.path.dirname(__file__), "..")


class ThePortalIsItsOwnChannel(unittest.TestCase):
    def setUp(self):
        for name, path in (("intake", "lambda_src/intake.py"),
                           ("auth", "lambda_src/auth.py"),
                           ("stack", "expensifyai/stack.py"),
                           ("app", "../PORTAL/app.html")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_intake_recognises_it(self):
        expr = self.intake.split("channel = (", 1)[1].split('else "email")', 1)[0]
        self.assertIn('"portal"', expr)

    def test_the_console_no_longer_submits_as_a_third_party(self):
        body = self.auth.split("def _receipt_submit(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"path": "/intake/portal"', body)
        self.assertNotIn('"path": "/intake/api"', body)

    def test_no_http_route_is_opened_for_it(self):
        # The only caller is auth.py, in process, after it has already
        # authenticated the session. An HTTP route would be an unauthenticated
        # way to spend credits.
        routes = self.stack.split('intake_res = api.root.add_resource("intake")', 1)[1][:300]
        self.assertNotIn("portal", routes)

    def test_the_key_requirement_still_applies_to_the_real_api(self):
        body = self.intake.split("lambda_handler", 1)[1]
        self.assertIn('if channel == "api":', body)

    def test_the_reason_reaches_the_reader(self):
        # "That receipt couldn't be accepted" was general enough to hide an
        # API key check from the one caller that could never have a key.
        body = self.auth.split("def _receipt_submit(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('outcome.get("error") or outcome.get("message")', body)

    def test_portal_receipts_are_labelled_as_such(self):
        block = self.app.split("const CHANNELS = {", 1)[1].split("};", 1)[0]
        self.assertIn("portal", dict(re.findall(r'(\w+)\s*:\s*"([^"]+)"', block)))


class TheErrorIsWhereTheActionIs(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_the_message_sits_inside_the_upload_panel(self):
        panel = self.app.split('<div class="upload">', 1)[1].split("</div>\n      <div", 1)[0]
        self.assertIn('id="up-msg"', panel)

    def test_the_button_sits_with_the_file_it_sends(self):
        row = self.app.split('<div class="up-row">', 1)[1].split('<p class="msg"', 1)[0]
        self.assertIn('id="up-file"', row)
        self.assertIn('id="up-go"', row)

    def test_an_error_looks_like_one(self):
        self.assertIn(".upload .msg.err", self.app)

    def test_submit_is_dead_until_there_is_something_to_send(self):
        # Better than a button that can be pressed and then complains.
        self.assertIn('id="up-go" disabled', self.app)
        body = self.app.split("function wireUpload()", 1)[1].split("\n}", 1)[0]
        self.assertIn("btn.disabled = false;", body)

    def test_the_button_reports_that_it_is_working(self):
        self.assertIn('btn.textContent = "Submitting…"', self.app)

    def test_a_failure_is_scrolled_into_view(self):
        self.assertIn('msg.scrollIntoView({ block: "nearest"', self.app)


class ConfirmationsAreVisible(unittest.TestCase):
    """The answer to an action that moves money has to look like one.

    "Settled in full. riyad was notified by email and whatsapp." rendered as
    plain text between the banner and the table - indistinguishable from a
    caption, for the one action on that page that pays somebody.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_the_settlement_message_has_its_own_treatment(self):
        self.assertIn(".paymsg.ok", self.app)
        self.assertIn(".paymsg.err", self.app)

    def test_it_carries_a_mark_as_well_as_a_colour(self):
        # Colour alone is not a signal everybody receives.
        marks = self.app.split(".paymsg.ok::before", 1)[1].split("}", 1)[0]
        self.assertIn("2713", marks)   # a tick, as a CSS escape
        fails = self.app.split(".paymsg.err::before", 1)[1].split("}", 1)[0]
        self.assertIn("2715", fails)   # a cross

    def test_it_is_brought_into_view(self):
        body = self.app.split("function payMsg(", 1)[1].split("\n}", 1)[0]
        self.assertIn("scrollIntoView", body)

    def test_the_class_is_applied_when_the_message_is_set(self):
        body = self.app.split("function payMsg(", 1)[1].split("\n}", 1)[0]
        self.assertIn('"msg paymsg "', body)


class TheSettlementFormIsCompact(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_the_note_shares_the_grid_rather_than_a_row_of_its_own(self):
        form = self.app.split("function settleForm(", 1)[1].split("</div>`", 1)[0]
        grid = form.split('class="settle-grid"', 1)[1].split("settle-foot", 1)[0]
        self.assertIn('id="sf-note"', grid)

    def test_every_field_is_still_there(self):
        form = self.app.split("function settleForm(", 1)[1].split("tr.appendChild", 1)[0]
        for field in ("sf-amt", "sf-mode", "sf-ref", "sf-date", "sf-note", "sf-go"):
            self.assertIn(f'id="{field}"', form, field)


class MoneyIsReleasedWhereTheClaimIs(unittest.TestCase):
    """Pending settlement is a queue; the claim page is where it is decided.

    Recording a payment from a row meant deciding with none of the claim in
    front of you - the receipt, the line items and the reasoning all live on
    the claim page, and that is the last chance anybody has to stop a wrong
    payment before the money moves.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_the_list_no_longer_carries_the_buttons(self):
        # It no longer carries an action cell at all: the row itself opens the
        # claim, so the column that used to say "Open to settle" was pointing
        # at something the reader was already standing on.
        row = self.app.split("// Only what is still owed.", 1)[1].split("tb.appendChild(tr);", 1)[0]
        self.assertNotIn("data-open", row)
        self.assertNotIn("data-reject", row)
        self.assertNotIn("opencue", row)
        self.assertIn("opensClaim(tr, c.sub.id)", row)

    def test_the_claim_page_does(self):
        self.assertIn("function renderSettleOnClaim(", self.app)
        body = self.app.split("function renderSettleOnClaim(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("settleForm(c, ccy)", body)

    def test_only_a_reviewer_and_only_where_something_is_owed(self):
        body = self.app.split("function renderSettleOnClaim(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("!maySettle || c.outstanding <= 0", body)

    def test_the_forms_are_no_longer_table_rows(self):
        # They were a <tr> spanning the settlement list; nothing opens them
        # from a table any more.
        body = self.app.split("function settleForm(", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn('createElement("tr")', body)
        self.assertIn("settle-block", body)

    def test_the_confirmation_follows_the_action(self):
        body = self.app.split("function payMsg(", 1)[1].split("\n}", 1)[0]
        self.assertIn("claim-pay-msg", body)

    def test_the_confirmation_is_not_wiped_by_the_re_render(self):
        # It sits outside the slot, which is emptied on every render.
        slot = self.app.split('<div id="settle-slot"></div>', 1)[1][:120]
        self.assertIn('id="claim-pay-msg"', slot)


class APaidClaimStaysPaid(unittest.TestCase):
    """A recorded payment survived only in the browser that made it.

    `outcome: settled` was written to the row, but the console rebuilt its
    payment history from a local object - so after a reload a settled claim
    reappeared under Pending settlement showing the full amount still owing.
    The obvious thing to do with a claim that says it is awaiting payment is
    to pay it, which is how somebody gets paid twice.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src/auth.py"), encoding="utf-8") as h:
            self.auth = h.read()
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_the_amount_paid_is_written_down(self):
        # Not just that a payment happened - the figure, or it cannot be
        # rebuilt.
        body = self.auth.split("def _claim_outcome(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("outcome_paid = :p", body)
        self.assertIn('":p": str(claim.get("paid")', body)

    def test_the_console_can_read_it_back(self):
        view = self.auth.split("def _submission_view(", 1)[1].split("\ndef ", 1)[0]
        for field in ('"outcome"', '"paid"', '"paid_by"', '"paid_at"'):
            self.assertIn(field, view, field)

    def test_payments_are_rebuilt_on_load(self):
        block = self.app.split("async function loadRecords()", 1)[1]
        self.assertIn("if (sub.settlement) settlements[sub.id] = [sub.settlement];", block)
        self.assertIn("else delete settlements[sub.id];", block)


class NothingIsInventedBeforeTheBillIsRead(unittest.TestCase):
    """The console answered for a receipt nobody had opened.

    A portal upload put an optimistic row on the page with `type: "meals"`,
    `currency: "INR"` and no line items - and the local policy engine, handed
    that, produced a verdict from it. A software subscription for INR 2,033
    therefore appeared as a meals claim for INR 0.00, blocked on a headcount,
    with an expense type and a currency nobody had read off anything.

    There is no honest verdict before the bill has been read.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_an_unread_claim_is_recognised(self):
        self.assertIn("const isPending = (sub) =>", self.app)
        fn = self.app.split("const isPending = (sub) =>", 1)[1].split(";", 1)[0]
        self.assertIn('"queued"', fn)
        self.assertIn('"auditing"', fn)

    def test_the_local_engine_is_not_asked(self):
        body = self.app.split("const evaluate = (sub) => {", 1)[1].split("\n};", 1)[0]
        self.assertIn("if (isPending(sub)) return pendingResult(sub);", body)
        self.assertLess(body.index("isPending"), body.index("evaluatePolicy"))

    def test_it_claims_no_figures_and_no_findings(self):
        block = self.app.split("const pendingResult = (sub) => ({", 1)[1].split("});", 1)[0]
        self.assertIn("receiptTotal: null", block)
        self.assertIn("violations: []", block)
        self.assertNotIn('"meals"', block)

    def test_an_uncomputed_figure_is_a_dash_not_a_zero(self):
        # "0.00" for a receipt nobody has read states something false about
        # somebody's money.
        body = self.app.split("const fmt = (m,c) => {", 1)[1].split("\n};", 1)[0]
        self.assertIn('if (m === null || m === undefined) return "—";', body)

    def test_the_placeholder_does_not_guess_a_type_or_a_currency(self):
        row = self.app.split("SUBMISSIONS.unshift({", 1)[1].split("});", 1)[0]
        self.assertIn("pending:true", row)
        self.assertNotIn('type:"meals"', row)
        self.assertNotIn('currency:"INR"', row)

    def test_the_correction_controls_are_disabled_while_unread(self):
        # Folded in with every other reason a control is dead, rather than
        # applied by a pass of its own: a second assignment to the same
        # controls does not narrow the first, it replaces it.
        self.assertIn("const unread = res.verdict === \"pending\";", self.app)
        # In `busy`, which both rules are built from, so a control on either
        # of them is dead while the bill is still being read.
        busy = self.app.split("const busy =", 1)[1].split(";", 1)[0]
        self.assertIn("unread", busy)
        for rule in ("const frozen =", "const classFrozen ="):
            self.assertIn("busy", self.app.split(rule, 1)[1].split(";", 1)[0])

    def test_the_page_refreshes_itself_once_the_audit_lands(self):
        # Otherwise the placeholder sits there until somebody reloads.
        self.assertIn("[6000, 15000, 30000].forEach(ms => setTimeout(loadRecords, ms));", self.app)


class JustNowAgo(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()

    def test_a_complete_phrase_does_not_get_ago_appended(self):
        fn = self.app.split("function whenLabel(sub) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("const since = (age) =>", fn)
        self.assertIn("just now", fn)
        self.assertNotIn('sub.age + " ago"', fn)


class YouSeeWhatYouAreSending(unittest.TestCase):
    """A receipt is the evidence for a claim.

    The panel named the file and nothing more, so "which photo did I just
    attach?" could only be answered by submitting it and looking afterwards.
    Images showed a thumbnail; a PDF showed its filename.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()
        self.body = self.app.split("function wireUpload()", 1)[1].split("\n}\n", 1)[0]

    def test_there_is_somewhere_to_show_it(self):
        self.assertIn('id="up-view"', self.app)

    def test_a_pdf_is_previewed_not_just_named(self):
        self.assertIn('f.type === "application/pdf"', self.body)
        self.assertIn('createElement("iframe")', self.body)

    def test_an_image_is_previewed_too(self):
        self.assertIn('createElement("img")', self.body)

    def test_the_name_and_the_size_are_both_shown(self):
        self.assertIn('size.className = "size"', self.body)
        self.assertIn('name.className = "name"', self.body)

    def test_choosing_again_releases_the_previous_preview(self):
        # An object URL holds the file in memory until it is revoked.
        self.assertIn("URL.revokeObjectURL(previewUrl)", self.body)
        self.assertIn("clearChoice();", self.body)

    def test_submitting_clears_the_choice(self):
        self.assertIn('file.value = ""; clearChoice();', self.app)

    def test_the_button_sits_beside_the_file_not_at_the_far_edge(self):
        css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.app, re.S))
        self.assertIn(".up-preview:empty ~ .btn { margin-left:auto; }", css)
        self.assertNotIn(".up-row .btn { margin-left:auto; }", css)
