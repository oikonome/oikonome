"""GET /api/transactions/one reads one ledger row, scoped to the household.

A screen that opens a transaction it was not handed (a deep link, the
phone after a restart) reads the row itself: exactly the row the list
door sends, a 404 for an id the household does not hold — another
household's row included, which must never be read across — and a 400
for no id at all.
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config


def _household():
    from oikonome.web.app import app
    c = TestClient(app)
    c.post("/api/signup", data={
        "email": f"one-{uuid.uuid4().hex[:8]}@example.dev",
        "password": "correct-horse-battery"})
    return c, c.get("/api/me").json()["tenant_id"]


class OneTransactionDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.a, tid_a = _household()
        cls.b, tid_b = _household()
        cls.d = dt.date.today().replace(day=10)
        cls.ids = {}
        for tid, key in ((tid_a, "a"), (tid_b, "b")):
            conn = tenancy.tenant_connect(tid)
            try:
                seed_accounts(conn)
                write_config(conn)
                cls.ids[key] = add_txn(
                    conn, cls.d, 42.0, f"SAMPLE SHOP {key.upper()}",
                    account="chk", txn_id=f"one-{key}-{uuid.uuid4().hex[:6]}")
            finally:
                conn.close()

    def test_the_row_is_the_list_doors_row(self):
        listed = self.a.get("/api/transactions", params={
            "y": self.d.year, "m": self.d.month}).json()["rows"]
        want = next(r for r in listed if r["id"] == self.ids["a"])
        r = self.a.get("/api/transactions/one", params={"id": self.ids["a"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), want)

    def test_another_households_row_is_not_found(self):
        r = self.b.get("/api/transactions/one", params={"id": self.ids["a"]})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertNotIn("SAMPLE SHOP A", r.text)
        # and each household still reads its own
        mine = self.b.get("/api/transactions/one", params={"id": self.ids["b"]})
        self.assertEqual(mine.status_code, 200, mine.text)
        self.assertEqual(mine.json()["id"], self.ids["b"])

    def test_unknown_and_missing_ids(self):
        self.assertEqual(self.a.get("/api/transactions/one",
                                    params={"id": "no-such-row"}).status_code,
                         404)
        self.assertEqual(self.a.get("/api/transactions/one",
                                    params={"id": ""}).status_code, 400)
        self.assertEqual(self.a.get("/api/transactions/one").status_code, 422)

    def test_signed_out_is_refused(self):
        from oikonome.web.app import app
        r = TestClient(app).get("/api/transactions/one",
                                params={"id": self.ids["a"]})
        self.assertIn(r.status_code, (401, 403))



class PhoneDetailReadsItsRow(unittest.TestCase):
    def test_the_detail_fetches_the_row_it_was_not_handed(self):
        import pathlib
        import oikonome
        root = pathlib.Path(oikonome.__file__).parent.parent.parent
        api = root / "mobile" / "src" / "lib" / "api.ts"
        txn = root / "mobile" / "src" / "app" / "txn.tsx"
        if not (api.exists() and txn.exists()):
            self.skipTest("mobile/ not present")
        self.assertIn('"/api/transactions/one?"', api.read_text())
        src = txn.read_text()
        self.assertIn("queryFn: () => client!.txn(String(id))", src)
        self.assertIn("enabled: !!client && !!id && !initial", src)
        self.assertIn('<FailedLine q={one} what="this transaction" />', src)

if __name__ == "__main__":
    unittest.main()
