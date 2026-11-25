"""Today's bills still due is one figure on the money map, the cash tiles
and the email: the bill occurrences not yet paid through month end plus
what is left of the envelopes (report.bills_remaining_month). The map's
Bills row used to print its schedule less what posted, so a bill that
posted a little under its plan still counted the difference as due, and
the two figures disagreed. Beside the plan line's bills (the evened
average), the page, the app and both emails say whether this month is
light or heavy against it. Figures are invented."""

import datetime as dt
import unittest

from oikonome.web import charts, report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config


class StillDueTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=900)
        add_bill(self.conn, "RIVERSIDE RENT", 700.0,
                 next_due=dt.date(2026, 7, 3),
                 last_seen=dt.date(2026, 6, 3))
        # rent posted $6.50 under its plan: paid, nothing of it still due
        add_txn(self.conn, dt.date(2026, 7, 3), 693.5, "RIVERSIDE RENT",
                primary="RENT_AND_UTILITIES", account="chk")
        add_bill(self.conn, "VALLEY POWER", 300.0,
                 next_due=dt.date(2026, 7, 25),
                 last_seen=dt.date(2026, 6, 25))
        # due already, not posted: awaiting
        add_bill(self.conn, "HARBOR INSURANCE", 150.0,
                 next_due=dt.date(2026, 7, 10),
                 last_seen=dt.date(2026, 6, 10))
        # a yearly bill in another month lifts the average over this
        # month's schedule by $2,400 / 12 = $200
        add_bill(self.conn, "COUNTY TAX", 2400.0, frequency="YEARLY",
                 next_due=dt.date(2026, 10, 1),
                 last_seen=dt.date(2025, 10, 1))
        self.st = report.gather(self.conn, TODAY)

    def tearDown(self):
        self.conn.close()

    def bills_row(self, st):
        rows, _t = todayview.today_map(st)
        return next(r for r in rows if r["label"] == "Bills")

    def test_the_map_prints_the_cash_tiles_still_due(self):
        row = self.bills_row(self.st)
        due = self.st["bills_remaining_month"]
        self.assertAlmostEqual(due, 450.0, places=2)
        self.assertAlmostEqual(row["pending"], 150.0, places=2)
        # the old figure counted the rent's $6.50 as still due
        self.assertNotAlmostEqual(
            row["budget"] - row["actual"] - row["pending"], due, places=0)
        self.assertEqual(row["status"]["amount"], due)
        # the row's words are its one still-due figure; the awaiting part
        # is the status's `pending` and the bar's amber, the $6 the rent
        # left of its plan a row of the table
        self.assertEqual(row["status"]["text"], "$450 still due")
        self.assertEqual(row["status"]["pending"], 150.0)
        self.assertIn(("Under plan", "+$6"),
                      [(r["label"], r["figure"])
                       for r in row["breakdown"]["table"]["rows"]])
        rw = self.st["runway"]
        self.assertAlmostEqual(self.st["after_cards_bills"],
                               rw["checking"] - rw["card_debt"] - due,
                               places=2)

    def test_a_past_day_has_no_still_due(self):
        st = report.gather(self.conn, TODAY - dt.timedelta(days=1),
                           live=False)
        self.assertIsNone(self.bills_row(st)["status"])

    def test_a_light_month_says_so_beside_the_bills(self):
        """The plan line's bills are the evened-out average; the words
        beside them name it so and give this month's own bills, never
        calling the average this month's. Figures are invented."""
        weight = todayview.bills_weight(self.st)
        sched = charts._money(self.st["scheduled_month"])
        self.assertEqual(weight, f"evened out; this month lighter: {sched}")
        self.assertEqual(todayview.build_context(self.st)["day"]
                         ["bills_weight"], weight)
        heavy = dict(self.st, scheduled_month=self.st["avg_bills"] + 350)
        self.assertEqual(
            todayview.bills_weight(heavy),
            "evened out; this month heavier: "
            f"{charts._money(heavy['scheduled_month'])}")
        typical = dict(self.st, scheduled_month=self.st["avg_bills"] + 60)
        self.assertIsNone(todayview.bills_weight(typical))

    def test_both_emails_print_both(self):
        write_config(self.conn, food_monthly=600, other_monthly=900,
                     budgeted_income_monthly=6000)
        st = report.gather(self.conn, TODAY)
        _s, plain, html = report.build(st)
        words = todayview.bills_weight(st)
        self.assertIn(f"bills ({words})", plain)
        self.assertIn(f"({words})</span>", html)
        self.assertNotIn("under your average", plain + html)
        # the chain ends in the Budget page's words for the excess
        words = todayview.plan_excess_words(st)["text"]
        ex = charts._money(st["plan_line"]["excess"])
        self.assertIn(f"-> {ex} {words}", plain)
        self.assertIn(f"</b> {words}</span>", html)
        self.assertEqual(todayview.build_context(st)["day"]
                         ["plan_excess_words"]["text"], words)
        self.assertIn("$450 still due", html)
        # the table is not mailed
        self.assertNotIn("Under plan", html)


if __name__ == "__main__":
    unittest.main()
