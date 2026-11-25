"""Beside each scenario's low point the server says what that walk still
holds on the cards that day (specs/today-figures.md, "Each low says what
the cards still hold"): "Pay on the due date" kept the most checking only
because it carried the most card debt, and nothing beside its low said
so. The chart's dated payment lines an installed phone labels "autopay"
are sent only for a card the household really pays by autopay. Figures
and names are invented."""

import unittest

from oikonome.web import report, todayview

from .test_each_card_is_paid_its_own_way import _household
from .util import TODAY

money = todayview.charts._money


class EachLow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        conn = _household(5, checking=12000)
        try:
            cls.st = report.gather(conn, TODAY)
        finally:
            conn.close()
        cls.fc = cls.st["forecast"]
        cls.ctx = todayview.build_context(cls.st)

    def test_every_low_says_what_its_walk_holds_on_the_cards(self):
        walks = {k: w for k, _l, w in todayview.CASH_SCENARIOS}
        said = 0
        for sc in self.ctx["scenarios"]:
            w = self.fc[walks[sc["key"]]]
            held = dict(w["cards_owed"])[sc["min_date"]]
            with self.subTest(scenario=sc["key"]):
                if held > 0.5:
                    said += 1
                    self.assertEqual(
                        sc["low_cards_note"],
                        f"with {money(held)} still on your cards then")
                else:
                    self.assertIsNone(sc["low_cards_note"])
        self.assertGreater(said, 0)

    def test_every_surface_prints_it_beside_the_low(self):
        _s, plain, html = report.build(self.st)
        for sc in self.ctx["scenarios"]:
            lo = next(x for x in self.ctx["fc_lows"]
                      if x["short"] == sc["key"])
            self.assertEqual(lo["cards_note"], sc["low_cards_note"])
            if sc["low_cards_note"]:
                # a low that ends its own sentence is joined without it
                self.assertIn(f"{sc['low_words'].rstrip('.')}, "
                              f"{sc['low_cards_note']}", plain)
                self.assertIn(sc["low_cards_note"], html)
        self.assertNotIn("., with", plain)

    def test_autopay_lines_name_only_autopay_cards(self):
        modes = {c["card"]: c["mode"] for c in self.fc["card_timing"]}
        self.assertIn("rounds", modes.values())
        for sc in self.ctx["scenarios"]:
            with self.subTest(scenario=sc["key"]):
                self.assertEqual(
                    {modes[name] for _d, _a, name in sc["marks"]} - {"autopay"},
                    set())
        self.assertEqual(
            {modes[name] for _d, _a, name
             in todayview.autopay_marks(self.fc, self.fc["pace_stmt"])}
            - {"autopay"}, set())
        # the due-date walk pays the round card on a due date too: it is
        # drawn as a due date, never as an autopay
        self.assertEqual(
            todayview.autopay_marks(self.fc, self.fc["pace_stmt"]),
            [m for m in self.fc["pace_stmt"]["marks"]
             if modes[m[2]] == "autopay"])
        due = next(sc for sc in self.ctx["scenarios"] if sc["key"] == "due")
        self.assertIn("Harbor card due", [m["label"] for m in due["due_marks"]])


class TwoCardsOneName(unittest.TestCase):
    def test_autopay_is_matched_by_card_not_by_name(self):
        conn = _household(5, checking=12000)
        try:
            # the autopay card shown under the round card's name
            conn.execute("UPDATE accounts SET display_name='Harbor card' "
                         "WHERE id='auto'")
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        fc = st["forecast"]
        due = fc["pace_due"]
        raw = len(due["marks"])
        got = todayview.autopay_marks(fc, due)
        # the round card's due-date payments are not autopays
        self.assertTrue(got)
        self.assertLess(len(got), raw)


if __name__ == "__main__":
    unittest.main()
