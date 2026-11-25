"""The one-off rule judges the PURCHASE a merchant's largest charge makes
in the pace window, never the merchant's whole set of rows: a deli the
household visits every week keeps its visits in the pace when one large
catering order there leaves; a diagnostic and the repair a week later at
a shop seen for nothing else leave together; and a purchase of that size
made at the same place a year before is a habit, so it stays. Figures
and names are invented."""

import datetime as dt
import unittest

from oikonome.engine import forecast

from .util import TODAY, add_txn, make_db, write_config


def _household():
    conn = make_db()
    write_config(conn, food_monthly=600, other_monthly=900)
    d = TODAY - dt.timedelta(days=70)
    while d < TODAY:
        add_txn(conn, d, 30.0, "MAPLE GROCER", primary="FOOD_AND_DRINK")
        d += dt.timedelta(days=1)
    return conn


class TheOutlierLeavesTheVisitsStay(unittest.TestCase):
    def test_a_weekly_deli_keeps_its_visits_when_a_large_order_leaves(self):
        conn = _household()
        try:
            d = TODAY - dt.timedelta(days=200)
            while d < TODAY:
                add_txn(conn, d, 18.0, "ELM STREET DELI",
                        primary="FOOD_AND_DRINK")
                d += dt.timedelta(days=7)
            add_txn(conn, TODAY - dt.timedelta(days=20), 950.0,
                    "ELM STREET DELI", primary="FOOD_AND_DRINK",
                    txn_id="catering")
            fp = forecast.forecast_pace(conn, TODAY)
        finally:
            conn.close()
        self.assertEqual([(o["txn_id"], o["amount"]) for o in fp["one_offs"]],
                         [("catering", 950.0)])
        kept = sum(r["amount"] for r in fp["rows"]) - 950.0
        self.assertAlmostEqual(fp["daily"], kept / fp["days"], places=6)
        # the deli's weekly visits are still in the pace
        visits = [r for r in fp["rows"]
                  if r["payee"] == "ELM STREET DELI"
                  and r["txn_id"] != "catering"]
        self.assertGreaterEqual(len(visits), 8)

    def test_a_diagnostic_and_the_repair_leave_together(self):
        conn = _household()
        try:
            add_txn(conn, TODAY - dt.timedelta(days=30), 90.0,
                    "ROUTE 9 AUTO", txn_id="diag")
            add_txn(conn, TODAY - dt.timedelta(days=25), 1400.0,
                    "ROUTE 9 AUTO", txn_id="repair")
            fp = forecast.forecast_pace(conn, TODAY)
        finally:
            conn.close()
        self.assertEqual([(o["txn_id"], o["amount"]) for o in fp["one_offs"]],
                         [("repair", 1490.0)])

    def test_a_comparable_purchase_a_year_before_is_a_habit(self):
        conn = _household()
        try:
            add_txn(conn, TODAY - dt.timedelta(days=380), 1300.0,
                    "OAK FURNISHINGS")
            add_txn(conn, TODAY - dt.timedelta(days=340), 1250.0,
                    "OAK FURNISHINGS")
            add_txn(conn, TODAY - dt.timedelta(days=15), 1200.0,
                    "OAK FURNISHINGS")
            fp = forecast.forecast_pace(conn, TODAY)
        finally:
            conn.close()
        self.assertEqual(fp["one_offs"], [])

    def test_small_visits_are_no_habit_for_a_purchase_ten_times_their_size(
            self):
        conn = _household()
        try:
            d = TODAY - dt.timedelta(days=300)
            while d < TODAY - dt.timedelta(days=60):
                add_txn(conn, d, 60.0, "BIRCH HARDWARE")
                d += dt.timedelta(days=10)
            add_txn(conn, TODAY - dt.timedelta(days=12), 800.0,
                    "BIRCH HARDWARE", txn_id="mower")
            fp = forecast.forecast_pace(conn, TODAY)
        finally:
            conn.close()
        self.assertEqual([o["txn_id"] for o in fp["one_offs"]], ["mower"])
