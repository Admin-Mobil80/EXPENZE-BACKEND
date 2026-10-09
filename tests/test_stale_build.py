"""Telling somebody the page they are looking at is out of date.

The console is a single-page app. Moving between People and Reports and back
never re-fetches app.html, so a person can sit on one build for days: a fix
ships, the server starts answering differently, and the page carries on
rendering with the code it loaded that morning.

That is indistinguishable from a bug to the person seeing it, which is the
expensive part - a removed colleague still listed, a tab that was added and
isn't there, counts that do not match the database. All true of the page, none
true of the product, and no way to tell from the inside.

The page's own ETag is the version. Nothing to generate and nothing to keep in
step with a build: CloudFront hands one out already and changes it whenever the
file changes.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


class TheConsoleNoticesItIsOld(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()
        self.fn = self.app.split("async function checkBuild(", 1)[1].split("\n}", 1)[0]

    def test_it_runs_on_a_timer_of_its_own(self):
        # It rode the data refresh, which fires only on clicking Refresh or
        # returning to the tab - so somebody working in one window for an
        # afternoon was never told, which is precisely the person most likely
        # to be looking at an old build.
        self.assertIn("setInterval(checkBuild,", self.app)

    def test_the_baseline_is_taken_at_boot_not_on_the_first_refresh(self):
        # Otherwise the first refresh after a deploy is spent establishing a
        # baseline the page could have had all along, and the reader hears
        # about the release one refresh later than they could have.
        boot = self.app.rsplit("setInterval(paintRefreshedAt", 1)[1]
        self.assertIn("checkBuild();", boot)
        # Before the session guard, which is `requireSession()` now that
        # signing out actually ends a session and the back button has to be
        # asked the same question.
        self.assertLess(boot.index("checkBuild();"), boot.index("if (requireSession())"))

    def test_the_data_refresh_still_asks_too(self):
        body = self.app.split("async function refreshNow(", 1)[1].split("\n}", 1)[0]
        self.assertIn("checkBuild()", body)

    def test_it_asks_with_a_head_request_and_no_cache(self):
        probe = self.app.split("async function buildOf(", 1)[1].split("\n}", 1)[0]
        self.assertIn('method: "HEAD"', probe)
        self.assertIn('cache: "no-store"', probe)

    def test_the_first_answer_is_the_current_build_not_a_change(self):
        # Otherwise every page would announce itself as stale on first refresh.
        self.assertIn("if (!loadedBuild) { loadedBuild = now; return; }", self.fn)

    def test_a_failed_probe_says_nothing(self):
        # A banner claiming the page is old, shown because the network
        # hiccupped, is worse than not asking.
        probe = self.app.split("async function buildOf(", 1)[1].split("\n}", 1)[0]
        self.assertIn('return "";', probe)
        self.assertIn("if (!now) return;", self.fn)

    def test_dismissing_it_is_not_silencing_it_for_ever(self):
        # "Not now" must mean this release. A further one is a new fact and
        # speaks up again.
        self.assertIn("now !== dismissedBuild", self.fn)
        self.assertIn("dismissedBuild = latestBuild;", self.app)

    def test_it_can_be_dismissed_at_all(self):
        # Somebody part way through editing a policy has to be able to finish
        # and save before reloading. A wall would lose their work.
        self.assertIn('id="stale-dismiss"', self.app)
        self.assertIn("location.reload()", self.app)

    def test_it_sits_above_the_tabs(self):
        # It is about the whole console, not whichever screen is open.
        self.assertLess(self.app.index('id="stale-build"'),
                        self.app.index('<nav class="nav" id="nav">'))

    def test_it_starts_hidden(self):
        bar = self.app.split('id="stale-build"', 1)[1].split(">", 1)[0]
        self.assertIn("hidden", bar)


if __name__ == "__main__":
    unittest.main()


class TheServerMovingIsADifferentFactFromThePageMoving(unittest.TestCase):
    """The page's own ETag says nothing about the API.

    `checkBuild` catches "you are running yesterday's JavaScript". A backend
    deploy leaves a page that is genuinely current holding data that silently
    is not - the float figures, a budget total, a claim's stage - with no
    prompt either way. The only reliable move was a hard refresh of something
    that did not need refreshing, which is what somebody actually did.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()
        with open(os.path.join(ROOT, "lambda_src/auth.py"), encoding="utf-8") as h:
            self.auth = h.read()
        with open(os.path.join(ROOT, "expensifyai/stack.py"), encoding="utf-8") as h:
            self.stack = h.read()

    def test_every_answer_carries_the_build_that_produced_it(self):
        # On the reply rather than in the body: every endpoint goes through
        # `_reply`, so there is one place to set it and nothing added to a
        # payload shape callers parse.
        self.assertIn('headers["X-Expenze-Build"] = BUILD_ID', self.auth)
        self.assertIn('BUILD_ID = os.environ.get("BUILD_ID", "")', self.auth)

    def test_an_unset_build_sends_no_header_at_all(self):
        # Rather than an empty one, which the console would have to special
        # case into meaning nothing.
        self.assertIn("if BUILD_ID:", self.auth)

    def test_the_browser_is_allowed_to_read_it(self):
        # A response header is invisible to cross-origin JavaScript unless it
        # is named here, and the console is served from a different origin -
        # so the header would arrive, be dropped, and the check would silently
        # never fire.
        self.assertIn('"Access-Control-Expose-Headers": "X-Expenze-Build"', self.auth)

    def test_the_build_changes_when_the_code_does_and_not_otherwise(self):
        # A deploy that alters a table's throughput must not tell everybody
        # their figures are stale, and two deploys of identical code agree.
        self.assertIn("def _build_id(", self.stack)
        self.assertIn('"BUILD_ID": API_BUILD,', self.stack)
        fn = self.stack.split("def _build_id(", 1)[1].split("\nAPI_BUILD", 1)[0]
        self.assertIn("hashlib.sha256()", fn)
        self.assertIn("sorted(root.rglob", fn)       # not filesystem order
        self.assertIn("path.relative_to(root)", fn)  # a deletion registers
        self.assertIn('suffix == ".pyc"', fn)        # a stale cache is not a release

    def test_the_console_notes_it_on_every_call(self):
        call = self.app.split("async function authCall(", 1)[1].split("\n}", 1)[0]
        self.assertIn('noteApiBuild(res.headers.get("X-Expenze-Build"));', call)

    def test_the_first_build_seen_is_a_baseline_not_a_change(self):
        # Otherwise every console announces itself as stale on its first call.
        fn = self.app.split("function noteApiBuild(build) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (!apiBuild) { apiBuild = build; return; }", fn)

    def test_a_missing_header_says_nothing(self):
        fn = self.app.split("function noteApiBuild(build) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (!build) return;", fn)

    def test_the_two_reasons_ask_for_different_things(self):
        """A new page has to be fetched. New data behind the same page does not.

        Telling somebody to reload when a refresh would do is how a product
        teaches people to hard-refresh at every surprise - which is what was
        happening, because nothing said which kind of change had shipped.
        """
        fn = self.app.split("function paintStaleBuild() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("const pageMoved = !!latestBuild && latestBuild !== loadedBuild;",
                      fn)
        self.assertIn('act.textContent = pageMoved ? "Reload" : "Refresh"', fn)
        self.assertIn("The server has been updated.", fn)

    def test_and_the_button_does_whichever_it_is(self):
        act = self.app.split('$("stale-reload").addEventListener("click", () => {',
                             1)[1].split("});", 1)[0]
        self.assertIn("if (latestBuild && latestBuild !== loadedBuild) return location.reload();",
                      act)
        self.assertIn("refreshNow();", act)

    def test_dismissing_one_does_not_silence_the_other(self):
        act = self.app.split('$("stale-dismiss").addEventListener("click", () => {',
                             1)[1].split("});", 1)[0]
        self.assertIn("dismissedBuild = latestBuild;", act)
        self.assertIn("apiBuildDismissed = apiBuildLatest;", act)

    def test_a_refresh_moves_the_baseline_on(self):
        # Otherwise the bar comes straight back after the refresh that
        # answered it.
        act = self.app.split('$("stale-reload").addEventListener("click", () => {',
                             1)[1].split("});", 1)[0]
        self.assertIn("apiBuild = apiBuildLatest || apiBuild;", act)
