"""The plan-CSV window-replace prune retires only rows the plan door wrote.

A daily CSV restates every date it carries in full, so rows on those dates
that the CSV no longer reports are soft-retired. That prune used to key on
account + date alone: a transaction some OTHER importer (CSV/OFX, a manual
entry) had landed on the same plan account for a restated date was retired
too, and the holdings recomputation dropped its shares. The coinbase door
already namespaces its prune by id prefix; the plan door does the same.
"""

import unittest

from oikonome.sync import plan_csv

from .util import make_db

HEADER = ("Trade Date,Investments,Ticker,Transaction,"
          "Transaction Amount,Share Price,Total shares")

CSV = f"""{HEADER}
07/01/2025,Vanguard 500 Idx,VFIAX,Contribution,"1,000.00",100.00,10.000
06/15/2025,Vanguard 500 Idx,VFIAX,Contribution,500.00,100.00,5.000
"""


class PlanCsvPruneNamespaceTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_foreign_row_on_a_restated_date_survives(self):
        plan_csv.import_csv(self.conn, "401A", CSV)
        # a row another importer put on the plan account, on a date the
        # next plan CSV restates
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                                         pending, removed)
               VALUES ('ofx:plan-401a:x1', 'plan-401a', '2025-07-01',
                       -25, 'Manual adjustment', 0, 0)""")
        plan_csv.import_csv(self.conn, "401A", CSV)     # daily replay
        row = self.conn.execute(
            "SELECT removed FROM transactions WHERE id='ofx:plan-401a:x1'"
        ).fetchone()
        self.assertEqual(row["removed"], 0)

    def test_own_dropped_row_is_still_retired(self):
        plan_csv.import_csv(self.conn, "401A", CSV)
        thinner = f"""{HEADER}
07/01/2025,Vanguard 500 Idx,VFIAX,Contribution,"1,000.00",100.00,10.000
06/15/2025,Vanguard 500 Idx,VFIAX,Contribution,400.00,100.00,4.000
"""
        plan_csv.import_csv(self.conn, "401A", thinner)
        live = self.conn.execute(
            "SELECT amount FROM transactions WHERE id LIKE 'plan:%' "
            "AND date='2025-06-15' AND removed=0").fetchall()
        self.assertEqual([float(r["amount"]) for r in live], [-400.0])


if __name__ == "__main__":
    unittest.main()
