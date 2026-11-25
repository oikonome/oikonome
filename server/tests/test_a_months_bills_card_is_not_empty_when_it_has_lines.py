"""The Month view's bills card never says "No tracked bill activity"
under lines about the month's bills.

A closed month with a bill nobody paid prints the server's "not paid: …"
line; the web's empty rule looked only at payments, due entries and
envelopes, so it printed the empty sentence right below it. Both clients
count a planned or posted amount and the server's lines as activity. The
phone keys those lines by place: a weekly bill unpaid on two dates sends
the same line twice, and a list keyed by its text would drop one.
"""
import pathlib
import re
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src" / "pages" / "MonthLens.tsx"
_MOB = _APP / "mobile" / "src" / "app" / "month.tsx"
_EMPTY = "No tracked bill activity this month."


def _src(test: unittest.TestCase, p: pathlib.Path) -> str:
    if not p.exists():
        test.skipTest(f"{p} not present")
    return p.read_text()


def _rule_before_empty_line(src: str) -> str:
    """The condition that guards the empty sentence: the source between
    the card's last list and the sentence."""
    at = src.index(_EMPTY)
    return src[max(0, at - 900):at]


class BillsCardEmptyRuleTests(unittest.TestCase):
    def test_the_web_counts_plan_posted_and_the_servers_lines(self):
        rule = _rule_before_empty_line(_src(self, _WEB))
        for needs in ("d.bills.planned > 0", "d.bills.posted > 0",
                      "d.bills.notes"):
            self.assertIn(needs, rule)

    def test_the_phone_counts_the_same(self):
        # the phone's card is one ternary: its condition opens the card
        # and the empty sentence is the other branch
        src = _src(self, _MOB)
        start = src.index("{past ? null : (")
        self.assertLess(start, src.index(_EMPTY))
        rule = src[start:src.index(") ? (", start)]
        for needs in ("d.bills.planned > 0", "d.bills.posted > 0",
                      "d.bills.notes"):
            self.assertIn(needs, rule)

    def test_the_phone_keys_the_lines_by_place(self):
        src = _src(self, _MOB)
        m = re.search(r"\(d\.bills\.notes \?\? \[\]\)\.map\(\(([^)]*)\) => \(\s*"
                      r"<Text key=\{(\w+)\}", src)
        self.assertIsNotNone(m, "the notes list was not found")
        args = [a.strip() for a in m.group(1).split(",")]
        self.assertEqual(len(args), 2, "the list needs its index")
        self.assertEqual(m.group(2), args[1])


if __name__ == "__main__":
    unittest.main()
