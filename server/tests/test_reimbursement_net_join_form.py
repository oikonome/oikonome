"""The join form of the partial-reimbursement net equals the subquery form.

reporting.NET_AMOUNT nets partial reimbursements off an expense with a
correlated subquery; the full-scan aggregates use NET_AMOUNT_JOINED over
REIMB_PARTIAL_JOIN instead. They must agree row for row — including the
floor at zero for an over-reimbursed charge, an expense with several
partials, rows with no reimbursement at all, and a FULL (partial = 0) pair,
which neither form may net — or the Spending/Cash Flow totals would drift
from the verdict's spend rows.

Also pins the one-pass income query: _income_year_month must report exactly
what the two single-grain _income_by calls report.
"""
import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import TODAY, add_txn, make_db, write_config


class NetAmountFormsAgreeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        c = self.conn
        add_txn(c, TODAY, 400.0, "DENTIST", txn_id="exp-two-partials")
        add_txn(c, TODAY, 80.0, "PHARMACY", txn_id="exp-over-reimbursed")
        add_txn(c, TODAY, 25.0, "COFFEE", txn_id="exp-untouched")
        add_txn(c, TODAY, 60.0, "DINNER", txn_id="exp-full-pair")
        for rid in ("rb-1", "rb-2", "rb-3", "rb-4"):
            add_txn(c, TODAY, -10.0, "REIMB", txn_id=rid, primary="INCOME")
        c.execute("INSERT INTO reimbursements (expense_id, reimburse_id, partial, amount) "
                  "VALUES ('exp-two-partials','rb-1',1,150), "
                  "       ('exp-two-partials','rb-2',1,100), "
                  "       ('exp-over-reimbursed','rb-3',1,500), "
                  "       ('exp-full-pair','rb-4',0,60)")

    def tearDown(self):
        self.conn.close()

    def test_row_by_row(self):
        sub = {r["id"]: r["net"] for r in self.conn.execute(
            f"SELECT t.id, {reporting.NET_AMOUNT} AS net FROM transactions t "
            f"WHERE t.amount > 0")}
        joined = {r["id"]: r["net"] for r in self.conn.execute(
            f"SELECT t.id, {reporting.NET_AMOUNT_JOINED} AS net "
            f"FROM transactions t {reporting.REIMB_PARTIAL_JOIN} WHERE t.amount > 0")}
        self.assertEqual(joined, sub)
        self.assertEqual(sub["exp-two-partials"], 150.0)      # 400 − 150 − 100
        self.assertEqual(sub["exp-over-reimbursed"], 0.0)     # floored, never −420
        self.assertEqual(sub["exp-untouched"], 25.0)
        self.assertEqual(sub["exp-full-pair"], 60.0)          # full pairs are not netted

    def test_aggregate_under_spend_where(self):
        sub = self.conn.execute(
            f"SELECT SUM({reporting.NET_AMOUNT}) AS s FROM transactions t "
            f"WHERE {reporting.SPEND_WHERE}").fetchone()["s"]
        joined = self.conn.execute(
            f"SELECT SUM({reporting.NET_AMOUNT_JOINED}) AS s "
            f"FROM transactions t {reporting.REIMB_PARTIAL_JOIN} "
            f"WHERE {reporting.SPEND_WHERE}").fetchone()["s"]
        self.assertAlmostEqual(joined, sub, places=6)
        self.assertAlmostEqual(sub, 150.0 + 0.0 + 25.0 + 60.0, places=6)

    def test_spending_report_nets_partials(self):
        sp = reporting.compute_spending(self.conn)
        by_year = {y: amt for y, amt, _n in sp["by_year"]}
        self.assertEqual(by_year[str(TODAY.year)], 235.0)


class IncomeOnePassTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for i in range(8):
            add_txn(self.conn, TODAY - dt.timedelta(days=40 * i), -2500.005,
                    "ACME PAYROLL", account="chk", primary="INCOME",
                    txn_id=f"pay-{i}")

    def tearDown(self):
        self.conn.close()

    def test_matches_single_grain_queries(self):
        years, months = reporting._income_year_month(self.conn)
        self.assertEqual(years, {r["p"]: r["amt"]
                                 for r in reporting._income_by(self.conn, "year")})
        self.assertEqual(months, {r["p"]: r["amt"]
                                  for r in reporting._income_by(self.conn, "month")})
        self.assertTrue(years and months)


if __name__ == "__main__":
    unittest.main()
