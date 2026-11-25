"""A closed month on Bills & Income shows that month's own figures.

Stepping back to a past month used to keep today's headline (the live
schedule's bill load and income average), the next seven days, today's
offers and the next five weeks' calendar on screen above that month's
paid list. A closed month shows its frozen figures
everywhere — the bill load frozen with it (what Budget history prints),
the month's plan as the Month lens and the monthly report judge it, and
a calendar of that month's own money; nothing of today's.
"""

import calendar
import datetime as dt
import unittest

from oikonome import localtime
from oikonome.engine import bills, budget
from oikonome.web import api, lenses  # noqa: F401 — api mounts the areas
from oikonome.web import api_bills

from .util import add_bill, add_txn, make_db, write_config


def _user(conn):
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    return {"tenant_id": tid, "user_id": "u", "role": "owner",
            "email": "o@example.com"}


class ClosedMonthTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=500, other_monthly=500)
        today = localtime.now_local(budget.load_config(self.conn)).date()
        self.today = today
        # the month before last: closed, with a whole month after it
        y, m = today.year, today.month - 2
        if m < 1:
            y, m = y - 1, m + 12
        self.y, self.m = y, m
        self.ym = f"{y:04d}-{m:02d}"
        last = calendar.monthrange(y, m)[1]
        add_bill(self.conn, "Harbor Lofts", 1500.0,
                 next_due=dt.date(y, m, 3), last_seen=dt.date(y, m, 3))
        add_bill(self.conn, "Kestrel Power", 90.0,
                 next_due=dt.date(y, m, 20), last_seen=None)
        add_bill(self.conn, "Northwind Mfg", 3000.0, income=True,
                 next_due=dt.date(y, m, 15), last_seen=dt.date(y, m, 15))
        add_txn(self.conn, dt.date(y, m, 3), 1500.0, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        add_txn(self.conn, dt.date(y, m, 15), -3000.0,
                "NORTHWIND MFG PAYROLL", account="chk", primary="INCOME")
        self.conn.commit()
        self.assertTrue(budget.snapshot_month(self.conn, dt.date(y, m, last)))
        self.frozen = budget.snapshot_bills_monthly(self.conn, y, m)
        # the live schedule moves on after the month closed
        add_bill(self.conn, "Bluebell Dance", 400.0,
                 next_due=today + dt.timedelta(days=5))
        bills._insert_proposal(
            self.conn, pid="prop:cm", kind="add", payee="Lantern Cleaners",
            amount=80.0, frequency="WEEKLY", interval=2,
            next_due=today + dt.timedelta(days=3),
            evidence={"hits": 5, "cycle_days": 14})
        self.conn.commit()

    def page(self, month):
        return api_bills.bills_list(user=_user(self.conn), month=month)

    def test_the_headline_is_the_months_own(self):
        out = self.page(self.ym)
        hero = {t["key"]: t for t in out["totals"]["hero"]}
        # the bill load frozen with the month, not today's (which now has
        # the dance class in it)
        self.assertEqual(hero["bills"]["amount"], round(self.frozen))
        self.assertIn("frozen", hero["bills"]["note"])
        live = {t["key"]: t for t in self.page(None)["totals"]["hero"]}
        self.assertNotEqual(live["bills"]["amount"], hero["bills"]["amount"])
        # the month's plan as the Month lens judges it: what landed
        st = lenses.month_state(self.conn, self.y, self.m, self.today)
        self.assertEqual(hero["income"]["amount"], st["plan_line"]["income"])
        self.assertEqual(hero["income"]["amount"], 3000)
        self.assertEqual(hero["left"]["amount"],
                         st["plan_line"]["to_spend"] + st["plan_line"]["excess"])

    def test_nothing_of_todays_is_on_a_closed_month(self):
        out = self.page(self.ym)
        self.assertEqual(out["proposals"], [])
        self.assertEqual((out["totals"]["week_total"],
                          out["totals"]["week_count"]), (0.0, 0))
        self.assertTrue(self.page(None)["proposals"])

    def test_the_calendar_is_the_months_money(self):
        days = {d["date"]: d for d in self.page(self.ym)["month_calendar"]}
        rent = days[f"{self.ym}-03"]
        self.assertEqual((rent["went_out"], rent["came_in"]), (1500.0, 0.0))
        self.assertEqual(rent["events"][0]["label"], "Harbor Lofts")
        pay = days[f"{self.ym}-15"]
        self.assertEqual(pay["came_in"], 3000.0)
        self.assertEqual(pay["events"][0]["kind"], "income")
        # the bill the month never paid sits on its due day, said so
        power = days[f"{self.ym}-20"]
        self.assertEqual(power["events"][0]["label"], "Kestrel Power — not paid")
        self.assertEqual(power["went_out"], 90.0)
        self.assertTrue(all(d.startswith(self.ym) for d in days))
        self.assertNotIn("month_calendar", self.page(None))


class TheReportSaysItsBillsBasis(unittest.TestCase):
    def test_the_monthly_bills_bar_says_it_is_the_months_due_dates(self):
        from oikonome.web import lens_email
        from .test_monthly_email_reads_like_the_month_lens import _month, _year
        ctx = lens_email.month_mail_context(_month(), _year(), 2026, 9)
        self.assertIn("planned from the month's due dates", ctx["rows"][0]["note"])


if __name__ == "__main__":
    unittest.main()
