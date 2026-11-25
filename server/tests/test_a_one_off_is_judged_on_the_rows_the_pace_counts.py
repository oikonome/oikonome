"""The forecast's one-off rule judges exactly the rows the daily pace
counts, posted or pending, and a merchant's rows inside the pace window as
one purchase: a small charge a few days before a big one at the same shop
(a diagnostic before a repair) is part of the purchase, not evidence of a
habit. A habit is an earlier charge outside the window that is at least a
third of the purchase — one in the 180 days before it, or two in the year.
A merchant visited every few days stays in the pace even when its visits
add up past the floor. Figures and names are invented."""

import datetime as dt
import unittest

from markupsafe import escape

from oikonome.engine import budget, forecast
from oikonome.web import report, todayview

from .util import TODAY, add_txn, make_db, write_config

BIG, SMALL, SOFA = 1420.0, 55.0, 1240.0


class OneOffRuleTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=900)
        self.conn.execute("UPDATE accounts SET balance_available=4000, "
                          "balance_current=4000 WHERE id='chk'")
        for day in range(1, 15):
            add_txn(self.conn, dt.date(2026, 7, day), 15.0, "MAPLE BAKERY",
                    primary="FOOD_AND_DRINK")

    def tearDown(self):
        self.conn.close()

    def _one_offs(self):
        st = budget.month_status(self.conn, TODAY)
        return st, forecast.one_off_charges(self.conn, st, TODAY)

    def test_a_pending_charge_in_the_pace_can_be_a_one_off(self):
        add_txn(self.conn, dt.date(2026, 7, 9), SOFA, "BLUEWATER SOFAS",
                pending=1)
        st, got = self._one_offs()
        self.assertEqual([(o["amount"], o["date"]) for o in got],
                         [(SOFA, "2026-07-09")])
        # the numerator counted it, and the walk's rate leaves it out
        self.assertIn(SOFA, [r["amount"] for k in ("food", "other")
                             for r in st["buckets"][k]["rows"]])
        fc = forecast.build(self.conn, TODAY)
        rate = budget.paced_daily(st) - SOFA / st["pace_days"]
        self.assertAlmostEqual(fc["pace_stmt"]["rate"], round(rate, 2),
                               places=2)

    def test_a_small_charge_before_a_big_one_is_part_of_it(self):
        add_txn(self.conn, dt.date(2026, 7, 3), SMALL, "GEARBOX AUTO")
        add_txn(self.conn, dt.date(2026, 7, 8), BIG, "GEARBOX AUTO")
        _st, got = self._one_offs()
        self.assertEqual([(o["amount"], o["date"]) for o in got],
                         [(BIG + SMALL, "2026-07-08")])

    def test_a_small_earlier_visit_outside_the_window_is_no_habit(self):
        add_txn(self.conn, dt.date(2026, 4, 2), SMALL, "GEARBOX AUTO")
        add_txn(self.conn, dt.date(2026, 7, 8), BIG, "GEARBOX AUTO")
        _st, got = self._one_offs()
        self.assertEqual([o["amount"] for o in got], [BIG])

    def test_a_comparable_visit_in_the_half_year_before_is_a_habit(self):
        add_txn(self.conn, dt.date(2026, 4, 2), 480.0, "GEARBOX AUTO")
        add_txn(self.conn, dt.date(2026, 7, 8), BIG, "GEARBOX AUTO")
        _st, got = self._one_offs()
        self.assertEqual(got, [])

    def test_two_comparable_visits_in_the_year_are_a_habit(self):
        add_txn(self.conn, dt.date(2025, 10, 2), 480.0, "GEARBOX AUTO")
        add_txn(self.conn, dt.date(2026, 7, 8), BIG, "GEARBOX AUTO")
        _st, got = self._one_offs()
        self.assertEqual([o["amount"] for o in got], [BIG])
        add_txn(self.conn, dt.date(2025, 11, 20), 510.0, "GEARBOX AUTO")
        _st, got = self._one_offs()
        self.assertEqual(got, [])

    def test_a_merchant_visited_every_few_days_stays_in_the_pace(self):
        for day in (2, 5, 8, 11, 14):
            add_txn(self.conn, dt.date(2026, 7, day), 90.0, "FRESHWAY FOODS",
                    primary="FOOD_AND_DRINK")
        _st, got = self._one_offs()
        self.assertEqual(got, [])

    def test_every_surface_names_both_shapes(self):
        add_txn(self.conn, dt.date(2026, 7, 3), SMALL, "GEARBOX AUTO")
        add_txn(self.conn, dt.date(2026, 7, 8), BIG, "GEARBOX AUTO")
        add_txn(self.conn, dt.date(2026, 7, 11), SOFA, "BLUEWATER SOFAS",
                pending=1)
        st = report.gather(self.conn, TODAY)
        ctx = todayview.build_context(st)
        note = ctx["pace_note"]
        self.assertIn("$1,475 (07/08/26)", note)
        self.assertIn("$1,240 (07/11/26)", note)
        self.assertEqual(note.lower().count("gearbox"), 1, note)
        _s, plain, html = report.build(st)
        self.assertIn(note, plain)
        self.assertIn(str(escape(note)), html)



class TiesAreDecidedInCents(unittest.TestCase):
    """The one-off rule's ties are decided in whole cents: two equal rows
    at one merchant (the lead exactly half the total) and an earlier
    charge exactly a third of the total."""

    def test_two_equal_rows_lead_exactly_half(self):
        rows = [{"date": dt.date(2026, 7, d), "amount": 0.1 + 0.2 + 2744.7,
                 "txn_id": f"t{d}", "payee": "TWIN SHOP",
                 "merchant_id": None} for d in (3, 4)]
        got = forecast.window_one_offs(None, rows, dt.date(2026, 7, 1), 14,
                                       habit_of=lambda *a: (0, 0))
        self.assertEqual([o["amount"] for o in got], [5490.0])

    def test_an_earlier_third_is_a_habit(self):
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=900)
            add_txn(conn, dt.date(2026, 4, 2), 400.0, "GEARBOX AUTO")
            add_txn(conn, dt.date(2026, 7, 8), 1200.0, "GEARBOX AUTO")
            for day in range(1, 15):
                add_txn(conn, dt.date(2026, 7, day), 15.0, "MAPLE BAKERY",
                        primary="FOOD_AND_DRINK")
            st = budget.month_status(conn, TODAY)
            got = forecast.one_off_charges(conn, st, TODAY)
        finally:
            conn.close()
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
