"""The ledger page's download must hold exactly the rows the page would
list under the same filters — every page of them — in a file a
spreadsheet opens cleanly: a CSV, or a one-sheet .xlsx, with payees that
look like formulas neutralised and the sign a person reads (money out
negative)."""
import csv
import datetime as dt
import io
import os
import unittest
import uuid
import zipfile
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config

LOCAL_NOW = dt.datetime.combine(TODAY, dt.time(9, 0))


class TransactionsDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"txdl-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=1000, other_monthly=1000)
            d = dt.date(TODAY.year, TODAY.month, 3)
            add_txn(conn, d, 40.0, "SAFEWAY", primary="FOOD_AND_DRINK")
            add_txn(conn, d, 12.5, "SAFEWAY FUEL", primary="TRANSPORTATION")
            add_txn(conn, d, 99.0, "=HYPERLINK(\"x\")", primary="GENERAL_MERCHANDISE")
            add_txn(conn, d, -2500.0, "PAYROLL", primary="INCOME")
        finally:
            conn.close()

    def get(self, path: str, **headers):
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL_NOW):
            return self.client.get(path, headers=headers)

    def _csv_rows(self, r):
        rows = list(csv.reader(io.StringIO(r.text)))
        return rows[0], rows[1:]

    def test_a_search_downloads_the_rows_the_page_lists(self):
        page = self.get("/api/transactions?q=safeway").json()
        r = self.get("/api/transactions/export?q=safeway&format=csv")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertIn("transactions-safeway.csv", r.headers["content-disposition"])
        header, rows = self._csv_rows(r)
        self.assertEqual({row[header.index("id")] for row in rows},
                         {x["id"] for x in page["rows"]})
        self.assertEqual(len(rows), page["total"])

    def test_the_month_view_downloads_the_whole_month(self):
        page = self.get(f"/api/transactions?y={TODAY.year}&m={TODAY.month}").json()
        r = self.get(f"/api/transactions/export?y={TODAY.year}&m={TODAY.month}")
        self.assertEqual(r.status_code, 200, r.text)
        header, rows = self._csv_rows(r)
        self.assertEqual(len(rows), len(page["rows"]))
        self.assertIn(f"transactions-{TODAY.year}-{TODAY.month:02d}.csv",
                      r.headers["content-disposition"])

    def test_money_out_is_negative_and_money_in_positive(self):
        r = self.get("/api/transactions/export?q=payroll")
        header, rows = self._csv_rows(r)
        self.assertEqual(float(rows[0][header.index("amount")]), 2500.0)
        r = self.get("/api/transactions/export?q=safeway%20fuel")
        header, rows = self._csv_rows(r)
        self.assertEqual(float(rows[0][header.index("amount")]), -12.5)

    def test_a_payee_that_looks_like_a_formula_is_neutralised(self):
        r = self.get("/api/transactions/export?q=hyperlink")
        header, rows = self._csv_rows(r)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][header.index("payee")].startswith("'="))

    def test_xlsx_is_a_workbook_with_the_same_rows(self):
        page = self.get("/api/transactions?q=safeway").json()
        r = self.get("/api/transactions/export?q=safeway&format=xlsx")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("spreadsheetml.sheet", r.headers["content-type"])
        self.assertIn(".xlsx", r.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            names = set(z.namelist())
            self.assertIn("xl/workbook.xml", names)
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
        for x in page["rows"]:
            self.assertIn(x["id"], sheet)
        self.assertIn("SAFEWAY FUEL", sheet)
        # one header row plus one row per transaction
        self.assertEqual(sheet.count("<row "), 1 + len(page["rows"]))

    def test_an_unknown_format_and_a_cross_site_start_are_refused(self):
        self.assertEqual(
            self.get("/api/transactions/export?format=pdf").status_code, 400)
        r = self.get("/api/transactions/export?format=csv",
                     **{"sec-fetch-site": "cross-site"})
        self.assertEqual(r.status_code, 403)
