"""A Spending figure's rows, through the doors a client reaches them by.

The Spending window says it can open a figure's rows (`figures_open_rows`),
so a phone built for this server links its figures and a phone talking to
a server from before the drill does not. The download of a drill holds
exactly the rows the drill lists — the same ids, every page of them —
each at its whole amount, as the page lists them; a bad drill is a 400
there too, never some other list. The design is specs/spending-drill.md.
"""
import csv
import datetime as dt
import io
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config

LOCAL_NOW = dt.datetime.combine(TODAY, dt.time(9, 0))
FOOD = "FOOD_AND_DRINK"


class SpendingDrillDoors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"drill-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=1000, other_monthly=1000)
            d = dt.date(TODAY.year, TODAY.month, 3)
            add_txn(conn, dt.date(2020, 1, 1), 5.0, "COFFEE HUT",
                    primary=FOOD)
            add_txn(conn, d, 40.0, "MARKET CAFE", primary=FOOD)
            add_txn(conn, d, 22.0, "CORNER DELI", primary=FOOD)
            add_txn(conn, d, 70.0, "HARDWARE BARN",
                    primary="GENERAL_MERCHANDISE")
            # one charge split across two categories: the food figure
            # counts its part, the row is listed once at its whole amount
            cls.split = add_txn(conn, d, 100.0, "MARKET STORE")
            for line, cat, amt in ((1, FOOD, 60.0),
                                   (2, "GENERAL_MERCHANDISE", 40.0)):
                conn.execute(
                    "INSERT INTO transaction_splits (txn_id, line, category,"
                    " amount, source) VALUES (%s,%s,%s,%s,'hand')",
                    (cls.split, line, cat, amt))
            add_txn(conn, d, -1500.0, "PAYROLL DEP", primary="INCOME")
        finally:
            conn.close()

    def get(self, path: str):
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL_NOW):
            return self.client.get(path)

    def _csv(self, r):
        rows = list(csv.reader(io.StringIO(r.text)))
        return rows[0], rows[1:]

    def test_the_spending_window_says_its_figures_open_their_rows(self):
        r = self.get("/api/reports/spending/window?range=cur")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json().get("figures_open_rows"), True)

    def test_a_drill_downloads_exactly_the_rows_it_lists(self):
        for q in (f"spend=cur&cat={FOOD}", "spend=cur",
                  "spend=cur&merchant=MARKET%20CAFE"):
            with self.subTest(drill=q):
                page = self.get(f"/api/transactions?{q}").json()
                self.assertTrue(page.get("drill_label"))
                r = self.get(f"/api/transactions/export?{q}&format=csv")
                self.assertEqual(r.status_code, 200, r.text)
                header, rows = self._csv(r)
                self.assertEqual({row[header.index("id")] for row in rows},
                                 {x["id"] for x in page["rows"]})
                self.assertEqual(len(rows), page["total"])

    def test_a_drills_download_lists_a_split_charge_once_at_its_whole_amount(self):
        page = self.get(f"/api/transactions?spend=cur&cat={FOOD}").json()
        self.assertEqual(page["amount_sum"], 122.0)
        r = self.get(f"/api/transactions/export?spend=cur&cat={FOOD}")
        header, rows = self._csv(r)
        split = [row for row in rows
                 if row[header.index("id")] == str(self.split)]
        self.assertEqual(len(split), 1)
        self.assertEqual(float(split[0][header.index("amount")]), -100.0)

    def test_a_bad_drill_is_refused_at_the_download_too(self):
        for q in ("spend=2w", "spend=cur&side=next",
                  f"spend=cur&cat={FOOD}&merchant=MARKET%20CAFE"):
            with self.subTest(drill=q):
                r = self.get(f"/api/transactions/export?{q}")
                self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
