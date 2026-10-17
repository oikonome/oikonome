"""The Plan line reads the month's bill SCHEDULE, not its cash.

The variable budget is scaled from what income leaves after the bills this
month is scheduled to owe. When next month's rent autopays a few days early
its cash leaves in this month — the fixed bar counts it — but it is not this
month's plan. If the Plan line quoted the cash figure, income -> bills ->
to spend would stop adding up in exactly the month the household is most
likely to check it. The page and the email read the same figure.
"""

import datetime as dt
import re
import unittest

from oikonome.web import report

from .util import add_bill, add_txn, make_db, write_config

DAY = dt.date(2026, 7, 29)


def _amount(s: str) -> float:
    return float(s.replace("$", "").replace(",", ""))


class EarlyAutopayKeepsThePlanBalanced(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, today_view="detail",
                     dynamic_variable_budget=True,
                     budgeted_income_monthly=3000)
        add_bill(self.conn, "Rent", 1500.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 7, 1))
        add_txn(self.conn, dt.date(2026, 6, 1), 1500.0, "RENT")
        add_txn(self.conn, dt.date(2026, 7, 1), 1500.0, "RENT")
        # August's rent, autopaid three days early
        add_txn(self.conn, dt.date(2026, 7, 28), 1500.0, "RENT")
        self.d = report.gather(self.conn, DAY)
        _, self.plain, self.html = report.build(self.d)

    def tearDown(self):
        self.conn.close()

    def test_the_fixed_bar_keeps_the_cash(self):
        self.assertAlmostEqual(self.d["buckets"]["fixed"]["month_budget"],
                               3000.0, places=2)

    def test_the_email_plan_line_balances(self):
        m = re.search(r"Plan: (\$[\d,.]+) in -> (\$[\d,.]+) bills due .*?"
                      r"-> (\$[\d,.]+) to spend", self.plain)
        self.assertIsNotNone(m, self.plain)
        income, bills, spend = (_amount(g) for g in m.groups())
        self.assertAlmostEqual(bills, 1500.0, places=2)
        self.assertAlmostEqual(income - bills - spend, 0.0, places=2)
        self.assertIn("typical month", self.plain)

    def test_the_page_plan_line_balances(self):
        m = re.search(r"(\$[\d,.]+)</b> in</span>.*?(\$[\d,.]+)</b> bills due"
                      r"</span>.*?(\$[\d,.]+)</b> to spend", self.html, re.S)
        self.assertIsNotNone(m)
        income, bills, spend = (_amount(g) for g in m.groups())
        self.assertAlmostEqual(bills, 1500.0, places=2)
        self.assertAlmostEqual(income - bills - spend, 0.0, places=2)
