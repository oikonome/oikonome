"""The ledger row is ONE shape across server, web and mobile.

specs/ledger-row.schema.json is the contract; scripts/gen-ledger-row-types.py
writes the Txn type for both clients from it. Here: the generated files
are current (a schema edit without a regenerate fails), and the API emits
EXACTLY the schema's keys — a server field no client knows, or a client
field the server never sends, is a failure. A field can otherwise escape
both Txn types indefinitely without anything noticing."""

import datetime as dt
import json
import pathlib
import subprocess
import sys
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "specs" / "ledger-row.schema.json").read_text())


class GeneratedTypesTests(unittest.TestCase):
    def test_generated_ts_is_current(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen-ledger-row-types.py"),
                            "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_both_clients_import_the_generated_type(self):
        for f in ("webapp/src/api/client.ts", "mobile/src/lib/api.ts"):
            src = (ROOT / f).read_text(encoding="utf-8")
            self.assertIn('from "./ledger-row.generated"', src, f)
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
        want = set(SCHEMA["properties"])
        got = set(row) - (extras or set())
        self.assertEqual(got - want, set(),
                         f"{where}: server sends fields the contract lacks")
        self.assertEqual(want - got, set(),
                         f"{where}: contract fields the server never sent")
        for k in SCHEMA["required"]:
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
