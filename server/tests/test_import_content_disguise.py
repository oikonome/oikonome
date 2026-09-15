"""A full-ledger restore must not be reachable by disguising the payload.

The import hub treats a ``.zip`` upload as a full-ledger restore
(restore.restore_zip) and refuses that branch for script-token callers —
a leaked collector token must never merge an attacker's export into the
tenant. The refusal keys on the principal; the dispatch to restore keys
on the FILENAME, and nothing on this path may ever sniff content. These
tests pin both halves: bytes that ARE a ZIP archive but claim a CSV name
or content-type can never reach restore_zip at all, and the name-keyed
refusal holds regardless of the claimed content-type or name casing. If
a future change routes uploads by magic bytes, this fails first.
"""

import csv
import io
import os
import time
import unittest
import uuid
import zipfile
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


def _export_zip(rowid: str = "zdg-1") -> bytes:
    """A minimal export ZIP the way /export writes one. Each test plants
    its own row id so a test that legitimately restores can never satisfy
    another test's must-not-restore assertion."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(
            s, fieldnames=["id", "account_id", "date", "amount", "name"])
        w.writeheader()
        w.writerow({"id": rowid, "account_id": "chk",
                    "date": "2026-07-01", "amount": "12.5",
                    "name": "RESTORED ROW"})
        z.writestr("transactions.csv", s.getvalue())
    return buf.getvalue()


class DisguisedZipRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"dz-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        minted = cls.client.post(
            "/api/tokens",
            json={"name": "zip-disguise",
                  "password": "correct-horse-battery"}).json()
        cls.hdr = {"Authorization": f"Bearer {minted['token']}"}

    def _count_restored(self, rowid: str = "zdg-1"):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT count(*) n FROM transactions "
                "WHERE id=%s", (rowid,)).fetchone()["n"]
        finally:
            conn.close()

    def test_zip_bytes_under_a_csv_name_never_reach_restore(self):
        """ZIP-magic bytes claiming to be a CSV go down the CSV path —
        restore_zip is not merely refused, it is never invoked."""
        from oikonome.sync import restore
        bare = TestClient(self.client.app)
        cases = [("statement.csv", "text/csv"),
                 ("export.zip.csv", "application/zip"),
                 ("upload", "application/octet-stream")]
        for fname, ctype in cases:
            with self.subTest(fname=fname, ctype=ctype), \
                    mock.patch.object(restore, "restore_zip") as rz:
                r = bare.post("/api/import", headers=self.hdr,
                              data={"account_id": "chk",
                                    "amount_sign": "bank"},
                              files={"file": (fname, _export_zip(), ctype)})
                # not the restore refusal, and not a restore either: the
                # bytes fall through to the CSV/mapping path
                self.assertEqual(r.status_code, 200, r.text)
                rz.assert_not_called()
            self.assertEqual(self._count_restored(), 0)

    def test_zip_name_refusal_ignores_content_type_and_case(self):
        """The name-keyed refusal must not be bypassed by an innocent
        content-type or an upper-case extension."""
        bare = TestClient(self.client.app)
        for fname, ctype in [("export.zip", "text/csv"),
                             ("EXPORT.ZIP", "text/csv")]:
            with self.subTest(fname=fname):
                r = bare.post("/api/import", headers=self.hdr,
                              data={"account_id": "",
                                    "amount_sign": "bank"},
                              files={"file": (fname, _export_zip(), ctype)})
                self.assertEqual(r.status_code, 403, r.text)
                self.assertEqual(self._count_restored(), 0)

    def test_a_signed_in_session_still_restores(self):
        """The guard scopes to token principals — the owner's own session
        restore (the recovery path) must keep working."""
        r = self.client.post("/api/import",
                             data={"account_id": "", "amount_sign": "bank"},
                             files={"file": ("export.zip",
                                             _export_zip("zdg-sess"),
                                             "application/zip")})
        self.assertEqual(r.status_code, 200, r.text)
        # the owner's restore starts a background job (a real archive
        # outlives the edge timeout); wait for it, then check it landed
        self.assertTrue((r.json().get("result") or {}).get("restore_started"))
        for _ in range(100):
            if self.client.get("/api/restore/progress").json()["state"] \
                    in ("done", "error"):
                break
            time.sleep(0.1)
        self.assertEqual(self._count_restored("zdg-sess"), 1)
