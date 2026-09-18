"""A button that rendered, looked live, and did nothing.

"Open full size" was an `<a target="_blank">` pointing at the receipt's
presigned S3 URL. That link had to go: the URL authorises itself for as long
as its signature lasts, so opening it in a tab puts a self-authorising link to
a colleague's receipt in the address bar, in browser history, in anything
syncing that history, and one copy-paste from somebody with no account.

It became a `<button>` labelled Zoom in, and the line that had set its `href`
stayed exactly as it was:

    $("r-open").href = currentOriginal ? currentOriginal.url : "#";

Assigning `.href` to a button sets a property nothing reads. No click handler
replaced it, so the control was drawn on every claim, looked like every other
control, and was inert.

The shape of that mistake is worth naming: a change of element type silently
invalidates every line that spoke to the old one, and the compiler here is a
person. Nothing failed, nothing logged, and the only way to find it was to
press it.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def code_only(js):
    """The source with its prose removed.

    The comment explaining why `$("r-open").href` is gone contains
    `$("r-open").href`. Sixth time.
    """
    js = re.sub(r"/\*[\s\S]*?\*/", " ", js)
    return re.sub(r"(?m)^\s*//[^\n]*", " ", js)


class TheControlIsWiredToSomething(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.paint = self.app.split("function paintOriginal(sub, retried) {",
                                    1)[1].split("\n}", 1)[0]

    def test_it_opens_the_viewer(self):
        self.assertIn("openLightbox(original, sub.vendor", self.paint)

    def test_the_dead_href_assignment_is_gone(self):
        self.assertNotIn('$("r-open").href', code_only(self.app))

    def test_it_is_a_button_and_not_a_link(self):
        # The whole reason the handler was needed.
        self.assertIn('<button type="button" class="rlink" id="r-open" hidden>',
                      self.app)
        self.assertNotIn('<a id="r-open"', self.app)

    def test_nothing_opens_the_signed_url_in_a_tab(self):
        self.assertNotIn('target="_blank" rel="noopener" hidden>Open full size',
                         self.app)

    def test_it_is_dead_when_there_is_no_original_to_show(self):
        # Rather than opening an empty viewer over the page.
        self.assertIn("zoom.disabled = !original;", self.paint)
        self.assertIn(": null;", self.paint)

    def test_repainting_cannot_stack_two_handlers(self):
        # `paintOriginal` runs on every render and the button outlives the
        # claim. `onclick` replaces; `addEventListener` would accumulate.
        self.assertIn("zoom.onclick = original", self.paint)
        self.assertNotIn('zoom.addEventListener', self.paint)

    def test_a_pdf_says_what_it_actually_does(self):
        # The browser's own viewer opens for a PDF - it already zooms, scrolls
        # and prints - so labelling that button "Zoom in" would misdescribe it.
        self.assertIn('original.type === "application/pdf"', self.paint)
        self.assertIn('"Open full size" : "Zoom in"', self.paint)


class TheViewerZoomsAndPans(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.fn = self.app.split("function openLightbox(original, label) {",
                                 1)[1].split("\n}\n", 1)[0]

    def test_the_bounds_exist_and_are_enforced_in_one_place(self):
        self.assertIn("const ZOOM_MIN = 1, ZOOM_MAX = 8, ZOOM_STEP = 1.4;",
                      self.app)
        self.assertIn("next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next));",
                      self.fn)

    def test_every_way_in(self):
        for event in ("wheel", "dblclick", "pointerdown", "pointermove",
                      "pointerup"):
            self.assertIn(f'stage.addEventListener("{event}"', self.fn)

    def test_zoom_is_about_the_point_under_the_cursor(self):
        # Zooming about the centre makes the detail somebody is pointing at
        # slide away from them as they magnify it.
        self.assertIn("tx = px - (px - tx) * k;", self.fn)
        self.assertIn("ty = py - (py - ty) * k;", self.fn)

    def test_the_receipt_cannot_be_dragged_out_of_view(self):
        # A firm drag would otherwise fling it off the stage with no way back
        # but closing the viewer.
        self.assertIn("const maxX = Math.max(0, (w - stage.clientWidth) / 2);",
                      self.fn)
        self.assertIn("tx = Math.min(maxX, Math.max(-maxX, tx));", self.fn)

    def test_the_clamp_measures_the_size_it_is_about_to_be(self):
        # `getBoundingClientRect` reports the scale being replaced, so the
        # clamp would be one zoom step behind. `offsetWidth` ignores the
        # transform.
        self.assertIn("const w = img.offsetWidth * scale, h = img.offsetHeight * scale;",
                      self.fn)

    def test_at_rest_it_is_centred_and_cannot_be_panned(self):
        self.assertIn("if (scale === 1) { tx = 0; ty = 0; }", self.fn)
        self.assertIn("if (scale <= 1) return;", self.fn)

    def test_there_are_controls_for_people_who_do_not_scroll_on_images(self):
        for label in ('mk("−", "Zoom out"', 'mk("+", "Zoom in"',
                      'mk("Fit", "Fit the whole receipt"'):
            self.assertIn(label, self.fn)

    def test_and_a_keyboard(self):
        self.assertIn('if (e.key === "+" || e.key === "=")', self.fn)
        self.assertIn('if (e.key === "-" || e.key === "_")', self.fn)
        self.assertIn('if (e.key === "0")', self.fn)

    def test_escape_closes_it_and_unbinds(self):
        self.assertIn('if (e.key === "Escape") return dismiss();', self.fn)
        self.assertIn('document.removeEventListener("keydown", onKey)', self.fn)

    def test_a_pdf_keeps_the_browsers_own_viewer(self):
        # It already zooms, scrolls and prints. Reimplementing that over an
        # iframe would be worse at all three.
        branch = self.fn.split('if (original.type === "application/pdf") {',
                               1)[1].split("return;", 1)[0]
        self.assertIn("iframe", branch)
        self.assertNotIn("lbstage", branch)


if __name__ == "__main__":
    unittest.main()
