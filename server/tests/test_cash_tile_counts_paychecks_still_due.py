"""The Cash timeline's month-end figure counts the paychecks still due this
month next to the bills still due. Bills alone read as if the product had
forgotten the household's pay — a reader whose checks land mid-month saw a
"what's left" that ignored them. With an income series the tile is
"paychecks still due" (+amount, the dates) and "after cards, bills &
paychecks"; without one it stays "after cards & bills" and says that no
paycheck is set up, so the reader knows what to add rather than what they
got wrong. Email HTML, plain text and the SPA payload carry the same
figure from one gathered status."""

import calendar
import datetime as dt
import unittest

from oikonome.web import report

from .util import TODAY, add_bill, add_txn, make_db, write_config

MONTH_END = dt.date(TODAY.year, TODAY.month,
                    calendar.monthrange(TODAY.year, TODAY.month)[1])


class PaychecksStillDueTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()          # seeds the fixture accounts itself
        write_config(self.conn)
        # a spend floor so the day has a ledger
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 40.0, "GROCER")

    def tearDown(self):
        # a pooled checkout: left open, three of these starve the pool test
        self.conn.close()

    def _gather(self):
        return report.gather(self.conn, TODAY)

    def test_without_an_income_series_the_tile_says_no_paycheck_is_set_up(self):
        d = self._gather()
        self.assertIsNone(d.get("income_due_total"))
        self.assertIsNone(d.get("income_due_dates"))
        subj, plain, html = report.build(d)
        self.assertIn("after cards & bills", plain)
        self.assertIn("no paycheck set up", plain)
        self.assertNotIn("paychecks still due", plain)
        self.assertIn("after cards &amp; bills", html)
        self.assertIn("no paycheck set up", html)
        self.assertNotIn("after cards, bills &amp; paychecks", html)

    def test_with_a_paycheck_still_due_the_figure_counts_it(self):
        # a biweekly paycheck whose next occurrence is still inside this
        # month: the tile names its amount and date and adds it to the
        # month-end figure; an occurrence past month end counts nowhere
        payday = TODAY + dt.timedelta(days=1)
        if payday > MONTH_END:
            self.skipTest("no payday left in this month from today")
        add_bill(self.conn, "ACME HOLDINGS PAYROLL", 2400.0, income=True,
                 frequency="WEEKLY", interval=2, next_due=payday)
        d = self._gather()
        # one check per biweekly occurrence left in the month, each at
        # the series' amount
        self.assertIn(payday.isoformat(), d["income_due_dates"])
        self.assertAlmostEqual(d["income_due_total"],
                               2400.0 * len(d["income_due_dates"]), places=2)
        subj, plain, html = report.build(d)
        rw = d["runway"]
        after = (rw["checking"] - rw["card_debt"] - d["bills_remaining_month"]
                 + d["income_due_total"])
        self.assertIn("paychecks still due", plain)
        self.assertIn("after cards, bills & paychecks", plain)
        self.assertIn(report._money(after), plain)
        self.assertNotIn("no paycheck set up", plain)
        self.assertIn("paychecks still due", html)
        self.assertIn("after cards, bills &amp; paychecks", html)
        self.assertIn(report._money(after), html)
        self.assertNotIn("no paycheck set up", html)

    def test_a_pending_paycheck_the_balance_cannot_see_still_counts(self):
        # Payday: the check is pending, the bank reports no available
        # balance, so checking starts from the current balance that does
        # not hold it. The month's income halves read the pending deposit
        # as received; the cash walk re-dates the occurrence to tomorrow.
        # The tile follows the walk, or it would show +$0 beside a cash
        # figure that never saw the money.
        payday = TODAY
        if payday + dt.timedelta(days=1) > MONTH_END:
            self.skipTest("no tomorrow left in this month")
        add_bill(self.conn, "ACME HOLDINGS PAYROLL", 2400.0, income=True,
                 frequency="WEEKLY", interval=2, next_due=payday)
        self.conn.execute("UPDATE accounts SET balance_available = NULL "
                          "WHERE id = 'chk'")
        add_txn(self.conn, payday, -2400.0, "ACME HOLDINGS PAYROLL",
                primary="INCOME", account="chk", pending=1)
        d = self._gather()
        self.assertEqual(d["income_due_dates"][0],
                         (payday + dt.timedelta(days=1)).isoformat())
        self.assertAlmostEqual(d["income_due_total"],
                               2400.0 * len(d["income_due_dates"]), places=2)

    def test_a_payday_on_the_months_last_day_still_counts(self):
        # Semi-monthly payroll pays on the last day; the check has not
        # posted by the morning email. The walk re-dates an unposted check
        # due today to tomorrow, and on the last day tomorrow is next
        # month — the tile reserves to the same horizon the bills do, so
        # the month-end payday is not read as +$0.
        add_bill(self.conn, "ACME HOLDINGS PAYROLL", 2400.0, income=True,
                 frequency="MONTHLY", interval=1, next_due=MONTH_END)
        d = report.gather(self.conn, MONTH_END)
        self.assertEqual(d["income_due_dates"],
                         [(MONTH_END + dt.timedelta(days=1)).isoformat()])
        self.assertAlmostEqual(d["income_due_total"], 2400.0, places=2)

    def test_next_months_first_day_paycheck_is_not_this_months(self):
        # On the month's last day the walk already looks into next month;
        # a pension genuinely due on the 1st is next month's money, not a
        # re-dated check of this one.
        first_next = MONTH_END + dt.timedelta(days=1)
        add_bill(self.conn, "STATE PENSION", 2000.0, income=True,
                 frequency="MONTHLY", interval=1, next_due=first_next)
        d = report.gather(self.conn, MONTH_END)
        self.assertEqual(d["income_due_dates"], [])
        self.assertEqual(d["income_due_total"], 0.0)

    def test_next_months_first_day_bill_is_not_this_months(self):
        # The bills side of the same figure: rent genuinely due on the 1st
        # is next month's bill. Counting it on the last day alone dips the
        # tile by a whole month's rent, and next to a pension due the same
        # day that the paycheck side rightly leaves out.
        first_next = MONTH_END + dt.timedelta(days=1)
        add_bill(self.conn, "ROOFTOP RENT", 2000.0,
                 frequency="MONTHLY", interval=1, next_due=first_next)
        add_bill(self.conn, "STATE PENSION", 3000.0, income=True,
                 frequency="MONTHLY", interval=1, next_due=first_next)
        d = report.gather(self.conn, MONTH_END)
        self.assertEqual(d["income_due_total"], 0.0)
        self.assertEqual(d["bills_remaining_month"], 0.0)

    def test_a_bill_due_on_the_last_day_or_overdue_still_counts_there(self):
        # Both are re-dated past month end by the walk and are still this
        # month's money, so the month-end boundary must not drop them.
        add_bill(self.conn, "TODAYS UTILITY", 100.0,
                 frequency="MONTHLY", interval=1, next_due=MONTH_END)
        add_bill(self.conn, "LATE GARAGE", 70.0,
                 frequency="MONTHLY", interval=1,
                 next_due=MONTH_END - dt.timedelta(days=9))
        d = report.gather(self.conn, MONTH_END)
        self.assertAlmostEqual(d["bills_remaining_month"], 170.0, places=2)

    def test_a_past_day_has_no_still_due_dates(self):
        d = report.gather(self.conn, TODAY - dt.timedelta(days=2), live=False)
        self.assertIsNone(d.get("income_due_dates"))
        self.assertIsNone(d.get("income_due_total"))


if __name__ == "__main__":
    unittest.main()
