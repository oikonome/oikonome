""""Left at month end" is the balance after the month's expected spending:
the figure the cash timeline ends the month on, one server value both
read (todayview.month_end_left). The tile used to be checking less card
debt less the bills still due plus the paychecks, which leaves the rest
of the month's spending out and read green beside a forecast heading
below zero. Figures are invented."""

import datetime as dt
import unittest

from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

MONTH_END = "2026-07-31"


class MonthEndTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=900)
        for day in range(1, 15):
            add_txn(self.conn, dt.date(2026, 7, day), 35.0, "CORNER CAFE",
                    primary="FOOD_AND_DRINK")
        add_bill(self.conn, "RIVERSIDE RENT", 1200.0,
                 next_due=dt.date(2026, 7, 25),
                 last_seen=dt.date(2026, 6, 25))
        add_bill(self.conn, "ACME PAYROLL", 2000.0, income=True,
                 next_due=dt.date(2026, 7, 31),
                 last_seen=dt.date(2026, 6, 30))
        self.st = report.gather(self.conn, TODAY)
        self.ctx = todayview.build_context(self.st)

    def tearDown(self):
        self.conn.close()

    def test_the_tile_is_the_walks_month_end_balance(self):
        me = self.ctx["month_end"]
        walk = dict(self.st["forecast"]["pace_stmt"]["series"])
        self.assertEqual(me["date"], MONTH_END)
        self.assertEqual(me["balance"], walk[MONTH_END])
        # the old tile leaves the rest of the month's spending out
        self.assertGreater(self.st["after_cards_bills_paychecks"],
                           me["balance"] + 100)

    def test_the_timeline_ends_the_month_on_the_same_balance(self):
        # the timeline the page and the email print: the usual walk's
        # (forecast_rows is the due-date walk an installed client reads)
        rows = self.ctx["scenarios"][0]["rows"]
        july = [r for r in rows if r["date"] <= MONTH_END]
        last = july[-1]
        self.assertEqual((last["date"], last["amount"], last["label"]),
                         (MONTH_END, None, "checking at month end"))
        self.assertEqual(last["balance"], self.ctx["month_end"]["balance"])
        # after the paycheck that lands that day
        self.assertEqual(july[-2]["label"], "ACME PAYROLL")
        # and shown even when the low point comes earlier
        self.assertIn((MONTH_END, None, "checking at month end"),
                      [r[:3] for r in self.ctx["tl_pre"]])

    def test_every_surface_prints_it(self):
        _s, plain, html = report.build(self.st)
        shown = report._money(self.ctx["month_end"]["balance"])
        self.assertIn(f"checking at month end {shown} (after this month's "
                      "bills", plain)
        self.assertIn("checking at month end</span>", html)
        self.assertIn(shown, html)
        self.assertNotIn("checking at month end after cards", plain + html)


if __name__ == "__main__":
    unittest.main()
