"""A hand-typed amount reads either separator convention the way the CSV
importer does.

Stripping every comma before parsing stores a decimal-comma "12,50" as
1250 and "1.234,56" as 1.23456, with no error to say so. A typed balance,
a typed amount field, a settings figure, and the bulk importer's sign
sniff all read "1.234,56" and "12,50" as decimals, while "1,234.56" still
reads as 1234.56 and something that is not an amount is still refused."""

import os
import unittest
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import api, bulk_import

from .util import _ensure_db, seed_accounts, write_config


class TypedMoneyParsing(unittest.TestCase):
    def test_both_separator_conventions_read_as_the_same_number(self):
        for raw, want in (("1.234,56", 1234.56), ("12,50", 12.5),
                          ("1,234.56", 1234.56), ("$1,234", 1234.0),
                          ("-12,50", -12.5), ("1234.5", 1234.5),
                          ("", 0.0), ("  ", 0.0), (12.5, 12.5)):
            with self.subTest(raw=raw):
                self.assertAlmostEqual(api._typed_money(raw), want)

    def test_a_money_field_refuses_what_is_not_an_amount(self):
        for raw in ("1,2,3", "twelve", "12,5,0"):
            with self.subTest(raw=raw):
                with self.assertRaises(HTTPException) as cm:
                    api._money_float(raw, "amount")
                self.assertEqual(cm.exception.status_code, 400)

    def test_a_money_field_reads_a_decimal_comma(self):
        self.assertAlmostEqual(api._money_float("1.234,56", "amount"),
                               1234.56)
        self.assertAlmostEqual(api._money_float("12,50", "amount"), 12.5)


class DecimalCommaSignSniff(unittest.TestCase):
    def test_a_decimal_comma_bank_export_is_sniffed_as_money_out(self):
        csv = (b'Date,Description,Amount\n'
               b'15/01/2024,NORTHWIND CAFE,"-12,50"\n'
               b'16/01/2024,CONTOSO RENT,"-1.234,56"\n'
               b'17/01/2024,FABRIKAM MARKET,"-45,07"\n'
               b'18/01/2024,SPRINGFIELD PAYROLL,"2.000,00"\n')
        self.assertEqual(bulk_import._csv_sign_guess(csv), ("bank", True))

    def test_whole_numbers_are_not_votes(self):
        csv = (b"Date,Description,Amount\n"
               b"2024-01-15,NORTHWIND,\"1,234\"\n"
               b"2024-01-16,CONTOSO,-12\n"
               b"2024-01-17,FABRIKAM,-45\n")
        self.assertEqual(bulk_import._csv_sign_guess(csv), ("bank", False))


class TypedBalanceRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"comma-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute("INSERT INTO items (id,aggregator) "
                         "VALUES ('manual','manual') "
                         "ON CONFLICT (tenant_id,id) DO NOTHING")
            conn.execute("INSERT INTO accounts (id,item_id,name,type) "
                         "VALUES ('dc-manual','manual','Cash','depository')")
        finally:
            conn.close()

    def _balance(self, where: str, arg: str):
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                f"SELECT balance_current FROM accounts WHERE {where}",
                (arg,)).fetchone()
            return row["balance_current"] if row else None
        finally:
            conn.close()

    def test_a_typed_balance_reads_both_conventions(self):
        for raw, want in (("1.234,56", 1234.56), ("12,50", 12.5),
                          ("1,234.56", 1234.56)):
            with self.subTest(raw=raw):
                r = self.client.post("/accounts/balance", data={
                    "account_id": "dc-manual", "balance": raw},
                    follow_redirects=False)
                self.assertIn(r.status_code, (200, 303), r.text)
                self.assertAlmostEqual(
                    self._balance("id=%s", "dc-manual"), want)

    def test_a_new_manual_accounts_starting_balance_reads_a_decimal_comma(
            self):
        name = f"Fabrikam Savings {uuid.uuid4().hex[:6]}"
        r = self.client.post("/accounts/add", data={
            "name": name, "balance": "1.234,56"}, follow_redirects=False)
        self.assertIn(r.status_code, (200, 303), r.text)
        self.assertAlmostEqual(self._balance("name=%s", name), 1234.56)

    def test_a_settings_figure_reads_a_decimal_comma(self):
        r = self.client.post("/api/settings",
                             json={"food_monthly": "1.234,56"})
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get("/api/settings").json().get("food_monthly")
        self.assertAlmostEqual(float(got), 1234.56)


if __name__ == "__main__":
    unittest.main()
