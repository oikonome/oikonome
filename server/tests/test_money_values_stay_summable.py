"""Every money value a person or an archive can store must stay summable.

A value can be finite on its own and still break every total over it: two
balances near the float ceiling add up to infinity, and net worth, the
runway and the other summaries then fail on every read until someone finds
and edits the balance. So the typed-money doors refuse anything past a
ceiling far above any household figure, and the restore door refuses an
archive whose numbers are not finite at all ('nan', 'inf', '1e999'),
landing nothing.
"""
import io
import os
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import restore

from .util import _ensure_db, make_db, write_config

_HUGE = "1e308"


class TypedMoneyIsBounded(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"summable-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            write_config(conn)
            conn.execute("INSERT INTO items (id,aggregator) "
                         "VALUES ('manual','manual') "
                         "ON CONFLICT (tenant_id,id) DO NOTHING")
            conn.execute("INSERT INTO accounts (id,item_id,name,type,"
                         "balance_current) VALUES "
                         "('sum-manual','manual','Jar','depository',10)")
        finally:
            conn.close()

    def _balance(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute("SELECT balance_current FROM accounts "
                                "WHERE id='sum-manual'").fetchone()[
                                    "balance_current"]
        finally:
            conn.close()

    def test_adding_an_account_with_an_overflowing_balance_is_refused(self):
        r = self.client.post("/api/accounts/add",
                             json={"name": "Big Jar", "balance": _HUGE})
        self.assertEqual(r.status_code, 400, r.text)

    def test_setting_an_overflowing_manual_balance_is_refused(self):
        r = self.client.post("/api/accounts/balance",
                             json={"account_id": "sum-manual",
                                   "balance": _HUGE})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._balance(), 10)

    def test_a_large_but_real_balance_is_still_accepted(self):
        r = self.client.post("/api/accounts/balance",
                             json={"account_id": "sum-manual",
                                   "balance": "-123,456.78"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._balance(), -123456.78)
        self.client.post("/api/accounts/balance",
                         json={"account_id": "sum-manual", "balance": "10"})

    def test_form_balance_door_does_not_store_an_overflowing_value(self):
        self.client.post("/accounts/balance",
                         data={"account_id": "sum-manual", "balance": _HUGE},
                         follow_redirects=False)
        self.assertNotEqual(self._balance(), 1e308)
        self.client.post("/api/accounts/balance",
                         json={"account_id": "sum-manual", "balance": "10"})

    def test_manual_asset_value_past_the_ceiling_is_refused(self):
        r = self.client.post("/api/settings", json={"manual_assets": [
            {"name": "Castle", "kind": "property", "value": _HUGE}]})
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_negative_mortgage_value_is_accepted(self):
        r = self.client.post("/api/settings", json={"manual_assets": [
            {"name": "Mortgage", "kind": "mortgage", "value": "-180000.5"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.post("/api/settings", json={"manual_assets": []})

    def test_settings_money_past_the_ceiling_is_refused(self):
        r = self.client.post("/api/settings", json={"food_monthly": 1e308})
        self.assertEqual(r.status_code, 400, r.text)


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


class RestoredNumbersAreFinite(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_non_finite_cells_are_refused_by_the_cell_readers(self):
        for bad in ("nan", "NaN", "inf", "-Infinity", "1e999"):
            with self.assertRaises(ValueError, msg=bad):
                restore._f(bad)
            with self.assertRaises(ValueError, msg=bad):
                restore._i(bad)
        self.assertEqual(restore._f("-12.5"), -12.5)
        self.assertIsNone(restore._f(""))

    def test_an_archive_with_a_nan_amount_lands_nothing(self):
        data = _zip({"transactions.csv": (
            "id,account_id,date,amount,name\n"
            "fin-ok,chk,2026-07-01,12.5,Invented Store\n"
            "fin-bad,chk,2026-07-02,nan,Invented Store\n")})
        with self.assertRaises(ValueError):
            restore.restore_zip(self.conn, data)
        self.conn.rollback()
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM transactions WHERE id IN ('fin-ok','fin-bad')"
        ).fetchone())

    def test_an_ordinary_archive_still_restores(self):
        counts = restore.restore_zip(self.conn, _zip({"transactions.csv": (
            "id,account_id,date,amount,name\n"
            "fin-ok2,chk,2026-07-01,-12.5,Invented Store\n")}))
        self.assertEqual(counts.get("transactions"), 1)


if __name__ == "__main__":
    unittest.main()
