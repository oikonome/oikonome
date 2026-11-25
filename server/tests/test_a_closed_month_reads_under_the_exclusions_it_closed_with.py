"""Which accounts a closed month leaves out — one rule per page.

The Month and Year lenses judge a closed month AS IT CLOSED: its frozen
snapshot carries the budget, the bill schedule and the accounts excluded
at the time, and every figure the lens shows for that month — its spend,
its income, the net between them, the year's totals that sum the months,
the category rows and the drill-downs that open them — reads under that
same exclusion list. Excluding an account later does not reach back into
a report card that already closed.

Cash Flow is the other view: every month of its window reads under
TODAY's exclusions (an account excluded now leaves its whole history), so
its picture of the same month can differ, and that is intended
(tests/test_cashflow_closed_months_agree_with_the_month_lens.py pins it).

The failure this guards: the lens read a closed month's spend under the
snapshot's exclusions but its income under today's, so a checking account
excluded since kept its outflows in the month and lost its deposits — the
cell reported a loss that never happened, and the cells no longer summed
to the year's headline.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, reporting
from oikonome.web import lenses

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 6, 15)


class ClosedMonthExclusionTests(unittest.TestCase):
    def setUp(self):
        reporting._FIXED_MONTH_CACHE.clear()
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('chk2','it1','Second Checking',"
            "'depository','checking',800)")
        # a bank feed from January, so every month of the year is known
        add_txn(self.conn, dt.date(2025, 12, 28), 12.0, "Corner Grocer",
                account="chk")
        add_txn(self.conn, dt.date(2026, 3, 3), 41.0, "Corner Grocer",
                account="chk")
        for m in (1, 2, 3):
            add_txn(self.conn, dt.date(2026, m, 1), -3000.0, "Acme Payroll",
                    account="chk", primary="INCOME")
        add_txn(self.conn, dt.date(2026, 4, 1), -3000.0, "Acme Payroll",
                account="chk", primary="INCOME")
        add_txn(self.conn, dt.date(2026, 5, 1), -3000.0, "Acme Payroll",
                account="chk", primary="INCOME")
        add_txn(self.conn, dt.date(2026, 5, 2), -1000.0, "Side Gig Co",
                account="chk2", primary="INCOME")
        add_txn(self.conn, dt.date(2026, 5, 3), 130.0, "Rent Co",
                account="chk", primary="RENT_AND_UTILITIES")
        add_txn(self.conn, dt.date(2026, 5, 9), 40.0, "Quillsoft",
                account="card", primary="GENERAL_SERVICES")
        add_txn(self.conn, dt.date(2026, 5, 12), 500.0, "Corner Grocer",
                account="chk")
        add_txn(self.conn, dt.date(2026, 5, 20), 200.0, "Hilltop Market",
                account="chk2", primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 6, 1), -3000.0, "Acme Payroll",
                account="chk", primary="INCOME")
        add_txn(self.conn, dt.date(2026, 6, 4), 60.0, "Hilltop Market",
                account="chk2", primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 6, 5), 90.0, "Corner Grocer",
                account="chk")
        add_bill(self.conn, "Rent Co", 130.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 6, 3))
        add_bill(self.conn, "Quillsoft", 40.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 6, 9))
        # May closes with nothing excluded; in June the household excludes
        # the card and the second checking account
        budget.snapshot_month(self.conn, dt.date(2026, 5, 1))
        cfg = budget.load_config(self.conn)
        cfg["excluded_accounts"] = ["card", "chk2"]
        budget.save_config(self.conn, cfg)

    def tearDown(self):
        self.conn.close()
        reporting._FIXED_MONTH_CACHE.clear()

    def test_the_month_lens_reads_both_halves_under_the_snapshot(self):
        d = lenses.month_summary(self.conn, 2026, 5, TODAY)
        self.assertEqual(d["spend_total"], 870.0)
        self.assertEqual(d["income"]["actual"], 4000.0)

    def test_the_year_cell_and_totals_agree_with_the_months(self):
        y = lenses.year_summary(self.conn, 2026, TODAY)
        may = next(c for c in y["cells"] if c["m"] == 5)
        self.assertEqual(may["status"], "final")
        self.assertEqual((may["income"], may["spend"], may["net"]),
                         (4000.0, 870.0, 3130.0))
        june = next(c for c in y["cells"] if c["m"] == 6)
        # the live month under today's exclusions: chk2's $60 is out
        self.assertEqual(june["spend"], 90.0)
        shown = [c for c in y["cells"] if c["status"] != "future"]
        self.assertAlmostEqual(y["totals"]["spend"],
                               sum(c["spend"] for c in shown), places=2)
        self.assertAlmostEqual(y["totals"]["income"],
                               sum(c["income"] for c in shown), places=2)

    def test_category_rows_and_their_drill_downs_follow_the_months(self):
        y = lenses.year_summary(self.conn, 2026, TODAY)
        cats = {c[3]: c[1] for c in y["categories"]}
        # May's $200 at the second checking counts (May closed with it),
        # June's $60 does not (excluded now)
        self.assertEqual(cats.get("FOOD_AND_DRINK"), 200.0)
        self.assertEqual(cats.get("GENERAL_SERVICES"), 40.0)
        for key in ("FOOD_AND_DRINK", "GENERAL_SERVICES"):
            led = lenses.year_category_ledger(self.conn, key, 2026)
            self.assertEqual(led["amount"], cats[key], key)

    def test_the_yearly_detail_net_matches_the_cells(self):
        y = lenses.year_summary(self.conn, 2026, TODAY, with_detail=True)
        row = next(r for r in y["detail"]["years"] if r["y"] == 2026)
        self.assertAlmostEqual(row["spend"], y["totals"]["spend"], places=2)
        self.assertAlmostEqual(row["income"], y["totals"]["income"],
                               places=2)

    def test_cash_flow_reads_the_same_month_under_todays_exclusions(self):
        flow = reporting.flow_breakdown(self.conn, TODAY, "1m")
        self.assertEqual(flow["out"]["total"], 630.0)
        self.assertEqual(flow["out"]["fixed"], 130.0)
        self.assertEqual(flow["in"]["total"], 3000.0)




class AMalformedRestoredSnapshotIsNoMonth(unittest.TestCase):
    def test_a_negative_year_snapshot_does_not_break_the_lenses(self):
        import datetime as _dt
        from oikonome.web import lenses as _l
        from .util import make_db as _mk, write_config as _wc
        conn = _mk()
        self.addCleanup(conn.close)
        _wc(conn)
        conn.execute("INSERT INTO budget_snapshots (year, month, config) "
                     "VALUES (-1, 6, '{\"excluded_accounts\": [\"x\"]}')")
        _l.lens_excluded_sql(conn, _dt.date(2026, 6, 15),
                             since=_dt.date(2026, 1, 1))
        _l.lens_excluded_sql(conn, _dt.date(2026, 6, 15))


if __name__ == "__main__":
    unittest.main()
