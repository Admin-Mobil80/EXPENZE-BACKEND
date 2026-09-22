"""Structural checks on the hand-written console pages.

Two defects shipped from this file that neither a syntax check nor a glance
caught, because a browser silently repairs both:

* **An unclosed <div>.** One missing close inside the Reports tab meant
  `view-reports` swallowed every view declared after it, so Settled, People,
  Budgets and the rest rendered blank. Nothing errored; the tabs just went
  empty.

* **A duplicated id.** `r-reimb` existed on the review-queue panel and on the
  Reports tile, so `getElementById` fed both from the first, and the Reports
  figure stayed blank for weeks.

Neither needs a browser to catch, so they are caught here instead.
"""
from __future__ import annotations

import os
import re
import unittest
from collections import Counter

ROOT = os.path.join(os.path.dirname(__file__), "..")
PAGES = [
    "../PORTAL/app.html",
    "../PORTAL/index.html",
    "../PORTAL/login.html",
    "../BMS/home.html",
    "../BMS/index.html",
]

# Elements that never take a closing tag.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}

TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?(/?)>")

# A quoted attribute may legally contain "<" - a placeholder of "+<code>", say.
# Blanking attribute values first keeps those out of the tag scan.
ATTR_VALUE = re.compile(r'="[^"]*"')


def strip_attribute_values(line: str) -> str:
    return ATTR_VALUE.sub('=""', line)


def markup_of(path: str) -> tuple[list[str], int]:
    """The page's markup, stopping at the inline script.

    Everything after `<script>` is JavaScript, where `<` and `>` are operators
    rather than tags.
    """
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("<script"):
            return lines[:i], i
    return lines, len(lines)


class TagBalance(unittest.TestCase):
    def test_every_element_closes_in_the_right_order(self):
        for path in PAGES:
            with self.subTest(page=path):
                lines, _ = markup_of(path)
                stack: list[tuple[int, str]] = []
                problems: list[str] = []
                for number, line in enumerate(lines, 1):
                    for close, raw_name, self_closing in TAG.findall(strip_attribute_values(line)):
                        name = raw_name.lower()
                        if name in VOID or self_closing:
                            continue
                        if close:
                            if stack and stack[-1][1] == name:
                                stack.pop()
                            elif stack:
                                opened_at, opened = stack.pop()
                                problems.append(
                                    f"line {number}: </{name}> closes <{opened}> "
                                    f"opened on line {opened_at}")
                            else:
                                problems.append(f"line {number}: stray </{name}>")
                        else:
                            stack.append((number, name))

                # The document's own wrappers close after the script block, so
                # they are still open where this check stops looking.
                left = [f"<{n}> opened on line {ln}" for ln, n in stack
                        if n not in ("html", "head", "body")]
                self.assertEqual(problems, [], f"{path}: mismatched tags")
                self.assertEqual(left, [], f"{path}: unclosed elements")


class UniqueIds(unittest.TestCase):
    def test_no_id_is_used_twice(self):
        for path in PAGES:
            with self.subTest(page=path):
                lines, _ = markup_of(path)
                ids = re.findall(r'\sid="([^"]+)"', "\n".join(lines))
                dupes = sorted(k for k, n in Counter(ids).items() if n > 1)
                self.assertEqual(
                    dupes, [],
                    f"{path}: getElementById returns the first match only, so a "
                    f"duplicate id silently starves every later use of it")


class ScriptReferencesResolve(unittest.TestCase):
    """Every id the script reaches for through $() has to exist in the markup."""

    def test_dollar_lookups_have_an_element(self):
        for path in PAGES:
            with self.subTest(page=path):
                with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
                    source = fh.read()
                lines, script_start = markup_of(path)
                script = "\n".join(source.splitlines()[script_start:])
                present = set(re.findall(r'\sid="([^"]+)"', "\n".join(lines)))
                # Panels the script builds itself carry their ids in template
                # literals rather than in the page, and are just as real.
                present |= set(re.findall(r'\bid="([a-zA-Z][\w-]*)"', script))
                present |= set(re.findall(r'\.id\s*=\s*"([a-zA-Z][\w-]*)"', script))

                # Only literal lookups; anything built from a variable is skipped.
                wanted = set(re.findall(r'\$\("([a-zA-Z][\w-]*)"\)', script))
                missing = sorted(wanted - present)
                self.assertEqual(missing, [], f"{path}: $() reaches for ids that do not exist")


if __name__ == "__main__":
    unittest.main()


class OwnClaimsAreFoundByIdentity(unittest.TestCase):
    """My expenses must match the signed-in person by email, not by name.

    The regression: a WhatsApp receipt was accepted, audited and answered, and
    sat correctly in the review queue - but never appeared under My expenses.
    The filter compared the claim's display name against the signed-in user's,
    and a claim built before the People list had loaded carries the local part
    of the address ("riyad") rather than a full name. Two colleagues sharing a
    name would have collided the other way.

    Checked in the source rather than in a browser, for the same reason as
    everything else in this file: it is cheap, and the bug is invisible until
    somebody happens to look at the right tab.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_the_filter_runs_through_ismine(self):
        # Through `isMine`, whatever else it also filters on - the list also
        # drops companion documents now, so one purchase is one row.
        self.assertIn("SUBMISSIONS.filter(s => isMine(s) && !isCompanion(s))", self.source)
        self.assertNotIn("SUBMISSIONS.filter(s => s.who === currentUser.name)", self.source)

    def test_ismine_compares_email_addresses(self):
        body = self.source.split("function isMine(sub) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("LIVE.email", body)
        self.assertIn("sub.whoEmail", body)
        self.assertIn("toLowerCase()", body, "addresses differ only in case all the time")

class EveryApiHelperExists(unittest.TestCase):
    """A call to an API helper must reach a function that exists.

    `postJSON` did not. It was renamed to `authCall` and three call sites were
    missed, so viewing a stored original and uploading a receipt from the
    portal both raised a ReferenceError at the moment of use. Nothing failed at
    load, nothing appeared in the server log - the request was never made - and
    the panel simply sat there saying "Fetching the original…" forever.

    Scoped deliberately to calls whose first argument is an API path. A general
    undefined-identifier check needs a real JavaScript parser, and a regex
    imitation of one produces enough false positives to be turned off within a
    week. This catches the whole class of rename-and-miss for the calls where
    it actually costs something.
    """

    CALL = re.compile(r'\b([A-Za-z_$][\w$]*)\(\s*(?:AUTH_API\s*\+\s*)?"(/[\w/{}.-]*)"')

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()
        self.script = "\n".join(re.findall(
            r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", self.source, re.S))

    def _defined(self, name: str) -> bool:
        return bool(re.search(
            r"\b(?:async\s+)?function\s+%s\b|\b(?:const|let|var)\s+%s\s*=" % (name, name),
            self.script))

    def test_helpers_called_with_an_api_path_are_defined(self):
        called = {name for name, _ in self.CALL.findall(self.script)}
        # Native and DOM calls that legitimately take a path-shaped string.
        called -= {"fetch", "test", "match", "startsWith", "endsWith", "includes",
                   "split", "replace", "querySelector", "querySelectorAll", "push",
                   "setAttribute", "matches", "closest", "indexOf", "join"}
        missing = sorted(n for n in called if not self._defined(n))
        self.assertEqual(missing, [], f"called with an API path but never defined: {missing}")

    def test_the_console_reaches_the_api_through_one_helper(self):
        # Two helpers is how one of them ends up renamed and the other left to
        # rot. `authCall` attaches the session token; a second path that forgot
        # to would 401 instead of working.
        self.assertNotIn("postJSON", self.script)
        self.assertIn("async function authCall", self.script)


class NoSamplePhoneNumbers(unittest.TestCase):
    """A plausible mobile number in a placeholder reads as a filled-in field.

    The WhatsApp panel hinted "98450 11237" in grey, which is indistinguishable
    at a glance from a number already entered - so people either trusted it or
    had to work out whether it was real. The shape of a phone number is not
    something anyone needs telling, and the country is already a dropdown.
    """

    PLACEHOLDER = re.compile(r'placeholder="([^"]*)"')

    @staticmethod
    def _reads_as_a_real_number(text: str) -> bool:
        """Seven or more digits that are not all the same one.

        "M80-0000" is a shape being described - the repeated zero is what makes
        it obviously a template. "98450 11237" is a number somebody could dial.
        """
        digits = [c for c in text if c.isdigit()]
        return len(digits) >= 7 and len(set(digits)) > 1

    def test_no_placeholder_looks_like_a_real_mobile(self):
        for page in ("../PORTAL/app.html", "../PORTAL/login.html", "../PORTAL/index.html"):
            with open(os.path.join(ROOT, page), encoding="utf-8") as handle:
                found = [t for t in self.PLACEHOLDER.findall(handle.read())
                         if self._reads_as_a_real_number(t)]
            self.assertEqual(found, [], f"{page}: {found}")


class TheCodeFieldTakesFocus(unittest.TestCase):
    """One action on the panel, and the code is on the screen in front of them."""

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_the_verification_field_is_focused(self):
        self.assertIn("code.focus()", self.source)

    def test_a_half_typed_code_survives_a_re_render(self):
        # renderAll fires on unrelated state changes and rebuilds this panel
        # wholesale; without carrying the draft across, a code disappears
        # mid-entry for no reason the person can see.
        self.assertIn("code.value = draft", self.source)
        self.assertIn("setSelectionRange(caret, caret)", self.source)


class TheDetailPaneMatchesTheListBesideIt(unittest.TestCase):
    """What fills the right-hand pane must be a row on the left, or say so.

    `selectedId` was chosen once at module load, when SUBMISSIONS was still
    empty, and never revisited after the real records arrived. renderDetail
    therefore fell back to SUBMISSIONS[0] - the newest receipt - whether or not
    it was awaiting review. A claim the agent had released filled the whole
    detail pane with no corresponding row in the queue, which reads as a
    missing row rather than a wrong selection.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_the_selection_is_repaired_once_real_records_load(self):
        block = self.source.split("async function loadRecords()", 1)[1]
        self.assertIn("const stillListed", block)
        self.assertIn("SUBMISSIONS.filter(isQueued)", block)

    def test_it_prefers_a_claim_that_is_actually_in_the_queue(self):
        block = self.source.split("const stillListed", 1)[1].split("}", 1)[0]
        self.assertIn("queued[0] || SUBMISSIONS[0]", block)

    def test_a_claim_outside_the_queue_says_where_it_lives(self):
        # "released by agent" is true but does not answer the question the
        # reviewer actually has, which is why there is no row for it.
        self.assertIn("see Pending settlement", self.source)


class ClaimRowsOpenTheClaim(unittest.TestCase):
    """A reviewer about to release money can see what they are paying for.

    Pending settlement and Settled both listed a vendor and an amount with no
    way through to the claim behind them - the only route was another tab and a
    hunt. Both tables now open the claim, and both go through one helper so a
    third table cannot quietly behave differently.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_every_claim_table_marks_its_rows(self):
        # Pending settlement, Settled, Rejected, and My expenses - plus the
        # Rejected sub-tab of the Review queue, which lists the same claims
        # where they were decided rather than under the tab about money that
        # moved.
        calls = re.findall(r"^\s+opensClaim\(tr, ", self.source, re.M)
        self.assertEqual(len(calls), 6,
                         "Review queue and its Rejected tab, Pending "
                         "settlement, Settled, Rejected and My expenses")

    def test_every_one_is_wired_after_its_rows_are_built(self):
        self.assertEqual(self.source.count("wireClaimRows(tb);"), 5)

    def test_a_click_on_a_control_does_not_navigate(self):
        # Record payment opens an inline form, and the receipt link opens the
        # image. Navigating away in the same click makes either look broken.
        body = self.source.split("function wireClaimRows(", 1)[1].split("\n}", 1)[0]
        for control in ("button", "a", "input", "select", "label", "[data-receipt]"):
            self.assertIn(control, body.split("closest(", 1)[1].split(")", 1)[0])

    def test_the_row_is_reachable_without_a_mouse(self):
        helper = self.source.split("function opensClaim(", 1)[1].split("\n}", 1)[0]
        self.assertIn("tabIndex", helper)
        self.assertIn('setAttribute("role", "link")', helper)
        body = self.source.split("function wireClaimRows(", 1)[1].split("\n}", 1)[0]
        self.assertIn('e.key === "Enter"', body)

    def test_changing_tab_goes_through_one_function(self):
        # Two ways to switch view is how one of them forgets to scroll to the
        # top, or to re-render.
        self.assertIn("function goTo(", self.source)
        self.assertIn('b.addEventListener("click", () => goTo(id));', self.source)


class AClaimHasItsOwnPage(unittest.TestCase):
    """Clicking a claim opens the claim, not a different tab.

    A row in Pending settlement used to send the reviewer to the Review queue -
    a page whose whole left half is a list that, by definition, did not contain
    the claim they had just clicked. The tab highlight moved too, so it read as
    having been thrown somewhere rather than having drilled in.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_the_claim_view_exists_and_is_shown_like_any_other(self):
        self.assertIn('id="view-claim"', self.source)
        # The show/hide loop drives every view; one missing from it stays
        # display:none forever.
        listed = self.source.split("const ALL_VIEWS = [", 1)[1].split("];", 1)[0]
        self.assertIn('"claim"', listed)

    def test_it_belongs_to_no_tab_but_does_not_bounce_back_to_one(self):
        self.assertIn('view !== "claim" && view !== "mine" && !MANAGE_TABS.includes(view)',
                      self.source)

    def test_the_panels_are_moved_not_duplicated(self):
        # Two copies of a panel this size is two renderers, and two renderers
        # eventually disagree about what a claim says.
        self.assertEqual(self.source.count('id="detail-panel"'), 1)
        self.assertEqual(self.source.count('id="split-evidence"'), 1)
        self.assertIn("function hostDetail(", self.source)

    def test_both_hosts_are_reachable(self):
        body = self.source.split("function hostDetail(", 1)[1].split("\n}", 1)[0]
        self.assertIn("claim-slot", body)
        # One column beside the queue now, rather than the panel in the split
        # and the evidence dropped back under the whole view - which put the
        # receipt image below the queue, so a long list pushed the photograph
        # of the claim being read off the bottom of the screen.
        self.assertIn("detail-col", body)
        self.assertIn("col.append(panel, evidence)", body)

    def test_the_queue_draws_a_list_and_nothing_else(self):
        # The detail panels live on the claim page now. Rendering them under
        # the queue would be work nobody can see - and `renderDetail` picks a
        # claim when none is selected, which is how the split pane used to put
        # a claim on screen the reader had not chosen.
        line = [l for l in self.source.splitlines()
                if 'if (view === "queue")' in l and "renderQueue()" in l][0]
        self.assertIn('hostDetail("claim")', line)
        self.assertNotIn("renderDetail()", line)

    def test_a_queue_row_opens_the_claim_page(self):
        body = self.source.split("function renderQueue()", 1)[1].split("\n}", 1)[0]
        self.assertIn("opensClaim(tr, sub.id)", body)

    def test_the_queue_is_no_longer_half_a_split(self):
        # A receipt photograph and a table of line items had half the width,
        # and deciding a claim silently swapped the other half to a different
        # one.
        self.assertIn('<div id="split-queue">', self.source)

    def test_back_names_the_tab_it_returns_to(self):
        # Where the reader came from. It used to name wherever the claim now
        # belongs, which sounds helpful and is not: approve the last claim in
        # the review queue and Back silently became "Pending settlement", so
        # the way out of the list you were working led somewhere you had never
        # been. Deciding a claim moves the claim, not the reader.
        self.assertIn("TAB_LABEL[home]", self.source)
        self.assertIn("goTo(claimHome(", self.source)
        fn = self.source.split("function claimHome(sub) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (claimFrom) return claimFrom;", fn)

    def test_a_claim_opened_from_nowhere_still_has_a_way_out(self):
        # A link or a refresh names no list, and a dead button is worse than a
        # guess at where the claim belongs.
        fn = self.source.split("function claimHome(sub) {", 1)[1].split("\n}", 1)[0]
        self.assertIn('if (settlementStage(sub)) return "settled";', fn)
        self.assertIn('if (isQueued(sub)) return "queue";', fn)

    def test_one_list_of_tab_names(self):
        # The nav and the Back link read the same map, so a button cannot say
        # "Back to Payments" for a tab labelled "Pending settlement".
        self.assertIn("const TAB_LABEL = {", self.source)
        self.assertIn("const label = TAB_LABEL[id];", self.source)
        for tab in ("mine", "payments", "settled", "queue", "channels"):
            self.assertIn(f"{tab}:", self.source.split("const TAB_LABEL = {", 1)[1].split("};", 1)[0])

    def test_rows_open_the_claim_page_not_the_queue(self):
        self.assertIn("openClaim(row.dataset.claim)", self.source)
        self.assertNotIn('goTo("queue", row.dataset.claim)', self.source)


class MyExpensesLeadsSomewhere(unittest.TestCase):
    """Your own claims open, and you can take one back.

    My expenses listed a vendor, an amount and a status with no way through to
    the receipt or the reasoning behind the verdict - the one screen most staff
    ever see was also the one that answered the fewest questions.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_rows_open_the_claim(self):
        mine = self.source.split("function renderMine()", 1)[1].split("\n}", 1)[0]
        self.assertIn("opensClaim(tr, sub.id)", mine)
        self.assertIn("wireClaimRows(tb)", mine)

    def test_opening_the_receipt_image_does_not_also_navigate(self):
        body = self.source.split("function wireClaimRows(", 1)[1].split("\n}", 1)[0]
        self.assertIn("[data-receipt]", body)

    def test_reviewer_controls_are_gated(self):
        # My expenses is open to every member of staff and now leads to the
        # claim page. Showing Approve and Reject to someone the server would
        # refuse is worse than not showing them at all.
        detail = self.source.split("const abox = $(\"actions\")", 1)[1]
        self.assertIn("const mayReview = reviewingHere();", detail)
        self.assertIn("} else if (!mayReview) {", detail)

    def test_withdraw_is_offered_only_on_your_own_unpaid_claim(self):
        detail = self.source.split("const abox = $(\"actions\")", 1)[1].split("if (decision)", 1)[0]
        self.assertIn("isMine(sub)", detail)
        self.assertIn("!isPaid(sub)", detail)

    def test_a_withdrawn_claim_is_its_own_end_state(self):
        self.assertIn("withdrawn: \"Withdrawn\"", self.source)
        self.assertIn('.s-withdrawn', self.source)
        stage = self.source.split("function claimStage(", 1)[1].split("\n}", 1)[0]
        self.assertIn('stage:"withdrawn"', stage)


class ViewingLinksExpire(unittest.TestCase):
    """A cached link outlives the signature on it.

    Receipt originals are fetched as presigned URLs signed for five minutes,
    and the console cached the link on the claim indefinitely. Open a claim,
    go elsewhere, come back later, and the panel painted an image at a URL S3
    had stopped honouring - a broken icon, while the file size and the "open
    full size" link beside it looked perfectly healthy, because those came from
    the cached metadata rather than from loading anything.

    Nothing errored. The only symptom was a broken image, which reads as a lost
    receipt rather than an expired link.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_the_console_agrees_with_the_server_about_the_lifetime(self):
        with open(os.path.join(ROOT, "lambda_src/receipts.py"), encoding="utf-8") as handle:
            server = handle.read()
        ttl = int(re.search(r"VIEW_TTL_SECONDS = (\d+)", server).group(1))
        page = int(re.search(r"VIEW_TTL_MS = (\d+) \* 1000", self.source).group(1))
        self.assertEqual(page, ttl, "the page's idea of the TTL has drifted from the signer's")

    def test_a_link_goes_stale_before_it_expires(self):
        # One that dies mid-fetch is the same broken image to the reader.
        margin = re.search(r"VIEW_STALE_MS = VIEW_TTL_MS - (\d+) \* 1000", self.source)
        self.assertTrue(margin)
        self.assertGreater(int(margin.group(1)), 0)

    def test_every_reader_of_the_cache_checks_it(self):
        # The detail panel and the lightbox share one cached link; one of them
        # forgetting to check is the bug coming back through the other door.
        self.assertIn("if (stale(sub.original)) sub.original = null;", self.source)
        self.assertIn("stale(sub.original) ? await fetchOriginal(sub.id)", self.source)

    def test_the_helper_is_defined_before_anything_calls_it(self):
        # `const` is not hoisted; a render during evaluation would throw.
        self.assertLess(self.source.index("const stale = (original)"),
                        self.source.index("if (stale(sub.original))"))

    def test_a_fetched_link_is_stamped(self):
        body = self.source.split("async function fetchOriginal(", 1)[1].split("\n}", 1)[0]
        self.assertIn("at: Date.now()", body)

    def test_a_failed_image_is_retried_exactly_once(self):
        # Expiry is not the only way a link stops working, but a missing object
        # must not put the panel in a loop.
        body = self.source.split("function paintOriginal(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('img.addEventListener("error"', body)
        self.assertIn("if (retried)", body)
        self.assertIn("paintOriginal(sub, true)", body)


class TheSubmitterIsNotAlwaysAnEmployee(unittest.TestCase):
    """A receipt can come from a consultant or a contractor.

    The console called them "employee" throughout - a column header, a panel
    title, a button, the wording of a rejection prompt. The one thing every
    such person is, whatever their contract, is the person who submitted the
    claim.
    """

    PAGES = ("../PORTAL/app.html",)

    def setUp(self):
        self.text = {}
        for page in self.PAGES:
            with open(os.path.join(ROOT, page), encoding="utf-8") as handle:
                self.text[page] = handle.read()

    def test_no_visible_string_calls_them_an_employee(self):
        # Comments are prose about the design and may say what they like; this
        # is about the words on the screen.
        for page, body in self.text.items():
            script = "\n".join(re.findall(
                r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", body, re.S))
            markup = re.sub(r"<script(?![^>]*\bsrc=)[^>]*>.*?</script>", "", body, flags=re.S)
            markup = re.sub(r"<style[^>]*>.*?</style>", "", markup, flags=re.S)
            markup = re.sub(r"<!--.*?-->", "", markup, flags=re.S)
            self.assertNotIn("mployee", markup, f"{page}: markup still says employee")
            for quoted in re.findall(r'"([^"\n]{4,120})"', script):
                if "mployee" in quoted and "//" not in quoted:
                    self.fail(f"{page}: user-visible string says employee: {quoted}")

    def test_the_column_and_the_panel_name_the_submitter(self):
        app = self.text["../PORTAL/app.html"]
        self.assertIn('<th scope="col">Submitter</th>', app)
        self.assertIn("What the submitter is told", app)


class EveryChannelHasALabel(unittest.TestCase):
    """The console's channel names must match the ones the server writes.

    They had drifted in both directions at once. `telegram` and `concur` were
    prototype labels for channels nobody built, each taking a column in the
    intake report and always reading zero. Meanwhile `api` - real, shipping,
    and the one integrators use - had no label at all, so a receipt submitted
    through the REST endpoint rendered `undefined` as its own channel.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        with open(os.path.join(ROOT, "lambda_src/intake.py"), encoding="utf-8") as handle:
            self.intake = handle.read()
        block = self.app.split("const CHANNELS = {", 1)[1].split("};", 1)[0]
        self.labels = dict(re.findall(r'(\w+)\s*:\s*"([^"]+)"', block))

    def test_every_channel_intake_can_write_has_a_label(self):
        # The ternary in intake.py is the whole vocabulary the server uses,
        # plus "portal" for an upload made in the console itself.
        expr = self.intake.split("channel = (", 1)[1].split('else "email")', 1)[0] + '"email"' 
        written = set(re.findall(r'"(\w+)"', expr)) - {"/whatsapp", "/api"}
        written = {c for c in written if not c.startswith("/")}
        written.add("portal")
        self.assertTrue(written)
        self.assertEqual(written - set(self.labels), set(),
                         "a channel the server writes has no label in the console")

    def test_no_label_names_a_channel_that_does_not_exist(self):
        self.assertNotIn("telegram", self.labels)
        self.assertNotIn("concur", self.labels)

    def test_the_api_channel_is_named_generically(self):
        # Integrators are travel desks, HRMS and ERPs, not one named product.
        self.assertIn("api", self.labels)
        self.assertNotIn("concur", self.labels["api"].lower())


class ReviewingIsAScreenNotARole(unittest.TestCase):
    """Approve and Reject belong to the Review queue, nowhere else.

    They were gated on "may this person review", which in a small organisation
    is true of the owner - who is also, routinely, a submitter. So opening your
    own claim from My expenses offered you Approve as reviewed, on your own
    lunch, from the screen you were looking at as its claimant.

    The same person wears both hats. Which hat they have on is a property of
    the screen they are on, not of the account they signed in with.

    It is now two questions asked of the same screen, because two roles act on
    a claim from it: whether it can be *decided* here, and whether a payment
    can be *recorded* here. A finance executive answers no to the first and
    yes to the second.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()
        self.screen = self.source.split(
            "function aboutSomebodyElsesClaim() {", 1)[1].split("\n}", 1)[0]

    def test_the_decision_panel_asks_about_the_screen(self):
        self.assertIn("const mayReview = reviewingHere();", self.source)

    def test_the_review_queue_reviews(self):
        self.assertIn('if (view === "queue") return true;', self.screen)

    def test_my_expenses_does_not(self):
        # The claim page inherits the context it was opened from.
        self.assertIn('if (view === "claim") return claimFrom !== "mine";',
                      self.screen)

    def test_nothing_else_counts(self):
        # Pending settlement has its own controls on its own rows; a claim
        # opened from there goes to the claim page, handled above.
        self.assertTrue(self.screen.rstrip().endswith("return false;"),
                        self.screen)

    def test_a_member_of_staff_neither_reviews_nor_settles_anywhere(self):
        review = self.source.split("function reviewingHere() {", 1)[1].split(
            "\n}", 1)[0]
        settle = self.source.split("function settlingHere() {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("can.review() && aboutSomebodyElsesClaim()", review)
        self.assertIn("can.seeQueue() && aboutSomebodyElsesClaim()", settle)

    def test_the_two_bars_are_different_heights(self):
        # The whole point of the split. Deciding is an administrator's;
        # recording a payment is a finance executive's.
        block = self.source.split("const can = {", 1)[1].split("};", 1)[0]
        self.assertIn("review:     () => rankOf(currentUser.tier) >= RANK.admin,",
                      block)
        self.assertIn("seeQueue:   () => rankOf(currentUser.tier) >= RANK.finance,",
                      block)

    def test_the_payment_controls_hang_on_the_lower_one(self):
        # Gating them on `reviewingHere` would lock a finance executive out of
        # the one job that is theirs.
        self.assertIn("renderSettleOnClaim(sub, settlingHere());", self.source)

    def test_the_submitter_keeps_their_own_actions(self):
        # Withdraw is the claimant's, so it keys off whose claim it is - not
        # off which screen they are on, and not off mayReview.
        withdraw = self.source.split("wd.textContent = \"Withdraw\"", 1)[0]
        guard = withdraw.rsplit("if (", 1)[1]
        self.assertIn("isMine(sub)", guard)
        self.assertIn("!mayReview", guard)

    def test_a_claim_waiting_on_you_says_so_rather_than_blaming_finance(self):
        self.assertIn("Waiting on your answer above.", self.source)

    def test_a_finance_executive_is_told_why_they_cannot_act(self):
        # "You will hear the outcome by email and WhatsApp" is the submitter's
        # line, and they reach this branch too now.
        self.assertIn("Awaiting review by an owner or administrator.",
                      self.source)
        self.assertIn("In review. You will hear the outcome", self.source)

class ButtonsLookLikeButtons(unittest.TestCase):
    """A secondary button was a white rectangle with a faint outline.

    "Back to My expenses" and "Withdraw" sat on white or near-white panels and
    read as labels rather than controls. Affordance first - a raised edge, a
    hover that fills, a press that moves - and colour second, to say quietly
    what kind of action it is.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()
        self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.source, re.S))

    def test_a_plain_button_is_raised_hovers_and_presses(self):
        base = self.css.split(".btn { font:inherit", 1)[1].split(".btn.primary", 1)[0]
        self.assertIn("box-shadow:inset", base)
        self.assertIn(".btn:hover:not(:disabled)", base)
        self.assertIn(".btn:active:not(:disabled)", base)

    def test_keyboard_focus_is_visible(self):
        self.assertIn(".btn:focus-visible", self.css)

    def test_withdrawing_your_own_claim_is_cautionary_not_a_refusal(self):
        # Amber, not the red reserved for refusing somebody else's money.
        self.assertIn(".btn.caution", self.css)
        caution = self.css.split(".btn.caution {", 1)[1].split("}", 1)[0]
        self.assertIn("--warn", caution)
        self.assertIn('wd.className = "btn caution"', self.source)

    def test_going_back_is_a_control_but_never_the_next_thing_to_do(self):
        self.assertIn(".btn.back", self.css)
        self.assertIn('class="btn back"', self.source)

    def test_every_variant_is_defined_from_tokens(self):
        # A literal colour would be right in one theme and wrong in the other.
        for variant in (".btn.primary {", ".btn.danger {", ".btn.caution {", ".btn.back {"):
            rule = self.css.split(variant, 1)[1].split("}", 1)[0]
            self.assertNotIn("#", rule, variant)


class TwoSurfacesNotElevenTabs(unittest.TestCase):
    """Everybody has their own receipts; only some administer other people's.

    Mixing both into one strip gave a member of staff a tab bar with a single
    tab in it - furniture, not navigation - and gave an owner their own lunch
    filed beside the organisation's billing settings.

    The switch is a change of view, not of privilege: nobody gains or loses
    anything by flipping it. But it does put on screen the question the console
    already answers internally, which is which hat the person is wearing.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()
        self.nav = self.source.split("function renderNav() {", 1)[1].split("\n}", 1)[0]

    def test_a_member_of_staff_gets_no_tab_strip(self):
        self.assertIn("nav.hidden = true;", self.nav)

    def test_but_still_gets_a_page(self):
        # Both early returns have to show a view; skipping it would render a
        # console with nothing in it.
        before_first_return = self.nav.split("return;", 1)[0]
        self.assertIn("showView();", before_first_return)

    def test_every_path_out_of_the_nav_shows_a_view(self):
        exits = self.nav.count("return;")
        self.assertGreater(exits, 0)
        self.assertEqual(self.nav.count("showView()"), exits + 1,
                         "one per early return, plus the fall-through")
        for before in self.nav.split("return;")[:-1]:
            self.assertIn("showView()", before[-90:])

    def test_the_managing_tabs_are_hidden_on_your_own_expenses(self):
        self.assertIn("if (!managing) { nav.hidden = true; showView(); return; }", self.nav)

    def test_the_switch_is_named_for_the_role(self):
        # A Finance Executive does not administer the organisation and an owner
        # does more than finance; one generic word serves neither.
        self.assertIn('owner: "Administration"', self.source)
        self.assertIn('finance: "Finance"', self.source)
        # An administrator administers the organisation - that is the word for
        # it. Missing here, they fell through to the generic "Manage", the
        # same omission that made the sign-in demote them to a submitter.
        self.assertIn('admin: "Administration"', self.source)

    def test_switching_back_returns_where_you_were(self):
        self.assertIn("lastManageView", self.nav)

    def test_the_switch_lives_with_the_identity_it_switches(self):
        # Not in the tab strip it was pushing onto two lines - and being
        # outside `.nav` is what stops `.nav button:hover` painting the
        # selected half's label the same shade as its fill.
        header = self.source.split('<header class="masthead">', 1)[1].split("</header>", 1)[0]
        self.assertIn('id="modesw"', header)
        self.assertNotIn('id="modesw"', self.source.split("</header>", 1)[1])

    def test_each_half_has_its_own_hover(self):
        css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.source, re.S))
        self.assertIn('.modesw button[aria-pressed="false"]:hover', css)
        self.assertIn('.modesw button[aria-pressed="true"]:hover', css)


class BackIsVisiblyAControl(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", handle.read(), re.S))

    def test_it_is_tinted_rather_than_muted(self):
        # Muting it made a control look like a caption.
        #
        # Anchored on the newline so a more specific selector ending in the
        # same text - `.verdict .btn.back`, which only sets its size - is not
        # mistaken for the rule that carries the colour.
        rule = self.css.split("\n.btn.back {", 1)[1].split("}", 1)[0]
        self.assertIn("--review", rule)
        self.assertIn("background", rule)

    def test_it_lives_on_the_nav_bar_not_in_the_verdict_row(self):
        # There were two ways out sitting four inches apart: a Back button in
        # the claim's header, and a bar above naming the same list. One bar
        # carries both now - out of the list, and along it.
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            app = handle.read()
        self.assertEqual(0, app.count("claim-back"))
        self.assertIn('<button class="btn back" type="button" id="claim-pos">', app)

class TheAgentIsCreditedHonestly(unittest.TestCase):
    """What the agent did, said exactly. It reads receipts and applies the
    policy; it does not settle anything, and a page claiming otherwise takes
    credit for a payment a person made."""

    PAGES = ("../PORTAL/app.html", "../PORTAL/index.html", "../PORTAL/login.html")

    def test_nothing_claims_a_claim_settles_itself(self):
        for page in self.PAGES:
            with open(os.path.join(ROOT, page), encoding="utf-8") as handle:
                body = handle.read()
            for overclaim in ("no human action required",
                              "releases on its own",
                              "Settled &middot; no human"):
                self.assertNotIn(overclaim, body, f"{page}: {overclaim}")

    def test_the_agent_is_credited_with_what_it_did(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            app = handle.read()
        self.assertIn("Reviewed by the agent", app)
        self.assertIn("Reviewed with no human involved.", app)


class AnEmptyQueueLooksEmpty(unittest.TestCase):
    """Deciding a claim took it out of the queue and left it on screen.

    So an empty Review queue sat beside the last thing approved, with Record
    payment and Reject still offered - on the one tab whose entire job is
    answering "what is still waiting for me".
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()
        self.fn = self.source.split("function renderDetail() {", 1)[1].split("\n}", 1)[0]

    def test_the_queue_only_shows_a_claim_that_is_in_it(self):
        self.assertIn('if (view === "queue") {', self.fn)
        self.assertIn("!sub || !isQueued(sub)", self.fn)

    def test_with_nothing_queued_it_shows_nothing(self):
        # null, which is what drives the blank state.
        self.assertIn("SUBMISSIONS.filter(isQueued)[0] || null", self.fn)

    def test_other_pages_keep_their_selection(self):
        # The claim page shows whatever was opened, queued or not.
        self.assertIn("sub = sub || SUBMISSIONS[0];", self.fn)

    def test_the_empty_message_does_not_overclaim(self):
        # Some of those were approved by a person, not cleared on their own.
        self.assertNotIn("Every receipt today cleared policy on its own", self.source)
        self.assertIn("Every claim has been decided. Nothing here needs you.",
                      self.source)

    def test_it_says_where_the_cleared_ones_went(self):
        # As somewhere to go rather than as a sentence. The old line said
        # "anything cleared is in Pending settlement until it is paid", which
        # is true and leaves the reader to go and count it.
        self.assertIn('["payments", "Pending settlement",', self.source)
        self.assertIn("owed to submitters, waiting to be paid.", self.source)


class SignOutIsAControl(unittest.TestCase):
    def test_it_is_styled_like_every_other_button(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            source = handle.read()
        header = source.split('<header class="masthead">', 1)[1].split("</header>", 1)[0]
        self.assertIn('class="btn back"', header)
        self.assertNotIn('class="chip"', header)


class EachRoleLandsWhereItWorks(unittest.TestCase):
    """An owner signed in and arrived at their own lunch.

    The console renders once before /auth/me answers, and until it does the
    signed-in person is assumed to be staff - so the first render forced the
    view to My expenses. That stuck when the real role arrived a moment later,
    because My expenses is a perfectly valid view for an owner too, and nothing
    distinguished "the default nobody chose" from "the tab they clicked".
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_a_chosen_view_is_distinguishable_from_a_defaulted_one(self):
        self.assertIn("viewChosen = false", self.source)
        goto = self.source.split("function goTo(id, claimId) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("viewChosen = true;", goto)

    def test_the_default_is_recomputed_not_latched(self):
        # The role arrives after the first render; latching would keep whatever
        # was guessed before it did.
        nav = self.source.split("function renderNav() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (!viewChosen) {", nav)
        self.assertIn('view = admin ? (HOME_TAB[currentUser.tier] || "queue") : "mine";', nav)

    def test_each_role_starts_where_its_day_starts(self):
        # An owner opens the console to see how the month is going; a finance
        # executive opens it to pay people.
        self.assertIn('HOME_TAB = { owner: "reports", admin: "reports", finance: "payments" }',
                      self.source)

    def test_the_landing_tab_is_a_real_managing_tab(self):
        homes = re.findall(r'HOME_TAB = \{([^}]*)\}', self.source)[0]
        listed = set(re.findall(r'"(\w+)"', homes))
        tabs = set(re.findall(r'"(\w+)"',
                              self.source.split("const MANAGE_TABS = [", 1)[1].split("];", 1)[0]))
        self.assertEqual(listed - tabs, set(), "a landing tab that is not in the strip")

    def test_switching_away_and_back_returns_there(self):
        nav = self.source.split("function renderNav() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (admin) lastManageView = view;", nav)


class TheNavCountsWhatIsWaiting(unittest.TestCase):
    """Three kinds of count, and they are not the same kind of news.

    A queue length is work waiting for a person: it goes down when they do
    something. A headcount is not work at all, and giving it the colour that
    means "deal with me" teaches the eye to discount every badge in the strip.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()
        self.nav = self.source.split("function renderNav() {", 1)[1].split("\n}", 1)[0]
        self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.source, re.S))

    def test_the_queue_carries_what_is_waiting(self):
        self.assertIn('if (id === "queue") {', self.nav)
        self.assertIn("SUBMISSIONS.filter(isQueued).length", self.nav)

    def test_settlement_carries_what_is_owed(self):
        self.assertIn('if (id === "payments") {', self.nav)
        self.assertIn("payableClaims().filter(c => c.outstanding > 0).length", self.nav)

    def test_people_carries_a_headcount_and_the_dormant_ones(self):
        self.assertIn('if (id === "people") {', self.nav)
        self.assertIn('x.invite !== "accepted"', self.nav)

    def test_the_headcount_is_who_can_send_a_receipt_not_how_many_rows(self):
        # 59 on an account with 46 unaccepted invitations describes a company
        # that does not exist yet. Thirteen people are using it.
        self.assertIn('const active = PEOPLE.filter(x => x.invite === "accepted").length;',
                      self.nav)
        self.assertNotIn("const total = PEOPLE.length;", self.nav)

    def test_nothing_is_shown_when_there_is_nothing_to_show(self):
        # A row of zeroes is noise, and teaches people to stop reading badges.
        for guard in ("if (waiting)", "if (owed)", "if (active)", "if (dormant)"):
            self.assertIn(guard, self.nav, guard)

    def test_work_and_headcount_are_different_colours(self):
        todo = self.css.split(".navflag.todo {", 1)[1].split("}", 1)[0]
        count = self.css.split(".navflag.count {", 1)[1].split("}", 1)[0]
        self.assertIn("--review", todo)
        self.assertNotIn("--review", count)

    def test_dormant_reads_as_something_to_look_at(self):
        rule = self.css.split(".navflag.dormant {", 1)[1].split("}", 1)[0]
        self.assertIn("--warn", rule)

    def test_every_badge_says_what_it_counts(self):
        # A bare number in a nav strip is a riddle.
        calls = re.findall(r"badge\((.*?)\);", self.nav, re.S)
        self.assertGreaterEqual(len(calls), 5)
        for call in calls:
            self.assertGreaterEqual(call.count(","), 2, f"no title: badge({call[:60]}")
        self.assertIn("flag.title = title;", self.nav)


class TheBalanceIsAlwaysOnScreen(unittest.TestCase):
    """How much is left is a question you want answered before you ask it.

    Running out does not fail loudly: receipts keep arriving and queue
    unaudited. So the balance sits in the nav whatever it is, and changes
    colour only once the answer has started to matter.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.source = handle.read()
        self.nav = self.source.split("function renderNav() {", 1)[1].split("\n}", 1)[0]
        self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.source, re.S))

    def test_it_is_shown_whatever_it_is(self):
        # Unlike the queue badges, which are hidden at zero: a balance of zero
        # is the single most important number on the page.
        block = self.nav.split('if (id === "billing") {', 1)[1].split("\n    }", 1)[0]
        self.assertIn("badge(cls,", block)
        self.assertNotIn("if (left)", block)

    def test_low_is_measured_against_how_fast_this_organisation_spends(self):
        # A fixed number would shout at a company filing four receipts a month
        # and stay quiet for one filing four hundred.
        block = self.nav.split('if (id === "billing") {', 1)[1].split("\n    }", 1)[0]
        self.assertIn("runway", block)
        self.assertIn("credits.used", block)

    def test_empty_is_louder_than_low(self):
        block = self.nav.split('if (id === "billing") {', 1)[1].split("\n    }", 1)[0]
        self.assertIn('left <= 0 ? "balance out"', block)
        self.assertIn('"balance low"', block)
        self.assertIn(".navflag.balance.out", self.css)
        self.assertIn(".navflag.balance.low", self.css)

    def test_a_healthy_balance_is_quiet(self):
        rule = self.css.split(".navflag.balance {", 1)[1].split("}", 1)[0]
        self.assertIn("--ok", rule)

    def test_it_says_what_running_out_means(self):
        self.assertIn("receipts are queued but not audited", self.nav)


class BadgesSitWithTheirLabel(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", handle.read(), re.S))

    def test_the_gap_is_not_applied_twice(self):
        # The nav button's own flex gap already separates the badge from the
        # label; a margin on top of it pushed the number out to roughly halfway
        # between its tab and the next.
        rule = self.css.split(".navflag {", 1)[1].split("}", 1)[0]
        self.assertIn("margin-left:0", rule)


class TheOwnerIsTheBusinessNotUs(unittest.TestCase):
    """"Solution Owner" named the wrong party.

    We build the solution; the customer owns a business. Read on their screen,
    "Solution Owner" describes Mobil80, not the person who writes the expense
    policy and buys the credits. That person is Owner/Management.
    """

    PAGES = ("../PORTAL/app.html", "../PORTAL/index.html", "../PORTAL/login.html",
             "../BMS/home.html", "../BMS/index.html")
    SOURCES = ("lambda_src/auth.py", "lambda_src/policy.py", "lambda_src/budgets.py",
               "lambda_src/notify.py")

    def read(self, path):
        with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
            return handle.read()

    def test_no_page_calls_the_role_a_solution_owner(self):
        for page in self.PAGES:
            self.assertNotIn("solution owner", self.read(page).lower(), page)

    def test_no_server_string_calls_the_role_a_solution_owner(self):
        for source in self.SOURCES:
            self.assertNotIn("solution owner", self.read(source).lower(), source)

    def test_the_console_labels_the_role_for_the_business(self):
        # "Owner", plainly. It was "Owner/Management" from when the role did
        # two jobs - owning the account and running it. Those are now two
        # roles, so the name that hedged between them names neither.
        app = self.read("../PORTAL/app.html")
        self.assertIn('owner: "Owner"', app)
        self.assertIn('admin: "Administrator"', app)
        # And never offered as something to grant: there is one owner, and it
        # moves by transfer.
        self.assertNotIn('<option value="owner">', app)


class ThePortalIsAChannelToo(unittest.TestCase):
    """Four ways in, not three.

    Receipts have been submittable from the console for a while, but every
    place that listed the routes still named three. A customer counting the
    channels we advertise should find the one they are looking at.
    """

    def read(self, path):
        with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
            return handle.read()

    def test_the_masthead_names_the_portal(self):
        masthead = self.read("../PORTAL/app.html").split('<header class="masthead">', 1)[1]
        tagline = masthead.split("</p>", 1)[0]
        for channel in ("WhatsApp", "email", "portal", "API"):
            self.assertIn(channel, tagline, f"the tagline drops {channel}")

    def test_the_marketing_page_has_a_fourth_card(self):
        site = self.read("../PORTAL/index.html")
        channels = site.split('<section id="channels">', 1)[1].split("</section>", 1)[0]
        self.assertEqual(4, channels.count("<article>"), "not four channels")
        self.assertIn("<h3>Portal</h3>", channels)
        self.assertIn("Four ways in", channels)
        # A three-column grid would leave the fourth card alone on its row.
        self.assertIn('class="grid g4"', channels)

    def test_nothing_still_advertises_three_ways_in(self):
        for page in ("../PORTAL/index.html", "../PORTAL/app.html", "../PORTAL/login.html"):
            body = self.read(page).lower()
            self.assertNotIn("three ways in", body, page)
            self.assertNotIn("3</span><span class=\"l\">ways in", body, page)


class AnExpenseTypeInUseCannotBeDeleted(unittest.TestCase):
    """A type is the word every claim ever judged under it is filed by.

    Deleting one was a single unconfirmed click that spliced the rule out of
    the array, and every historical claim of that type was left pointing at a
    label that no longer existed - with no warning, and no count of what was
    about to lose its rule.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        # Deleting a type is part of what a type *is*, so it moved to the
        # Organisation editor along with its name and its categories. The
        # Policy rules panel sets caps and sets nothing else.
        self.detail = self.app.split("function renderTypeDetail(", 1)[1].split(
            "\nfunction ", 1)[0]

    def test_a_type_with_claims_offers_no_delete_at_all(self):
        # Not a disabled button and not a confirm: the option is absent, and
        # what to do instead is named in its place.
        self.assertIn("if (!ro && count) {", self.detail)
        self.assertIn("cannot be deleted while claims are filed under it", self.detail)

    def test_the_alternatives_are_named_rather_than_left_to_be_found(self):
        self.assertIn("Switch it to ", self.detail)
        self.assertIn("edit the name to ", self.detail)

    def test_deleting_an_unused_type_is_still_confirmed(self):
        # Nothing loaded uses it, which is not the same as nothing ever
        # having used it - this console holds the most recent claims.
        block = self.detail.split("Delete type", 1)[1]
        self.assertIn("window.confirm(", block)
        self.assertLess(block.index("window.confirm("), block.index("splice"))

    def test_the_count_does_not_claim_to_be_todays(self):
        # It counts every claim loaded, not today's, and said "of today's
        # receipts" - so a type with a year of history read as having two.
        self.assertNotIn("of today's receipts", self.detail)


class TheChangeHistoryIsNotInvented(unittest.TestCase):
    """Four seeded entries signed by somebody who does not exist.

    They sat on the one screen whose entire job is saying who changed what,
    above the real entries and rendered identically to them.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_no_seeded_author(self):
        self.assertNotIn('by:"A. Fernandes"', self.app)

    def test_the_log_starts_empty(self):
        block = self.app.split("const changelog = ", 1)[1].split(";", 1)[0]
        self.assertEqual("[]", block.strip())

    def test_an_empty_log_says_so_rather_than_rendering_nothing(self):
        self.assertIn("No changes yet. Every edit to a rule is recorded here", self.app)

    def test_the_auto_clear_rate_is_not_measured_against_an_invented_baseline(self):
        # "+7 pts" described movement from a constant nobody ever set.
        self.assertNotIn("baselineAutoClear", self.app)
        self.assertNotIn("unchanged from v4", self.app)

    def test_the_version_starts_at_one(self):
        self.assertIn("  version: 1,", self.app)


class TheBudgetScopeListIsBounded(unittest.TestCase):
    """One row per person, in a column as tall as the payroll.

    At two people it fits. At two hundred the Save button sits below every
    name in the organisation, so setting one person's limit means scrolling
    past everybody else to reach it.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.app, re.S))

    def test_the_list_scrolls_inside_itself(self):
        outer = self.css.split(".budscopes {", 1)[1].split("}", 1)[0]
        self.assertIn("max-height", outer)
        # The cap is on the box; the scrolling is on the list inside it, so
        # the filter above stays put without being sticky.
        inner = self.css.split(".scopelist {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-y:auto", inner)

    def test_a_filter_narrows_it(self):
        self.assertIn('filter.className = "scopefilter"', self.app)
        self.assertIn("Filter people", self.app)

    def test_the_one_row_list_is_not_given_a_filter(self):
        # Nothing to narrow: the Organisation tab holds a single row called
        # "Everyone", and a search box over it is furniture.
        block = self.app.split("function renderBudgets(", 1)[1].split("\n}", 1)[0]
        self.assertIn('const filterable = budScopeKind !== "org"', block)

    def test_the_filter_stays_reachable_while_the_names_scroll(self):
        # Outside the scrolling part rather than sticky over it, so it cannot
        # sit on top of the first name.
        self.assertTrue('scopeList.className = "scopelist"' in self.app,
                        "the scope list is no longer built as its own element")
        rule = self.css.split(".scopelist {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-y:auto", rule)

    def test_the_cap_is_not_overruled_by_the_grid(self):
        # A grid item defaults to min-height:auto, which refuses to shrink
        # below its content - so max-height alone did nothing and the column
        # still grew to the whole list.
        rule = self.css.split(".budscopes {", 1)[1].split("}", 1)[0]
        self.assertIn("min-height:0", rule)
        self.assertIn("min-height:0", self.css.split(".scopelist {", 1)[1].split("}", 1)[0])

    def test_typing_in_it_does_not_lose_what_was_typed(self):
        # The list redraws on every keystroke, so the value lives outside the
        # render and focus is put back afterwards.
        self.assertIn("let budFilter", self.app)
        self.assertIn("again.focus()", self.app)

    def test_the_tabs_have_the_width_of_the_panel(self):
        # Three labels and two counts do not fit a 224px column: the first
        # word was clipped and the others touched.
        view = self.app.split('<p class="hint" id="bud-hint">', 1)[1].split(
            '<div class="formrow">', 1)[0]
        self.assertIn('id="bud-scopekinds"', view)
        self.assertLess(view.index('id="bud-scopekinds"'), view.index('class="budgrid"'))
        self.assertIn(".scopekinds {", self.css)
        # And they are drawn there, not inside the column.
        block = self.app.split("function renderBudgets(", 1)[1].split("\n}", 1)[0]
        self.assertIn('const tabs = $("bud-scopekinds")', block)
        self.assertNotIn("scopes.appendChild(tabs)", block)

    def test_each_kind_of_scope_is_its_own_tab(self):
        # Three headed sections of one column meant reaching a person by
        # scrolling past every group.
        self.assertIn('const SCOPE_KINDS = [["org", "Organisation"], '
                      '["groups", "Groups"], ["people", "People"]]', self.app)
        block = self.app.split("function renderBudgets(", 1)[1].split("\n}", 1)[0]
        self.assertIn('budScopeKind === "org"', block)
        self.assertIn('budScopeKind === "groups"', block)
        self.assertNotIn("scopehead", block)

    def test_only_the_chosen_kind_is_drawn(self):
        block = self.app.split("function renderBudgets(", 1)[1].split("\n}", 1)[0]
        rows = block.split("const groups = ORG_PROFILE.groups.filter", 1)[1]
        self.assertIn("} else if (budScopeKind ===", rows)
        # Choosing a tab sets the kind and moves the selection with it.
        self.assertIn("budScopeKind = kind;", block)
        self.assertIn("budScope = firstScopeOf(kind);", block)

    def test_an_empty_tab_says_which_kind_is_empty(self):
        block = self.app.split("function renderBudgets(", 1)[1].split("\n}", 1)[0]
        self.assertIn("No groups yet.", block)
        self.assertIn("Nothing matches that.", block)


class TheBackOfficeShowsHowBigAnAccountIs(unittest.TestCase):
    """Balance and receipts, and no idea how many people are behind them.

    An organisation that invited forty people and has two signed in is a very
    different account from one with two people in it, and nothing on the row
    said which.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../BMS/home.html"), encoding="utf-8") as handle:
            self.page = handle.read()
        with open(os.path.join(ROOT, "lambda_src/admin.py"), encoding="utf-8") as handle:
            self.admin = handle.read()

    def test_the_table_has_a_people_column(self):
        head = self.page.split("<thead>", 1)[1].split("</thead>", 1)[0]
        self.assertIn(">People<", head)
        # A header with no cell under it shifts every column after it.
        body = self.page.split("function renderOrgs(", 1)[1]
        row = body.split("`<td><span class=\"orgname\">", 1)[1].split("tb.appendChild", 1)[0]
        self.assertEqual(head.count("<th"), row.count("`<td") + 1,
                         "the header and the row disagree about how many columns there are")
        # And the empty state spans the same width, or it sits under the
        # wrong columns the moment one is added.
        self.assertIn(f'colspan="{head.count("<th")}"', body)

    def test_the_server_counts_them(self):
        self.assertIn("def _headcounts(", self.admin)
        listing = self.admin.split("def _list_orgs(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"users": seat["users"]', listing)
        self.assertIn('"active_users": seat["active"]', listing)

    def test_a_removed_person_is_not_counted(self):
        fn = self.admin.split("def _headcounts(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('row.get("status") == "removed"', fn)

    def test_it_pages_rather_than_reading_one_scan(self):
        # A scan returns a page. Counting only the first would under-report
        # every organisation once the table outgrows 1MB.
        fn = self.admin.split("def _headcounts(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("LastEvaluatedKey", fn)

    def test_the_row_says_how_many_have_not_signed_in(self):
        self.assertIn("not signed in", self.page)


class TheSettlementFieldsShareALine(unittest.TestCase):
    """Five fields on one row, sitting at five different heights.

    `align-items:end` lined up the bottoms of the cells, so Amount - the only
    one carrying a hint underneath - was pushed a whole line above the rest,
    and a select, a text input and a date input each brought their own height
    to the other four.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", handle.read(), re.S))

    def test_the_cells_are_aligned_from_the_top(self):
        rule = self.css.split(".settle-grid {", 1)[1].split("}", 1)[0]
        self.assertIn("align-items:start", rule)
        self.assertNotIn("align-items:end", rule)

    def test_every_control_is_the_same_height(self):
        rule = self.css.split(".settle .sf input, .settle .sf select {", 1)[1].split("}", 1)[0]
        self.assertIn("height:30px", rule)

    def test_no_hint_is_taken_out_of_the_flow(self):
        # They were absolutely positioned into 17px of reserved padding, which
        # holds exactly as long as every hint is one line. "Outstanding $23.60.
        # Lower it to part settle." wraps to two in a 122px column, and the
        # half that did not fit was drawn straight up over the input above it.
        #
        # Reserving a fixed height for text of unknown length is the bug. The
        # grid aligns from the top, so a taller cell does not move the others
        # and the controls stay on one line whatever the hints beneath them do.
        self.assertNotIn(".settle-grid .sf .sf-hint {", self.css)
        rule = self.css.split(".settle .sf-hint {", 1)[1].split("}", 1)[0]
        self.assertNotIn("position:absolute", rule)
        self.assertNotIn("padding-bottom", self.css.split(".settle-grid .sf {", 1)[1].split("}", 1)[0])


class EveryRouteInLooksDifferent(unittest.TestCase):
    """Four channels that behave differently, all tagged in the same grey.

    A WhatsApp claim can be asked a question and answered in the same thread;
    an API claim has nobody at the other end. Which one a row came in on is
    worth seeing without reading it.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.app, re.S))

    def test_every_channel_has_its_own_tint(self):
        for channel in ("whatsapp", "email", "portal", "api"):
            self.assertIn(f".src.ch-{channel} {{", self.css, f"{channel} has no tint")

    def test_the_mark_carries_it_as_well_as_the_colour(self):
        # A colour alone is lost on a monochrome screen, and on anybody who
        # cannot separate the two darker shades.
        marks = self.app.split("const CHANNEL_MARK = {", 1)[1].split("}", 1)[0]
        for channel in ("whatsapp", "email", "portal", "api"):
            self.assertIn(channel, marks)
        glyphs = re.findall(r'"(\\u[0-9A-Fa-f]{4})"', marks)
        self.assertEqual(4, len(set(glyphs)), f"two channels share a glyph: {glyphs}")

    def test_one_helper_draws_them_all(self):
        # Four call sites drawing the tag four ways is how one of them keeps
        # the old grey after a change like this.
        self.assertEqual(0, self.app.count('<span class="src">'))
        self.assertGreaterEqual(self.app.count("channelTag("), 4)


class ADecisionIsSignedWithAName(unittest.TestCase):
    """The queue read RIYAD@MOBIL80.COM in a column of first names.

    A decision records who made it at the moment it was made, and somebody
    with no name on file at that moment was recorded by their email address.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_an_address_is_resolved_through_people(self):
        fn = self.app.split("function personName(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("PEOPLE.find(", fn)
        # Naming somebody later has to fix the rows already stamped.
        self.assertIn('p.email || ""', fn)

    def test_an_unmatched_address_is_still_shown(self):
        fn = self.app.split("function personName(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn('said.split("@")[0]', fn)

    def test_it_is_used_wherever_a_decider_is_named(self):
        self.assertGreaterEqual(self.app.count("personName("), 5)


class WhatTheSubmitterIsToldIsWhatWasSent(unittest.TestCase):
    """The panel quotes the message, rather than composing its own.

    It showed `sub.rationale` - the model's audit explanation, written for the
    verdict and dispatched to nobody - under a heading that reads "What the
    submitter is told". A reviewer read a paragraph the person had never seen,
    while the real message said the same thing in different words.

    The settlement half was the same fault the other way up: a second
    implementation of `settled_notice` written in JavaScript, assembling its
    own sentences from the payment record. Two authors of one message, free to
    drift, with nothing that would catch it if they did. There is one author
    now - notify.py - and it records what it sent.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.fn = self.app.split("function paintWhatTheyWereTold(", 1)[1].split(
            "\nfunction ", 1)[0]

    def test_it_prints_the_stored_message_verbatim(self):
        self.assertIn("const n = sub.lastNotice;", self.fn)
        self.assertIn("box.textContent = (waOnly ? n.whatsapp : n.text)", self.fn)

    def test_it_no_longer_composes_a_settlement_notice_of_its_own(self):
        for stray in ("has been reimbursed.", "Still owed: ", "Reference: ",
                      "Settled by: ", "Part of your claim"):
            self.assertNotIn(stray, self.fn,
                             f"the console is still writing {stray!r} itself")

    def test_the_wording_a_whatsapp_only_submitter_got_is_the_wording_shown(self):
        # The two are not the same sentence, and the one they have is theirs.
        self.assertIn("const waOnly = n.wa && !n.email;", self.fn)

    def test_the_caption_names_which_message_it_is(self):
        self.assertIn('id="told-when"', self.app)
        self.assertIn("NOTICE_WHEN[n.kind]", self.fn)
        kinds = self.app.split("const NOTICE_WHEN = {", 1)[1].split("};", 1)[0]
        for kind in ("outcome", "approved", "rejected", "disputed", "settled"):
            self.assertIn(f"{kind}:", kinds)

    def test_every_kind_the_server_sends_has_a_caption(self):
        notify = open(os.path.join(ROOT, "lambda_src/notify.py"), encoding="utf-8").read()
        sent = set(re.findall(r'"(\w+)": \w+_notice',
                              notify.split("NOTICES = {", 1)[1].split("}", 1)[0]))
        kinds = self.app.split("const NOTICE_WHEN = {", 1)[1].split("};", 1)[0]
        captioned = set(re.findall(r"(\w+):", kinds))
        # `low_credits` goes to whoever can top up, never to a claimant, so it
        # is never recorded against a claim.
        self.assertEqual(set(), sent - captioned - {"low_credits"})

    def test_the_attribution_says_which_channels_it_reached(self):
        self.assertIn('id="told-attrib"', self.app)
        self.assertIn('n.email && "email"', self.fn)
        self.assertIn('n.wa && "WhatsApp"', self.fn)

    def test_a_claim_with_no_recorded_message_says_so_plainly(self):
        # A portal or API submission has no thread to answer on, and anything
        # from before this was stored has nothing kept. The rationale stands
        # in, labelled as the agent's reading rather than as something sent.
        self.assertIn("the agent's reading — not sent", self.fn)
        self.assertIn("sub.rationale", self.fn)

    def test_nothing_that_failed_to_send_is_shown_as_sent(self):
        notify = open(os.path.join(ROOT, "lambda_src/notify.py"), encoding="utf-8").read()
        rec = notify.split("def record(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('if not (notice.get("email") or notice.get("wa")):', rec)

    def test_recording_it_can_never_fail_the_thing_it_reports(self):
        notify = open(os.path.join(ROOT, "lambda_src/notify.py"), encoding="utf-8").read()
        rec = notify.split("def record(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("except Exception:", rec)

    def test_the_notice_is_not_reflowed_as_prose(self):
        css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.app, re.S))
        rule = css.split(".rationale.asnotice {", 1)[1].split("}", 1)[0]
        self.assertIn("white-space:pre-line", rule)


class ReportsSayWhatWasActuallyPaid(unittest.TestCase):
    """Four headline figures, none of which was money out.

    Reimbursable is what policy agreed to; settled is what somebody has been
    paid. Reading the tab you could tell how much had been approved for
    payment in a month and not how much of it had left the bank.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_there_is_a_settled_tile(self):
        tiles = self.app.split('<section class="tiles4">', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="rp-settled"', tiles)
        self.assertIn(">Settled<", tiles)

    def test_it_counts_the_claims_as_well_as_the_money(self):
        block = self.app.split("const settledPaid =", 1)[1].split("// ---- by expense type", 1)[0]
        self.assertIn("settledCount", block)
        self.assertIn("reimbursed", block)

    def test_it_reports_what_was_paid_not_what_was_approved(self):
        # A claim approved for 5,000 and not yet paid is not settled money.
        block = self.app.split("const settledPaid =", 1)[1].split(";", 1)[0]
        self.assertIn("r.settled", block)
        self.assertIn("settled: paidFor(sub.id)", self.app)
        self.assertNotIn("reimbursable", block)

    def test_a_part_payment_is_not_counted_as_a_whole_one(self):
        block = self.app.split("const settledPaid =", 1)[1].split("// ---- by expense type", 1)[0]
        self.assertIn("in part", block)
        self.assertIn("still owed", block)

    def test_an_empty_period_renders_it_too(self):
        # Every other tile is blanked there; one left holding last month's
        # figure is worse than a dash.
        empty = self.app.split("function renderEmptyPeriod(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn('$("rp-settled")', empty)

    def test_what_a_claim_is_owed_has_one_definition(self):
        # Three places derived it from the same two fields, and a reviewer's
        # override was missing from at least one of them.
        self.assertIn("const approvedFor =", self.app)
# One definition, now inside `payable()` - which converts it into
        # the currency payouts are made in.
        self.assertIn("const approvedFor = (sub) =>", self.app)
        self.assertIn("approvedInClaimCcy(sub)", self.app)


class AHintDoesNotClaimAHandsWidthOfPanel(unittest.TestCase):
    """`.hint` carries `flex:1 1 200px` for the hints that sit in a row.

    Put one in a column and that basis becomes a *height*: the note under
    Default currency claimed 200px and grew, opening a blank band across the
    middle of the Organisation panel between it and the line beneath.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.app, re.S))

    def test_a_hint_in_a_column_does_not_grow(self):
        self.assertIn(".setgrid .f .hint { flex:0 0 auto; }", self.css)

    def test_a_hint_sitting_straight_in_a_panel_does_not_either(self):
        rule = self.css.split(".panel > .hint {", 1)[1].split("}", 1)[0]
        self.assertIn("flex:none", rule)

    def test_the_row_hints_keep_their_growth(self):
        # The base rule still has to work where it was written for: a hint
        # filling the rest of a row of controls.
        base = self.css.split(".hint { font-size", 1)[1].split("}", 1)[0]
        self.assertIn("flex:1 1 200px", base)


class TheOrganisationSaveIsJustSave(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_the_button_says_save(self):
        self.assertIn('id="o-save">Save</button>', self.app)

    def test_it_reads_as_the_commit_control(self):
        self.assertIn('class="btn save" type="button" id="o-save"', self.app)


class ByMonthSaysWhatWasPaid(unittest.TestCase):
    """Claimed, reimbursable and disallowed — and no money out.

    The table answered what a month cost and what policy agreed to, but not
    how much of it had actually been reimbursed, which is the figure finance
    reconciles against a bank statement.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.fn = self.app.split("function renderByMonth(", 1)[1].split("\nfunction ", 1)[0]

    def test_the_header_has_the_column(self):
        head = self.app.split('<tbody id="r-months">', 1)[0].rsplit("<thead>", 1)[1]
        self.assertIn(">Settled<", head)

    def test_the_header_the_rows_and_the_year_total_agree(self):
        # A header with no cell under it shifts every column after it, and the
        # year total is a separate row that has to match too.
        head = self.app.split('<tbody id="r-months">', 1)[0].rsplit("<thead>", 1)[1]
        columns = head.count("<th")
        row = self.fn.split("tr.innerHTML +=", 1)[1].split("tb.appendChild(tr)", 1)[0]
        # The month name is written in the branch above this one.
        self.assertEqual(columns, row.count("<td") + 1, "the month row is the wrong width")
        foot = self.fn.split("foot.innerHTML =", 1)[1].split("tb.appendChild(foot)", 1)[0]
        self.assertEqual(columns, foot.count("<td"), "the year total is the wrong width")

    def test_it_counts_what_was_paid(self):
        # Through `analysed()` now, which converts each claim at its own
        # stamped rate so a month with receipts in two currencies charts both.
        self.assertIn("settled: add(r => r.settled)", self.fn)
        self.assertIn("settled: paidFor(sub.id)", self.app)

    def test_the_year_total_adds_it_up(self):
        self.assertIn("settled: t.settled + m.settled", self.fn)

    def test_a_month_that_has_not_happened_shows_nothing_rather_than_a_dash(self):
        # "—" reads as "nothing was paid"; a month in the future has not had
        # the chance.
        cell = self.fn.split("m.settled ? fmt(m.settled, ccy)", 1)[1].split("+", 1)[0]
        self.assertIn("ahead", cell)


class SettledRunsThroughEveryReport(unittest.TestCase):
    """Money out is the figure finance reconciles against a bank statement.

    It existed on exactly one sub-tab. Claimed, reimbursable and disallowed
    were everywhere; what had actually been paid was on none of them, so the
    only way to answer "how much did we reimburse this quarter, by team" was
    to open every claim.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def table(self, tbody):
        """The header cells and the row template for one report table."""
        head = self.app.split(f'<tbody id="{tbody}">', 1)[0].rsplit("<thead>", 1)[1]
        return head

    def test_by_expense_type_has_it(self):
        self.assertIn(">Settled<", self.table("r-types"))
        block = self.app.split("// ---- by expense type ----", 1)[1].split(
            "// ---- by group ----", 1)[0]
        self.assertIn("settled: sum(all, r => r.settled)", block)

    def test_by_group_has_it(self):
        self.assertIn(">Settled<", self.table("r-groups"))
        block = self.app.split("// ---- by group ----", 1)[1].split(
            "// ---- where the money goes", 1)[0]
        self.assertIn("settled: sum(b.rows, r => r.settled)", block)

    def test_by_month_has_it(self):
        self.assertIn(">Settled<", self.table("r-months"))

    def test_intake_has_it_per_channel(self):
        block = self.app.split("function renderIntake(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("paidFor(s.id)", block)
        self.assertIn("settled", block)

    def test_intake_says_which_currency_the_money_is_in(self):
        # The counts beside it span every currency; the money cannot.
        self.assertIn('id="intake-ccy"', self.app)
        block = self.app.split("function renderIntake(", 1)[1].split("\nfunction ", 1)[0]
# One currency across every channel, because every payment is
        # recorded in it - nothing is dropped for being in another.
        self.assertIn("orgCurrency()", block)

    def test_the_summary_carries_what_is_still_owed(self):
        # The Settlement position tab was a fourth copy of three numbers
        # already on the headline row. The one thing it had that the tiles
        # did not was the outstanding figure, so that moved up.
        block = self.app.split("const settledPaid =", 1)[1].split(
            "// ---- by expense type", 1)[0]
        self.assertIn("still owed", block)
        self.assertNotIn('["settlement", "Settlement position"]', self.app)
        self.assertNotIn('id="rsec-settlement"', self.app)

    def test_owed_is_measured_against_cleared_claims_only(self):
        # Counting the queue as owed put the whole month's claimed total
        # behind the payment run. Nobody owes money on a claim that has not
        # been approved and may yet be rejected.
        block = self.app.split("const settledPaid =", 1)[1].split(
            "// ---- by expense type", 1)[0]
        self.assertIn("clearedRows", block)
        self.assertIn("const clearedRows = live.filter(r =>", self.app)
        self.assertIn("sum(clearedRows, r => r.reimb) - settledPaid", block)
        head = self.app.split("const clearedRows =", 1)[1].split(";", 1)[0]
        self.assertIn('"approved", "part_settled", "settled"', head)

    def test_the_widened_tables_blank_at_their_new_width(self):
        empty = self.app.split("function renderEmptyPeriod(", 1)[1].split("\nfunction ", 1)[0]
        for tbody in ("r-types", "r-groups"):
            columns = self.table(tbody).count("<th")
            self.assertIn(f'blank("{tbody}", {columns},', empty,
                          f"{tbody} blanks at the wrong width")


class OrganisationAndGroupsAreTwoSurfaces(unittest.TestCase):
    """Two panels stacked on one page is a page you scroll past one to reach.

    Billing details are set once when an account opens; groups are edited
    whenever a team changes. Different questions on different days, so they
    are two tabs - the split Reports and Budgets already use.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_the_view_has_a_subtab_strip(self):
        self.assertIn('id="o-subtabs"', self.app)
        self.assertIn('const ORG_SECTIONS = [["org", "Organisation"], ["groups", "Groups"],\n'
                      '                      ["types", "Expense types"], ["audit", "Audit log"]]',
                      self.app)

    def test_each_panel_sits_in_its_own_section(self):
        view = self.app.split('id="view-org"', 1)[1].split("ONE CLAIM", 1)[0]
        self.assertIn('id="osec-org"', view)
        self.assertIn('id="osec-groups"', view)
        # The group list belongs to the groups section, not the billing one.
        self.assertLess(view.index('id="osec-groups"'), view.index('id="grp-list"'))
        self.assertLess(view.index('id="osec-org"'), view.index('id="o-save"'))

    def test_only_the_chosen_section_is_shown(self):
        fn = self.app.split("function renderOrgTabs(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn('sec.hidden = orgSection !== id', fn)

    def test_the_groups_tab_says_how_many(self):
        fn = self.app.split("function renderOrgTabs(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("ORG_PROFILE.groups", fn)
        self.assertIn("navflag count", fn)

    def test_switching_redraws(self):
        fn = self.app.split("function renderOrgTabs(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("orgSection = id;", fn)
        self.assertIn("renderOrg();", fn)


class AnExpiredSessionSignsYouOut(unittest.TestCase):
    """A token in the tab is not proof of a session; the server decides that.

    Left open overnight, the console came back to a page that looked signed in
    and was not: a name rendered as an ellipsis, nought submissions, an upload
    button that would refuse. `/me` had answered 401 and the loader simply
    returned, leaving the shell drawn for nobody - which reads as the product
    being broken rather than as twelve hours having passed.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        with open(os.path.join(ROOT, "../PORTAL/login.html"), encoding="utf-8") as handle:
            self.login = handle.read()

    def test_a_401_ends_the_session_here_too(self):
        fn = self.app.split("async function authCall(", 1)[1].split("\n}", 1)[0]
        self.assertIn("res.status === 401", fn)
        self.assertIn("sessionOver()", fn)

    def test_a_403_does_not(self):
        # Signed in, not permitted. Throwing somebody out for opening a tab
        # their role cannot see would lose whatever they were in the middle of.
        fn = self.app.split("async function authCall(", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("res.status === 403", fn)

    def test_the_stale_token_is_cleared_not_just_navigated_away_from(self):
        fn = self.app.split("function sessionOver(", 1)[1].split("\n}", 1)[0]
        self.assertIn('sessionStorage.removeItem("expenze_token")', fn)
        self.assertIn("login.html?expired=1", fn)

    def test_it_redirects_once_however_many_calls_fail(self):
        # A page load fires several calls at once; every one comes back 401.
        fn = self.app.split("function sessionOver(", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (sessionEnded) return;", fn)
        self.assertIn("let sessionEnded = false;", self.app)

    def test_the_sign_in_page_says_why_you_are_there(self):
        # "Time passed" and "something broke" look identical on a bare sign-in
        # page, and only one of them makes somebody call support.
        self.assertIn('id="expired-note"', self.login)
        self.assertIn("Your session ended while you were away", self.login)
        self.assertIn('get("expired")', self.login)

    def test_the_note_is_hidden_for_an_ordinary_sign_in(self):
        note = self.login.split('id="expired-note"', 1)[1].split(">", 1)[0]
        self.assertIn("hidden", note)


class WorkAndSettingsAreSeparated(unittest.TestCase):
    """Ten tabs of equal weight made Channels & API look as important as the
    review queue.

    One divider, not one per group: everything to the left of it is somewhere
    you go because something is waiting, everything to the right is somewhere
    you go because you decided to change something. Five groups across ten
    tabs would have been more structure than anybody internalises, and two of
    them would have been a single tab - which reads as a stray line rather
    than as a group.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.app, re.S))

    def test_there_is_exactly_one_break(self):
        self.assertIn('const SETTINGS_FROM = "budgets";', self.app)
        nav = self.app.split("function renderNav(", 1)[1].split("\nfunction ", 1)[0]
        self.assertEqual(1, nav.count("settingsfrom"),
                         "more than one tab is marked as starting a group")

    def test_the_break_falls_where_authoring_starts(self):
        # Budgets and policy rules are limits somebody writes; the queues,
        # reports and people are places something is waiting.
        tabs = self.app.split("const MANAGE_TABS = ", 1)[1].split("]", 1)[0]
        order = re.findall(r'"(\w+)"', tabs)
        self.assertEqual(order.index("budgets"), order.index("people") + 1)
        self.assertLess(order.index("budgets"), order.index("billing"))

    def test_the_line_belongs_to_the_tab_not_to_the_row(self):
        # The row wraps on a narrow window. A divider of its own would be
        # stranded at the end of the first line instead of heading the second.
        rule = self.css.split(".nav button.settingsfrom {", 1)[1].split("}", 1)[0]
        self.assertIn("border-left", rule)
        self.assertNotIn('class="navsep"', self.app)

    def test_it_is_a_rule_and_not_a_colour(self):
        # The console already uses colour semantically - over budget, waiting,
        # cleared. A second colour dimension meaning "category" would collide
        # with the first.
        rule = self.css.split(".nav button.settingsfrom {", 1)[1].split("}", 1)[0]
        self.assertNotIn("background", rule)
        self.assertIn("var(--rule-firm)", rule)

    def test_the_badges_are_untouched(self):
        # They already encode the hierarchy that matters day to day.
        nav = self.app.split("function renderNav(", 1)[1].split("\nfunction ", 1)[0]
        for tab in ('id === "queue"', 'id === "payments"', 'id === "billing"'):
            self.assertIn(tab, nav)


class TheConsoleKeepsUpWithTheServer(unittest.TestCase):
    """It loaded once at sign-in and never again, so a tab left open was a
    photograph.

    Somebody who had accepted their invitation an hour earlier still read as
    "invitation not accepted", and the only cure was a browser reload nobody
    thinks to do - the page looks fine, it is just old.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.app, re.S))

    def test_coming_back_to_the_tab_reloads(self):
        # The moment staleness starts to cost something, and it asks nothing
        # of the person.
        self.assertIn('document.addEventListener("visibilitychange", refreshOnReturn)', self.app)
        self.assertIn('window.addEventListener("focus", refreshOnReturn)', self.app)

    def test_it_does_not_poll(self):
        # A timer hitting the API every minute for every open tab buys almost
        # nothing over reloading on focus and costs it on every account.
        self.assertNotIn("setInterval(refreshNow", self.app)
        self.assertIn("setInterval(paintRefreshedAt", self.app)

    def test_a_reload_cannot_discard_what_somebody_typed(self):
        # Budgets, policy rules and the organisation form hold their edits
        # locally until Save.
        fn = self.app.split("function mayAutoRefresh(", 1)[1].split("\n}", 1)[0]
        self.assertIn("SAFE_TO_REFRESH.has(view)", fn)
        self.assertIn("payOpen", fn)
        self.assertIn("INPUT|SELECT|TEXTAREA", fn)
        safe = self.app.split("const SAFE_TO_REFRESH = new Set(", 1)[1].split(")", 1)[0]
        for held in ("budgets", "rules", "org", "channels"):
            self.assertNotIn(f'"{held}"', safe, f"{held} holds unsaved edits")

    def test_the_page_says_how_old_it_is(self):
        # A button alone answers "can I get fresh data". The stamp answers
        # "do I need to", which is the question somebody actually has.
        self.assertIn('id="refresh-when"', self.app)
        fn = self.app.split("function sinceLabel(", 1)[1].split("\n}", 1)[0]
        self.assertIn("just now", fn)
        self.assertIn("m ago", fn)

    def test_the_stamp_ages_on_its_own(self):
        # Otherwise it reads "just now" for an hour.
        self.assertIn("setInterval(paintRefreshedAt, 30 * 1000)", self.app)

    def test_the_button_speaks_up_once_the_figures_are_old(self):
        fn = self.app.split("function paintRefreshedAt(", 1)[1].split("\n}", 1)[0]
        self.assertIn('classList.toggle("stale"', fn)
        self.assertIn(".btn.refresh.stale {", self.css)

    def test_two_refreshes_do_not_overlap(self):
        fn = self.app.split("async function refreshNow(", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (refreshing", fn)

    def test_the_control_is_in_the_masthead_not_per_page(self):
        # It reloads everything the console knows, so it belongs beside the
        # identity rather than repeated on each tab.
        head = self.app.split('<header class="masthead">', 1)[1].split("</header>", 1)[0]
        self.assertIn('id="refresh"', head)
        self.assertEqual(1, self.app.count('id="refresh"'))


class TheBudgetEditorFollowsTheTab(unittest.TestCase):
    """Open Groups and the editor beside it still showed Everyone's limits.

    Two panels describing different things, side by side, with nothing in the
    list highlighted - and the line above the form still reading "applies to
    everyone with no group or personal limit of their own".
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.app, re.S))

    def test_choosing_a_tab_moves_the_selection(self):
        self.assertIn("budScope = firstScopeOf(kind)", self.app)

    def test_a_leftover_selection_is_corrected_on_render(self):
        # Belt as well as braces: the tab is not the only thing that can leave
        # the two out of step - a person can be removed while selected.
        self.assertIn("if (!scopeBelongsTo(budScope, budScopeKind))", self.app)

    def test_it_knows_which_kind_a_scope_is(self):
        fn = self.app.split("function scopeBelongsTo(", 1)[1].split("\n}", 1)[0]
        self.assertIn('scope.startsWith("g:")', fn)
        self.assertIn('scope.startsWith("p:")', fn)

    def test_an_empty_tab_falls_back_rather_than_selecting_nothing(self):
        fn = self.app.split("function firstScopeOf(", 1)[1].split("\n}", 1)[0]
        self.assertIn('return "org"', fn)


class TheBudgetPanelIsProportioned(unittest.TestCase):
    """Cramped on the left, empty on the right.

    A 224px column held long group names and their note touching each other,
    while the form beside it stretched the full width of a 1500px panel with
    the amount marooned at the far end.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", handle.read(), re.S))

    def test_the_column_can_grow(self):
        rule = self.css.split(".budgrid {", 1)[1].split("}", 1)[0]
        self.assertIn("minmax(", rule)

    def test_the_form_is_read_at_its_own_width(self):
        self.assertIn(".budeditor > * { max-width", self.css)

    def test_a_long_name_does_not_shove_its_note_off(self):
        rule = self.css.split(".scope > span:first-child {", 1)[1].split("}", 1)[0]
        self.assertIn("text-overflow:ellipsis", rule)
        # `flex:0 0 auto` on the note, so the name is what gives way rather
        # than the figure beside it. It used to be its own rule further up the
        # sheet; merged into the one rule for this selector, since two rules
        # for one selector is how a later copy silently overrides an earlier.
        rule = self.css.split(".scope .setn {", 1)[1].split("}", 1)[0]
        self.assertIn("flex:0 0 auto", rule)

    def test_the_column_does_not_show_bare_rule_colour_under_the_list(self):
        # The gap colour belongs to the list, not to the box around it - it
        # read as a dead grey block below the last row.
        rule = self.css.split(".budscopes {", 1)[1].split("}", 1)[0]
        self.assertIn("background:var(--surface)", rule)


class InheritsIsOnlySaidWhenItIsTrue(unittest.TestCase):
    """The organisation has nothing above it to inherit from.

    And on a new account, where nothing is set anywhere, the word appeared on
    every row at once: a column of one repeated word carrying no information.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_the_organisation_never_claims_to_inherit(self):
        fn = self.app.split("const inheritsFrom = (", 1)[1].split("};", 1)[0]
        self.assertIn('if (kind === "org") return false;', fn)

    def test_a_group_inherits_only_when_the_organisation_sets_something(self):
        fn = self.app.split("const inheritsFrom = (", 1)[1].split("};", 1)[0]
        self.assertIn('if (kind === "groups") return orgHasLimits;', fn)

    def test_a_person_inherits_from_their_groups_as_well(self):
        fn = self.app.split("const inheritsFrom = (", 1)[1].split("};", 1)[0]
        self.assertIn("theirGroups.some(", fn)

    def test_otherwise_the_cell_is_empty(self):
        # "Nothing set" is already what an empty cell means.
        self.assertIn('const note = n ? n + " set" : (inheritsFrom(kind, key) ? "inherits" : "");',
                      self.app)

    def test_the_word_explains_itself_on_hover(self):
        self.assertIn("A limit set above this level still applies.", self.app)


class TheBackOfficeSaveFollowsTheEdit(unittest.TestCase):
    """The WhatsApp number is at the top of the panel; Save is below the
    pricing table.

    So somebody changing the number types it, watches "unsaved changes" appear
    beside a heading that has already scrolled off, and leaves. The change is
    lost without a word - which is exactly what happened to the first attempt
    at moving to the dedicated number.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../BMS/home.html"), encoding="utf-8") as handle:
            self.page = handle.read()
        self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.page, re.S))

    def test_the_actions_stick_to_the_window_while_there_is_something_to_save(self):
        rule = self.css.split(".formrow.pinned {", 1)[1].split("}", 1)[0]
        self.assertIn("position:sticky", rule)
        self.assertIn("bottom:0", rule)

    def test_it_is_pinned_only_when_dirty(self):
        fn = self.page.split("function paintSettingsState(", 1)[1].split("\n}", 1)[0]
        self.assertIn('classList.toggle("pinned", dirty)', fn)

    def test_every_edit_repaints_it(self):
        # Three separate handlers used to set the label by hand; one of them
        # forgetting would leave the bar up after a save or down after a
        # change.
        self.assertGreaterEqual(self.page.count("paintSettingsState()"), 4)
        self.assertNotIn('$("settings-sub").textContent = settingsDirty()', self.page)

    def test_saving_and_discarding_both_put_it_away(self):
        # Split on the handler's own closing line, not the first "});" - the
        # fetch options inside it end with one too.
        save = self.page.split('$("s-save").addEventListener', 1)[1].split("\n});", 1)[0]
        self.assertIn("paintSettingsState()", save)
        reset = self.page.split('$("s-reset").addEventListener', 1)[1].split("\n});", 1)[0]
        self.assertIn("paintSettingsState()", reset)


class TheRollIsReadBackNotGuessedAt(unittest.TestCase):
    """People disagreed with the account.

    An invitation pushed an invented row onto the list saying "invitation not
    accepted", which stayed that way until the next full reload - so somebody
    who signed in five minutes later still read as pending. And re-inviting an
    address that already had a row added a second copy beside it, because the
    server overwrites by (email, org) and a local push does not: the same
    person appeared twice, once as removed and once as invited, while the
    table held one active row.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_a_successful_invitation_reloads(self):
        handler = self.app.split('$("p-add").addEventListener', 1)[1].split("\n});", 1)[0]
        self.assertIn("await loadRecords()", handler)
        # And no longer invents the row it just asked the server to create.
        after = handler.split("Invitation emailed to", 1)[1]
        self.assertNotIn("addLocally(", after)

    def test_the_invented_row_is_only_for_the_sessionless_demo(self):
        handler = self.app.split('$("p-add").addEventListener', 1)[1].split("\n});", 1)[0]
        before = handler.split("btn.disabled = true", 1)[0]
        self.assertIn("addLocally(name, email)", before)
        self.assertIn("role switcher has no session", before)

    def test_removing_and_reinstating_both_reload(self):
        # Removal reloaded without waiting and then rendered over the top, so
        # the screen showed the guess and the answer arrived unseen.
        block = self.app.split('act.textContent = removed ? "Reinstate access"', 1)[1]
        block = block.split("foot.appendChild(act)", 1)[0]
        self.assertIn("await loadRecords()", block)
        self.assertEqual(1, block.count("loadRecords("),
                         "one reload, after both branches")

    def test_a_refused_change_puts_the_row_back_and_stops(self):
        block = self.app.split('act.textContent = removed ? "Reinstate access"', 1)[1]
        block = block.split("foot.appendChild(act)", 1)[0]
        failure = block.split("if (!ok) {", 1)[1].split("}", 1)[0]
        self.assertIn("person.status = prev", failure)
        self.assertIn("return", failure)


class AReceiptStillBeingReadIsNotWorkWaiting(unittest.TestCase):
    """The review queue listed receipts the agent had not finished reading.

    `queued` and `auditing` are the agent's own states: no figures, no line
    items, no verdict. They appeared in the queue anyway, with a dash in every
    column and live Approve and Reject buttons underneath - and approving one
    means approving a bill nobody has read, the agent included.

    A receipt whose audit died mid-flight then sat there for ever, because
    nothing ever moved it on.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_the_agent_s_own_states_are_not_queued(self):
        fn = self.app.split("const stillReading =", 1)[1].split(";", 1)[0]
        for state in ("queued", "auditing", "pending"):
            self.assertIn(f'"{state}"', fn)
        queued = self.app.split("const isQueued = (sub) => {", 1)[1].split("\n};", 1)[0]
        # A claim with no figures yet is not work waiting for a person - with
        # one exception, added later: one a reviewer just corrected stays on
        # the list while it is re-read, or it vanishes from under them.
        self.assertIn("if (stillReading(sub)) return !!sub.correctedAt", queued)

    def test_a_receipt_that_cannot_be_read_does_stay_in_the_queue(self):
        # Those are genuinely waiting on a person, and dropping them would be
        # the opposite mistake - a receipt charged for and never seen.
        fn = self.app.split("const stillReading =", 1)[1].split(";", 1)[0]
        for terminal in ("needs_human", "no_original"):
            self.assertNotIn(terminal, fn)

    def test_the_submitter_still_sees_it(self):
        # Hiding it entirely would leave somebody who just sent a receipt with
        # no evidence it arrived.
        self.assertIn("It is only not presented as work", self.app)

    def test_an_audited_claim_does_not_claim_to_be_still_reading(self):
        # "Being read…" is a statement about now. A claim with a verdict and
        # no vendor is not being read; it has no vendor, which is different.
        block = self.app.split("vendor: s.vendor ||", 1)[1].split("),", 1)[0]
        self.assertIn('s.status === "audited" ? "Vendor not printed"', block)


class ModelNumbersAreWrittenAsDecimals(unittest.TestCase):
    """DynamoDB refuses a float, and `json` happily preserves one.

    Every model-derived structure was round-tripped through JSON before being
    written, with a comment explaining that this made it safe. It did not: the
    round trip preserves a float exactly, and the model returns one whenever it
    answers a quantity or an amount as a number rather than a string.

        TypeError: Float types are not supported. Use Decimal types instead.

    The write threw, the receipt stayed in `auditing`, and the person who sent
    it watched "Being read…" for ever - with the credit already spent.
    """

    def test_a_json_round_trip_is_only_used_for_writes(self):
        # `json.loads(json.dumps(x))` means "make this safe for DynamoDB"
        # throughout this codebase. Using it as a deep copy as well makes the
        # two indistinguishable - to a reader and to the check below.
        with open(os.path.join(ROOT, "lambda_src", "handler.py"), encoding="utf-8") as h:
            handler = h.read()
        schema = handler.split("def _receipt_schema(", 1)[1].split("\nRECEIPT_SCHEMA", 1)[0]
        self.assertIn("copy.deepcopy(RECEIPT_SCHEMA)", schema)
        self.assertNotIn("json.dumps(RECEIPT_SCHEMA)", schema)

    def test_every_model_derived_write_converts_floats(self):
        import re as _re
        offences = []
        for name in ("auditor_worker.py", "handler.py", "fx.py"):
            path = os.path.join(ROOT, "lambda_src", name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for line, text in enumerate(source.splitlines(), 1):
                if "json.dumps(" in text and "json.loads(" in text:
                    if "parse_float=Decimal" not in text:
                        # Allow the closing half of a statement split over two
                        # lines to carry the argument instead.
                        window = "\n".join(source.splitlines()[line - 1:line + 1])
                        if "parse_float=Decimal" not in window:
                            offences.append(f"{name}:{line} {text.strip()}")
        self.assertEqual(offences, [], "\n".join(offences))

    def test_the_comment_no_longer_claims_the_round_trip_is_enough(self):
        with open(os.path.join(ROOT, "lambda_src", "auditor_worker.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("Float types are not supported", source)
        self.assertNotIn("DynamoDB refuses float, and every\n                # amount in here is already a decimal string.", source)


class PolicyRulesAreRealNow(unittest.TestCase):
    """The Policy rules tab was decoration.

    Every edit lived in the browser, bumped a version number nobody stored,
    and the auditor went on judging every receipt by the built-in set. An
    owner could add a type, change a cap, disable one, watch the console
    recompute in front of them - and nothing about any receipt ever changed.
    """

    def setUp(self):
        for name, path in (("auth", "lambda_src/auth.py"),
                           ("policy", "lambda_src/policy.py"),
                           ("handler", "lambda_src/handler.py"),
                           ("stack", "expensifyai/stack.py"),
                           ("app", "../PORTAL/app.html")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_the_auditor_reads_the_organisation_s_own_rules(self):
        audit = self.handler.split("def audit(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("rules = policy.rules_for(org)", audit)
        self.assertIn("_run_policy(receipt, resolved[\"currency\"], rules)", audit)

    def test_the_org_row_is_read_once_per_audit(self):
        # Reading it twice could give two answers if a policy change landed in
        # between, and a verdict computed half under each is unexplainable.
        audit = self.handler.split("def audit(", 1)[1].split("\ndef ", 1)[0]
        self.assertEqual(1, audit.count("_org(org_id)"))

    def test_the_model_is_offered_the_types_that_exist(self):
        # Otherwise a receipt for a type somebody added is classified as the
        # nearest built-in one.
        self.assertIn("_receipt_schema(rules)", self.handler)
        fn = self.handler.split("def _receipt_schema(", 1)[1].split("\nRECEIPT_SCHEMA", 1)[0]
        self.assertIn("policy.expense_type_ids(rules)", fn)
        self.assertIn('"not_covered"', fn)

    def test_an_account_that_never_saved_one_still_works(self):
        fn = self.policy.split("def rules_for(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("return DEFAULT_RULES", fn)

    def test_a_policy_with_nothing_enabled_is_refused(self):
        # Every receipt would resolve to "no rule covers this" - the product
        # still runs and has stopped doing the thing it is for.
        fn = self.policy.split("def normalise_rules(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("Leave at least one expense type enabled", fn)

    def test_saving_is_owner_only(self):
        # A budget flags spend after the fact; a policy decides what is paid.
        put = self.auth.split("def _rules_put(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('org["_role"] != "owner"', put)

    def test_the_version_is_the_server_s_to_set(self):
        # It is how a claim records which policy judged it, so it moves on
        # every save whatever the browser thinks it is.
        put = self.auth.split("def _rules_put(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("max(current, int(cleaned.get(\"version\") or 1)) + 1", put)

    def test_the_change_history_is_stored_and_bounded(self):
        put = self.auth.split("def _rules_put(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("rules_changelog", put)
        self.assertIn("history[:50]", put)

    def test_the_route_is_wired(self):
        self.assertIn('org_res.add_resource("rules").add_method("POST", auth_integration)',
                      self.stack)
        self.assertIn('path.endswith("/org/rules")', self.auth)

    def test_the_console_loads_them_rather_than_carrying_a_copy(self):
        self.assertIn("if (o.rules && Array.isArray(o.rules.expense_types) && !rulesDirty.length) {",
                      self.app)
        self.assertIn("rulesFromApi(o.rules);", self.app)

    def test_an_edit_is_a_draft_until_saved(self):
        self.assertIn("let rulesDirty = []", self.app)
        self.assertIn('id="rules-save"', self.app)
        self.assertIn("unsaved changes", self.app)

    def test_money_crosses_the_boundary_as_a_decimal_string(self):
        # Assigning "1500.00" straight across reads a fifteen-hundred-rupee
        # cap as fifteen rupees.
        out = self.app.split("function rulesToApi(", 1)[1].split("\n}", 1)[0]
        self.assertIn("(minor / 100).toFixed(2)", out)
        back = self.app.split("function rulesFromApi(", 1)[1].split("\n}", 1)[0]
        # `toMinor`, not `money` - this assertion originally named the
        # formatter and so held the bug in place rather than catching it.
        self.assertIn("toMinor(amount)", back)


class ThePaidColumnEarnsItsPlace(unittest.TestCase):
    """Approved and Outstanding were two columns printing one figure.

    Every claim on Pending settlement is awaiting reimbursement in full -
    nothing has been paid against it - so the two differ only on a claim
    somebody has part paid, which is not something this product tracks. Three
    right-aligned money columns, two of them identical on every row, on the
    screen where finance reads figures.

    One column, "Owed". The pair comes back, with Paid between them, only when
    a part payment has made them genuinely different numbers.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_the_split_appears_only_when_something_is_part_paid(self):
        self.assertIn("const anyPartPaid = part.length > 0;", self.app)
        self.assertIn("paidHead.hidden = !anyPartPaid", self.app)
        self.assertIn("approvedHead.hidden = !anyPartPaid", self.app)

    def test_approved_and_paid_appear_together_or_not_at_all(self):
        # Without a part payment, Approved *is* Owed - showing one of the two
        # would be half the removal.
        self.assertIn('owedHead.textContent = anyPartPaid ? "Outstanding" : "Owed";',
                      self.app)

    def test_the_row_emits_one_figure_or_three(self):
        # A wider slice than 400 characters: the Owed cell grew the billed
        # amount beneath it, and a fixed-length window is a test that passes
        # or fails on how much sits above the line it is looking for.
        row = self.app.split("// One figure, unless a part payment", 1)[1].split(
            "tb.appendChild(tr);", 1)[0]
        self.assertIn("(anyPartPaid", row)
        self.assertIn("fmt(c.approved, ccy)", row)
        self.assertIn("fmt(c.paid, ccy)", row)
        self.assertIn("fmt(c.outstanding, ccy)", row)

    def test_the_empty_row_spans_whichever_width_is_showing(self):
        self.assertIn("anyPartPaid ? 9 : 7", self.app)


class TheClaimActionsAreOneRow(unittest.TestCase):
    """Two stacked bars, each with its own stamp and its own buttons.

    The review decision in one, the payment in the other - the same question
    asked of the same person at the same moment. Split across two bands, three
    buttons read as two unrelated sets and cost a whole row of height on a page
    that is already long.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.fn = self.app.split("function renderSettleOnClaim(", 1)[1].split("\n}", 1)[0]

    def test_the_settlement_buttons_join_the_actions_row(self):
        self.assertIn('const bar = $("actions");', self.fn)
        self.assertNotIn('bar.className = "actions"', self.fn)

    def test_the_form_still_opens_below_where_it_has_room(self):
        self.assertIn("slot.appendChild(settleForm(c, ccy))", self.fn)
        # Rejecting is no longer offered here - see the class below.
        self.assertNotIn("rejectForm", self.fn)

    def test_the_stamp_does_not_repeat_what_the_row_already_says(self):
        # "Cleared —" was said twice once the two rows became one.
        self.assertNotIn("`Cleared — ${fmt(c.outstanding", self.fn)
        self.assertIn("owed to ${sub.who}", self.fn)


class TheSettlementIsOneAct(unittest.TestCase):
    """Five fields, then a group, then a sentence, then the button.

    Recording a payment is one act - the amount, how it was paid, the
    reference, the date, a note and which group it belongs to - and the thing
    that commits it was two bands below the fields with an explanation in
    between.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", self.app, re.S))
        self.form = self.app.split("function settleForm(", 1)[1].split(
            'td.querySelector("#sf-go")', 1)[0]

    def test_every_field_and_the_button_share_the_grid(self):
        grid = self.form.split('<div class="settle-grid">', 1)[1].split("</div>\n      <p", 1)[0]
        for field in ("sf-amt", "sf-mode", "sf-ref", "sf-date", "sf-note",
                      "sf-group-wrap", "sf-go"):
            self.assertIn(field, grid, f"{field} is not in the row")

    def test_the_explanation_sits_under_the_row_it_explains(self):
        self.assertIn('class="sf-hint sf-foot"', self.form)
        self.assertIn(".sf-foot {", self.css)

    def test_an_account_with_no_groups_leaves_no_hole(self):
        self.assertIn("wrap.hidden = true", self.app)

    def test_the_button_is_on_its_own_line_and_does_not_wrap(self):
        # It shared a 1fr track with the fields, so its label had to fit
        # whatever width was left - and "Record 6,632.00 as paid" wrapped onto
        # two lines and spilled out of the box. Still inside the grid, because
        # recording a payment is one act; just on its own line, where the
        # label can be as long as it needs.
        act = self.css.split(".settle-grid .sf-act {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-column:1 / -1", act)
        rule = self.css.split(".settle-grid .sf-act .btn {", 1)[1].split("}", 1)[0]
        self.assertIn("white-space:nowrap", rule)
        self.assertIn("width:auto", rule)


class TheRouteLineDoesNotGiveDirectionsYouFollowed(unittest.TestCase):
    """"See Pending settlement", read on a claim opened from there."""

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_the_claim_page_states_the_stage_without_the_directions(self):
        # Split on the statement's end, not the first semicolon - the ternary
        # chain contains several.
        block = self.app.split('$("detail-route").textContent', 1)[1].split(
            '"queued for human review";', 1)[0]
        self.assertIn('view === "claim" ? "released by the agent"', block)

    def test_the_queue_keeps_the_pointer(self):
        # There it is useful: you are looking at a list the claim is not in.
        block = self.app.split('$("detail-route").textContent', 1)[1].split(
            '"queued for human review";', 1)[0]
        self.assertIn("not in the queue, see Pending settlement", block)


class ASettledClaimIsNotGivenInstructions(unittest.TestCase):
    """"Reverse the payment before changing anything", beside disabled controls.

    Advice on something nobody can attempt, pointing at a step that does not
    exist in the product. The badge says SETTLED and the stamp says who settled
    it, which is the whole story.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_a_reimbursed_claim_is_told_why_it_is_shut(self):
        # It used to be told nothing at all, which reads as a fault rather
        # than as a closed door.
        block = self.app.split('$("controls-hint").textContent = paid', 1)[1] \
                        .split(";", 1)[0]
        self.assertIn("Reimbursed", block)
        self.assertIn("the money has moved", block)

    def test_the_sentence_is_gone(self):
        self.assertNotIn("reverse the payment before changing anything", self.app)


class TwoThingsCalledMoney(unittest.TestCase):
    """A converter and a formatter, sharing one name.

    `toClaim` defines a local `money(v)` that turns a decimal string into
    minor units. The file also has a global `money(amount, ccy)` that formats
    a number for display. Inside `toClaim` the local one wins; anywhere else
    the same call reaches the formatter and returns a string.

    It shipped twice. `rulesFromApi` read every cap as "1,500", and dividing
    that by a hundred put NaN against every expense type in the policy list.
    `budgetValue` did the same to converted foreign-currency amounts, where
    the failure is silent: a display string compared against a limit is
    simply never over it.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.script = "\n".join(re.findall(
            r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", self.app, re.S))

    def _toclaim_span(self):
        start = self.script.index("function toClaim(")
        # To the next top-level declaration, which is where the local shadow
        # stops applying.
        rest = self.script[start:]
        end = start + min(
            i for i in (rest.find("\nfunction "), rest.find("\nconst "))
            if i > 0)
        return start, end

    def test_the_single_argument_form_is_only_used_where_it_is_local(self):
        start, end = self._toclaim_span()
        offences = []
        for m in re.finditer(r"[^A-Za-z_.]money\(([^(),]+)\)", self.script):
            if start <= m.start() < end:
                continue
            line = self.script[:m.start()].count("\n") + 1
            offences.append(f"line {line}: {m.group(0).strip()}")
        self.assertEqual(offences, [],
                         "these reach the formatter, not the converter:\n"
                         + "\n".join(offences))

    def test_the_two_places_it_shipped_use_the_converter(self):
        rules = self.app.split("function rulesFromApi(", 1)[1].split("\n}", 1)[0]
        self.assertIn("toMinor(amount)", rules)
        budget = self.app.split("function budgetValue(", 1)[1].split("\n}", 1)[0]
        self.assertIn("toMinor(sub.budgetValue.amount)", budget)

    def test_the_converter_and_the_formatter_still_differ(self):
        # If they ever became the same function this test would pass for the
        # wrong reason.
        self.assertIn("const toMinor = (s) => Math.round(parseFloat(s) * 100);", self.app)
        self.assertIn("function money(amount, ccy)", self.app)


class AgentAutonomyWasNeverWired(unittest.TestCase):
    """Four checkboxes that existed only in one browser.

    Nothing in `lambda_src` ever read them, they were not part of the stored
    policy, and toggling one changed what that tab displayed until the next
    reload and nothing else - not a receipt, not a notification, not a queue.

    Which made the one that mattered the dangerous one. Ticking `rejected`
    looked like authorising the agent to refuse somebody's money with no human
    involved, and the console warned about exactly that. It did nothing at
    all, and a control that appears to carry that weight and carries none is
    worse than no control.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_the_section_is_gone(self):
        for gone in ("autonomy-block", 'id="autonomy"', "renderAutonomy",
                     "AUTONOMY_ORDER", "autoRelease"):
            self.assertNotIn(gone, self.app, f"{gone} is still here")

    def test_nothing_the_agent_does_refuses_somebody_money(self):
        released = self.app.split("const AUTO_RELEASED = new Set(", 1)[1].split(")", 1)[0]
        self.assertNotIn("rejected", released)
        self.assertNotIn("needs_review", released)

    def test_no_server_code_ever_read_it(self):
        # Which is why removing it changes no behaviour anywhere.
        for name in sorted(os.listdir(os.path.join(ROOT, "lambda_src"))):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(ROOT, "lambda_src", name), encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("auto_release", source, f"{name} reads an auto-release setting")


class TheSettlementListShowsBothDates(unittest.TestCase):
    """When it was sent in, and what the bill itself is dated.

    Finance settling a run needs the second one. A receipt dated March arriving
    in September is not the same claim as one from last night - it may be a
    genuine late submission, or it may be somebody clearing out a drawer at
    year end, and either way it is the row to stop on. The list had neither
    date, so the only way to find out was to open every claim.

    The column they replaced was "Open to settle", which pointed at something
    the reader was already standing on: the whole row has opened the claim
    since it was made clickable. A column spent restating an affordance is a
    column not spent on a fact.
    """

    def setUp(self):
        for name, path in (("app", "../PORTAL/app.html"), ("auth", "lambda_src/auth.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
                setattr(self, name, handle.read())
        self.row = self.app.split("// Only what is still owed.", 1)[1] \
                           .split("tb.appendChild(tr);", 1)[0]

    def test_the_date_on_the_bill_travels_from_the_model_to_the_table(self):
        # It was extracted and stored all along and simply never returned, so
        # the whole path has to be pinned or it silently goes quiet again.
        view = self.auth.split("def _submission_view(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"receipt_date": receipt.get("date", "")', view)
        self.assertIn("receiptDate: s.receipt_date || \"\"", self.app)
        self.assertIn("dayStamp(c.sub.receiptDate)", self.row)

    def test_both_dates_are_on_the_row_and_in_the_header(self):
        self.assertIn("dayStamp(c.sub.date)", self.row)
        self.assertIn("<th scope=\"col\">Submitted</th>", self.app)
        self.assertIn("<th scope=\"col\">Receipt date</th>", self.app)

    def test_the_two_dates_lead_the_row(self):
        # The comparison between them is the point, and it only reads at a
        # glance if the pair is where the eye lands rather than four columns in.
        head = self.app.split('<tbody id="paylist">', 1)[0].rsplit("<thead>", 1)[1]
        for later in ("Submitter", "Vendor", "Cleared by", "Group", "Approved"):
            self.assertLess(head.index("Receipt date"), head.index(later),
                            f"{later} comes before the dates")
        cells = self.row.split("tr.innerHTML =", 1)[1]
        self.assertLess(cells.index("dayStamp(c.sub.date)"), cells.index("c.sub.who"))
        self.assertLess(cells.index("billed ?"), cells.index("c.sub.who"))

    def test_an_illegible_date_says_so_rather_than_leaving_a_gap(self):
        # An empty cell in a table reads as a rendering fault. "not printed" is
        # a fact about the receipt, and the reviewer can act on it.
        self.assertIn("not printed", self.row)

    def test_the_year_appears_only_when_it_is_not_this_one(self):
        # Which is the whole point: a stale bill has to stand out from the rows
        # around it, and a year repeated down every row stands out from nothing.
        fn = self.app.split("function dayStamp(", 1)[1].split("\n}", 1)[0]
        self.assertIn("d.getFullYear() === new Date().getFullYear()", fn)
        self.assertIn('{ day:"numeric", month:"short" }', fn)
        self.assertIn('{ day:"numeric", month:"short", year:"numeric" }', fn)

    def test_an_absent_or_unparseable_date_renders_nothing_not_the_word_invalid(self):
        fn = self.app.split("function dayStamp(", 1)[1].split("\n}", 1)[0]
        self.assertIn('if (!iso) return "";', fn)
        self.assertIn('if (isNaN(d)) return "";', fn)

    def test_the_cue_column_and_its_styling_are_both_gone(self):
        for gone in ("opencue", "payacts", "Open to settle"):
            self.assertNotIn(gone, self.app, f"{gone} survived")


class ALineIsADescriptionAndAnAmount(unittest.TestCase):
    """Line categories are gone from both halves, and stay gone.

    They were a vocabulary configured per expense type, with four universal
    ones beneath, chosen by the model on every line - a second taxonomy under
    the one that had already been chosen, and no category ever moved a rupee.
    The console carries its own port of the policy engine, so a removal that
    lands in only one half leaves the two disagreeing about what a claim is.
    """

    def setUp(self):
        for name, path in (("app", "../PORTAL/app.html"), ("policy", "lambda_src/policy.py"),
                           ("handler", "lambda_src/handler.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
                setattr(self, name, handle.read())

    def test_no_half_still_carries_the_vocabulary(self):
        for name in ("ALL_CATEGORIES", "KNOWN_CATEGORIES", "UNIVERSAL_CATEGORIES",
                     "all_category_ids", "category_labels", "categoriesFor",
                     "categoryOptions", "categoryLabels"):
            self.assertNotIn(name, self.app, f"the console still has {name}")
            self.assertNotIn(name, self.policy, f"the engine still has {name}")
            self.assertNotIn(name, self.handler, f"the handler still has {name}")

    def test_the_editor_that_set_them_is_gone_with_them(self):
        for stray in ("Add category", "Always available:", "t.categories.splice"):
            self.assertNotIn(stray, self.app)

    def test_the_model_is_steered_by_the_type_descriptions_instead(self):
        # The enum names the types; on its own it says nothing about what
        # belongs under each one. The description is the part that steers, and
        # it is the organisation's own words rather than a fixed vocabulary.
        fn = self.handler.split("def _type_guidance(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('t.get("hint")', fn)
        self.assertIn("not_covered", fn)
        schema = self.handler.split("def _receipt_schema(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('schema["properties"]["expense_type"]["description"]', schema)

    def test_a_line_the_console_renders_has_two_columns_of_substance(self):
        items = self.app.split('const tbody = $("items");', 1)[1] \
                        .split('const vbox = $("violations")', 1)[0]
        self.assertNotIn("category", items)

    def test_the_claim_table_is_down_to_what_a_line_is(self):
        # Two columns. "Claim" read "claimable" on every line of every receipt
        # once a cap became the only deduction the engine makes, and "Category"
        # was a vocabulary that decided no money at all.
        items = self.app.split("const tbody = $(\"items\");", 1)[1] \
                        .split("const vbox = $(\"violations\")", 1)[0]
        self.assertNotIn("claimable", items)
        self.assertNotIn("td3", items)
        self.assertIn("tr.append(td1,td2)", items)
        import re
        header = self.app.split('<tbody id="items">', 1)[0].rsplit("<thead", 1)[1]
        # The cells themselves, not the comment above them explaining what was
        # removed - which names the column it removed.
        self.assertEqual(["Line item", "Amount"],
                         re.findall(r"<th[^>]*>([^<]+)</th>", header))

    def test_the_instruction_to_fix_a_block_appears_only_when_something_blocks(self):
        # It was printed beside these controls on every open claim, including
        # ones whose own badge two lines above read APPROVED.
        # To the end of the statement, not to the first ";" - one of the
        # sentences it can print contains a semicolon of its own.
        hint = self.app.split('$("controls-hint").textContent', 1)[1] \
                       .split('\n    : "";', 1)[0]
        self.assertIn("blocking && !frozen", hint)
        self.assertIn("const blocking = res.violations.some(v => v.blocking);", self.app)


class NamingAThingAndPricingItAreTwoScreens(unittest.TestCase):
    """Expense types moved to Organisation; the rules that judge them stayed.

    One panel used to carry both: a rename, an enable toggle and a delete sat
    directly above a cap amount and a headcount requirement, with nothing to
    say which of them moved money. They are different questions asked at
    different moments - naming a cost line is housekeeping, setting a cap is a
    decision about what the company pays - and one of the two is the reason
    this product exists.

    They are still one stored document, so the split must not reach the save:
    a rename made on one tab and a cap changed on the other go up together,
    under one version, as one line in the change history.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.types = self.app.split("function renderTypeDetail(", 1)[1] \
                             .split("\nfunction ", 1)[0]
        self.rule = self.app.split("function renderRuleDetail(", 1)[1] \
                            .split("\nfunction ", 1)[0]

    def test_what_a_type_is_lives_under_organisation(self):
        self.assertIn('id="osec-types"', self.app)
        for owned in ('title.className = "title"', "Description",
                      "Delete type", "Expense type name"):
            self.assertIn(owned, self.types, f"the type editor lost {owned}")

    def test_the_description_is_sized_for_what_it_now_carries(self):
        # It was a one-line input holding a caption. It is the only steering
        # the agent gets since line categories went, so a reviewer has to be
        # able to see what they are writing.
        self.assertIn('document.createElement("textarea")', self.types)
        self.assertIn("hi.maxLength = 600", self.types)
        self.assertIn("MAX_HINT = 600", open(
            os.path.join(ROOT, "lambda_src", "policy.py"), encoding="utf-8").read())

    def test_the_organisation_s_own_currency_leads_and_cannot_be_removed(self):
        # A capped type with no cap in the currency its receipts arrive in
        # raises `unsupported_currency` on every one of them, so the row that
        # matters sat in a list of optional ones with the same "remove" beside
        # it - and opened on whichever currency happened to be stored first.
        cap = self.rule.split("// ---- cap ---", 1)[1]
        self.assertIn("const home = orgCurrency();", cap)
        self.assertIn(
            "const ordered = [home, ...Object.keys(t.caps).filter(c => c !== home).sort()];",
            cap)
        self.assertIn('if (!ro && ccy !== home) {', cap)
        # And it is not offered again by Add currency, which would be a second
        # row for a currency that already has a pinned one.
        self.assertIn("capCurrencies().filter(c => c !== home && !(c in t.caps))", cap)

    def test_a_missing_home_cap_is_shown_empty_and_named(self):
        # Never 0: a cap of nothing rejects every claim, which is the opposite
        # of what an unset field means.
        cap = self.rule.split("// ---- cap ---", 1)[1]
        self.assertIn('inp.value = minor === undefined ? "" :', cap)
        self.assertIn("cap-missing", cap)

    def test_a_new_type_is_capped_in_the_organisation_s_currency(self):
        self.assertIn("caps: { [orgCurrency()]: 1000000 }", self.app)
        self.assertNotIn("caps: { INR: 1000000 }", self.app)

    def test_neither_screen_carries_the_other_s_controls(self):
        for stray in ("capped per head", "Add currency", "requiresItemisation"):
            self.assertNotIn(stray, self.types, f"a cap control is still on the type editor")
        for stray in ("Delete type", "Description", 'title.className = "title"'):
            self.assertNotIn(stray, self.rule, f"{stray} is still on the rule editor")

    def test_the_rule_screen_says_where_the_rest_went(self):
        # A control that has moved and says nothing reads as a control that
        # was removed.
        self.assertIn("Organisation", self.rule)
        self.assertIn("Expense types", self.rule)

    def test_the_name_is_shown_there_but_not_editable(self):
        # A disabled input reads as broken; this is a heading.
        self.assertIn('name.className = "rd-name"', self.rule)
        self.assertNotIn("addEventListener", self.rule.split("rd-name", 1)[1].split("box.appendChild(head)", 1)[0])

    def test_one_rule_set_one_save_from_either_screen(self):
        self.assertIn('$("rules-save").addEventListener("click", () => saveRules("rules-save", "rules-msg"));',
                      self.app)
        self.assertIn('$("et-save").addEventListener("click", () => saveRules("et-save", "et-msg"));',
                      self.app)
        # One request, carrying the whole document however it was edited.
        save = self.app.split("async function saveRules(", 1)[1].split("\n}", 1)[0]
        self.assertIn('authCall("/org/rules"', save)
        self.assertIn("rules: rulesToApi()", save)

    def test_an_unsaved_edit_lights_up_both_bars(self):
        # Either screen can be holding it, and a Save that only lights up on
        # the tab you are not on is a Save nobody presses.
        paint = self.app.split("function paintRulesState(", 1)[1].split("\n}", 1)[0]
        self.assertIn('["rules-msg", "rules-actions"], ["et-msg", "et-actions"]', paint)

    def test_creating_a_type_happens_where_types_are_named(self):
        self.assertIn('id="et-new"', self.app)
        rules_view = self.app.split('id="view-rules"', 1)[1].split("STAFF", 1)[0]
        self.assertNotIn('id="rt-new"', rules_view)
        # And the new type opens in the editor that made it.
        new = self.app.split('$("et-new").addEventListener', 1)[1].split("\n});", 1)[0]
        self.assertIn('querySelector("#et-detail input.title")', new)

    def test_both_lists_are_the_same_list_with_different_captions(self):
        # The rule tab summarises the cap because that is what it sets; the
        # Organisation tab counts categories because that is what it names.
        self.assertIn('renderTypeIndex("et-index"', self.app)
        self.assertIn('renderTypeIndex("rules-index"', self.app)
        self.assertIn("capSummary(t)", self.app)


class AGroupCountsItsPeopleAndItsReceipts(unittest.TestCase):
    """Two counts, because a group can have plenty of one and none of the other.

    "12 tagged" beside a group named Mobil80 Developers reads as twelve
    developers. It was twelve receipts. The unit was the only thing that could
    have said which, and there wasn't one.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.row = self.app.split("P.groups.forEach((g, i) => {", 1)[1] \
                           .split("gl.appendChild(row)", 1)[0]

    def test_each_count_names_its_unit(self):
        for said in ('" expense"', '" expenses"', '" person"', '" people"'):
            self.assertIn(said, self.row)

    def test_the_empty_states_say_which_is_empty(self):
        self.assertIn('"no expenses yet"', self.row)
        self.assertIn('"nobody yet"', self.row)

    def test_the_default_group_holds_everybody(self):
        # Nobody is put in it; being in the organisation is what puts you
        # there, so counting explicit memberships would report nil for it.
        self.assertIn("g.default", self.row)
        self.assertIn('PEOPLE.filter(p => p.status !== "removed").length', self.row)

    def test_a_removed_person_is_not_counted(self):
        self.assertIn('p.status !== "removed"', self.row)

    def test_the_row_has_a_column_for_each(self):
        grid = self.app.split(".grprow { display:grid; grid-template-columns:", 1)[1] \
                       .split(";", 1)[0]
        self.assertEqual(4, len(grid.split()), grid)


class ASettledClaimIsDeadInEveryControl(unittest.TestCase):
    """The money has moved, so nothing on the claim may still be set.

    The freeze was computed correctly and then undone. Two hundred lines below
    the decision, a second pass wrote `disabled = unread` over the same four
    controls - an assignment, not a narrowing - so on anything the agent had
    finished reading it put `false` back, and a claim settled in full offered
    an editable Expense type and Currency again.

    The per-line category select had a different version of the same fault: it
    read `locked`, which is an in-session decision, and a claim settled on a
    previous visit carries none.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.detail = self.app.split("function renderDetail(", 1)[1] \
                              .split("\nfunction ", 1)[0]

    def test_every_reason_sits_in_one_expression(self):
        # Plus `rechecking`: while the agent has the claim back, the type,
        # the currency and the group are about to be re-judged, so they are
        # not editable either.
        self.assertIn("const busy = paid || unread || saving || deciding;",
                      self.detail)
        self.assertIn("const frozen = busy || !reviewingHere();", self.detail)
        # The expense type classifies rather than prices, so finance holds it
        # too - but every reason a control is dead is still in `busy`.
        self.assertIn("const classFrozen = busy || !(reviewingHere() || settlingHere());",
                      self.detail)

    def test_the_four_controls_are_assigned_exactly_once(self):
        # The regression was a second assignment, so what is pinned is that
        # there is no second one - not the wording of any particular guard.
        self.assertEqual(1, self.detail.count(
            '["headcount", "src", "ccy", "claim-group"].forEach'),
            "a second pass assigns to these controls again")
        self.assertEqual(1, self.detail.count('["etype"].forEach'),
                         "a second pass assigns to the expense type again")
        self.assertEqual(1, self.detail.count("if (el) el.disabled = frozen;"))
        self.assertEqual(1, self.detail.count("if (el) el.disabled = classFrozen;"))
        self.assertNotIn("el.disabled = unread", self.detail)

    def test_nothing_on_a_settled_claim_reads_as_editable(self):
        # A per-line select used to sit here, disabled on `locked` - which is
        # false on a settled claim, because a settled claim carries no
        # in-session decision - so every line stayed editable under a claim
        # whose money had already moved. There is no per-line control now;
        # what remains must still take `frozen` and never `locked`.
        self.assertNotIn("sel.disabled = locked;", self.detail)
        self.assertNotIn("= locked;", self.detail.split("const tbody", 1)[-1]
                                                 .split("const vbox", 1)[0])

    def test_being_settled_is_one_of_the_reasons(self):
        self.assertIn("const closed = !!settlementStage(sub);", self.detail)


class NamingAndPricingStaySplitButReachable(unittest.TestCase):
    """The rule screen names where a type is renamed, and gets you there.

    The split is deliberate: what a cost line is called is housekeeping, what
    it may cost is a decision about money. What was not deliberate is that the
    sentence saying so was inert - a different top-level tab, a sub-tab inside
    it, and the same type to find again once you arrived. Naming a place with
    no way to reach it is most of why somebody concludes a thing cannot be
    edited at all.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as h:
            self.app = h.read()
        self.rule = self.app.split("function renderRuleDetail(", 1)[1] \
                             .split("\nfunction ", 1)[0]

    def test_the_pointer_is_something_you_can_press(self):
        self.assertIn('go.className = "linkish"', self.rule)
        self.assertIn("Organisation \\u203a Expense types", self.rule)

    def test_it_lands_on_the_right_sub_tab(self):
        self.assertIn('orgSection = "types";', self.rule)
        self.assertIn('goTo("org")', self.rule)

    def test_and_on_the_type_that_was_being_read(self):
        self.assertIn("selectedTypeId = t.id;", self.rule)

    def test_the_editing_controls_are_still_only_on_the_one_screen(self):
        # The walk got shorter; the split did not move.
        types = self.app.split("function renderTypeDetail(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn('document.createElement("textarea")', types)
        self.assertIn('title.className = "title"', types)
        self.assertNotIn('document.createElement("textarea")', self.rule)


class ASubmittersOwnListIsARecordNotAToDo(unittest.TestCase):
    """The status badges are grey on My expenses and coloured everywhere else.

    Amber means money waiting to go out and red means somebody has to look -
    on the screens of the people who can do either. A submitter can act on
    none of it: their list is a record of what happened to receipts they have
    already sent. Colour there reads as a call to do something, and red on a
    rejected claim reads as an accusation rather than an outcome.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "..", "PORTAL", "app.html"), encoding="utf-8") as h:
            self.app = h.read()
        self.css = self.app.split("<style>", 2)[2].split("</style>", 1)[0]

    def test_every_badge_there_is_neutral(self):
        rule = self.css.split("#mine .stage {", 1)[1].split("}", 1)[0]
        self.assertIn("var(--surface-2)", rule)
        self.assertIn("var(--muted)", rule)

    def test_it_is_scoped_so_the_reviewer_s_screens_keep_their_colour(self):
        # The queue and the settlement list use the same classes, and there
        # the colour is an instruction to somebody who can follow it.
        self.assertIn(".s-rejected { background:var(--bad-bg); color:var(--bad); }",
                      self.css)
        self.assertIn(".s-settled { background:var(--ok-bg); color:var(--ok); }",
                      self.css)

    def test_the_states_stay_apart_without_hue(self):
        # A border on the ones that ended, none on the ones still moving - so
        # the list is still scannable when every badge is the same grey.
        rule = self.css.split("#mine .s-settled", 1)[1].split("}", 1)[0]
        self.assertIn("border:1px solid", rule)
        self.assertIn("#mine .s-rejected", self.css)

    def test_and_the_badge_still_says_which_in_words(self):
        labels = self.app.split("const STAGE_LABEL = {", 1)[1].split("};", 1)[0]
        for word in ("Settled", "Rejected", "In review", "Approved"):
            self.assertIn(word, labels)


class ABadgeLooksTheSameWhereverItSits(unittest.TestCase):
    """A count on a sub-tab and a count on a top-level tab are one object.

    `.nav button` is a flex row that centres what it holds. `.subtabs button`
    was not, so the same `.navflag` span sat on the text baseline there -
    dropped against the label beside it, and visibly a different component from
    the identical badge one row above.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def _rule(self, selector):
        return self.app.split(selector + " {", 1)[1].split("}", 1)[0]

    def test_both_tab_rows_centre_what_they_hold(self):
        for selector in (".nav button", ".subtabs button"):
            rule = self._rule(selector)
            self.assertIn("display:inline-flex", rule, selector)
            self.assertIn("align-items:center", rule, selector)

    def test_both_space_the_badge_the_same_way(self):
        self.assertIn("gap:5px", self._rule(".nav button"))
        self.assertIn("gap:5px", self._rule(".subtabs button"))

    def test_nothing_adds_a_second_gap_of_its_own(self):
        # A margin on the badge stacks on top of the flex gap rather than
        # replacing it.
        self.assertIn(".scopekinds .navflag { margin-left:0; }", self.app)

    def test_the_sub_tabs_that_carry_one_use_the_shared_class(self):
        self.assertEqual(2, self.app.count('flag.className = "navflag count"'))


class TheRuleHeadingSitsFlushWithItsPanel(unittest.TestCase):
    """A 9px inset that belonged to a control that is no longer there.

    `.rd-name` and `.tid` are indented to sit under the text inside the rename
    input on the Organisation tab, which carries its own padding. The Policy
    rules panel shows the name as a heading with no box around it, so the same
    inset pushed the title right of the description and the CAP label below it.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_the_rule_panel_marks_its_heading_as_boxless(self):
        rule = self.app.split("function renderRuleDetail(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn('head.className = "rd-head rd-head-plain"', rule)

    def test_the_editable_one_keeps_its_inset(self):
        # It is measuring against a real input there, so the inset is correct.
        types = self.app.split("function renderTypeDetail(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn('head.className = "rd-head";', types)

    def test_the_boxless_heading_and_its_id_are_both_flush(self):
        self.assertIn(".rd-head-plain .rd-name, .rd-head-plain .tid { padding-left:0; }",
                      self.app)


class TheTitleIsAlignedByItsTextNotItsBox(unittest.TestCase):
    """An inline-editable heading has an invisible box around it.

    The rename field carries 1px of transparent border and 8px of padding, so
    its *text* starts 9px right of where the element does. Left alone, the
    title stood 9px right of DESCRIPTION, CATEGORIES, the body copy and the
    left edge of every field under it - aligned to a box nobody can see, and
    out of line with everything a reader can.

    Measured in a browser at 1500px: with the pull applied, the title text, the
    type id, both section labels, the description paragraph, the "Always
    available" line and the left edge of both bordered inputs all sit on one
    edge.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def _rule(self, selector):
        return self.app.split(selector + " {", 1)[1].split("}", 1)[0]

    def test_the_field_is_pulled_left_by_exactly_its_own_inset(self):
        rule = self._rule(".rd-head input.title")
        self.assertIn("padding:5px 8px", rule)
        self.assertIn("border:1px solid transparent", rule)
        # 8px of padding plus 1px of border is the whole of the offset.
        self.assertIn("margin-left:-9px", rule)

    def test_the_id_beneath_it_is_flush_too(self):
        # It used to carry a matching 9px so it sat under the title's text.
        # The title's text is now at the edge, so the match is zero.
        self.assertIn("padding-left:0", self._rule(".rd-head .tid"))

    def test_the_heading_that_has_no_box_needs_no_pull(self):
        rule = self._rule(".rd-name")
        self.assertIn("padding:6px 0 5px", rule)
        self.assertNotIn("margin-left", rule)

    def test_the_box_still_overhangs_so_hover_has_something_to_sit_in(self):
        # The pull moves the field, not its padding: the border that appears on
        # hover and focus still has room around the text.
        self.assertIn(".rd-head input.title:hover:not(:disabled) { border-color:var(--rule-firm); }",
                      self.app)
        self.assertIn(".rd-head input.title:focus { border-color:var(--ink);", self.app)


class TwoAdministratorsCannotDeleteEachOthersGroups(unittest.TestCase):
    """Adding a group sends the whole list, so a save is a replace.

    A replace driven by a copy a browser loaded some minutes ago deletes
    whatever anybody else added in between. Two people administering an
    organisation at once is not an edge case - it is the first week of every
    account, and it is exactly what happened: one owner added groups, a second
    was promoted and added one more, and a list went back over the top.

    The list now carries a revision. A write against a stale one is refused and
    the current list comes back with the refusal, so the reader ends up looking
    at what is stored rather than at a copy of something that no longer exists.
    """

    def setUp(self):
        for name, path in (("app", "../PORTAL/app.html"), ("auth", "lambda_src/auth.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
                setattr(self, name, handle.read())
        self.put = self.auth.split("def _groups_put(", 1)[1].split("\ndef ", 1)[0]

    def test_the_revision_travels_both_ways(self):
        view = self.auth.split("def _org_get(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"groups_rev": int(org.get("groups_rev") or 0)', view)
        self.assertIn("groups_rev: o.groups_rev || 0", self.app)
        self.assertIn("rev: ORG_PROFILE.groups_rev", self.app)

    def test_a_stale_save_is_refused_not_applied(self):
        self.assertIn("if sent_rev is not None and int(sent_rev) != current_rev:", self.put)
        self.assertIn("409", self.put)

    def test_the_refusal_carries_the_list_that_is_actually_stored(self):
        # A 409 that says only "no" leaves the browser showing a list nobody
        # has, which is the same defect one step further along.
        refusal = self.put.split("if sent_rev is not None", 1)[1].split("# The default", 1)[0]
        self.assertIn('"groups": org.get("groups", [])', refusal)
        self.assertIn('"groups_rev": current_rev', refusal)
        self.assertIn("if (data && data.groups) ORG_PROFILE.groups = data.groups;", self.app)

    def test_the_write_is_conditional_as_well(self):
        # The revision check closes the window a person opens by leaving a tab
        # sitting; the condition closes the milliseconds inside one request.
        self.assertIn("ConditionExpression=\"attribute_not_exists(groups_rev) OR groups_rev = :cur\"",
                      self.put)
        self.assertIn("ConditionalCheckFailedException", self.put)

    def test_the_revision_moves_on_every_write(self):
        self.assertIn("SET #g = :g, groups_rev = :next", self.put)
        self.assertIn('":next": current_rev + 1', self.put)


class TheGroupsPanelSaysWhatHappened(unittest.TestCase):
    """It had nothing that could report anything at all.

    Adding a name that already existed hit a bare `return`. A save the server
    refused was thrown away unread. Both look identical from the chair: you
    type a name, press Add, and nothing happens.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.add = self.app.split('$("grp-add").addEventListener', 1)[1].split("\n});", 1)[0]

    def test_the_panel_has_somewhere_to_say_it(self):
        panel = self.app.split('<h2>Groups</h2>', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="grp-msg"', panel)

    def test_a_name_that_already_exists_is_named_back(self):
        # And named by the label that is actually in the list, since the clash
        # is on the derived id - "Yandle" against an existing "yandle".
        self.assertIn("is already a group", self.add)
        self.assertIn("ORG_PROFILE.groups.find(g => g.id === id)", self.add)

    def test_an_empty_name_says_so_rather_than_just_moving_the_cursor(self):
        self.assertIn("Give the group a name first.", self.add)

    def test_a_refused_save_reaches_the_reader(self):
        save = self.app.split("async function saveGroups(", 1)[1].split("\n}", 1)[0]
        self.assertIn('if (!ok) grpMsg(data && data.error || "Could not save the groups.", false);',
                      save)

    def test_the_field_is_only_cleared_once_the_save_worked(self):
        # Clearing it first loses what they typed when the save is refused.
        self.assertLess(self.add.index("await saveGroups()"),
                        self.add.index('$("grp-new").value = ""'))

    def test_removing_one_reports_too_and_says_what_it_does_not_undo(self):
        block = self.app.split('del.textContent = "Remove"', 1)[1].split("act.appendChild(del)", 1)[0]
        self.assertIn("await saveGroups()", block)
        self.assertIn("Claims already tagged with it keep the tag", block)


class AnInvitationSaysWhichTeamTheyJoin(unittest.TestCase):
    """Everyone used to land in the default group and be moved afterwards.

    The move was a second job nobody remembered, so spend sat under the wrong
    cost centre until a report looked wrong. The moment somebody knows the
    answer is the moment they are adding a named person to a named team, so
    that is where it is asked. Changeable under Manage.
    """

    def setUp(self):
        for name, path in (("app", "../PORTAL/app.html"), ("auth", "lambda_src/auth.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
                setattr(self, name, handle.read())
        self.invite = self.auth.split("def _invite(", 1)[1].split("\ndef ", 1)[0]

    def test_the_form_asks_for_it(self):
        row = self.app.split('id="people-add"', 1)[1].split("</div>\n      <div", 1)[0]
        self.assertIn('id="p-group"', row)
        self.assertIn('for="p-group"', row)

    def test_the_choice_is_sent(self):
        self.assertIn('group: $("p-group").value', self.app)

    def test_the_server_refuses_a_group_it_does_not_have(self):
        # An id that does not exist would tag their receipts with nothing and
        # put them in the unattributed bucket the default group exists to
        # prevent - so an unknown one falls back rather than being stored.
        self.assertIn('known = {g["id"] for g in all_groups}', self.invite)
        self.assertIn("chosen = wanted if wanted in known else fallback", self.invite)

    def test_an_invitation_with_no_group_still_lands_somewhere(self):
        # The console asks outright; this is the floor under that, for any
        # caller that does not. The default if one is flagged, otherwise the
        # first - a list rebuilt without the flag still has somewhere to put
        # people.
        self.assertIn('fallback = next((g["id"] for g in all_groups if g.get("default")),',
                      self.invite)
        self.assertIn('all_groups[0]["id"] if all_groups else None)', self.invite)

    def test_the_console_will_not_send_one_without_a_group(self):
        add = self.app.split('$("p-add").addEventListener', 1)[1].split("\n});", 1)[0]
        self.assertIn('if (!$("p-group").value) {', add)
        self.assertIn("Choose the group their receipts belong to.", add)
        # And the offending control is marked, like every other field here.
        self.assertIn('["p-name", "p-staff", "p-email", "p-group"]', add)

    def test_the_dropdown_is_editable_whenever_there_is_anything_to_pick(self):
        # It was dead below two groups, which on screen is indistinguishable
        # from the control being broken.
        block = self.app.split('const gsel = $("p-group");', 1)[1].split('const tb = $("people")', 1)[0]
        self.assertIn("gsel.disabled = !groups.length;", block)
        self.assertIn("No groups yet", block)

    def test_the_choices_are_rebuilt_on_every_render(self):
        # Groups are edited on another tab and by other people. A list built
        # once at load offers one somebody has since deleted and omits the one
        # they added a minute ago.
        block = self.app.split('const gsel = $("p-group");', 1)[1].split("const tb = $(\"people\")", 1)[0]
        self.assertIn("ORG_PROFILE.groups || []", block)
        self.assertIn("gsel.textContent = \"\";", block)

    def test_a_choice_already_made_survives_a_rerender(self):
        block = self.app.split('const gsel = $("p-group");', 1)[1].split("const tb = $(\"people\")", 1)[0]
        self.assertIn("const chosen = gsel.value;", block)
        self.assertIn("groups.some(g => g.id === chosen) ? chosen", block)

    def test_it_opens_on_the_default_group(self):
        block = self.app.split('const gsel = $("p-group");', 1)[1].split("const tb = $(\"people\")", 1)[0]
        self.assertIn("groups.find(g => g.default)", block)
        self.assertIn('g.default ? " (default)" : ""', block)

    def test_the_confirmation_names_the_group(self):
        self.assertIn("${intoGroup}", self.app)
        self.assertIn("an owner can change the role or the ", self.app)


class TheInviteRowIsOneLine(unittest.TestCase):
    """Six controls that each fell through to a different generic rule.

    The text inputs came out 27px tall, the email input 22, the selects 29 and
    the button 35. The row is bottom-aligned, so four heights put the labels on
    three different lines - which is why this row has always read as slightly
    up and down. Measured in a browser at 1500px after the fix: one height, one
    top, one line of labels.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_every_control_is_given_the_same_height(self):
        rule = self.app.split(".people-add input, .people-add select {", 1)[1].split("}", 1)[0]
        self.assertIn("height:34px", rule)
        # Without this the border and padding are added to the height and the
        # inputs come out taller than the selects again.
        self.assertIn("box-sizing:border-box", rule)
        self.assertIn(".people-add .btn { height:34px; }", self.app)

    def test_a_long_group_name_cannot_push_the_button_off_the_row(self):
        self.assertIn("#p-group { max-width:14rem; }", self.app)


class NoResponseCanBeKilledByADynamoNumber(unittest.TestCase):
    """Every number out of DynamoDB is a Decimal, and `json.dumps` refuses one.

    Each endpoint wrapped its own numbers in `int()` on the way out, which
    works until a field is added that nobody wrapped - and then the *whole*
    response raises rather than that one value being wrong. `/org` returns the
    stored policy wholesale, so the first time an organisation saved a policy
    its `version` came back a Decimal and the endpoint stopped answering
    entirely: the console loaded with no groups, no credits and no rules, and
    nothing on the screen said why.

    Closed at the boundary rather than one field at a time, because the next
    field nobody wraps is the same outage again.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as handle:
            self.auth = handle.read()

    def test_every_reply_goes_through_the_encoder(self):
        reply = self.auth.split("def _reply(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("json.dumps(body, default=_jsonable)", reply)

    def test_a_whole_number_stays_a_whole_number(self):
        fn = self.auth.split("def _jsonable(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("int(value) if value == value.to_integral_value() else float(value)", fn)

    def test_it_still_refuses_what_is_genuinely_unserialisable(self):
        # A blanket `str(value)` would turn a bug into a wrong value shipped to
        # the browser, which is worse than a 500 nobody can miss.
        fn = self.auth.split("def _jsonable(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("raise TypeError", fn)

    def test_the_field_that_caused_it_is_covered(self):
        # `/org` hands back the stored rule set as it came out of the table.
        view = self.auth.split("def _org_get(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"rules": policy.rules_for(org)', view)
        self.assertIn('"rules_changelog"', view)


class NoRuleIsWrittenTwice(unittest.TestCase):
    """A hundred and forty lines of CSS existed twice, and the later copy won.

    Two of the rules in the duplicate were the *older* versions of ones changed
    deliberately since, so the paste silently undid both fixes: `.settle-grid`
    went back to `align-items:end`, which dropped every field without a hint
    under it to the bottom of its row, and `.settle .sf input` lost the
    `height:30px` that keeps the controls matching each other.

    Neither was visible from the source. The intended rule was right there,
    forty lines above, doing nothing - and the only way to see it was to read
    the computed style in a browser.

    So: no selector is written twice. A second rule for the same selector is
    either dead weight or a silent override of the first, and both are worth
    failing over.
    """

    # Selectors a stylesheet legitimately states more than once, because the
    # second is narrowing the first rather than repeating it. Empty today, and
    # anything added here should say why.
    ALLOWED: set = set()

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.css = max(re.findall(r"<style[^>]*>(.*?)</style>", self.app, re.S), key=len)

    def _duplicates(self):
        # A rule inside `@media` is narrowing the base rule for one width, not
        # repeating it, so the media blocks come out before anything is counted.
        base = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", self.css)
        seen = {}
        for m in re.finditer(r"^\s*([.#][A-Za-z][^{\n]*?)\s*\{([^}]*)\}", base, re.M):
            seen.setdefault(m.group(1).strip(), []).append(" ".join(m.group(2).split()))
        return {k: v for k, v in seen.items()
                if len(v) > 1 and k not in self.ALLOWED}

    def test_no_selector_is_defined_more_than_once(self):
        dups = self._duplicates()
        self.assertEqual({}, dups,
                         "these selectors are written twice; the later one wins:\n"
                         + "\n".join(f"  {k}: {v}" for k, v in sorted(dups.items())))

    def test_the_two_rules_the_duplicate_was_overriding_are_the_live_ones(self):
        grid = self.css.split(".settle-grid {", 1)[1].split("}", 1)[0]
        self.assertIn("align-items:start", grid)
        self.assertNotIn("align-items:end", grid)
        controls = self.css.split(".settle .sf input, .settle .sf select {", 1)[1].split("}", 1)[0]
        self.assertIn("height:30px", controls)


class AClaimSomebodyPulledBackSaysSo(unittest.TestCase):
    """It sat in the review queue wearing the badge APPROVED.

    The engine still computes "approved" - sending a claim back does not change
    the arithmetic - but the claim's state is that a named person took it out
    of the payment run and it is undecided again. Printing the agent's verdict
    at the top of a claim in the review queue tells the reviewer the opposite
    of why it is in front of them.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_the_claim_reads_as_sent_back(self):
        # The badge that said so is gone; the route line under the heading
        # still names who took it out of the payment run.
        self.assertIn("const sentBackNow = sub.pulledBack && !decision && !stage;", self.app)
        self.assertIn("sent back by", self.app)

    def test_settlement_still_outranks_it(self):
        # Money having moved is the more important fact, and a settled claim
        # cannot be in the queue anyway.
        self.assertIn("!stage;", self.app.split("const sentBackNow =", 1)[1].split("\n", 1)[0])

    def test_the_stamp_names_who_sent_it_back(self):
        self.assertIn("`sent back by ${sub.pulledBy || \"finance\"}`", self.app)

    def test_it_is_in_the_queue_in_the_first_place(self):
        # The routing was already right - this class is only about what it says
        # once it is there.
        fn = self.app.split("const isQueued = (sub) => {", 1)[1].split("\n};", 1)[0]
        self.assertIn("!agentMayRelease(sub)", fn)
        # `pulledBack` is one of the three things that predicate weighs.
        pred = self.app.split("const agentMayRelease = ", 1)[1].split(";\n", 1)[0]
        self.assertIn("sub.pulledBack", pred)


class RefreshReloadsWhatTheReaderIsLookingAt(unittest.TestCase):
    """The refresh button re-read `/me` and not one receipt.

    `refreshNow` called `loadChannels` alone - the WhatsApp number and the
    intake address - while `loadRecords` is the one that fetches
    `/submissions`. So pressing refresh refreshed nothing anybody presses it
    for, and neither did the automatic reload on returning to the tab.

    It bites hardest on the channels this product is built around: a receipt
    sent by WhatsApp or email arrives with no browser involved, so an open
    console had no way at all to learn about it. A claim sat audited in the
    table while the person who sent it and the finance executive waiting for it
    both watched a page that would only tell them after a full reload.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.fn = self.app.split("async function refreshNow(", 1)[1].split("\n}", 1)[0]

    def test_it_reloads_the_receipts(self):
        self.assertIn("loadRecords()", self.fn)

    def test_it_still_reloads_the_channels(self):
        self.assertIn("loadChannels()", self.fn)

    def test_the_button_and_the_return_to_tab_both_use_it(self):
        self.assertIn('$("refresh").addEventListener("click", refreshNow);', self.app)
        self.assertIn("&& Date.now() - loadedAt > 60 * 1000) refreshNow();", self.app)

    def test_it_cannot_run_twice_at_once(self):
        self.assertIn("if (refreshing || !sessionToken()) return;", self.fn)


class AClaimsGroupIsAFactAboutTheClaim(unittest.TestCase):
    """It was re-derived from the submitter's membership, every render.

    The console never read `group_id` off a claim - it did not carry it - and
    instead worked it out afresh from whichever groups that person is in right
    now. Wrong in two directions at once.

    It ignored an answer already given. Somebody in more than one group was
    reported as "awaiting their reply" for ever, including after they had
    tapped a group on WhatsApp and the receipt had been tagged with it minutes
    before. Finance read "unresolved" on a claim that was resolved.

    And it rewrote history. Add a person to a second group and every receipt
    they had ever sent stopped being attributed: months of settled spend moving
    out of a cost centre because a list was edited today. The reports group on
    this, so the figures moved with it.
    """

    def setUp(self):
        for name, path in (("app", "../PORTAL/app.html"), ("auth", "lambda_src/auth.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
                setattr(self, name, handle.read())
        self.fn = self.app.split("function groupOf(sub) {", 1)[1].split("\n}", 1)[0]

    def test_the_claim_carries_its_own_group(self):
        view = self.auth.split("def _submission_view(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"group_id": row.get("group_id", "")', view)
        self.assertIn('"group_status": row.get("group_status", "")', view)
        self.assertIn('groupId: s.group_id || ""', self.app)
        self.assertIn('groupStatus: s.group_status || ""', self.app)

    def test_what_is_recorded_wins_over_what_is_inferred(self):
        # The recorded answer is consulted before any membership is looked at.
        self.assertLess(self.fn.index("sub.groupId"), self.fn.index("PEOPLE.find"))
        self.assertIn("if (sub.groupId) return { id: sub.groupId,", self.fn)

    def test_an_answer_given_on_whatsapp_is_honoured(self):
        # `assigned_by_user` is what the WhatsApp reply writes, and it was in no
        # lookup anywhere in the console.
        self.assertIn("assigned_by_user", self.app)
        self.assertIn('assigned_by_user: "user"', self.app)

    def test_awaiting_a_reply_is_only_said_when_one_is_awaited(self):
        self.assertIn('if (sub.groupStatus === "ask") return { id: "", how: "asked" };', self.fn)
        # And it is reached only after the recorded group is ruled out.
        self.assertLess(self.fn.index("sub.groupId"), self.fn.index('=== "ask"'))

    def test_inference_survives_only_for_claims_that_predate_the_field(self):
        self.assertIn("Only a claim stored before the group was recorded", self.fn)
        # Any claim carrying a status at all is answered from the claim.
        self.assertIn('if (sub.groupStatus) return { id: "", how: "none" };', self.fn)

    def test_the_fallback_matches_on_address_before_name(self):
        # Two people can share a name, and a rename must not silently
        # re-attribute the claims of whoever is matched instead.
        self.assertIn("PEOPLE.find(p => p.email === sub.whoEmail)", self.fn)


class ATaxRegistrationAttributesAReceipt(unittest.TestCase):
    """The console half: configuring the number, and showing what was found."""

    def setUp(self):
        for name, path in (("app", "../PORTAL/app.html"), ("auth", "lambda_src/auth.py"),
                           ("handler", "lambda_src/handler.py"),
                           ("worker", "lambda_src/auditor_worker.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
                setattr(self, name, handle.read())

    def test_a_group_can_carry_one(self):
        row = self.app.split("P.groups.forEach((g, i) => {", 1)[1].split("gl.appendChild(row)", 1)[0]
        self.assertIn('class="gtax"', row)
        self.assertIn("g.tax_id", row)

    def test_it_is_prefilled_from_the_organisation(self):
        # Most companies have one registration; a per-site one is the
        # exception, so the common case should need no typing.
        row = self.app.split("P.groups.forEach((g, i) => {", 1)[1].split("gl.appendChild(row)", 1)[0]
        self.assertIn("ORG_PROFILE.tax_id", row)
        put = self.auth.split("def _groups_put(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('org_tax = taxid.normalise(org.get("tax_id"))', put)

    def test_the_server_normalises_what_is_typed(self):
        # So a GSTIN entered with spaces still matches one printed without.
        put = self.auth.split("def _groups_put(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('tax_id = taxid.normalise(raw.get("tax_id"))', put)

    def test_the_model_is_asked_for_both_and_told_them_apart(self):
        # Swapping them would attribute every receipt to whichever group
        # happened to share a number with a supplier.
        self.assertIn('"vendor_tax_id"', self.handler)
        self.assertIn('"buyer_tax_id"', self.handler)
        self.assertIn("The SELLER's tax registration", self.handler)
        self.assertIn("The BUYER's tax registration", self.handler)
        # Wrapped across two source lines, so match the halves.
        self.assertIn("Never put ", self.handler)
        self.assertIn("the seller's number here.", self.handler)

    def test_the_match_happens_after_the_bill_is_read(self):
        # It cannot happen at intake: the registration is not known until the
        # model has read the receipt.
        block = self.worker.split("# Which cost centre this receipt belongs to", 1)[1] \
                           .split("# Whether this claim", 1)[0]
        # Through `grouping`, which tries the registration first and the
        # buyer's name second - both of them read off the bill, so neither is
        # available until the model has read it.
        self.assertIn("grouping.group_for(_org_groups(org_id), receipt)", block)
        self.assertIn('"assigned_by_tax_id"', block)
        self.assertIn('"assigned_by_buyer_name"', block)

    def test_a_group_already_settled_is_not_overruled_by_the_paper(self):
        block = self.worker.split("# Which cost centre this receipt belongs to", 1)[1] \
                           .split("# Whether this claim", 1)[0]
        self.assertIn('if group_status in ("ask", "unset") and not group_id:', block)

    def test_the_console_knows_the_new_state(self):
        self.assertIn('assigned_by_tax_id: "taxid"', self.app)
        self.assertIn("the bill is made out to its tax ID", self.app)

    def test_the_vendors_registration_is_printed_on_the_claim(self):
        view = self.auth.split("def _submission_view(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"vendor_tax_id": receipt.get("vendor_tax_id", "")', view)
        self.assertIn('id="vendor-tax"', self.app)
        self.assertIn("vendorTaxId: s.vendor_tax_id", self.app)

    def test_that_row_is_hidden_when_the_bill_carried_none(self):
        # An empty field on every claim teaches people to stop reading the row.
        block = self.app.split('const vtax = $("vendor-tax");', 1)[1].split("$(\"receipt-src\")", 1)[0]
        self.assertIn("vtax.hidden = !bits.length;", block)


class ThePeopleListIsInAnOrderSomebodyCanUse(unittest.TestCase):
    """The server returns them in whatever order the table scanned.

    Which is neither insertion order nor anything a reader can predict, so a
    company of thirty is thirty rows to read one at a time when you are looking
    for one person.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_it_is_sorted_by_name(self):
        self.assertIn("PEOPLE.sort((a, b) => (a.name || a.email || \"\")", self.app)

    def test_it_sorts_names_rather_than_codepoints(self):
        # `localeCompare` files accented and non-Latin names where a reader
        # expects them; `<` files them after Z.
        block = self.app.split("PEOPLE.sort(", 1)[1].split(";", 1)[0]
        self.assertIn("localeCompare", block)
        self.assertIn('sensitivity: "base"', block)

    def test_somebody_with_no_name_still_sorts(self):
        block = self.app.split("PEOPLE.sort(", 1)[1].split(";", 1)[0]
        self.assertIn("a.name || a.email", block)


class AForeignReceiptIsPaidInTheHomeCurrency(unittest.TestCase):
    """Payouts leave the account in one currency however many the receipts were in.

    A dollar receipt is a rupee figure to whoever does the payment run, and
    asking them to convert it themselves is asking for a different answer every
    time at a rate nobody wrote down. So it is converted once, when the claim
    is audited, and the rate and its date are recorded with it.

    Stamped rather than recomputed, and that is the whole point: an amount
    somebody is owed that moves between the day it was approved and the day it
    is paid is not an amount owed. The employee is also entitled to know which
    rate their reimbursement was struck at, which is why both the rate and the
    day it was taken are printed rather than just the total.
    """

    def setUp(self):
        for name, path in (("app", "../PORTAL/app.html"), ("auth", "lambda_src/auth.py"),
                           ("handler", "lambda_src/handler.py"),
                           ("worker", "lambda_src/auditor_worker.py"),
                           ("fx", "lambda_src/fx.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
                setattr(self, name, handle.read())

    def test_it_converts_what_is_paid_not_what_was_spent(self):
        # The budget figure is the receipt total - money is committed whether
        # or not policy agrees to reimburse it. A payout is only ever what
        # policy agreed to.
        fn = self.fx.split("def for_payout(", 1)[1]
        self.assertIn('convert(verdict.get("reimbursable_total"), frm, to)', fn)
        budget = self.fx.split("def for_budget(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('convert(verdict.get("receipt_total"), frm, to)', budget)

    def test_the_rate_is_stamped_on_the_claim(self):
        # The receipt goes in too, so a bill that printed its own conversion
        # can override the looked-up one. See fx.printed_rate.
        self.assertIn('"payout": fx.for_payout(verdict, org_default, receipt)',
                      self.handler)
        self.assertIn("payout_value = :pv", self.worker)
        self.assertIn('":pv": json.loads(json.dumps(outcome.get("payout") or {}),',
                      self.worker)

    def test_it_reaches_the_browser(self):
        view = self.auth.split("def _submission_view(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"payout_value": row.get("payout_value") or {}', view)
        # Gated on the rate, not the amount: a blocked claim converts to zero
        # until somebody approves it, and zero is a real conversion.
        self.assertIn("payoutValue: s.payout_value && s.payout_value.rate", self.app)

    def test_the_figure_shown_is_what_is_owed_now_at_the_rate_then(self):
        # Printing the stored amount would be wrong on exactly the claims a
        # person has looked at: anything blocked reimburses nothing until it is
        # approved, so its stamped figure is zero, and the reviewer who
        # approves it would be shown a payout of nothing.
        block = self.app.split('const payRow = $("r-payout");', 1)[1].split("\n  }", 1)[0]
        self.assertIn("const owed = res.reimbursable;", block)
        self.assertIn("parseFloat(pv.rate)", block)
        self.assertIn("Math.round(owed * rate)", block)

    def test_nothing_is_said_when_there_was_nothing_to_convert(self):
        # A rupee receipt in a rupee organisation has no rate to report, and a
        # tile that repeats the figure beside it is noise.
        fn = self.fx.split("def for_payout(", 1)[1]
        self.assertIn("if not frm or not to or frm.upper() == to.upper():", fn)
        self.assertIn("return {}", fn)
        block = self.app.split('const payRow = $("r-payout");', 1)[1].split("\n  }", 1)[0]
        self.assertIn("payRow.hidden = !show;", block)

    def test_a_missing_rate_is_not_a_zero(self):
        # Counting an unconvertible claim as nothing would under-report a
        # payout run, which is the failure that actually costs somebody money.
        fn = self.fx.split("def for_payout(", 1)[1]
        self.assertIn("return converted or {}", fn)

    def test_the_rate_and_its_day_are_both_printed(self):
        block = self.app.split('const payRow = $("r-payout");', 1)[1].split("\n  }", 1)[0]
        self.assertIn("pv.rate", block)
        self.assertIn("rateDay(pv.as_of)", block)
        self.assertIn("pv.from", block)

    def test_it_is_not_shown_on_a_claim_nobody_has_decided(self):
        # There is no amount owed yet, so there is no amount to convert.
        # The payout row appears once a rate exists for the claim. There is no
        # longer a separate "undecided" state to suppress it in: an undecided
        # claim is worth the same figure as a decided one.
        block = self.app.split('const payRow = $("r-payout");', 1)[1].split("\n  }", 1)[0]
        self.assertIn("pv.rate", block)

    def test_the_converted_figure_is_read_as_a_decimal_string(self):
        # It crosses the API as "2257.92", not as minor units - reading it the
        # way the rest of this file reads money would show 22.57.
        fn = self.app.split("function fmtPlain(", 1)[1].split("\n}", 1)[0]
        self.assertIn("parseFloat(amount", fn)
        self.assertIn("* 100", fn)

    def test_policy_still_never_converts(self):
        # An exchange rate deciding whether a receipt passes a cap would make
        # the verdict depend on the day it was read.
        with open(os.path.join(ROOT, "lambda_src", "policy.py"), encoding="utf-8") as h:
            self.assertNotIn("import fx", h.read())


class TheQueueHasTheLeftColumnToItself(unittest.TestCase):
    """The receipt sat below the queue, so a long queue buried it.

    The review page was two splits stacked: queue beside verdict, then receipt
    beside rationale underneath. That put the photograph of the claim being
    read *under the list of claims* - so thirty waiting claims pushed it off
    the bottom of the screen, and reading the bill meant scrolling past every
    claim that was not it.

    The list grows. The thing being read should not move.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.view = self.app.split('id="split-queue"', 1)[1].split("RULES VIEW", 1)[0]

    def test_everything_but_the_queue_is_in_one_column(self):
        self.assertIn('id="detail-col"', self.view)
        # In the order a reviewer reads them: the verdict, the receipt it was
        # reached from, then what the submitter will be told about it.
        self.assertLess(self.view.index('id="detail-col"'), self.view.index('id="detail-panel"'))
        self.assertLess(self.view.index('id="detail-panel"'), self.view.index('id="split-evidence"'))

    def test_the_queue_is_alone_on_the_left(self):
        left = self.view.split('id="queue-list"', 1)[1].split('id="detail-col"', 1)[0]
        for stray in ('id="original"', 'id="rationale"', 'id="detail-panel"'):
            self.assertNotIn(stray, left, stray)

    def test_the_evidence_stacks_inside_that_column(self):
        css = max(re.findall(r"<style[^>]*>(.*?)</style>", self.app, re.S), key=len)
        self.assertIn(".detail-col #split-evidence { grid-template-columns:1fr; }", css)
        rule = css.split(".detail-col {", 1)[1].split("}", 1)[0]
        self.assertIn("flex-direction:column", rule)
        # Without this a long vendor name or a wide image stretches the column
        # and squeezes the queue.
        self.assertIn("min-width:0", rule)

    def test_the_claim_page_still_puts_them_side_by_side(self):
        # There is no queue there, so the column is the whole width. The claim
        # itself keeps that width rather than sharing it with the receipt: a
        # grocery run with twenty line items fills it on its own, and halving
        # it doubles the page length to reclaim space only a one-line claim
        # ever had spare.
        css = max(re.findall(r"<style[^>]*>(.*?)</style>", self.app, re.S), key=len)
        self.assertIn("#claim-slot #split-evidence { grid-template-columns:minmax(300px,1fr) minmax(300px,1fr); }",
                      css)


class ActiveAndPendingAreTwoLists(unittest.TestCase):
    """Forty-six unaccepted invitations interleaved with thirteen real people.

    Alphabetically, so the roll you look a colleague up in and the chase list
    of people who have never signed in are shuffled together - which makes the
    roll unreadable and the chase list invisible. They are two different jobs:
    one is everyday lookup, the other is occasional chasing.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_the_view_has_its_own_tabs(self):
        self.assertIn('id="pe-subtabs"', self.app)
        self.assertIn('const PEOPLE_TABS = [["active", "Active"], ["pending", "Pending"],\n'
                      '                     ["inactive", "Inactive"]];',
                      self.app)

    def test_active_is_what_opens(self):
        # Looking somebody up is the everyday job; chasing an invitation is
        # the occasional one.
        self.assertIn('let peopleTab = "active";', self.app)

    def test_the_two_lists_are_the_two_halves_of_the_roll(self):
        body = self.app.split("function renderPeople() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("active:   PEOPLE.filter(isActivePerson)", body)
        # Not `!isActivePerson`, which also caught everybody who had been
        # removed - see ARemovedPersonIsNotAnOutstandingInvitation.
        self.assertIn('pending:  PEOPLE.filter(p => p.invite === "pending")', body)

    def test_only_the_chosen_half_is_drawn(self):
        body = self.app.split("function renderPeople() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("shown.forEach(person =>", body)
        self.assertNotIn("PEOPLE.forEach(person =>", body)

    def test_each_tab_says_what_its_number_means(self):
        body = self.app.split("function renderPeople() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("can send receipts", body)
        self.assertIn("awaiting acceptance", body)

    def test_an_empty_half_says_which_half_is_empty(self):
        body = self.app.split("function renderPeople() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("Nobody has accepted an invitation yet", body)
        self.assertIn("Every invitation has been accepted.", body)

    def test_only_the_active_count_is_coloured_as_a_headcount(self):
        # Active is how many people you have. Pending is a chase list and
        # Inactive is history; neither is a headcount, and neither should be
        # coloured like one.
        fn = self.app.split("function renderPeopleTabs(", 1)[1].split("\n}", 1)[0]
        self.assertIn('id === "active" ? "count" : "dormant"', fn)


class SettlementPaysOrRefusesAndDoesNotReopen(unittest.TestCase):
    """Two actions at settlement: pay it, or refuse it with a reason.

    It had three, and one of them was wrong twice over. "Send back for review"
    put an agent-cleared claim into the review queue - a queue watched by the
    same two roles that settle, and a place the claim had never been, so "back"
    named a journey it never took. The case it existed for was a claim that is
    legitimate but wrongly read; that is now fixed where it is seen, because
    the expense type and the currency are editable until the money moves and
    changing either re-runs the policy engine.

    Refusing at settlement, by contrast, had no substitute. The reasons are not
    review judgments - they are about paper that never arrived, which no
    reviewer can produce and no re-reading of the claim can settle.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.fn = self.app.split("function renderSettleOnClaim(", 1)[1].split("\n}\n", 1)[0]

    def test_the_settlement_bar_offers_one_action(self):
        self.assertIn('[["Record payment", "primary", "pay"]]', self.fn)
        self.assertNotIn('"Reject"', self.fn)

    def test_send_back_is_gone(self):
        self.assertNotIn('back.textContent = "Send back for review";', self.app)
        self.assertNotIn('decide(sub, "Sent back", back)', self.app)

    def test_refusing_is_offered_beside_the_payment(self):
        self.assertIn('openReasonForm(sub, "settle_rejected", "Rejected")', self.app)

    def test_with_reasons_no_reviewer_could_have_reached(self):
        reasons = self.app.split("const SETTLE_REJECT_REASONS = [", 1)[1].split("];", 1)[0]
        self.assertIn("physical bill has not been received", reasons)
        self.assertIn("does not match this claim", reasons)

    def test_rejecting_still_exists_where_it_belongs_too(self):
        # In the review queue, on a claim that is actually under review.
        self.assertIn('["Reject","danger","Rejected"]', self.app)

    def test_the_settlement_reject_form_is_gone_rather_than_orphaned(self):
        # Left behind it would be sixty lines nothing calls, and the next
        # reader would wire it back up.
        self.assertNotIn("function rejectForm(", self.app)
        self.assertIn("const REJECT_REASONS = [", self.app)
        self.assertIn("openReasonForm(", self.app)


class ADecisionsWordsAreCollectedOnThePage(unittest.TestCase):
    """A rejection was typed into `window.prompt`.

    One line, no choices, no formatting, and the claim hidden behind the modal
    while you write about it - so what reached the employee was whatever could
    be typed blind in a sentence. "This is a duplicate" is a real example: true,
    and useless to somebody trying to work out which bill it duplicates.

    The words go to a person verbatim, so they are collected where the claim is
    visible, from a list of sentences somebody can act on, in a box that can be
    edited before it is sent.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.fn = self.app.split("function openReasonForm(", 1)[1].split("\n}\n", 1)[0]

    def test_the_duplicate_reason_names_the_other_claim(self):
        # The whole reason a list exists: "This is a duplicate" tells the
        # employee nothing they can check.
        fn = self.app.split("function reasonChoices(", 1)[1].split("\n}", 1)[0]
        self.assertIn("sub.duplicateOf", fn)
        self.assertIn("This is the same bill as ${sub.duplicateOf}", fn)
        self.assertIn("list.unshift(", fn)

    def test_a_choice_fills_the_box_rather_than_replacing_it_silently(self):
        # The reviewer sees what they are about to send, and can change it.
        self.assertIn('pick.addEventListener("change", () => { text.value = pick.value;', self.fn)
        self.assertIn('<textarea id="rr-text"', self.fn)

    def test_writing_your_own_is_an_option_not_a_word_that_gets_sent(self):
        # An "Other" option would arrive in somebody's inbox as the word
        # "Other", so it selects an empty box instead.
        self.assertIn('<option value="">Write my own…</option>', self.fn)

    def test_nothing_too_short_can_be_sent(self):
        self.assertIn("go.disabled = n < 4;", self.fn)
        self.assertIn("Say a little more", self.fn)

    def test_each_decision_asks_its_own_question(self):
        # Two kinds carry words, and both refuse a claim: one at review, one
        # at settlement. "queried" went when nothing asked a submitter
        # anything; "disputed" went with the button that triggered it.
        self.assertIn("REASON_PROMPT", self.app)
        prompts = self.app.split("const REASON_PROMPT = {", 1)[1].split("};", 1)[0]
        for kind in ("rejected", "settle_rejected"):
            self.assertIn(f"{kind}:", prompts)
        self.assertNotIn("queried", self.app)
        self.assertNotIn("QUERY_REASONS", self.app)
        self.assertNotIn("DISPUTE_REASONS", self.app)

    def test_the_button_says_what_it_will_do(self):
        self.assertIn('kind === "rejected" || kind === "settle_rejected"', self.fn)
        self.assertIn('"Reject claim"', self.fn)

class ABackgroundReloadNeverEatsUnsavedWork(unittest.TestCase):
    """Enabling an expense type, looking away, and finding it disabled again.

    `loadRecords` replaces the whole rule set from the server and clears the
    dirty list with it. That was harmless while it only ran at boot. It stopped
    being harmless the moment refresh started reloading records and the console
    began reloading on returning to the tab: enable Utilities, switch to
    another window, come back, and the toggle is sitting back where it started
    with nothing said about why.

    Unsaved work is the one thing a background refresh must never touch. The
    reader is the authority on what they have typed; the server's copy is only
    newer for the things nobody is editing.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()

    def test_the_policy_is_not_replaced_mid_edit(self):
        self.assertIn("&& !rulesDirty.length) {", self.app)

    def test_budgets_are_not_either(self):
        self.assertIn("budgets: budgetsDirty ? ORG_PROFILE.budgets : budgetsFromApi(o.budgets)",
                      self.app)

    def test_editing_a_budget_marks_it(self):
        # Both the limits themselves and the period they are measured over.
        self.assertEqual(2, self.app.count("budgetsDirty = true;"))

    def test_saving_releases_the_guard(self):
        # Otherwise the console never takes a server update again for the rest
        # of the session.
        self.assertIn("if (ok) budgetsDirty = false;", self.app)

    def test_saving_the_policy_releases_its_guard_too(self):
        # `rulesFromApi` empties `rulesDirty`, and the save calls it.
        save = self.app.split("async function saveRules(", 1)[1].split("\n}", 1)[0]
        self.assertIn("rulesFromApi(data.rules);", save)
        from_api = self.app.split("function rulesFromApi(stored) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("rulesDirty = [];", from_api)

    def test_the_reload_that_made_this_matter_is_still_there(self):
        # The fix is the guard, not backing the refresh out: a console that
        # cannot see a receipt until it is reloaded is the worse bug.
        fn = self.app.split("async function refreshNow(", 1)[1].split("\n}", 1)[0]
        self.assertIn("loadRecords()", fn)


class SpendByPerson(unittest.TestCase):
    """The same question asked of people rather than teams.

    A group answers "which part of the company spends this". A person answers
    "who", which is the one a budget conversation actually starts from.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.block = self.app.split("/* ---- by user ----", 1)[1] \
                             .split("// ---- where the money goes", 1)[0]

    def test_it_sits_after_by_group(self):
        sections = self.app.split("const REPORT_SECTIONS = [", 1)[1].split("];", 1)[0]
        self.assertLess(sections.index('"groups"'), sections.index('"people"'))
        self.assertLess(sections.index('"people"'), sections.index('"detail"'))
        self.assertIn('["people",     "By user"]', sections)

    def test_people_are_keyed_on_their_address(self):
        # Two people can share a name and somebody can be renamed. A report
        # that silently merged two colleagues into one row would be wrong in
        # the direction nobody checks.
        self.assertIn("String(r.sub.whoEmail || r.sub.who", self.block)
        self.assertIn(".toLowerCase()", self.block)

    def test_only_people_who_submitted_appear(self):
        # The People tab is the roll. Forty rows of zero would bury the handful
        # who spent anything.
        self.assertIn("rows.forEach(r => {", self.block)
        self.assertNotIn("PEOPLE.forEach", self.block)

    def test_somebody_since_removed_is_still_who_spent_the_money(self):
        self.assertIn("(person && person.name) || b.rows[0].sub.who || b.who", self.block)

    def test_it_counts_in_one_currency_like_every_other_report(self):
# Converted at each claim's own rate rather than filtered to one
        # currency and the rest silently dropped.
        self.assertIn("sum(b.rows, r => r.claimed)", self.block)

    def test_the_biggest_spender_is_at_the_top(self):
        self.assertIn("sort((a, b) => b.claimed - a.claimed", self.block)

    def test_an_empty_period_says_so_rather_than_rendering_nothing(self):
        self.assertIn("No receipts in ${periodLabel()}", self.block)
        self.assertIn('blank("r-users", 5, msg)', self.app)

    def test_the_columns_match_the_report_beside_it(self):
        head = self.app.split('id="rsec-people"', 1)[1].split('id="r-users"', 1)[0]
        for col in ("Person", "Receipts", "Claimed", "Settled",
                    "Share of spend"):
            self.assertIn(f">{col}<", head)

    def test_no_report_still_carries_a_reimbursable_column(self):
        # It was the engine's arithmetic from when a verdict computed a
        # fraction of a claim. A claim is worth what the receipt says now, so
        # the column printed Claimed again on nearly every row - and on the
        # rows where it did not, it was silently excluding refused claims,
        # which is what Settled and the queue counts already answer.
        # Comments stripped: the note explaining why the column is gone sits
        # inside the <thead> it was removed from, and contains the word.
        for head in re.findall(r"<thead>(.*?)</thead>", self.app, re.S):
            head = re.sub(r"<!--.*?-->", " ", head, flags=re.S)
            self.assertNotIn("Reimbursable", head)

    def test_the_pending_settlement_tile_still_uses_the_figure(self):
        # Cleared minus settled is a real number and a different question -
        # what the company owes right now. Only the column went.
        self.assertIn('$("rp-reimb").textContent = fmt(pending, home);', self.app)


class TaggingAClaimIsNotConfirmed(unittest.TestCase):
    """There was a dialog here, and the gate is a better control than it was.

    Giving a type to a claim the agent could not classify is a real judgment:
    it decides how the claim is reported and which budget pays it, and it is
    recorded against the reviewer's name. The dialog was not where that
    judgment lives, though. Nothing is decided by saving - the claim stays
    exactly where it is, in front of the same person - and Approve is still
    withheld until both the expense type and the cost centre are recorded.

    That gate cannot be clicked through. The dialog could, and taxed the
    common case to do it: a reviewer working a queue where most claims need a
    type answered it once per claim, and a confirmation everybody learns to
    dismiss is worth nothing on the day it matters.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.fn = self.app.split("async function saveAnswers()", 1)[1].split("\n}\n", 1)[0]

    def test_saving_asks_nothing(self):
        self.assertNotIn("window.confirm(", self.fn)

    def test_but_the_type_is_still_checked_before_it_is_stored(self):
        # A type that is not one of the configured, enabled ones is refused
        # here as well as on the server. Relaxing the friction is not the same
        # as accepting anything.
        self.assertIn("if (!rules.types.some(t => t.enabled && t.id === chosen))",
                      self.fn)

    def test_and_approve_still_waits_for_both_answers(self):
        # The control that replaced the dialog, and the reason removing it is
        # safe. `typeOk` and `needsGroup` withhold Approve; only Reject is
        # offered until they are answered.
        self.assertIn("const typeOk = rules.types.some(t => t.enabled && t.id === w.type);",
                      self.app)
        self.assertIn("(!typeOk || unsaved || needsGroup || unreadable)", self.app)

    def test_and_the_server_asks_the_same_two_questions(self):
        # Because the console is a page somebody can have open from before
        # this shipped.
        with open(os.path.join(ROOT, "lambda_src", "auth.py"), encoding="utf-8") as h:
            auth = h.read()
        review = auth.split("def _claim_review(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("Set an expense type first.", review)
        self.assertIn("Set a group first.", review)


class TheSettlementListCountsInOneCurrency(unittest.TestCase):
    """The columns showed a rupee figure under a dollar sign.

    `payable()` converts what is owed into the currency payouts are made in, so
    the amount in the row was right - 2,257.92 for a $23.60 bill - while the
    symbol beside it still came from the receipt. A number and a currency that
    disagree is worse than either being wrong on its own: it reads as a
    plausible dollar amount and it is nothing of the kind.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.row = self.app.split("// Only what is still owed.", 1)[1] \
                           .split("tb.appendChild(tr);", 1)[0]

    def test_the_row_takes_the_currency_the_payment_is_made_in(self):
        self.assertIn("const ccy = c.payCcy;", self.row)
        self.assertNotIn("c.res.currency", self.row)

    def test_every_money_column_uses_it(self):
        for col in ("fmt(c.approved, ccy)", "fmt(c.paid, ccy)", "fmt(c.outstanding, ccy)"):
            self.assertIn(col, self.row)

    def test_the_claim_page_and_the_stored_payment_agree_with_it(self):
        # One currency across the row, the form and what is written down.
        self.assertIn("const ccy = c.payCcy;", self.app.split("function renderSettleOnClaim(", 1)[1])
        self.assertIn("ccy: claim.payCcy || orgCurrency()", self.app)

    def test_the_form_says_what_rate_produced_the_figure(self):
        form = self.app.split("function settleForm(", 1)[1].split("\n}", 1)[0]
        self.assertIn("c.rate ?", form)
        self.assertIn("fmt(c.original, c.from)", form)

    def test_a_claim_that_could_not_be_converted_is_named(self):
        # It is not in `awaiting`, so without this it is simply missing from a
        # list of what the company owes.
        self.assertIn('const stuckNote = $("pay-stuck");', self.app)
        self.assertIn("cannot be paid yet: no exchange rate was available", self.app)


class TheReviewQueueSaysWhatItIsWorth(unittest.TestCase):
    """It counted claims and said nothing about money.

    "Two waiting" could be two coffees or two flights, and the only way to find
    out was to open both. Pending settlement has answered that question about
    its own list since it was built; this is the same answer for the list
    before it.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.fn = self.app.split("function paintQueueTiles(queue) {", 1)[1].split("\n}", 1)[0]
        # The arithmetic moved out of the tile when the list grew a total of
        # its own: one computation, two readers, so they cannot disagree.
        self.sum = self.app.split("function claimsTotal(subs) {", 1)[1].split(
            "\n}", 1)[0]

    def test_claimed_and_possible_payout_are_two_different_figures(self):
        # One figure. "Possible payout" was the same number written twice
        # once the engine stopped deducting anything - and ₹0.00 under it,
        # beside a real claimed figure, read as a refusal.
        self.assertIn('id="q-claimed"', self.app)
        self.assertNotIn('id="q-payout"', self.app)
        self.assertIn("at(sub, evaluate(sub).receiptTotal)", self.sum)

    def test_it_counts_in_the_currency_payouts_are_made_in(self):
        self.assertIn("const home = orgCurrency();", self.sum)
        self.assertIn("sub.payoutValue && parseFloat(sub.payoutValue.rate)",
                      self.sum)

    def test_a_claim_with_no_rate_is_named_not_summed_at_zero(self):
        self.assertIn("const stuck = subs.filter(", self.sum)
        self.assertIn("not converted — no rate available.", self.fn)

    def test_the_tile_and_the_list_total_are_one_figure(self):
        # Two sums over the same rows is a pair that eventually disagrees by a
        # rounding rule nobody remembers choosing.
        self.assertIn("const { total: claimed, stuck, home } = claimsTotal(queue);",
                      self.fn)
        foot = self.app.split("function totalRow(box, subs, columns) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("claimsTotal(subs)", foot)

    def test_the_total_is_not_a_row_anybody_can_click(self):
        foot = self.app.split("function totalRow(box, subs, columns) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn('tr.className = "listtotal";', foot)
        # `wireClaimRows` keys off `tr.opens`, which this does not carry.
        self.assertIn('tbody.querySelectorAll("tr.opens")', self.app)

    def test_a_mixed_currency_column_says_the_total_was_converted(self):
        # Thirteen rows of dollars and rupees over one rupee figure looks like
        # arithmetic that does not work unless it says what it did.
        foot = self.app.split("function totalRow(box, subs, columns) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn('foreign ? ` \u00b7 converted to ${home}` : ""', foot)
        self.assertIn("with no rate, counted as nil", foot)

    def test_each_row_shows_what_the_bill_said_and_what_it_comes_to(self):
        # So the column the total adds up is on the page to be added up.
        cell = self.app.split("function claimedCell(sub, res) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn('if (!ccy || ccy === home) return billed;', cell)
        self.assertIn('<span class="amtmain">', cell)
        self.assertIn('<span class="amtsub">', cell)
        self.assertIn('<span class="amtsub norate">no rate</span>', cell)

    def test_and_the_converted_figure_is_the_one_at_full_size(self):
        # An eye running down the column has to meet the same currency at the
        # same size on every row, or the foot of it appears to total the small
        # print. The converted figure is what the total adds up, so it leads
        # and the bill's own figure is the second line.
        cell = self.app.split("function claimedCell(sub, res) {", 1)[1].split(
            "\n}", 1)[0]
        converted = cell.index("res.receiptTotal * rate")
        main = cell.index('<span class="amtmain">', cell.index("if (!rate)"))
        sub = cell.index('<span class="amtsub">')
        # The conversion is inside the main span, and the billed line follows.
        self.assertLess(main, converted)
        self.assertLess(converted, sub)

    def test_a_row_with_no_rate_leads_with_the_only_figure_there_is(self):
        # Nothing to convert with, so the bill's own number is the top line
        # rather than a blank one, and the second says why no rupees follow.
        cell = self.app.split("function claimedCell(sub, res) {", 1)[1].split(
            "\n}", 1)[0]
        norate = cell.split("if (!rate)", 1)[1].split(";", 1)[0]
        self.assertIn('<span class="amtmain">${billed}</span>', norate)
        self.assertIn("norate", norate)

    def test_the_settlement_column_is_arranged_the_same_way(self):
        # It had reached this arrangement first, under names of its own. Two
        # vocabularies for one idea is how they drift apart again.
        self.assertNotIn('class="billed', self.app)
        self.assertNotIn('class="inhome', self.app)

    def test_it_says_how_long_the_oldest_has_waited(self):
        # The number that says whether the queue is being worked or silting up.
        self.assertIn('id="q-oldest"', self.app)
        self.assertIn("queue.reduce((a, b) =>", self.fn)

    def test_nothing_is_shown_when_nothing_is_waiting(self):
        self.assertIn("tiles.hidden = !queue.length;", self.fn)

    def test_it_is_painted_from_the_same_list_the_queue_renders(self):
        # Not a second filter that could drift from `isQueued` - and now not a
        # second copy of the submitter filter either. `queueRows` is the one
        # place both are applied; the tiles, the rows, the select boxes and
        # the Approve button all read it, so the count above the list cannot
        # describe a different set from the rows under it.
        body = self.app.split("function renderQueue() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("const queue = queueRows();", body)
        self.assertIn("paintQueueTiles(queue);", body)
        rows = self.app.split("function queueRows() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("SUBMISSIONS.filter(isQueued)", rows)
        # Exactly one place filters the queue by submitter.
        self.assertEqual(1, self.app.count("SUBMISSIONS.filter(isQueued).filter("))


class EveryReportCountsOneCurrency(unittest.TestCase):
    """Reports dropped every claim that was not in the selected currency.

    A month with two dollar receipts and one rupee receipt reported the rupee
    one and called it the total - so the Reports headline and the Pending
    settlement page sat side by side describing the same week with different
    numbers, and neither was wrong about its own arithmetic.

    Everything is converted at the rate stamped on each claim when it was
    audited, which is the rate it will be paid at. A historical month does not
    move when the rupee does, and the two screens cannot disagree.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.fn = self.app.split("function analysed() {", 1)[1].split("\n}", 1)[0]

    def test_the_conversion_happens_once_for_every_report(self):
        # No "dis:" - there is no disallowed figure to convert any more.
        for field in ("claimed:", "reimb:", "settled:"):
            self.assertIn(field, self.fn)
        self.assertIn("sub.payoutValue && parseFloat(sub.payoutValue.rate)", self.fn)

    def test_a_claim_already_in_the_home_currency_is_not_converted(self):
        self.assertIn("!ccy || ccy === home ? 1", self.fn)

    def test_a_claim_that_cannot_be_converted_is_flagged_not_guessed(self):
        self.assertIn("convertible: rate !== null", self.fn)
        self.assertIn("const stuck = rows.filter(r => !r.convertible);", self.app)

    def test_the_currency_selector_is_gone(self):
        # It was a way of saying "show me the rupee claims and ignore the
        # rest", which is not a total.
        self.assertNotIn("reportCcy", self.app)
        self.assertNotIn('id="r-ccy"', self.app)

    def test_the_three_money_tiles_are_one_figure_split_three_ways(self):
        self.assertIn("fmt(waiting + pending + settledPaid, home)", self.app)
        self.assertIn("const waiting = sum(inReview, r => r.claimed);", self.app)
        self.assertIn("const pending = Math.max(0, sum(clearedRows, r => r.reimb) - settledPaid);",
                      self.app)

    def test_a_rejected_claim_is_in_none_of_them(self):
        # It is not money the company owes, so it is out of Claimed too - or
        # the row would never add up and the reader would hunt for the gap.
        self.assertIn('const live = rows.filter(r => !["rejected", "withdrawn"].includes(r.stage));',
                      self.app)

    def test_the_headline_shows_the_split(self):
        # And splits "waiting" further - see `WaitingIsTwoDifferentWaits`.
        self.assertIn("in review", self.app)
        self.assertIn("awaiting a reply", self.app)
        self.assertIn("to pay", self.app)


class ReportsCountWhatAReviewerReleased(unittest.TestCase):
    """An approved duplicate showed claimed ₹1,913.49 and reimbursable nothing.

    The engine's reimbursable figure is zero while a finding blocks a claim -
    that is what blocking means. A reviewer who approves it anyway is the only
    source of what it is worth, and the reports were not reading that: so the
    cell was blank, and the headline disagreed with the Pending settlement page
    about the very same claims.

    One definition of what a claim is worth, shared by the settlement list and
    every report.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.fn = self.app.split("function analysed() {", 1)[1].split("\n}", 1)[0]

    def test_the_reports_read_the_released_figure(self):
        self.assertIn("reimb: at(approvedInClaimCcy(sub))", self.fn)
        self.assertNotIn("reimb: at(res.reimbursable)", self.fn)

    def test_it_is_the_same_helper_the_settlement_list_uses(self):
        payable = self.app.split("function payable(sub) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("approvedInClaimCcy(sub)", payable)

    def test_claimed_is_still_what_the_receipt_came_to(self):
        # Approving does not change what was spent, only what is owed.
        self.assertIn("claimed: at(res.receiptTotal)", self.fn)

    def test_a_claim_nobody_has_decided_is_still_worth_the_engines_figure(self):
        # The fallback only fires on an approval, so a blocked claim awaiting
        # review is correctly worth nothing yet.
        fn = self.app.split("const approvedInClaimCcy = (sub) => {", 1)[1].split("\n};", 1)[0]
        self.assertIn('d && d.action === "Approved"', fn)
        self.assertIn("return res.reimbursable;", fn)


class WaitingIsTwoDifferentWaits(unittest.TestCase):
    """One number covering both disagreed with the tab beside it.

    The review queue counts claims waiting on a *reviewer*. A claim somebody
    has asked the submitter a question about is waiting on **them** - it is not
    in that queue and nobody in finance can act on it. Summing the two into one
    "waiting" figure read as an arithmetic error against a badge saying 1.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.block = self.app.split("const live = rows.filter(", 1)[1] \
                              .split('$("rp-reimb")', 1)[0]

    def test_the_two_waits_are_counted_apart(self):
        self.assertIn("const asked = inReview.filter(r => decisions[r.sub.id]);", self.block)
        self.assertIn("const queued = inReview.filter(r => !decisions[r.sub.id]);", self.block)

    def test_the_note_names_each_of_them(self):
        self.assertIn("in review", self.block)
        self.assertIn("awaiting a reply", self.block)

    def test_the_total_still_covers_both(self):
        # Splitting the note must not change what Claimed adds up to.
        self.assertIn("const waiting = sum(inReview, r => r.claimed);", self.block)
        self.assertIn("fmt(waiting + pending + settledPaid, home)", self.block)

    def test_a_part_of_the_note_with_nothing_in_it_is_left_out(self):
        # A row of "₹0.00 paid" on every report teaches people to stop reading.
        self.assertIn(".filter(Boolean).join(\" · \")", self.block)

class ARejectedClaimIsStillSomewhere(unittest.TestCase):
    """It was in no list at all.

    A rejection is a decision, so the claim leaves the review queue. Nothing
    cleared, so it never reaches Pending settlement. Settled shows what was
    paid. So the only record of somebody refusing to pay a colleague was a
    field on a row with no screen that could reach it.

    Refusing a claim is the decision most likely to be questioned later, and it
    was the one decision the console could not show you.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.fn = self.app.split("function renderRejected() {", 1)[1].split("\n}\n", 1)[0]

    def test_it_has_a_tab_of_its_own(self):
        self.assertIn('["rejected","Rejected"]', self.app)
        self.assertIn('id="ssec-rejected"', self.app)

    def test_it_lists_every_rejected_claim(self):
        self.assertIn('claimStage(sub).stage === "rejected"', self.fn)

    def test_the_reason_the_submitter_was_given_is_a_column(self):
        # It is what makes a rejection reviewable; without it the list is a
        # set of names and amounts nobody can question.
        self.assertIn("Reason given", self.app)
        self.assertIn("No reason recorded", self.fn)

    def test_it_names_who_refused(self):
        self.assertIn("personName(d.by) || rev.by", self.fn)

    def test_newest_first(self):
        self.assertIn("shown.sort((a, b) => ((b.review || {}).at || 0)", self.fn)

    def test_the_rows_open_the_claim_like_every_other_list(self):
        self.assertIn("opensClaim(tr, sub.id);", self.fn)
        self.assertIn("wireClaimRows(tb);", self.fn)

    def test_it_follows_the_period_the_tab_is_showing(self):
        self.assertIn("inSettledRange(sub)", self.fn)
        self.assertIn("Widen it above.", self.fn)

class EachCostCentreHasItsOwnColour(unittest.TestCase):
    """A column of identically-green chips.

    Every group chip was `.state on`, so Pending settlement showed a column of
    same-shaped, same-coloured words that had to be read one at a time - on
    the screen where somebody is scanning for which budget a payment comes out
    of.

    Six tints, taken from the group's position in the organisation's own list.
    A hash of the id was the first attempt, and it is what anyone reaches for
    first: six buckets over arbitrary strings collide constantly, and this
    account's own groups proved it in the first probe - `mobil80` and
    `cocobble` landed on the same shade, which is worse than no colour on a
    screen that exists to tell them apart.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.css = self.app.split("<style>", 1)[1].rsplit("</style>", 1)[0]

    def test_the_tint_comes_from_the_position_in_the_list(self):
        fn = self.app.split("function groupTint(id) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("const at = (ORG_PROFILE.groups || []).findIndex(g => g.id === id);",
                      fn)
        self.assertIn('if (at >= 0) return "g" + (at % 6);', fn)

    def test_a_group_no_longer_on_the_list_still_gets_one(self):
        # A claim from before a group was deleted. It has nothing left to be
        # confused with, so a hash is fine there.
        fn = self.app.split("function groupTint(id) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("h = (h * 31 + id.charCodeAt(i)) >>> 0;", fn)

    def test_six_tints_are_defined_and_each_carries_its_own_ink(self):
        for n in range(6):
            self.assertIn(f".state.g{n} {{ background:var(--g{n}-bg); color:var(--g{n}); }}",
                          self.css)

    def test_they_are_defined_for_every_theme(self):
        # A token defined only in the light block renders as nothing in dark.
        for n in range(6):
            self.assertEqual(3, self.css.count(f"--g{n}:"),
                             f"--g{n} must be set in :root, the media query "
                             f"and the [data-theme] block")

    def test_the_chip_uses_it(self):
        self.assertIn('`<span class="state ${groupTint(g.id)}">', self.app)

    def test_an_unattributed_claim_is_still_visibly_not_a_group(self):
        # "not set" is not a sixth colour; it is the absence of one.
        self.assertIn('\'<span class="state off">not set</span>\'', self.app)

class TwoButtonsOneLabel(unittest.TestCase):
    """A claim settled in full by somebody who never saw the fields.

    The actions row carried "Record payment", which opens a form. The form
    appears directly beneath it, and the button that actually records
    anything - at the end of the form's own grid - said "Record payment" too.
    Two identical labels a few pixels apart, the second landing where the
    first had just been clicked.

    Pressing twice in the same spot settled a claim in full at the prefilled
    amount, with the mode, the reference and the date left as they were. The
    person doing it reported expecting "the entry boxes to record" - they had
    not seen them.

    So the first says it opens something, and the second says what it will do
    with the figure currently in the box. A button that names the amount it is
    about to record cannot be pressed by accident with the wrong number in it.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.form = self.app.split("function settleForm(c, ccy) {", 1)[1].split(
            "\n  return td;", 1)[0]

    def test_the_one_that_opens_a_form_says_so(self):
        opener = self.app.split("function renderSettleOnClaim(sub, maySettle) {",
                                1)[1].split("\n}", 1)[0]
        self.assertIn('b.textContent = open === kind ? "Cancel" : label + "\\u2026";',
                      opener)

    def test_the_one_that_records_names_the_amount(self):
        self.assertIn("Record ${", self.form)
        self.assertIn("as paid", self.form)

    def test_and_follows_the_box(self):
        # Part settling is done by lowering the amount, so the figure on the
        # button has to be the figure in the field.
        self.assertIn('amtBox.addEventListener("input", nameTheAmount);', self.form)
        self.assertIn("`Record ${fmt(Math.min(minor, c.outstanding), ccy)} as paid`",
                      self.form)

    def test_an_empty_amount_or_reference_disables_it(self):
        # The reference is required now, and the button says which of the two
        # is missing rather than sitting dead with no explanation.
        # And a float settlement waives the reference: no transfer was
        # made, so there is no line on any statement to tie it to.
        self.assertIn("goBtn.disabled = !(minor > 0) || (!onFloat && !ref);",
                      self.form)
        self.assertIn('"Enter an amount"', self.form)
        self.assertIn('"Enter the transaction reference"', self.form)

    def test_the_two_labels_are_not_the_same_string(self):
        # The regression in one assertion.
        self.assertNotIn('id="sf-go">Record payment</button>', self.app)

class SettledAndRefusedAreNotOneThing(unittest.TestCase):
    """My expenses had "Closed", which held both.

    Whether you were paid and whether you were refused are the two things a
    person most wants told apart about their own money, and the tab that
    grouped them answered with a word that means neither. Four tabs now:
    Pending settlement, Settled, Rejected, Withdrawn.

    A withdrawn claim had been sitting with the rejected ones, on the
    reasoning that from the claimant's side both mean "no money is coming".
    True, and not the distinction that matters: one is a decision somebody
    else made about your claim and the other is one you made yourself.
    Looking for a receipt you took back among the ones the company refused
    reads as being told off for it, and the count beside "Rejected" was
    inflated by claims nobody had rejected.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as handle:
            self.app = handle.read()
        self.sections = self.app.split("const MINE_SECTIONS = [", 1)[1].split(
            "\n];", 1)[0]

    def test_the_four_tabs(self):
        for label in ('"Pending settlement"', '"Settled"', '"Rejected"',
                      '"Withdrawn"'):
            self.assertIn(label, self.sections)
        self.assertNotIn('"Closed"', self.sections)

    def test_each_tab_carries_its_own_test(self):
        # One list of (id, label, predicate), so the tab strip and the rows
        # under it cannot disagree about what belongs where.
        self.assertIn('(sub) => !isClosed(sub)', self.sections)
        self.assertIn('claimStage(sub).stage === "settled"', self.sections)
        self.assertIn('claimStage(sub).stage === "rejected"', self.sections)
        self.assertIn('claimStage(sub).stage === "withdrawn"', self.sections)

    def test_a_claim_you_took_back_is_not_filed_as_one_they_refused(self):
        self.assertNotIn('["rejected", "withdrawn"].includes', self.sections)

    def test_the_rows_are_bucketed_by_the_same_list(self):
        body = self.app.split("function renderMine() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("MINE_SECTIONS.forEach(([id, , holds]) => { buckets[id] = mine.filter(holds); });",
                      body)
        self.assertIn("const shown = buckets[mineSection] || buckets.pending;", body)

    def test_the_finished_tabs_are_badged_quietly(self):
        # The red badge is for work somebody still has to do. A count of
        # finished things in an alarm colour is an alarm about nothing.
        tabs = self.app.split("function renderMineTabs(buckets) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn('flag.className = "navflag" + (id === "pending" ? "" : " count");',
                      tabs)
        css = self.app.split("<style>", 1)[1].rsplit("</style>", 1)[0]
        self.assertIn(".navflag.count { background:var(--surface-2); color:var(--muted);",
                      css)

    def test_each_tab_has_its_own_empty_state(self):
        body = self.app.split("function renderMine() {", 1)[1].split("\n}", 1)[0]
        self.assertIn('mineSection === "settled" ?', body)
        self.assertIn('mineSection === "rejected" ?', body)
        self.assertIn("Nothing of yours has been refused.", body)

    def test_stepping_follows_the_tab_you_are_on(self):
        sibs = self.app.split("function siblingsFor(from) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("MINE_SECTIONS.find(([id]) => id === mineSection)", sibs)
        # And falls back to the first rather than to nothing, so Previous and
        # Next never vanish because of a stale tab id.
        self.assertIn("|| MINE_SECTIONS[0])[2]", sibs)



class OneFailedRefreshIsNotTheLastOne(unittest.TestCase):
    """Madhusudhan settled three claims from a list of four while six waited.

    `refreshNow` set `refreshing = true`, awaited three requests, and cleared
    the flag afterwards - with nothing in between to survive a rejection. Any
    failure in there (a dropped connection, a timed-out request, a token that
    expired mid-flight) left the flag set for the life of the page, and the
    first line of the function returns early while it is set. So nothing
    refreshed again: not the poll, not the click, not the return to the tab.

    The page went on working perfectly against data that had stopped arriving.
    The figures were internally consistent and quietly out of date, and the
    two claims he could not see were the two approved after his last
    successful load.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()
        self.fn = self.app.split("async function refreshNow() {", 1)[1].split(
            "\n}", 1)[0]

    def test_the_flag_is_cleared_however_it_ends(self):
        self.assertIn("} finally {", self.fn)
        tail = self.fn.split("} finally {", 1)[1]
        self.assertIn("refreshing = false;", tail)
        self.assertIn("paintRefreshedAt();", tail)

    def test_and_the_early_return_is_what_made_it_permanent(self):
        # Worth pinning together: the guard is right, and it is only safe
        # because the flag is now guaranteed to clear.
        self.assertIn("if (refreshing || !sessionToken()) return;", self.fn)

    def test_a_failure_is_said_rather_than_swallowed(self):
        # A list that has stopped updating looks exactly like a quiet one.
        self.assertIn("loadFailed = true;", self.fn)
        self.assertIn("loadFailed = false;", self.fn)
        paint = self.app.split("function paintRefreshedAt(", 1)[1].split("\n}", 1)[0]
        self.assertIn('loadFailed ? "not up to date"', paint)

    def test_the_warning_has_a_class_of_its_own(self):
        # `.stale` is taken on this same button and means a newer version of
        # the console has shipped. Two meanings on one class is how the
        # spinner once animated the header's static glyph for ever.
        paint = self.app.split("function paintRefreshedAt(", 1)[1].split("\n}", 1)[0]
        self.assertIn('classList.toggle("offline"', paint)
        self.assertIn(".btn.refresh.offline {", self.app)
        self.assertIn(".btn.refresh.stale {", self.app)
