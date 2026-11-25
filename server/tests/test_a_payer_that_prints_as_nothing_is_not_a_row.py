"""Income by source and Top merchants list payers; one whose window total
prints as $0 is not a row (specs/shown-figures.md, "A percent or a row that
says nothing is left out"). A few cents of interest read "$0 · 0% · 1× ·
unchanged" on a line of its own, with a paycheck control beside it.
"""
import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)


class RowsThatPrintNothing(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for month in (8, 9, 10):
            add_txn(self.conn, dt.date(2026, month, 2), -2000.0,
                    "CLIENT DEPOSIT", account="chk", primary="INCOME")
            add_txn(self.conn, dt.date(2026, month, 9), 300.0, "Grocer",
                    account="chk", primary="FOOD_AND_DRINK")
        # a few cents of interest, once
        add_txn(self.conn, dt.date(2026, 9, 30), -0.31, "SAVINGS INTEREST",
                account="chk", primary="INCOME",
                detailed="INCOME_INTEREST_EARNED")
        # a charge of a few cents, once
        add_txn(self.conn, dt.date(2026, 9, 12), 0.20, "Kite Shop",
                account="card", primary="GENERAL_MERCHANDISE")

    def tearDown(self):
        self.conn.close()

    def test_income_by_source_lists_only_payers_that_print_a_figure(self):
        w = reporting.income_window(self.conn, TODAY, "3m")
        self.assertEqual([r[0] for r in w["sources"]], ["CLIENT DEPOSIT"])
        # what is counted has not moved: the cents are still in the total
        self.assertEqual(w["total"], 6000.31)
        for sibling in ("source_changes", "source_shares",
                        "source_paychecks"):
            self.assertEqual(len(w[sibling]), len(w["sources"]), sibling)
        self.assertEqual(w["source_shares"], [100])

    def test_top_merchants_lists_only_payees_that_print_a_figure(self):
        w = reporting.spending_window(self.conn, TODAY, "3m")
        self.assertEqual([r[0] for r in w["merchants"]], ["Grocer"])
        self.assertEqual(len(w["merchant_changes"]), 1)
        self.assertEqual(w["total"], 900.2)


if __name__ == "__main__":
    unittest.main()
