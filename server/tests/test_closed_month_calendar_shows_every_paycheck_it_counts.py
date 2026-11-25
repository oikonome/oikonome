"""A closed month's calendar shows every paycheck its Income counts.

A paycheck belongs to the month it pays (its occurrence's due month),
whatever day the deposit lands: one due on the 1st and deposited on the
last day of the month before is the new month's income. The closed
month's calendar used to keep only deposits dated inside the month, so
that paycheck was on no month's calendar while the month's Income tile
counted it. The calendar must place it in the month that owns it — on
the month's own grid, labelled with the day it landed — and the month
before must not show it a second time.
"""

import calendar
import datetime as dt
import unittest

from oikonome import localtime
from oikonome.engine import budget
from oikonome.web import api  # noqa: F401 — api mounts the areas
from oikonome.web import api_bills

from .util import add_bill, add_txn, make_db, write_config


def _user(conn):
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    return {"tenant_id": tid, "user_id": "u", "role": "owner",
            "email": "o@example.com"}


def _back(y, m, n):
    m -= n
    while m < 1:
        y, m = y - 1, m + 12
    return y, m


class EarlyPaycheckOnItsOwnMonthsCalendar(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=500, other_monthly=500)
        today = localtime.now_local(budget.load_config(self.conn)).date()
        # the month before last pays a check due on its 1st that landed on
        # the last day of the month before it
        self.y, self.m = _back(today.year, today.month, 2)
        self.py, self.pm = _back(today.year, today.month, 3)
        self.ym = f"{self.y:04d}-{self.m:02d}"
        self.pym = f"{self.py:04d}-{self.pm:02d}"
        landed = dt.date(self.py, self.pm,
                         calendar.monthrange(self.py, self.pm)[1])
        self.landed = landed
        add_bill(self.conn, "Northwind Mfg", 3000.0, income=True,
                 next_due=dt.date(self.y, self.m, 1),
                 last_seen=landed)
        add_txn(self.conn, landed, -3000.0, "NORTHWIND MFG PAYROLL",
                account="chk", primary="INCOME")
        self.conn.commit()

    def page(self, month):
        return api_bills.bills_list(user=_user(self.conn), month=month)

    def test_the_paycheck_is_on_the_month_that_counts_it_once(self):
        out = self.page(self.ym)
        hero = {t["key"]: t for t in out["totals"]["hero"]}
        self.assertEqual(hero["income"]["amount"], 3000)
        days = out["month_calendar"]
        came_in = round(sum(d["came_in"] for d in days), 2)
        self.assertEqual(came_in, 3000.0)
        # on the month's own grid, saying the day it landed
        self.assertTrue(all(d["date"].startswith(self.ym) for d in days))
        labels = [e["label"] for d in days for e in d["events"]]
        self.assertEqual(
            labels, [f"Northwind Mfg — landed {self.landed:%m/%d}"])
        # the month the deposit is dated in does not count it, and its
        # calendar does not show it
        before = self.page(self.pym)
        self.assertEqual(
            round(sum(d["came_in"] for d in before["month_calendar"]), 2),
            0.0)


if __name__ == "__main__":
    unittest.main()
