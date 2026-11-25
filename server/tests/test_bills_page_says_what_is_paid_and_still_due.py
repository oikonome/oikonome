"""Bills & Income says what is paid and what is still due, in one set of
words for the web and the phone.

- A bill paid this month whose next date is already next month's read
  "paid ✓" beside that next date, as though next month's were paid.
- A bill due twice in the month with one payment in said nothing of it.
- Today's one still-due figure (report.bills_remaining_month) was
  nowhere on Bills, so a reader arriving from Today's "still due" could
  not find it.
"""

import calendar
import datetime as dt
import unittest

from oikonome import localtime
from oikonome.engine import budget
from oikonome.web import api, api_bills, report  # noqa: F401 — api first

from .util import add_bill, add_txn, make_db, write_config

END = dt.date(2026, 5, 31)


class PaidWordsTests(unittest.TestCase):
    def test_paid_beside_next_months_date_says_both(self):
        row = {"occurrences": 1, "paid": 1, "paid_on": "2026-05-03",
               "due_on": "2026-06-03", "used": None, "income": False}
        self.assertEqual(api_bills.paid_words(row, END, live=True),
                         "paid 05/03 · next 06/03")
        # a closed month has no next date to name
        self.assertEqual(api_bills.paid_words(row, END, live=False),
                         "paid 05/03")

    def test_a_next_date_in_another_year_says_its_year(self):
        # a yearly bill paid this month is next due on the same day next
        # year: without the year it reads as due the day it was paid
        row = {"occurrences": 1, "paid": 1, "paid_on": "2026-05-07",
               "due_on": "2027-05-07", "used": None, "income": False}
        self.assertEqual(api_bills.paid_words(row, END, live=True),
                         "paid 05/07 · next 05/07/27")

    def test_a_bill_due_twice_counts_its_payments(self):
        row = {"occurrences": 2, "paid": 1, "paid_on": "2026-05-01",
               "due_on": "2026-05-15", "used": None, "income": False}
        self.assertEqual(api_bills.paid_words(row, END, live=True), "1 of 2 paid")
        row.update(paid=2, paid_on="2026-05-15", due_on="2026-06-01")
        self.assertEqual(api_bills.paid_words(row, END, live=True),
                         "2 of 2 paid · last 05/15 · next 06/01")

    def test_income_is_received_and_unpaid_says_nothing(self):
        row = {"occurrences": 1, "paid": 1, "paid_on": "2026-05-15",
               "due_on": "2026-05-29", "used": None, "income": True}
        self.assertEqual(api_bills.paid_words(row, END, live=True),
                         "received 05/15")
        row.update(paid=0)
        self.assertIsNone(api_bills.paid_words(row, END, live=True))
        # an envelope has no occurrences to be paid
        self.assertIsNone(api_bills.paid_words(
            {"occurrences": 0, "paid": 3, "used": 40.0}, END, live=True))


def _user(conn):
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    return {"tenant_id": tid, "user_id": "u", "role": "owner",
            "email": "o@example.com"}


class StillDueTests(unittest.TestCase):
    def test_the_page_prints_todays_still_due_figure(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, food_monthly=500, other_monthly=500,
                     budgeted_income_monthly=4000)
        today = localtime.now_local(budget.load_config(conn)).date()
        last = calendar.monthrange(today.year, today.month)[1]
        if today.day >= last - 1:
            self.skipTest("no day left in the month for a bill to be due")
        add_bill(conn, "Kestrel Power", 140.0,
                 next_due=today + dt.timedelta(days=1))
        add_bill(conn, "Harbor Lofts", 1500.0,
                 next_due=today.replace(day=1), last_seen=today.replace(day=1))
        add_txn(conn, today.replace(day=1), 1500.0, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        conn.commit()
        out = api_bills.bills_list(user=_user(conn), month=None)
        hero = {t["key"]: t for t in out["totals"]["hero"]}
        d = report.gather(conn, today)
        self.assertEqual(hero["due"]["amount"], round(d["bills_remaining_month"]))
        self.assertEqual(hero["due"]["amount"], 140)
        self.assertEqual(hero["due"]["label"], "Still due")
        rows = {r["payee"]: r for r in out["bills"]}
        self.assertTrue(rows["Harbor Lofts"]["paid_text"].startswith(
            f"paid {today.replace(day=1):%m/%d}"))
        self.assertIsNone(rows["Kestrel Power"]["paid_text"])


if __name__ == "__main__":
    unittest.main()
