"""The Plan line adds up: in -> bills -> to spend -> saved -> excess.

Every figure on the line is the plan's own, on the basis its excess is
computed from: bills evened over the year and the budgets as set. A month's
own schedule can be heavier or lighter than the evened figure, and its
scaled budget smaller or larger than the one set; printed beside an excess
computed from the plan, either leaves a chain whose terms do not produce
its last number. An early autopay of next month's rent is cash gone this
month — the fixed bar counts it — and moves nothing on the line. The page
and the email read the same figures.
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

    def _chain(self, text, pat):
        m = re.search(pat, text, re.S)
        self.assertIsNotNone(m, text[:400])
        return [_amount(g) for g in m.groups()]

    def test_the_email_plan_line_adds_up(self):
        income, bills, spend, excess = self._chain(
            self.plain,
            r"Plan: (\$[\d,.]+) in -> (\$[\d,.]+) bills .*?"
            r"-> (\$[\d,.]+) to spend.*?-> (-?\$[\d,.]+) planned excess")
        self.assertAlmostEqual(bills, 1500.0, places=2)
        self.assertAlmostEqual(income - bills - spend, excess, delta=1.0)

    def test_the_page_plan_line_adds_up(self):
        income, bills, spend, excess = self._chain(
            self.html,
            r"(\$[\d,.]+)</b> in</span>.*?(\$[\d,.]+)</b> bills"
            r"</span>.*?(\$[\d,.]+)</b> to spend.*?(-?\$[\d,.]+)</b> planned excess")
        self.assertAlmostEqual(bills, 1500.0, places=2)
        self.assertAlmostEqual(income - bills - spend, excess, delta=1.0)

    def test_the_line_is_the_plan_not_the_months_schedule(self):
        """A yearly bill due this month makes the schedule heavier than the
        evened figure; the line still reads the evened one, and adds up."""
        add_bill(self.conn, "Insurance", 1200.0, frequency="YEARLY",
                 next_due=dt.date(2026, 7, 30), last_seen=dt.date(2025, 7, 30))
        d = report.gather(self.conn, DAY)
        _, plain, _html = report.build(d)
        income, bills, spend, excess = self._chain(
            plain,
            r"Plan: (\$[\d,.]+) in -> (\$[\d,.]+) bills .*?"
            r"-> (\$[\d,.]+) to spend.*?-> (-?\$[\d,.]+) planned excess")
        self.assertAlmostEqual(bills, d["avg_bills"], delta=1.0)
        self.assertGreater(d["scheduled_month"], d["avg_bills"] + 500)
        self.assertAlmostEqual(income - bills - spend, excess, delta=1.0)


class AnExcessUsedUpStillAddsUp(unittest.TestCase):
    """A month whose overrun has used up the excess ends its line in the
    Budget summary's words — "planned excess · none this month" — and the
    line still adds up as printed. Figures are invented."""

    def test_the_email_line_adds_up_with_none_this_month(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, today_view="detail", budgeted_income_monthly=4000)
        add_bill(conn, "Rent", 1200.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 7, 1))
        add_txn(conn, dt.date(2026, 7, 1), 1200.0, "RENT")
        add_txn(conn, dt.date(2026, 7, 3), 3600.0, "BIRCHWOOD FURNITURE")
        _, plain, html = report.build(report.gather(conn, DAY))
        m = re.search(
            r"Plan: (\$[\d,.]+) in -> (\$[\d,.]+) bills .*?"
            r"-> (\$[\d,.]+) to spend.*?-> (-?\$[\d,.]+) "
            r"planned excess · none this month", plain, re.S)
        self.assertIsNotNone(m, plain)
        income, bills, spend, excess = (_amount(g) for g in m.groups())
        self.assertEqual(income - bills - spend, excess)
        self.assertIn("</b> planned excess · none this month</span>", html)
