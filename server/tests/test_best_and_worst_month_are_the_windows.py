"""Best and worst under "Saved, by month" are decided by the server over
the months of the chosen timeframe that are over (specs/counted-figures.md,
"Best and worst month are the window's"). Picked by each client from every
bar drawn, the month in progress — half a month of spending, a paycheck
still due — was named the worst month of windows that ended before it.
"""
import datetime as dt
import pathlib
import unittest

from oikonome.engine import reporting

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)
_ROOT = pathlib.Path(__file__).resolve().parents[2]
# saved per complete month, then the month in progress far below them all
SAVED = {6: 500.0, 7: 300.0, 8: 800.0, 9: -100.0, 10: 200.0}


class ExtremesAreTheWindows(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for month, saved in SAVED.items():
            add_txn(self.conn, dt.date(2026, month, 2), -2000.0,
                    "CLIENT DEPOSIT", account="chk", primary="INCOME")
            add_txn(self.conn, dt.date(2026, month, 10), 2000.0 - saved,
                    "Grocer", account="chk", primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 11, 2), -100.0, "CLIENT DEPOSIT",
                account="chk", primary="INCOME")
        add_txn(self.conn, dt.date(2026, 11, 5), 1000.0, "Grocer",
                account="chk", primary="FOOD_AND_DRINK")
        self.graph = reporting.compute_cashflow(self.conn, TODAY)["cash_graph"]

    def tearDown(self):
        self.conn.close()

    def test_the_month_in_progress_is_never_the_worst(self):
        self.assertEqual(self.graph["current"], ["2026-11", -900.0])
        for key in ("3m", "6m", "1y", "all"):
            self.assertNotEqual(self.graph["extremes"][key]["worst"][0],
                                "2026-11", key)

    def test_a_window_ranks_only_its_own_months(self):
        ex = self.graph["extremes"]
        self.assertEqual(ex["3m"], {"best": ["2026-08", 800.0],
                                    "worst": ["2026-09", -100.0]})
        self.assertEqual(ex["6m"]["best"], ["2026-08", 800.0])
        self.assertEqual(ex["6m"]["worst"], ["2026-09", -100.0])

    def test_one_month_is_neither_best_nor_worst(self):
        ex = self.graph["extremes"]
        for key in ("cur", "1m"):
            self.assertEqual(ex[key], {"best": None, "worst": None}, key)

    def test_neither_client_ranks_the_bars(self):
        for rel in ("webapp/src/pages/CashFlow.tsx",
                    "mobile/src/app/cashflow.tsx"):
            src = (_ROOT / rel).read_text()
            self.assertFalse("hist.reduce(" in src, rel)
            self.assertTrue("cash_graph.extremes" in src, rel)


if __name__ == "__main__":
    unittest.main()
