"""A file import takes the pairing and merchant-identity locks before it
locks any transaction row.

Every writer that pairs receipts or reimbursements takes the pairing lock
first and the rows second, and every merchant rename takes the household
identity lock before its rows. A file import writes its whole file in one
transaction and, near the end, matches waiting receipts (pairing lock) and
resolves merchants (identity lock). Taken in that order, the import holds
a restated row while it waits for a lock that a person's edit of that same
row already holds, and the edit then waits for the row: a cycle Postgres
breaks by killing one side, so the edit answers 500 or the import's
merchant and receipt work is silently dropped. Taking both locks first,
before any row, leaves no cycle to form: the import waits, holding
nothing the edit needs.
"""

import os
import threading
import time
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts

PW = "correct-horse-battery"
CSV = (b"Date,Description,Amount\n"
       b"2026-03-02,CORNER BAKERY,-12.00\n"
       b"2026-03-03,HARDWARE STORE,-40.00\n")


class FileImportLockOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.c = TestClient(self.app)
        email = f"lk-{uuid.uuid4().hex[:10]}@example.dev"
        r = self.c.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        admin = tenancy.admin_connect()
        try:
            self.tid = str(admin.execute(
                "SELECT tenant_id FROM users WHERE email=%s",
                (email,)).fetchone()["tenant_id"])
        finally:
            admin.close()
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
        finally:
            conn.close()
        self.assertFalse(self._import().get("error"))
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.row = conn.execute(
                "SELECT id FROM transactions WHERE name='CORNER BAKERY'"
            ).fetchone()["id"]
            # a receipt waiting for its charge: the matcher runs, and takes
            # the pairing lock, on every import while one waits
            conn.execute("INSERT INTO receipts (image, mime, status) "
                         "VALUES ('\\x00', 'image/png', 'uploaded')")
        finally:
            conn.close()

    def _import(self) -> dict:
        r = self.c.post("/api/import", data={"account_id": "chk",
                                             "amount_sign": "bank"},
                        files={"file": ("chk.csv", CSV, "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["result"] or {}

    def _import_waits_on_an_advisory_lock(self, holder_pid: int) -> bool:
        admin = tenancy.admin_connect()
        try:
            for _ in range(200):
                if admin.execute(
                        "SELECT 1 FROM pg_locks WHERE locktype='advisory'"
                        " AND NOT granted AND pid <> %s",
                        (holder_pid,)).fetchone():
                    return True
                time.sleep(0.05)
            return False
        finally:
            admin.close()

    @staticmethod
    def _deadlocks() -> int:
        admin = tenancy.admin_connect()
        try:
            admin.execute("SELECT pg_stat_clear_snapshot()")
            return admin.execute(
                "SELECT deadlocks FROM pg_stat_database"
                " WHERE datname = current_database()").fetchone()["deadlocks"]
        finally:
            admin.close()

    def _bulk_import(self) -> dict:
        """The same file re-imported through a folder import's job."""
        from unittest import mock

        from oikonome.jobs import bulk_import_job
        r = self.c.post("/api/import/bulk/analyze",
                        files=[("files", ("chk.csv", CSV, "text/csv"))])
        self.assertEqual(r.status_code, 200, r.text)
        plan = r.json()
        with mock.patch.object(bulk_import_job, "nudge"):
            r = self.c.post("/api/import/bulk/run", json={
                "token": plan["token"], "background": True,
                "files": [dict(f, account_id="chk") for f in plan["files"]]})
        self.assertEqual(r.status_code, 202, r.text)
        job_id = r.json()["job_id"]
        bulk_import_job.run_job(self.tid, job_id)
        conn = tenancy.tenant_connect(self.tid)
        try:
            st = bulk_import_job.status(conn, job_id)
        finally:
            conn.close()
        f = st["files"][0]
        return {} if f.get("ok") else {"error": f.get("error") or st}

    def _edit_while_importing(self, take_lock, importer=None) -> None:
        importer = importer or self._import
        before = self._deadlocks()
        edit = tenancy.tenant_connect(self.tid)
        out: dict = {}
        try:
            pid = edit.execute("SELECT pg_backend_pid() AS p").fetchone()["p"]
            with edit.transaction():
                take_lock(edit)
                worker = threading.Thread(
                    target=lambda: out.update(result=importer()))
                worker.start()
                self.assertTrue(self._import_waits_on_an_advisory_lock(pid),
                                "the re-import never reached a lock")
                # the person's edit of a row the import restates: must not
                # be the side a deadlock kills
                edit.execute("SET LOCAL lock_timeout = '20s'")
                edit.execute("UPDATE transactions SET name = name "
                             "WHERE id = %s", (self.row,))
            worker.join(timeout=60)
        finally:
            edit.close()
        self.assertFalse(worker.is_alive())
        self.assertFalse(out["result"].get("error"), out)
        # Either side of a cycle may be the one Postgres kills. When it is
        # the import, the matcher's and resolver's own savepoints swallow
        # it and the import still answers success — so the count of
        # deadlocks is the only honest witness. It is flushed by the
        # victim's backend within about a second of going idle.
        for _ in range(30):
            after = self._deadlocks()
            if after != before:
                break
            time.sleep(0.1)
        self.assertEqual(after, before, "the import and the edit deadlocked")

    def test_an_edit_holding_the_pairing_lock_meets_a_reimport(self):
        from oikonome.web.data import live_edit_ids
        self._edit_while_importing(lambda c: live_edit_ids(c, [self.row]))

    def test_a_rename_holding_the_identity_lock_meets_a_reimport(self):
        from oikonome.engine import merchant_identity
        self._edit_while_importing(merchant_identity._lock_identity)

    def test_a_folder_import_takes_them_in_the_same_order(self):
        from oikonome.engine import merchant_identity
        from oikonome.web.data import live_edit_ids
        self._edit_while_importing(lambda c: live_edit_ids(c, [self.row]),
                                   self._bulk_import)
        self._edit_while_importing(merchant_identity._lock_identity,
                                   self._bulk_import)


if __name__ == "__main__":
    unittest.main()
