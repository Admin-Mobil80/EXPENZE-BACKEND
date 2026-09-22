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
