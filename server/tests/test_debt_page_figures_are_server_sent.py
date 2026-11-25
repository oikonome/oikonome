"""The Debt page shows figures the server decided; it computes none.

"Saves $X vs minimums only" and "$Y more / less" under the other method
are differences of two schedules — server fields, so the web page and the
phone can never disagree on them or quote a truncated fifty-year total as
a saving. "Estimated" is the server's own flag, per debt and per plan: a
debt walked at a typical rate OR at an estimated minimum is an estimate.
And the box placeholders show what the plan uses when the household gives
nothing (`apr_default`, `min_default`), never a different figure chained
behind the issuer's.
"""

import unittest

from oikonome.engine import budget, debt
from oikonome.engine.compat import jsonb

from .util import TODAY, make_db, write_config


class DebtPageFigures(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()           # checking $5,000 · card $250
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,mask,"
            "balance_current) VALUES "
            "('auto','it1','Car Loan','loan','auto','0001',9000)")
        self.conn.execute(
            "INSERT INTO liabilities (account_id, raw) VALUES ('card', %s)",
            (jsonb({"aprs": [{"apr_type": "purchase_apr",
                              "apr_percentage": 24.0}],
                    "minimum_payment_amount": 35}),))

    def tearDown(self):
        self.conn.close()

    def rows(self):
        cfg = budget.load_config(self.conn)
        return {d["id"]: d for d in debt.load_debts(self.conn, cfg)}

    def test_each_debt_says_whether_it_is_an_estimate(self):
        rows = self.rows()
        self.assertFalse(rows["card"]["estimated"])
        self.assertTrue(rows["auto"]["estimated"])
        # an estimated minimum is an estimate too, whatever the rate
        write_config(self.conn, debt_plan={"debts": {"auto": {"apr": 6.0}}})
        auto = self.rows()["auto"]
        self.assertEqual((auto["apr_source"], auto["min_source"]),
                         ("you", "estimate"))
        self.assertTrue(auto["estimated"])
        write_config(self.conn, debt_plan={"debts": {
            "auto": {"apr": 6.0, "min_payment": 300}}})
        self.assertFalse(self.rows()["auto"]["estimated"])

    def test_the_default_figures_are_what_the_plan_uses_without_an_override(self):
        bare = self.rows()
        write_config(self.conn, debt_plan={"debts": {
            "card": {"apr": 18.0, "min_payment": 60},
            "auto": {"min_payment": 400}}})
        rows = self.rows()
        for k in ("card", "auto"):
            self.assertEqual(rows[k]["apr_default"], bare[k]["apr"], k)
            self.assertEqual(rows[k]["min_default"], bare[k]["min_payment"], k)
        self.assertEqual((rows["card"]["apr_default"],
                          rows["card"]["min_default"]), (24.0, 35.0))

    def test_savings_and_the_other_methods_difference_are_server_fields(self):
        r = debt.build(self.conn, TODAY, extra=200)
        self.assertEqual(r["saves_vs_minimums"],
                         round(r["minimums"]["total_interest"]
                               - r["plan"]["total_interest"], 2))
        self.assertEqual(r["alt_extra_interest"],
                         round(r["alt"]["total_interest"]
                               - r["plan"]["total_interest"], 2))

    def test_no_saving_is_quoted_against_minimums_that_never_finish(self):
        # $1 a month never covers the card's interest: the minimums-only
        # total is a fifty-year cut-off, not a cost to save against
        write_config(self.conn, debt_plan={"debts": {
            "card": {"min_payment": 1}, "auto": {"skip": True}}})
        r = debt.build(self.conn, TODAY, extra=100)
        self.assertFalse(r["minimums"]["finishes"])
        self.assertTrue(r["plan"]["finishes"])
        self.assertIsNone(r["saves_vs_minimums"])


if __name__ == "__main__":
    unittest.main()
