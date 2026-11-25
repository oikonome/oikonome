"""A household with no checking account is shown no cash forecast and no
short or tight notice, on the live day or a past one.

With no checking balance the forecast would walk from $0 minus the cards
and draw a false short line. The Today door therefore sends no forecast at
all for such a household (the walk is suppressed before the reply is
built), says why through runway.no_checking on the live day, and on a
past day sends neither a runway nor a forecast, since both are now-facts.
"""

from __future__ import annotations

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, write_config

TODAY = dt.date.today()


class NoCheckingNoForecast(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"nochk-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            conn.execute("INSERT INTO items (id, aggregator, "
                         "institution_name, access_token) VALUES "
                         "('it1', 'test', 'Test Bank', 'tok-test')")
            # a card and nothing else: no checking balance to walk from
            conn.execute("INSERT INTO accounts (id, item_id, name, type, "
                         "subtype, balance_current) VALUES ('card', 'it1', "
                         "'Test Card', 'credit', 'credit card', 900)")
            write_config(conn, food_monthly=600, other_monthly=400)
            for i in range(40):
                add_txn(conn, TODAY - dt.timedelta(days=i), 25.0, "SAFEWAY",
                        primary="FOOD_AND_DRINK")
        finally:
            conn.close()

    def _today(self, query=""):
        r = self.client.get("/api/today/full" + query)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _no_alarm(self, d):
        self.assertIsNone(d["forecast"])
        self.assertEqual(d["forecast_rows"], [])
        self.assertNotIn(d["cash_notice"]["state"], ("critical", "tight"))

    def test_the_live_day_says_why_and_draws_no_walk(self):
        d = self._today()
        self.assertTrue(d["runway"]["no_checking"])
        self._no_alarm(d)

    def test_a_past_day_draws_no_walk_either(self):
        past = TODAY - dt.timedelta(days=3)
        d = self._today(f"?date={past.isoformat()}")
        self.assertFalse(d["is_today"])
        self.assertIsNone(d["runway"])
        self._no_alarm(d)


if __name__ == "__main__":
    unittest.main()
