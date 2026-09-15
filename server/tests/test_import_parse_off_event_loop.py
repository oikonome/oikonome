"""The async import endpoints must hand the parse + upsert to a worker
thread. Parsing a 50 MB OFX/ZIP or OCRing a tax document takes seconds to
minutes; done on the event loop it freezes every other request the process
is serving. Each fake parser below asserts it is NOT called from a thread
with a running event loop — the shape of the bug, not its symptom."""

import asyncio
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


SEEN: list[str] = []


def _assert_off_loop():
    SEEN.append("called")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return                                   # a worker thread: good
    raise AssertionError("parser ran on the event loop thread")


class ImportParseOffEventLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"loop-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def setUp(self):
        SEEN.clear()

    def test_single_file_import_parses_in_a_worker_thread(self):
        # `role` rides along since the household gained a member tier —
        # a ZIP through this door is a restore, which is the owner's
        def fake_dispatch(conn, account_id, amount_sign, filename, data,
                          role=""):
            _assert_off_loop()
            return {"imported": 0}, None
        with mock.patch("oikonome.web.pages.dispatch_import", fake_dispatch):
            r = self.client.post(
                "/api/import",
                files={"file": ("bank.csv", b"Date,Amount\n", "text/csv")},
                data={"account_id": "chk", "amount_sign": "bank"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["result"], {"imported": 0})
        self.assertTrue(SEEN)

    def test_nojs_import_page_parses_in_a_worker_thread(self):
        """The no-JS /import form was the ONE door left running the parse
        on the event loop — a big upload froze the whole single-worker
        instance for every tenant until it finished."""
        def fake_dispatch(conn, account_id, amount_sign, filename, data,
                          role=""):
            _assert_off_loop()
            return {"imported": 0}, None
        with mock.patch("oikonome.web.pages.dispatch_import", fake_dispatch):
            r = self.client.post(
                "/import",
                files={"file": ("bank.csv", b"Date,Amount\n", "text/csv")},
                data={"account_id": "chk", "amount_sign": "bank"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(SEEN)

    def test_nojs_import_zip_goes_to_the_background_job(self):
        """A ZIP through the no-JS door is a whole-ledger restore that can
        run for minutes — it must start the background job (like
        /api/import's zip branch), never run inline on the request."""
        started = []
        def fake_start(tenant_id, data):
            started.append(tenant_id)
            return {"restore_started": True, "job": "restore"}
        with mock.patch("oikonome.sync.restore_job.start", fake_start):
            r = self.client.post(
                "/import",
                files={"file": ("export.zip", b"PK\x03\x04junk",
                                "application/zip")},
                data={"account_id": "", "amount_sign": "bank"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(started), 1)
        self.assertIn("restore started", r.text)

    def test_bulk_analyze_parses_in_a_worker_thread(self):
        def fake_analyze(conn, staged):
            _assert_off_loop()
            return {"files": [{"kind": "csv"} for _ in staged]}
        with mock.patch("oikonome.web.bulk_import.analyze", fake_analyze):
            r = self.client.post(
                "/api/import/bulk/analyze",
                files=[("files", ("a.csv", b"Date,Amount\n", "text/csv"))])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(SEEN)

    def test_taxdoc_analyze_parses_in_a_worker_thread(self):
        def fake_analyze(conn, filename, data, mime):
            _assert_off_loop()
            return {"rows": [], "token": "t"}
        with mock.patch("oikonome.engine.taxdocs.analyze", fake_analyze):
            r = self.client.post(
                "/api/import/taxdoc/analyze",
                files={"file": ("w2.pdf", b"%PDF-1.4", "application/pdf")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(SEEN)
