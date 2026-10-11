"""A past year's year-end package states its balance sheet and capital
account as of December 31 of that year.

The package is what an accountant reads a year-end cash position and an
owner's ending capital from. Built from today's balances and the
all-history capital account, a 2024 package downloaded in 2026 would carry
every later draw, contribution, charge and profit, and match neither the
year end nor the profit-and-loss file beside it.
"""

import csv
import io
import os
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class YearEndPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"yearend-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        cls.eid = cls.client.post("/api/business/entities", json={
            "name": "Ledger LLC", "structure": "sole_prop",
            "business_start_date": "2020-01-01"}).json()["id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute("UPDATE accounts SET entity_id=%s "
                         "WHERE id IN ('chk','card')", (cls.eid,))
            # checking holds 5,000 today; card owes 250 today
            for tid, acct, day, amt, name in (
                    ("ye-1", "chk", "2024-06-01", -3000.0, "Client A"),
                    ("ye-2", "chk", "2024-08-01", 500.0, "Supplies"),
                    ("ye-3", "card", "2024-11-01", 50.0, "Postage"),
                    ("ye-4", "chk", "2025-03-01", 700.0, "Rent"),
                    ("ye-5", "chk", "2025-05-01", -1200.0, "Client B"),
                    ("ye-6", "card", "2025-02-01", 100.0, "Software")):
                conn.execute(
                    "INSERT INTO transactions (id, account_id, date, amount, "
                    "name, pending, removed) VALUES (%s,%s,%s,%s,%s,0,0)",
                    (tid, acct, day, amt, name))
            for kind, amt, day in (("contribution", 1000, "2024-03-01"),
                                   ("draw", 400, "2025-04-01")):
                conn.execute(
                    "INSERT INTO equity_movement (entity_id, kind, amount, "
                    "date) VALUES (%s,%s,%s,%s)", (cls.eid, kind, amt, day))
        finally:
            conn.close()

    def _sheet(self, year):
        r = self.client.get(
            f"/api/business/entities/{self.eid}/package.zip?year={year}",
            headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r.status_code, 200, r.text)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        rows = list(csv.reader(io.StringIO(
            z.read("balance-sheet.csv").decode())))
        return {row[0]: row[1] for row in rows if len(row) >= 2}, \
            z.read("README.txt").decode()

    def test_balances_and_capital_stand_at_the_years_end(self):
        sheet, readme = self._sheet(2024)
        self.assertEqual(sheet["As of"], "2024-12-31")
        self.assertIn("2024-12-31", readme)
        # 5,000 today, less the later rent, plus the later deposit undone
        self.assertEqual(sheet["Test Checking"], "4500.00")
        # 250 owed today, less the later 100 charge
        self.assertEqual(sheet["Test Card"], "150.00")
        # contribution 1,000 + 2024 profit (3,000 - 500 - 50); the 2025
        # draw and 2025 profit are not in it
        self.assertEqual(sheet["Capital account"], "3450.00")

    def test_an_account_whose_history_starts_later_is_not_guessed(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current,entity_id) VALUES ('sav','it1',"
                "'Late Savings','depository','savings',900,%s)", (self.eid,))
            conn.execute(
                "INSERT INTO transactions (id, account_id, date, amount, "
                "name, pending, removed) VALUES "
                "('ye-7','sav','2025-07-01',-900,'Opening deposit',0,0)")
        finally:
            conn.close()
        try:
            sheet, _ = self._sheet(2024)
            self.assertIn("Late Savings", sheet)
            self.assertIn("after year end", sheet["Late Savings"])
            self.assertEqual(sheet["Total assets"], "4500.00")
        finally:
            conn = tenancy.tenant_connect(self.tid)
            try:
                conn.execute("DELETE FROM transactions WHERE id='ye-7'")
                conn.execute("DELETE FROM accounts WHERE id='sav'")
            finally:
                conn.close()

    def test_the_pnl_export_states_the_same_year_end_capital(self):
        r = self.client.get(
            f"/api/business/entities/{self.eid}/pnl.csv?year=2024",
            headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r.status_code, 200, r.text)
        rows = {row[0]: row[1] for row in csv.reader(io.StringIO(r.text))
                if len(row) >= 2}
        self.assertEqual(rows["Capital account balance"], "3450.00")


if __name__ == "__main__":
    unittest.main()
