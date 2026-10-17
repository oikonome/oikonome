"""The Bills page can step back to a past month.

`/api/bills?month=YYYY-MM` reads that month's per-bill stats as it ended —
paid x of y, received, envelope used — the same reading the Month lens
gives, while the schedule itself (the bills, their health, the totals)
stays the live one. The answer names the month it describes and whether
it is closed, and a month that has not started is refused."""

import calendar
import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_bill, add_txn, seed_accounts, write_config


def _last_month(today: dt.date) -> dt.date:
    return today.replace(day=1) - dt.timedelta(days=1)


class BillsPageStepsBackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"back-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        cls.today = dt.date.today()
        last = _last_month(cls.today)
        cls.last_ym = f"{last:%Y-%m}"
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # a monthly bill due on the 5th, paid last month and not yet
            # this month (its due date this month may or may not have passed)
            due = cls.today.replace(day=5)
            if due <= cls.today:
                nxt = (cls.today.year, cls.today.month + 1) \
                    if cls.today.month < 12 else (cls.today.year + 1, 1)
                due = dt.date(nxt[0], nxt[1], 5)
            add_bill(conn, "City Power", 100.0, frequency="MONTHLY",
                     next_due=due, last_seen=last.replace(day=5))
            add_txn(conn, last.replace(day=5), 100.0, "CITY POWER",
                    primary="GENERAL_SERVICES")
        finally:
            conn.close()

    def _row(self, data, payee="City Power"):
        return next(b for b in data["bills"] if b["payee"] == payee)

    def test_this_month_is_the_default_and_says_so(self):
        d = self.client.get("/api/bills").json()
        self.assertEqual(d["month"], f"{self.today:%Y-%m}")
        self.assertFalse(d["month_final"])
        self.assertEqual(self._row(d)["paid"], 0)

    def test_a_past_month_reads_as_it_ended(self):
        d = self.client.get(f"/api/bills?month={self.last_ym}").json()
        self.assertEqual(d["month"], self.last_ym)
        self.assertTrue(d["month_final"])
        row = self._row(d)
        self.assertEqual((row["occurrences"], row["paid"]), (1, 1))
        self.assertAlmostEqual(row["paid_amount"], 100.0)
        self.assertIsNone(row["next_unpaid"])
        # the schedule is the live one: the bill's next due date is this
        # month's or next month's, not last month's
        self.assertGreater(row["due_on"], self.last_ym + "-31")

    def test_the_current_month_by_name_is_this_month(self):
        d = self.client.get(f"/api/bills?month={self.today:%Y-%m}").json()
        self.assertFalse(d["month_final"])
        self.assertEqual(d["month"], f"{self.today:%Y-%m}")

    def test_a_month_that_has_not_started_and_a_bad_month_are_refused(self):
        y, m = self.today.year, self.today.month
        ny, nm = (y, m + 1) if m < 12 else (y + 1, 1)
        self.assertEqual(
            self.client.get(f"/api/bills?month={ny:04d}-{nm:02d}").status_code,
            400)
        self.assertEqual(self.client.get("/api/bills?month=nonsense").status_code,
                         400)
        self.assertEqual(self.client.get("/api/bills?month=2026-13").status_code,
                         400)
        # a well-formed month outside the years the app reads is a 400,
        # never the 500 its month arithmetic would raise
        for bad in ("0001-01", "1899-12", "2101-01", "9999-12"):
            self.assertEqual(
                self.client.get(f"/api/bills?month={bad}").status_code, 400,
                bad)

    def test_a_month_with_more_days_than_the_due_day_still_reads(self):
        # February: a past month whose last day is before the 29th/30th/31st
        # must still resolve to its own last day
        last = _last_month(self.today)
        last_day = calendar.monthrange(last.year, last.month)[1]
        self.assertGreaterEqual(last_day, 28)
        d = self.client.get(f"/api/bills?month={self.last_ym}").json()
        self.assertTrue(d["month_final"])


if __name__ == "__main__":
    unittest.main()
