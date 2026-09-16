"""Business-expense flagging: Schedule C marker — flag/unflag, list,
CSV export, verdict unaffected, export/restore round-trip."""

import datetime as dt
import io
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config
from .export_ticket import export_get


def _wait_restore(client, tries: int = 100) -> None:
    """The /import zip door starts a background restore; wait it out."""
    import time
    for _ in range(tries):
        if client.get("/api/restore/progress").json()["state"] \
                in ("done", "error"):
            return
        time.sleep(0.1)


def _seed_txns(conn):
    for i, (d, amt, name) in enumerate([
            ("2026-07-01", 44.0, "CLOUDGROVE HOSTING"),
            ("2026-07-05", 100.0, "SECRETARY OF STATE FILING FEE"),
            ("2026-07-08", -12.0, "CLOUDGROVE HOSTING REFUND"),
            ("2025-11-03", 12.99, "DOMAIN REGISTRAR"),
            ("2026-07-06", 60.0, "GROCERY MART")]):
        conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                                         pending, removed)
               VALUES (%s,'chk',%s,%s,%s,0,0)
               ON CONFLICT (tenant_id, id) DO NOTHING""",
            (f"biz:{i}", dt.date.fromisoformat(d), amt, name))


class BusinessFlagTests(unittest.TestCase):
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
            "email": f"biz-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            _seed_txns(conn)
        finally:
            conn.close()

    def test_flag_list_year_filter_and_unflag(self):
        for i in (0, 1, 2, 3):
            r = self.client.post(f"/api/business/biz:{i}/flag")
            self.assertEqual(r.status_code, 200)
        allb = self.client.get("/api/business").json()
        self.assertEqual(allb["count"], 4)
        # +44 +100 -12 +12.99 = 144.99 — refund nets against the total
        self.assertEqual(allb["total"], 144.99)
        self.assertEqual(allb["years"], [2026, 2025])
        y26 = self.client.get("/api/business?year=2026").json()
        self.assertEqual(y26["count"], 3)
        self.assertEqual(y26["total"], 132.0)
        # unflag drops it everywhere
        self.client.post("/api/business/biz:3/unflag")
        self.assertEqual(self.client.get("/api/business").json()["count"], 3)
        # flag is visible on the transactions feed
        rows = self.client.get("/api/transactions?y=2026&m=7").json()["rows"]
        flags = {r["id"]: r["biz_flag"] for r in rows}
        self.assertTrue(flags["biz:0"])
        self.assertFalse(flags["biz:4"])

    def test_worksheet_pages_but_totals_cover_the_whole_scope(self):
        """The list endpoint is bounded: rows honor limit/offset (a
        decade-old ledger must not ship whole to paint one screen), while
        total/count keep covering the entire scope so the headline stays
        truthful on any page — and the CSV export remains the full,
        unpaged worksheet."""
        for i in (0, 1, 2, 3):
            self.client.post(f"/api/business/biz:{i}/flag")
        full = self.client.get("/api/business").json()
        self.assertGreaterEqual(full["count"], 4)
        page = self.client.get("/api/business",
                               params={"limit": 2, "offset": 1}).json()
        self.assertEqual(len(page["rows"]), 2)
        self.assertEqual(page["count"], full["count"])
        self.assertEqual(page["total"], full["total"])
        # newest-first ordering means the page is a window, not a shuffle
        self.assertEqual([r["id"] for r in page["rows"]],
                         [r["id"] for r in full["rows"][1:3]])
        # the CSV export carries every flagged row regardless of paging
        csv_body = self.client.get("/api/business/export.csv").text
        for r in full["rows"]:
            self.assertIn(r["payee"], csv_body)

    def test_flag_nonexistent_txn_is_noop(self):
        r = self.client.post("/api/business/ghost:1/flag")
        self.assertEqual(r.status_code, 200)
        ids = [x["id"] for x in self.client.get("/api/business").json()["rows"]]
        self.assertNotIn("ghost:1", ids)

    def test_csv_export(self):
        self.client.post("/api/business/biz:0/flag")
        r = self.client.get("/api/business/export.csv?year=2026")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["content-type"])
        body = r.text
        self.assertIn("CLOUDGROVE HOSTING", body)
        self.assertIn("total", body)
        self.assertNotIn("DOMAIN REGISTRAR", body)   # 2025 row filtered out

    def test_verdict_math_unchanged_by_flagging(self):
        from oikonome.web import report
        conn = tenancy.tenant_connect(self.tid)
        try:
            before = report.gather(conn, dt.date(2026, 7, 15))["buckets"]
            self.client.post("/api/business/biz:4/flag")   # the grocery run
            after = report.gather(conn, dt.date(2026, 7, 15))["buckets"]
        finally:
            conn.close()
        self.assertEqual(
            {k: v["actual"] for k, v in before.items()},
            {k: v["actual"] for k, v in after.items()})
        self.client.post("/api/business/biz:4/unflag")

    def test_export_restore_roundtrip_carries_flags(self):
        self.client.post("/api/business/biz:0/flag")
        z = export_get(self.client, "/export")
        names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        self.assertIn("business_flags.csv", names)
        # restore into a FRESH tenant
        c2 = TestClient(self.client.app)
        c2.post("/api/signup", data={
            "email": f"biz2-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        r = c2.post("/import", files={"file": ("oikonome-export.zip",
                                               z.content, "application/zip")},
                    data={"account_id": ""})
        self.assertEqual(r.status_code, 200)
        # the no-JS door now starts the same background restore job as
        # /api/import (a big archive outlives the edge timeout); wait
        _wait_restore(c2)
        got = c2.get("/api/business").json()
        self.assertGreaterEqual(got["count"], 1)
        self.assertIn("biz:0", [x["id"] for x in got["rows"]])

    def test_restore_carries_reimbursement_pairs(self):
        from oikonome.db import tenancy as _t
        conn = _t.tenant_connect(self.tid)
        try:
            conn.execute(
                "INSERT INTO reimbursements (expense_id, reimburse_id) "
                "VALUES ('biz:0','biz:2') ON CONFLICT DO NOTHING")
        finally:
            conn.close()
        z = export_get(self.client, "/export")
        c2 = TestClient(self.client.app)
        c2.post("/api/signup", data={
            "email": f"biz3-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        c2.post("/import", files={"file": ("oikonome-export.zip",
                                           z.content, "application/zip")},
                data={"account_id": ""})
        _wait_restore(c2)
        tid2 = c2.get("/api/me").json()["tenant_id"]
        conn = _t.tenant_connect(tid2)
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM reimbursements "
                             "WHERE expense_id='biz:0'").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
