"""A new page opens at the top of itself.

A route change in an SPA is not a document load, so nothing resets the
scroll: the incoming page inherits wherever the outgoing one was left.
Scroll a long Transactions list down, click the logo, and Today opens
hundreds of pixels in with its verdict off-screen.

Source assertions, because there is no DOM here. That makes them worth
writing carefully — a source grep that stops matching is a test that
asserts nothing and reports success. Each one below anchors on the
smallest thing that would actually have to change for the behaviour to be
wrong.
"""

import pathlib
import re
import unittest

APP = pathlib.Path(__file__).resolve().parents[2] / "webapp" / "src" / "App.tsx"


class ScrollToTop(unittest.TestCase):

    def setUp(self):
        self.app = APP.read_text()
        m = re.search(r"function ScrollToTop\(\) \{.*?\n\}", self.app, re.S)
        self.assertIsNotNone(m, "ScrollToTop() is gone")
        self.body = m.group(0)

    def test_it_is_actually_mounted(self):
        """The component existing proves nothing. It renders null, so a
        stray edit that drops the element loses the behaviour in silence
        and every other test here still passes."""
        self.assertIn("<ScrollToTop />", self.app)

    def test_it_scrolls_the_window_not_a_wrapper(self):
        """Measured on the running page: main is overflow:visible with
        scrollHeight == clientHeight, so the document is the scroller.
        Resetting a wrapper's scrollTop would move nothing at all."""
        self.assertRegex(self.body, r"window\.scrollTo\(\{[^}]*top: 0")

    def test_back_and_forward_keep_their_position(self):
        """POP is the browser restoring where you were, which is most of
        what makes Back feel like going back rather than re-opening the
        page. Forcing those to the top is the same defect inverted."""
        self.assertIn("POP", self.body)
        self.assertRegex(self.body, r"if \(navType === \"POP\"[^)]*\) return;")
        self.assertIn("useNavigationType", self.app)

    def test_an_anchor_target_is_left_alone(self):
        """A hash is an explicit request for a position on the page; the
        reset would fight it and win."""
        self.assertRegex(self.body, r"navType === \"POP\" \|\| hash")
        self.assertIn("hash", re.search(r"useEffect\(.*?\}, \[([^\]]*)\]",
                                        self.body, re.S).group(1))

    def test_the_jump_is_not_animated(self):
        """smooth drags the OUTGOING page's content up the screen on every
        click, so the app reads as lurching rather than as changing page."""
        self.assertNotIn('behavior: "smooth"', self.body)
        self.assertIn('behavior: "instant"', self.body)

    def test_choosing_the_page_you_are_already_on_still_scrolls(self):
        """pathname does not change, so the effect never runs — and the
        logo while already on Today is precisely the click someone makes
        in order to get back to the top. Handled in the bar's click
        handler."""
        bar = self.app[self.app.index('<div className="bar"'):
                       self.app.index('<div className="topline">')]
        self.assertIn("window.scrollTo", bar)
        # not for the top bar's own links (sync, bell, help, identity) —
        # they are painted outside the rail and one of them is a menu
        self.assertIn('!link.closest(".rt")', bar)


if __name__ == "__main__":
    unittest.main()
