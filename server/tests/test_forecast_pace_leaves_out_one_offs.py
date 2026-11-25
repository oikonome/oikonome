"""A forecast that goes below $0 says when ("Cash runs out around ..."),
not that cash "gets tight", and its chart marks the same day in the
page's MM/DD/YY. The daily pace the 60-day walk burns leaves out the
month's single large one-off charges — at least five times the month's
median day, at least $200, from a merchant with no comparable charge
(a third of it or more) in the 180 days before — because one furniture order spread over the month's finished
days would otherwise be spent again every day for two months. A large
charge at a merchant the household uses, and a charge under the floor,
stay in the pace. Figures are invented."""

import datetime as dt
import unittest

from markupsafe import escape

from oikonome.engine import forecast
from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config


class OneOffPaceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=900)
        self.conn.execute("UPDATE accounts SET balance_available=2000, "
                          "balance_current=2000 WHERE id='chk'")
        # a market the household has spent that kind of money at before
        # the forecast's 60-day window
        add_txn(self.conn, dt.date(2026, 4, 20), 250.0, "HILLTOP MARKET",
                primary="FOOD_AND_DRINK")
        for day in range(1, 15):
            add_txn(self.conn, dt.date(2026, 7, day), 20.0, "CORNER CAFE",
                    primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 7, 5), 1800.0, "NORTHWIND GARAGE")
        add_txn(self.conn, dt.date(2026, 7, 8), 600.0, "HILLTOP MARKET",
                primary="FOOD_AND_DRINK")
        # new to the ledger but under the $200 floor
        add_txn(self.conn, dt.date(2026, 7, 9), 150.0, "PINE GIFT SHOP")
        add_bill(self.conn, "ACME PAYROLL", 500.0, income=True,
                 next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        add_bill(self.conn, "COUNTY TAX", 3000.0,
                 next_due=dt.date(2026, 8, 10),
                 last_seen=dt.date(2025, 8, 10), frequency="YEARLY")
        self.st = report.gather(self.conn, TODAY)
        self.fc = self.st["forecast"]

    def tearDown(self):
        self.conn.close()

    def test_only_the_new_large_charge_is_a_one_off(self):
        self.assertEqual([(o["amount"], o["date"])
                          for o in self.fc["one_offs"]],
                         [(1800.0, "2026-07-05")])

    def test_the_pace_leaves_it_out(self):
        # the forecast's window is the 60 finished days before today; the
        # ledger's only spending in it is this month's
        st = self.st
        self.assertEqual(self.fc["pace_basis"]["days"], 60)
        rate = ((st["variable_actual"] - (st.get("variable_today") or 0)
                 - 1800.0) / 60)
        self.assertAlmostEqual(self.fc["pace_stmt"]["rate"], rate, places=2)

    def test_a_forecast_below_zero_says_when_cash_runs_out(self):
        ctx = todayview.build_context(self.st)
        note = ctx["cash_notice"]
        neg = self.fc["pace_stmt"]["negative_date"]
        self.assertEqual(neg, "2026-08-10")
        self.assertEqual((note["state"], note["kind"], note["runs_out"]),
                         ("tight", "forecast", neg))
        self.assertTrue(note["text"].startswith(
            "Cash runs out around 08/10/26 (low "), note["text"])
        self.assertEqual(ctx["runs_out_label"], "runs out 08/10/26")
        self.assertIn("$1,800 (07/05/26)", ctx["pace_note"])
        _s, plain, html = report.build(self.st)
        for text in (note["text"], ctx["pace_note"]):
            self.assertIn(text, plain)
            self.assertIn(str(escape(text)), html)

    def test_the_rule_at_its_edges(self):
        # one charge at the floor's level from an unseen merchant: in
        self.assertEqual(forecast.ONE_OFF_MIN, 200.0)
        add_txn(self.conn, dt.date(2026, 7, 10), 200.0, "BIRCH HARDWARE")
        st = report.gather(self.conn, TODAY)
        got = {o["amount"] for o in forecast.one_off_charges(
            self.conn, st, TODAY)}
        self.assertEqual(got, {1800.0, 200.0})
        # today's charges are not in the pace, so never a one-off
        add_txn(self.conn, TODAY, 900.0, "QUARRY TOOLS")
        st = report.gather(self.conn, TODAY)
        self.assertNotIn(900.0, {o["amount"] for o in
                                 forecast.one_off_charges(self.conn, st,
                                                          TODAY)})


class EnvelopeOverspendStaysInThePace(unittest.TestCase):
    """An envelope's overspend is the household's ordinary spending, not a
    charge: its slices are never a one-off, merged or alone, and the pace
    keeps them. A genuine large charge beside them is still left out."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=900)
        self.conn.execute("UPDATE accounts SET balance_available=2000, "
                          "balance_current=2000 WHERE id='chk'")
        add_bill(self.conn, "Green Grocer", 100.0, bill_type="envelope")
        add_bill(self.conn, "Fuel Stop", 100.0, bill_type="envelope")
        for day in range(1, 15):
            add_txn(self.conn, dt.date(2026, 7, day), 20.0, "CORNER CAFE",
                    primary="FOOD_AND_DRINK")
        # $130 and $90 over their envelopes: $220 together, over the floor
        add_txn(self.conn, dt.date(2026, 7, 5), 230.0, "GREEN GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 7, 12), 190.0, "FUEL STOP")

    def tearDown(self):
        self.conn.close()

    def _overflow_slices(self, st):
        return [r for k in ("food", "other") for r in st["buckets"][k]["rows"]
                if r.get("overflow_of")]

    def test_two_envelope_overflows_are_no_one_off(self):
        st = report.gather(self.conn, TODAY)
        self.assertEqual(len(self._overflow_slices(st)), 2)
        self.assertEqual(forecast.one_off_charges(self.conn, st, TODAY), [])
        fc = st["forecast"]
        self.assertEqual(fc["one_offs"], [])
        rate = ((st["variable_actual"] - (st.get("variable_today") or 0))
                / st["pace_days"])
        self.assertAlmostEqual(fc["pace_stmt"]["rate"], rate, places=2)

    def test_a_genuine_large_new_charge_beside_them_still_counts(self):
        add_txn(self.conn, dt.date(2026, 7, 6), 1800.0, "NORTHWIND GARAGE")
        st = report.gather(self.conn, TODAY)
        got = forecast.one_off_charges(self.conn, st, TODAY)
        self.assertEqual([(o["amount"], o["date"]) for o in got],
                         [(1800.0, "2026-07-06")])
        self.assertTrue(all(isinstance(o["txn_id"], str) for o in got), got)


if __name__ == "__main__":
    unittest.main()
