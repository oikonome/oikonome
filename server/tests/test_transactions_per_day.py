"""The Transactions page states spending per day for the window it shows.

A month divides its spending by the days elapsed so far (every day, once
the month is closed); a month's category and a from/to range divide their
own spending by their own window; a search with no start date has no window
and carries no per-day figure.
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
TODAY = dt.date(2025, 4, 10)


class TransactionsPerDayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        email = f"perday-{uuid.uuid4().hex[:8]}@example.dev"
        cls.client.post("/api/signup", data={"email": email,
                                             "password": PASSWORD})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            for d, amt in ((dt.date(2025, 3, 1), 30.0),
                           (dt.date(2025, 3, 2), 30.0),
                           (dt.date(2025, 3, 20), 33.0),
                           (dt.date(2025, 4, 2), 50.0)):
                add_txn(conn, d, amt, "PANTRY MARKET",
                        primary="FOOD_AND_DRINK")
        finally:
            conn.close()
        cls.today = mock.patch("oikonome.web.api._today", return_value=TODAY)
        cls.today.start()

    @classmethod
    def tearDownClass(cls):
        cls.today.stop()

    def _get(self, **params):
        r = self.client.get("/api/transactions", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_closed_months_category_divides_by_every_day(self):
        d = self._get(cat="FOOD_AND_DRINK", y=2025, m=3)
        self.assertEqual(d["spend_sum"], 93.0)
        self.assertEqual((d["per_day"], d["per_day_days"]), (3.0, 31))

    def test_the_current_month_divides_by_the_days_so_far(self):
        d = self._get(cat="FOOD_AND_DRINK", y=2025, m=4)
        self.assertEqual((d["per_day"], d["per_day_days"]), (5.0, 10))

    def test_a_date_range_divides_by_its_own_days(self):
        d = self._get(cat="FOOD_AND_DRINK", date_from="2025-03-01",
                      date_to="2025-03-03")
        self.assertEqual((d["per_day"], d["per_day_days"]), (20.0, 3))

    def test_a_search_with_no_start_date_has_no_per_day(self):
        d = self._get(q="pantry")
        self.assertIsNone(d["per_day"])
        self.assertIsNone(d["per_day_days"])

    def test_the_month_browse_divides_its_spending_by_the_months_days(self):
        d = self._get(y=2025, m=3)
        self.assertEqual(d["per_day_days"], 31)
        self.assertEqual(d["per_day"], round(d["out_sum"] / 31, 2))


if __name__ == "__main__":
    unittest.main()
