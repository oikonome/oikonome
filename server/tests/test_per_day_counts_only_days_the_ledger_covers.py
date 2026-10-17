"""A per-day figure on the Transactions page divides by the days the ledger
covers, the way the month's pace does (budget.pace_window,
specs/short-ledger-pacing.md).

The days before a ledger's first transaction are no data, not days of no
spending. A ledger that began on the 10th and has spent $60 a day reads
"running $60/day" in the Today page's Why; the Food tile's listing, the
month header, a month's category and a lens label opened from the same
page must say the same $60 over the same 7 days, not $26 over 16.
"""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config

PASSWORD = "correct-horse-battery-staple-9"
TODAY = dt.date(2026, 10, 16)           # day 16 of October
FIRST = dt.date(2026, 10, 10)           # the ledger's first day
DAYS = (TODAY - FIRST).days + 1         # 7 days seen, today included


class PerDayFromTheLedgersFirstDayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"perday-short-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PASSWORD})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=1200, other_monthly=1200)
            d = FIRST
            while d <= TODAY:
                add_txn(conn, d, 60.0, "PANTRY MARKET",
                        primary="FOOD_AND_DRINK")
                d += dt.timedelta(days=1)
        finally:
            conn.close()
        cls.patches = [
            mock.patch("oikonome.web.api._today", return_value=TODAY),
            mock.patch("oikonome.localtime.now_local",
                       return_value=dt.datetime.combine(TODAY, dt.time(12)))]
        for p in cls.patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()

    def _get(self, **params):
        r = self.client.get("/api/transactions", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _assert_per_day(self, d):
        self.assertEqual((d["per_day"], d["per_day_days"]), (60.0, DAYS))

    def test_a_month_bucket_divides_from_the_ledgers_first_day(self):
        self._assert_per_day(self._get(bucket="food", y=2026, m=10))

    def test_a_bucket_read_on_a_past_day_counts_to_that_day(self):
        d = self._get(bucket="food", y=2026, m=10, as_of="2026-10-13")
        self.assertEqual((d["per_day"], d["per_day_days"]), (60.0, 4))

    def test_a_years_bucket_counts_only_the_days_the_ledger_covers(self):
        self._assert_per_day(self._get(bucket="food", y=2026))

    def test_the_month_header_divides_from_the_ledgers_first_day(self):
        d = self._get(y=2026, m=10)
        self.assertEqual(d["mode"], "month")
        self._assert_per_day(d)

    def test_a_months_category_divides_from_the_ledgers_first_day(self):
        self._assert_per_day(self._get(cat="FOOD_AND_DRINK", y=2026, m=10))

    def test_a_lens_label_divides_from_the_ledgers_first_day(self):
        self._assert_per_day(self._get(cat="FOOD_AND_DRINK", counted=1,
                                       y=2026, m=10))

    def test_a_years_lens_label_counts_only_the_days_the_ledger_covers(self):
        self._assert_per_day(self._get(
            cat="FOOD_AND_DRINK", counted=1,
            date_from="2026-01-01", date_to="2026-12-31"))


if __name__ == "__main__":
    unittest.main()
