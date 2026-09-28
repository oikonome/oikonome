"""Restoring a data ZIP is a step-up act, on every signed-in door.

A restore merges the archive's mail relay (`smtp_host`, `smtp_port`,
`smtp_user`, `smtp_from`) and AI endpoints over the household's own, and
keeps the household's stored relay password — so an archive pointing the
relay somewhere new hands that password, and every reset link after it,
to whoever runs that host. A Settings save may only do that after a
step-up; the restore doors owe the same proof, and they must ask BEFORE
the background job starts, because a running job cannot refuse.

Each door: a stale elevation is refused 403 elevation_required and the
stored relay is untouched; the same upload with the password in the body
restores (the legacy per-request proof both clients' retries rely on)."""

import csv
import io
import json
import os
import time
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db

PW = "correct-horse-battery"
OURS = "mail.household.test"
THEIRS = "relay.elsewhere.test"


def _archive() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps({"smtp_host": THEIRS,
                                          "smtp_port": "2525"})})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


class RestoreDoorsWantElevationTests(unittest.TestCase):
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
        self.email = f"rz-{uuid.uuid4().hex[:10]}@example.dev"
        r = self.c.post("/api/signup",
                        data={"email": self.email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        admin = tenancy.admin_connect()
        try:
            self.tid = str(admin.execute(
                "SELECT tenant_id FROM users WHERE email=%s",
                (self.email,)).fetchone()["tenant_id"])
        finally:
            admin.close()
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["smtp_host"] = OURS
        finally:
            conn.close()
        # elevated once, long enough ago that the window has closed
        self.assertEqual(self.c.post("/api/auth/elevate",
                                     json={"password": PW}).status_code, 200)
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE sessions SET elevated_at = now() - interval '20 min' "
                "WHERE user_id = (SELECT id FROM users WHERE email=%s)",
                (self.email,))
        finally:
            admin.close()

    def _smtp_host(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn).get("smtp_host")
        finally:
            conn.close()

    def _settled(self):
        for _ in range(200):
            st = self.c.get("/api/restore/progress").json()
            if st.get("state") != "running":
                return st
            time.sleep(0.05)
        self.fail("the restore never settled")

    def _refused(self, r):
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"]["error"], "elevation_required")
        self.assertEqual(self._smtp_host(), OURS)
        self.assertNotEqual(
            self.c.get("/api/restore/progress").json().get("state"),
            "running")

    def _restored(self):
        self._settled()
        self.assertEqual(self._smtp_host(), THEIRS)

    def test_the_api_upload_door(self):
        files = {"file": ("export.zip", _archive(), "application/zip")}
        self._refused(self.c.post("/api/import", files=files))
        r = self.c.post("/api/import", files=files, data={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["result"].get("restore_started"), r.text)
        self._restored()

    def test_the_form_door(self):
        files = {"file": ("export.zip", _archive(), "application/zip")}
        self._refused(self.c.post("/import", files=files))
        r = self.c.post("/import", files=files, data={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._restored()

    def test_the_bulk_plan_runner(self):
        r = self.c.post("/api/import/bulk/analyze", files=[
            ("files", ("export.zip", _archive(), "application/zip"))])
        self.assertEqual(r.status_code, 200, r.text)
        plan = r.json()
        decisions = [dict(f, action="import") for f in plan["files"]]
        body = {"token": plan["token"], "files": decisions}
        self._refused(self.c.post("/api/import/bulk/run", json=body))
        # the refusal consumed nothing: the same token runs once elevated
        r = self.c.post("/api/import/bulk/run", json=dict(body, password=PW))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["results"][0].get("restore_started"),
                        r.text)
        self._restored()

    def test_a_bulk_plan_without_an_archive_asks_nothing(self):
        csv_bytes = b"Date,Description,Amount\n2026-01-02,COFFEE,-4.50\n"
        r = self.c.post("/api/import/bulk/analyze", files=[
            ("files", ("chk.csv", csv_bytes, "text/csv"))])
        self.assertEqual(r.status_code, 200, r.text)
        plan = r.json()
        decisions = [dict(f, action="import", account_id=None,
                          new_account_name="Checking")
                     for f in plan["files"]]
        r = self.c.post("/api/import/bulk/run",
                        json={"token": plan["token"], "files": decisions})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
