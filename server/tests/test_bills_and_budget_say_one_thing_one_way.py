"""Bills & Income and Budget say one thing one way.

- The envelope fit read "across 3–3 charges".
- Budget history called the month still running "frozen", and offered to
  backfill every month back to the first imported statement — decades.
- The week ahead was "next 7 days" on the web and "Due this week" on the
  phone, each composing its own sentence.
- An envelope's "count all <category> spending" named the envelope's
  category while its charges carried another; the editor now says which.
"""

import datetime as dt
import unittest

from oikonome import localtime
from oikonome.engine import bills, budget
from oikonome.web import api, api_bills, api_today  # noqa: F401 — api first

from .util import add_bill, add_txn, make_db, write_config


def _user(conn):
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    return {"tenant_id": tid, "user_id": "u", "role": "owner",
            "email": "o@example.com"}


class FitLabelTests(unittest.TestCase):
    def _ev(self, days):
        amts = iter([31, 47, 18, 52, 29, 40, 22, 61, 15, 33, 27, 44])
        return [{"date": dt.date(2026, m, d), "amount": next(amts),
                 "payee": "Tutor", "category": None, "is_food": False}
                for m, ds in days.items() for d in ds]

    def test_an_even_count_is_one_number(self):
        fit = bills.ledger_fit(self._ev({2: [3, 11, 26], 3: [7, 15, 29],
                                         4: [1, 19, 24]}), dt.date(2026, 5, 20))
        self.assertEqual(fit["kind"], "envelope")
        self.assertIn("3 charges a month", fit["label"])
        self.assertNotIn("3–3", fit["label"])

    def test_an_uneven_count_is_a_range(self):
        fit = bills.ledger_fit(self._ev({2: [3, 11], 3: [7, 15, 29],
                                         4: [1, 19, 24, 27]}),
                               dt.date(2026, 5, 20))
        self.assertIn("2–4 charges a month", fit["label"])


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=500, other_monthly=500)
        self.today = localtime.now_local(budget.load_config(self.conn)).date()

    def test_the_running_month_is_so_far_and_closed_months_are_not(self):
        self.assertTrue(budget.snapshot_month(self.conn, self.today))
        out = api_today.budget_snapshots(user=_user(self.conn))
        now = out["months"][0]
        self.assertEqual((now["y"], now["m"]), (self.today.year, self.today.month))
        self.assertEqual(now["status_text"], "so far")

    def test_the_backfill_reaches_two_years_back_not_to_the_first_statement(self):
        add_txn(self.conn, self.today.replace(day=1) - dt.timedelta(days=365 * 20),
                12.0, "OLD STATEMENT LINE")
        out = api_today.budget_snapshots(user=_user(self.conn))
        self.assertEqual(out["missing"], budget.BACKFILL_MONTHS)
        self.assertEqual(budget.backfill_snapshots(self.conn, self.today),
                         budget.BACKFILL_MONTHS)


class BillsReplyWordsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=500, other_monthly=500)
        self.today = localtime.now_local(budget.load_config(self.conn)).date()

    def test_the_week_ahead_is_one_sentence(self):
        add_bill(self.conn, "Kestrel Power", 140.0,
                 next_due=self.today + dt.timedelta(days=2))
        add_bill(self.conn, "Lantern Gym", 25.0,
                 next_due=self.today + dt.timedelta(days=4))
        out = api_bills.bills_list(user=_user(self.conn), month=None)
        self.assertEqual(out["totals"]["week_text"], "$165 across 2 bills")

    def test_an_envelope_names_the_category_its_charges_carry(self):
        add_bill(self.conn, "Pine Ridge Labs", 120.0, bill_type="envelope",
                 frequency="MONTHLY", category="RENT_AND_UTILITIES")
        day = self.today.replace(day=1)
        for i in range(2):
            add_txn(self.conn, day, 40.0 + i, "PINE RIDGE LABS",
                    account="chk", primary="LEGAL")
        out = api_bills.bills_list(user=_user(self.conn), month=None)
        row = next(r for r in out["bills"] if r["payee"] == "Pine Ridge Labs")
        self.assertEqual(row["charges_category"], "LEGAL")


if __name__ == "__main__":
    unittest.main()
