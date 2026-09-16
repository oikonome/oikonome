"""Settings two-pane layout invariants that only the source can answer."""

import unittest


class PhoneSearchKeepsTheListTests(unittest.TestCase):
    """On a phone the list and the body are two screens, and the search box
    lives in the list. A narrow-layout rule that hides the list as soon as
    a search is running unmounts the pane holding the focused input on the
    first keystroke: the box disappears and the keyboard closes, making
    search unusable on the device it matters most on."""

    def _src(self):
        import pathlib
        import oikonome
        p = (pathlib.Path(oikonome.__file__).parent.parent.parent
             / "webapp" / "src" / "pages" / "Settings.tsx")
        if not p.exists():
            self.skipTest("webapp/ not present")
        return p.read_text()

    def test_the_list_pane_survives_a_running_search(self):
        src = self._src()
        self.assertIn("const showSide = !narrow || !active || searching;", src,
                      "the list pane must stay mounted while searching, or "
                      "the search input unmounts itself on the first "
                      "keystroke")

    def test_the_body_pane_waits_for_a_picked_section(self):
        src = self._src()
        self.assertIn(
            'const showBody = !narrow || (active !== "" && !searching);', src,
            "on a phone the body must not stack under the list mid-search")
