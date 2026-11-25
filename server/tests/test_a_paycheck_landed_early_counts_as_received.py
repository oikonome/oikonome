"""A closed month's Bills page counts a paycheck the month owns as
received, even when its deposit landed just before the 1st.

A paycheck is the income of the month it pays (budget._month_income_parts):
one due on May 1 that landed on April 30 is May's. The received dollars
beside the series counted only deposits dated inside the month, so May
read "1 of 2 received" with half its pay in dollars."""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web.api import _month_bill_stats

from .util import add_bill, add_txn, make_db, write_config


class PaidOnTheThirtieth(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "NORTHWIND PAYROLL", 2000.0, income=True,
                 frequency="MONTHLY", interval=1,
                 next_due=dt.date(2026, 6, 1))
        for day in (dt.date(2026, 3, 31), dt.date(2026, 4, 30),
                    dt.date(2026, 6, 1)):
            add_txn(self.conn, day, -2000.0, "NORTHWIND PAYROLL",
                    account="chk", primary="INCOME", detailed="INCOME_WAGES")
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_mays_paycheck_landed_april_30_is_mays_received(self):
        st = _month_bill_stats(self.conn, self.cfg, dt.date(2026, 5, 31),
                               closed=True)["NORTHWIND PAYROLL"]
        self.assertEqual(st["paid"], 1)
        self.assertEqual(abs(st["paid_amount"]), 2000.0)


if __name__ == "__main__":
    unittest.main()
