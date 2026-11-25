"""The ledger row is ONE shape across server, web and mobile.

The server's Txn model (web/schemas/ledger.py) is the contract; both
clients' Txn type is generated from it with every other shape
(scripts/gen-api-types.py), and the conformance hook checks each row a
test receives. Here: both clients take Txn from the generated file and
hand-write none, and the API emits EXACTLY the model's keys on every ledger
surface — a server field no client knows, or a client field the server
never sends, is a failure. The hook alone would let a key the model makes
optional quietly go missing; this pins the full key set.
"""

import datetime as dt
import pathlib
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config

from oikonome.web.schemas.ledger import Txn

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIELDS = set(Txn.model_fields)
REQUIRED = {n for n, f in Txn.model_fields.items() if f.is_required()
            and f.annotation is not None
            and "None" not in str(f.annotation)}


class ClientsTakeTheGeneratedTxn(unittest.TestCase):
    def test_both_clients_alias_the_generated_type(self):
        for f in ("webapp/src/api/client.ts", "mobile/src/lib/api.ts"):
            src = (ROOT / f).read_text(encoding="utf-8")
            self.assertIn('export type Txn = S["Txn"];', src, f)
            self.assertNotIn("export interface Txn {", src,
                             f"{f} must not hand-write Txn — it is generated")


class ApiShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"lrc-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.d = dt.date.today().replace(day=10)
            add_txn(conn, cls.d, 42.0, "SAMPLE SHOP", account="chk")
        finally:
            conn.close()

    def _check(self, row: dict, where: str, extras: set | None = None):
        want = FIELDS
        got = set(row) - (extras or set())
        self.assertEqual(got - want, set(),
                         f"{where}: server sends fields the contract lacks")
        self.assertEqual(want - got, set(),
                         f"{where}: contract fields the server never sent")
        for k in REQUIRED:
            self.assertIsNotNone(row.get(k), f"{where}: {k} is required, got null")

    def test_month_search_and_today_rows_match_the_contract(self):
        r = self.client.get("/api/transactions",
                            params={"y": self.d.year, "m": self.d.month}).json()
        self._check(r["rows"][0], "month view")
        r = self.client.get("/api/transactions", params={"q": "sample"}).json()
        self._check(r["rows"][0], "search")
        h = self.client.get("/api/bills/history", params={"payee": "Sample Shop"}).json()
        if h.get("txns"):
            # the history page's rows are ledger rows plus the page's own
            # two marks (HistoryTxn = Txn & {txn_id, hit} on both clients)
            self._check(h["txns"][0], "merchant history", extras={"txn_id", "hit"})


if __name__ == "__main__":
    unittest.main()
