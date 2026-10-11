"""A bulk edit takes the whole selection a person can make.

"Select all shown" on an unpaged view — a merchant's whole history, or a
busy month shown in full — selects every row on screen, which is routinely
more than a thousand. The bulk door accepts up to 10,000 ids, works through
them in slices so no one statement carries an unbounded id array, and stays
all-or-nothing: a failure part-way leaves none of the selection changed.
"""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts

N = 2500


class BulkEditWholeSelection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={
            "email": f"bulk-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            conn.execute(
                "INSERT INTO transactions (id, account_id, date, amount, name,"
                " merchant_name, category_primary, pending, removed, raw)"
                " SELECT 'b' || g, 'card', %s, 12.5, 'CORNER GROCER',"
                " 'CORNER GROCER', 'GENERAL_MERCHANDISE', 0, 0, '{}'::jsonb"
                " FROM generate_series(1, %s) AS g",
                (dt.date(2025, 3, 1), N))
        finally:
            conn.close()
        self.ids = [f"b{i}" for i in range(1, N + 1)]

    def _overridden(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT count(*) AS n FROM transactions"
                " WHERE category_override = 'FOOD_AND_DRINK'").fetchone()["n"]
        finally:
            conn.close()

    def test_a_selection_over_a_thousand_rows_is_applied_in_full(self):
        r = self.client.post("/api/transactions/bulk", json={
            "ids": self.ids, "action": "category",
            "category": "FOOD_AND_DRINK"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["applied"], N)
        self.assertEqual(r.json()["missing"], 0)
        self.assertEqual(self._overridden(), N)

    def test_a_failure_part_way_changes_nothing(self):
        from oikonome.web import data
        real = data.bulk_apply
        calls = []

        def flaky(conn, ids, action, category=""):
            calls.append(len(ids))
            if len(calls) == 2:
                raise RuntimeError("connection lost mid-selection")
            return real(conn, ids, action, category)
        client = TestClient(self.app, raise_server_exceptions=False)
        client.cookies = self.client.cookies
        with mock.patch.object(data, "bulk_apply", side_effect=flaky):
            r = client.post("/api/transactions/bulk", json={
                "ids": self.ids, "action": "category",
                "category": "FOOD_AND_DRINK"})
        self.assertEqual(r.status_code, 500, r.text)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(self._overridden(), 0,
                         "the slices before the failure must roll back too")

    def test_the_cap_is_ten_thousand(self):
        r = self.client.post("/api/transactions/bulk", json={
            "ids": [f"x{i}" for i in range(10_001)], "action": "category",
            "category": "FOOD_AND_DRINK"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"],
                         "ids must be a list of at most 10000")


if __name__ == "__main__":
    unittest.main()
