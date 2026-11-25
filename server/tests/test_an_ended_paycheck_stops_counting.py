"""An income series whose schedule has ended is not current income.

A household that ends a paycheck series (the job ended, the pension moved
to another account) sets its end date and expects the monthly income
figure to stop counting it — even when the same payer's deposits keep
arriving (a final settlement, back pay, a stray transfer). The deposit
average and the stored-schedule fallback must agree: an ended series is
out of both, and a series ending next month still counts until it ends.
"""
import datetime as dt
import unittest

from oikonome.engine import budget

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 8)


class AnEndedPaycheckStopsCounting(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _payroll(self, payee, amount, end_on):
        d = dt.date(2025, 9, 1)
        while d <= TODAY:
            add_txn(self.conn, d, -amount, payee.upper(), primary="INCOME",
                    account="chk")
            d = (d + dt.timedelta(days=32)).replace(day=1)
        add_bill(self.conn, payee, amount, income=True,
                 next_due=dt.date(2026, 12, 1),
                 last_seen=dt.date(2026, 11, 1))
        self.conn.execute(
            "UPDATE bills SET raw = jsonb_set(raw, '{recurrence,endOn}', %s) "
            "WHERE payee = %s", (f'"{end_on.isoformat()}"', payee))
        self.conn.commit()

    def _payees(self):
        cfg = budget.load_config(self.conn)
        return {b["payee"] for b in budget.income_series(
            self.conn, cfg, today=TODAY)}

    def test_a_series_ended_two_months_ago_is_not_in_the_series(self):
        self._payroll("Quillfeather Payroll", 3000.0, dt.date(2026, 9, 8))
        self.assertNotIn("Quillfeather Payroll", self._payees())
        cfg = budget.load_config(self.conn)
        fig = budget.income_monthly_figure(self.conn, cfg, TODAY)
        self.assertEqual(fig["monthly"], 0.0)

    def test_a_series_ending_next_month_still_counts(self):
        self._payroll("Quillfeather Payroll", 3000.0, dt.date(2026, 12, 20))
        self.assertIn("Quillfeather Payroll", self._payees())
        cfg = budget.load_config(self.conn)
        fig = budget.income_monthly_figure(self.conn, cfg, TODAY)
        self.assertAlmostEqual(fig["monthly"], 3000.0, places=2)

    def test_the_deposit_average_and_the_fallback_agree(self):
        self._payroll("Quillfeather Payroll", 3000.0, dt.date(2026, 9, 8))
        self._payroll("Saltmarsh Stipend", 1200.0, dt.date(2026, 12, 20))
        cfg = budget.load_config(self.conn)
        fig = budget.income_monthly_figure(self.conn, cfg, TODAY)
        stored = budget._stored_income_monthly(self.conn, None, TODAY)
        self.assertAlmostEqual(fig["monthly"], 1200.0, places=2)
        self.assertAlmostEqual(stored, 1200.0, places=2)


if __name__ == "__main__":
    unittest.main()
