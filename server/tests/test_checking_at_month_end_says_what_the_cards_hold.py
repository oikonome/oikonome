"""The month-end tile is checking at month end under the usual pattern,
with beneath it only facts that figure does not include
(specs/today-figures.md, "Checking at month end"): what the same walk
still holds on the cards that day and its next card payment after the
month. The bills-and-envelopes "still owed" line is not under it: part of
it is already out of the figure. Figures and names are invented."""

import unittest

from oikonome.web import report, todayview

from .test_every_view_marks_the_card_due_dates import _three_cards
from .util import TODAY


class CheckingAtMonthEnd(unittest.TestCase):
    def test_the_tile_says_what_the_cards_still_hold(self):
        conn = _three_cards(2)
        try:
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        fc = st["forecast"]
        ctx = todayview.build_context(st)
        me = ctx["month_end"]
        usual = fc["pace_usual"]
        held = dict(usual["cards_owed"])[me["date"]]
        self.assertGreater(held, 0.5)
        self.assertEqual(ctx["month_end_label"], "checking at month end")
        self.assertEqual(me["balance"], dict(usual["series"])[me["date"]])
        nxt = min(d for d, _a, _c in usual["outflows"] if d > me["date"])
        amt = sum(a for d, a, _c in usual["outflows"] if d == nxt)
        money = todayview.charts._money
        self.assertEqual(
            ctx["month_end_cards_note"],
            f"with {money(held)} still on your cards then · next card "
            f"payment {money(amt)} on {todayview.fmt_date(nxt)}")
        _s, plain, html = report.build(st)
        self.assertIn(ctx["month_end_cards_note"], plain)
        self.assertNotIn("still owed this month", plain)
        self.assertNotIn("still owed this month", html)

    def test_what_the_cards_hold_adds_up(self):
        conn = _three_cards(3)
        try:
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        fc = st["forecast"]
        for key in ("pace_usual", "pace_due", "pace_today"):
            w = fc[key]
            last = w["cards_owed"][-1][1]
            # today's debt plus what was charged to the cards, less what
            # the walk paid them: what is paid after the chart, and what
            # stays on a carried card
            with self.subTest(walk=key):
                self.assertAlmostEqual(
                    last, w["cards_after_horizon"] + w["stays_on_card"],
                    delta=0.05)


if __name__ == "__main__":
    unittest.main()
