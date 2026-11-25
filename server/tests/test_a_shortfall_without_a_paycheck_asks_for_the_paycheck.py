"""With no paycheck scheduled, the cash notice asks for one.

A cash forecast that counts no income always runs short, so "short $X
today — slow spending to $0/day" on a household that has not set its
paycheck up is a verdict on the setup, not on its money, and it sits
beside an "under budget" that says the opposite. The invariant: while no
paycheck is scheduled the page, the email and the plain text carry the
missing step instead of a shortfall; once a paycheck is scheduled the
shortfall is back.
"""

import datetime as dt
import unittest

import html as _html

from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

ASK = "Add your paycheck under Bills & Income"


class ShortfallWithoutAPaycheck(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 40.0, "GROCER")
        # a bill far larger than checking, due inside the window
        add_bill(self.conn, "Harbor Mortgage", 500_000.0,
                 next_due=TODAY + dt.timedelta(days=3))

    def tearDown(self):
        self.conn.close()

    def _render(self):
        d = report.gather(self.conn, TODAY)
        _, plain, html = report.build(d)
        return d, plain, _html.unescape(html)

    def test_no_paycheck_reads_as_the_missing_step(self):
        write_config(self.conn)
        d, plain, html = self._render()
        self.assertEqual(d["income_due_note"], "none")
        self.assertIn(ASK, plain)
        self.assertIn(ASK, html)
        self.assertNotIn("!! CASH", plain)
        self.assertNotIn("slow spending", plain)
        self.assertNotIn("slow spending", html)

    def test_income_switched_off_says_so(self):
        add_bill(self.conn, "Northwind Payroll", 2400.0, income=True,
                 frequency="WEEKLY", interval=2,
                 next_due=TODAY + dt.timedelta(days=1))
        write_config(self.conn, disabled_bills=["Northwind Payroll"])
        d, plain, _ = self._render()
        self.assertEqual(d["income_due_note"], "off")
        self.assertIn("switched off under Bills & Income", plain)
        self.assertNotIn("!! CASH", plain)

    def test_a_scheduled_paycheck_brings_the_shortfall_back(self):
        add_bill(self.conn, "Northwind Payroll", 2400.0, income=True,
                 frequency="WEEKLY", interval=2,
                 next_due=TODAY + dt.timedelta(days=1))
        write_config(self.conn)
        d, plain, html = self._render()
        self.assertIsNone(d["income_due_note"])
        self.assertIsNone(todayview.paycheck_ask(d))
        self.assertIn("!! CASH", plain)
        self.assertNotIn(ASK, plain)
        self.assertNotIn(ASK, html)


if __name__ == "__main__":
    unittest.main()
