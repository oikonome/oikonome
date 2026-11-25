"""One month, one "still due": the Month lens, Today/Budget and Bills
print the same bills-still-due figure for the same month.

The Month lens's running month said its schedule less what posted, while
Today's Bills row (which the Budget page draws) and the Bills page's
"Still due" said the unpaid occurrences through month end
(report.bills_remaining_month). A bill that posted under its plan is not
still due by the difference, so the lens named a figure the other two
pages did not. The running month now reads the one figure everywhere; a
closed month has nothing still due on any of them.
"""

import calendar
import datetime as dt
import unittest

from oikonome import localtime
from oikonome.engine import budget
from oikonome.web import api, api_bills, api_today, lenses, report  # noqa: F401

from .test_the_bills_row_is_one_breakdown import _parts, _table
from .util import TODAY, add_bill, add_txn, freeze_month, make_db, write_config


def _user(conn):
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    return {"tenant_id": tid, "user_id": "u", "role": "owner",
            "email": "o@example.com"}


def _bills_row(rows):
    return next(r for r in rows if r["label"] == "Bills")


class OneStillDueTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=500, other_monthly=500,
                     budgeted_income_monthly=4000)
        self.today = localtime.now_local(budget.load_config(self.conn)).date()
        last = calendar.monthrange(self.today.year, self.today.month)[1]
        if self.today.day >= last - 1 or self.today.day < 3:
            self.skipTest("needs a day behind and a day ahead in the month")
        first = self.today.replace(day=1)
        # posted this month for less than its plan: not still due by the
        # difference
        add_bill(self.conn, "Harbor Lofts", 1500.0, next_due=first,
                 last_seen=first)
        add_txn(self.conn, first, 1460.0, "HARBOR LOFTS", account="chk",
                primary="RENT_AND_UTILITIES")
        # due tomorrow, not paid yet
        add_bill(self.conn, "Kestrel Power", 140.0,
                 next_due=self.today + dt.timedelta(days=1))
        # due already this month and not posted: the awaiting part
        add_bill(self.conn, "Lantern Net", 70.0,
                 next_due=self.today - dt.timedelta(days=1))
        self.conn.commit()

    def _three(self):
        lens = lenses.month_summary(self.conn, self.today.year,
                                    self.today.month, today=self.today)
        st = report.gather(self.conn, self.today)
        rows, _total = __import__(
            "oikonome.web.todayview", fromlist=["x"]).today_map(st)
        bills = api_bills.bills_list(user=_user(self.conn), month=None)
        due = next((t for t in bills["totals"]["hero"] if t["key"] == "due"),
                   None)
        return lens, _bills_row(rows), due, st["bills_remaining_month"]

    def test_the_running_month_says_one_still_due_on_every_page(self):
        lens, today_row, due, figure = self._three()
        lens_row = _bills_row(lens["plan_rows"])
        self.assertGreater(figure, 0.5)
        self.assertEqual(lens_row["status"]["amount"], round(figure, 2))
        self.assertEqual(today_row["status"]["amount"], round(figure, 2))
        self.assertEqual(due["amount"], round(figure))
        # the same words and the same right-hand figure as Today's row
        self.assertEqual(lens_row["status"]["text"], today_row["status"]["text"])
        self.assertEqual(lens_row["budget"], round(today_row["budget"], 2))
        self.assertEqual(lens_row["caption"], today_row["caption"])

    def test_a_bill_due_and_not_posted_is_named_awaiting_on_both(self):
        lens, today_row, _due, _figure = self._three()
        lens_row = _bills_row(lens["plan_rows"])
        self.assertEqual(lens_row["status"]["pending"], 70.0)
        self.assertEqual(today_row["status"]["pending"], 70.0)
        # the awaiting part is the status's figure, never more words
        self.assertNotIn("awaiting", lens_row["status"]["text"])

    def test_a_closed_month_has_nothing_still_due_on_any_page(self):
        prev = (self.today.replace(day=1) - dt.timedelta(days=1))
        freeze_month(self.conn, prev.year, prev.month)
        self.conn.commit()
        lens = lenses.month_summary(self.conn, prev.year, prev.month,
                                    today=self.today)
        row = _bills_row(lens["plan_rows"])
        self.assertNotIn("still due", (row["status"] or {}).get("text", ""))
        bills = api_bills.bills_list(user=_user(self.conn),
                                     month=f"{prev:%Y-%m}")
        self.assertFalse([t for t in bills["totals"]["hero"]
                          if t["key"] == "due"])


class TheYearsBillsTests(unittest.TestCase):
    """The Year's Bills row: only the running month has anything still
    due; a closed month's unmet plan is "not paid"."""

    def test_a_closed_months_unpaid_bill_is_not_still_due(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        # paid in June and due again in July (TODAY is mid-July)
        add_bill(conn, "Harbor Lofts", 2000.0, next_due=dt.date(2026, 7, 1),
                 last_seen=dt.date(2026, 6, 1))
        add_txn(conn, dt.date(2026, 6, 1), 2000.0, "HARBOR LOFTS")
        # never paid in June, due later in July
        add_bill(conn, "Gym Pass", 50.0, next_due=dt.date(2026, 7, 20),
                 last_seen=dt.date(2026, 5, 20))
        freeze_month(conn, 2026, 6)
        y = lenses.year_summary(conn, 2026, today=TODAY)
        row = _bills_row(y["plan_rows"])
        july = lenses.month_summary(conn, 2026, 7, today=TODAY)
        still = _bills_row(july["plan_rows"])["status"]
        self.assertEqual(row["status"]["kind"], "due")
        # the year's still due is July's alone, with its awaiting part
        self.assertEqual(row["status"]["amount"], still["amount"])
        self.assertEqual(row["status"]["pending"], still["pending"])
        self.assertEqual(row["status"]["text"], still["text"])
        self.assertEqual(_parts(_table(row))["not_paid"], 50)
        self.assertLess(row["status"]["amount"],
                        round(row["budget"] - row["actual"], 2))

    def test_under_plan_nets_a_month_over_against_a_month_under(self):
        """One closed month $180 under its bills plan and the next $110
        over: the year is $70 under, as its bar shows — not $180."""
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        add_bill(conn, "Harbor Lofts", 1000.0, next_due=dt.date(2026, 7, 1),
                 last_seen=dt.date(2026, 6, 1))
        add_txn(conn, dt.date(2026, 5, 1), 820.0, "HARBOR LOFTS")
        add_txn(conn, dt.date(2026, 6, 1), 1110.0, "HARBOR LOFTS")
        add_txn(conn, dt.date(2026, 7, 1), 1000.0, "HARBOR LOFTS")
        freeze_month(conn, 2026, 5)
        freeze_month(conn, 2026, 6)
        y = lenses.year_summary(conn, 2026, today=TODAY)
        row = _bills_row(y["plan_rows"])
        self.assertEqual(round(row["budget"] - row["actual"]), 70)
        self.assertEqual(_parts(_table(row))["under"], 70)

    def test_last_months_late_bill_is_counted_once(self):
        """A bill due on June's last day and still unpaid on July 2 is in
        July's still-due figure; June's "not paid" does not count it
        again."""
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        add_bill(conn, "Kestrel Power", 120.0,
                 next_due=dt.date(2026, 6, 30),
                 last_seen=dt.date(2026, 5, 31))
        add_txn(conn, dt.date(2026, 5, 31), 120.0, "KESTREL POWER")
        freeze_month(conn, 2026, 6)
        day = dt.date(2026, 7, 2)
        july = lenses.month_summary(conn, 2026, 7, today=day)
        still = _bills_row(july["plan_rows"])["status"]
        self.assertGreaterEqual(still["amount"], 120.0)
        y = lenses.year_summary(conn, 2026, today=day)
        parts = _parts(_table(_bills_row(y["plan_rows"])))
        self.assertNotIn("not_paid", parts)
        self.assertNotIn("under", parts)

    def test_a_weekly_bills_missed_last_date_is_counted_once(self):
        """A weekly bill paid on its first June dates and not on its last
        is carried into July's still-due figure on July 1; June's plan is
        not also said to have that occurrence left over as "under plan",
        though June lists no "not paid" for a bill it paid other weeks."""
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        add_bill(conn, "Willow Daycare", 300.0, frequency="WEEKLY",
                 next_due=dt.date(2026, 6, 30),
                 last_seen=dt.date(2026, 6, 23))
        for d in (2, 9, 16, 23):
            add_txn(conn, dt.date(2026, 6, d), 300.0, "WILLOW DAYCARE")
        freeze_month(conn, 2026, 6)
        day = dt.date(2026, 7, 1)
        july = lenses.month_summary(conn, 2026, 7, today=day)
        still = _bills_row(july["plan_rows"])["status"]
        self.assertGreaterEqual(still["amount"], 300.0)
        y = lenses.year_summary(conn, 2026, today=day)
        row = _bills_row(y["plan_rows"])
        # carried from June and inside the year's plan (June planned it):
        # the Year's still-due figure is July's, in July's words
        self.assertEqual(row["status"]["text"], still["text"])
        t = _table(row)
        self.assertEqual(t["outside"], [])
        self.assertNotIn("under", _parts(t))
        self.assertNotIn("not_paid", _parts(t))


if __name__ == "__main__":
    unittest.main()
