"""The data downloads step up; a bare session cookie cannot start one.

A Lax cookie rides a cross-site GET, so downloads need a one-shot ticket
minted after step-up. `/export` (the household ZIP, receipt images
included) and `/export/dump` (the whole database on a single-tenant
self-host) are GETs; without the ticket, `<a href="https://<instance>/export">`
on any page the signed-in owner visits would drop their data into
Downloads, and the origin-check middleware only wraps write methods.

POST /api/export/token steps up (password, plus a live code on a TOTP
account — the same proof the connections bundle demands) and mints a
one-shot ticket the GET must redeem within a minute.
"""

import io
import os
import unittest
import uuid
import zipfile
from unittest import mock

from .util import _ensure_db, give_totp_factor

PW = "correct-horse-battery"


class ExportDownloadStepupTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.client = TestClient(appmod.app)
        cls.email = f"exs-{uuid.uuid4().hex[:8]}@example.dev"
        cls.client.post("/api/signup", data={"email": cls.email,
                                             "password": PW})

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def test_bare_get_is_refused(self):
        r = self.client.get("/export")
        self.assertEqual(r.status_code, 403)
        self.assertNotIn(b"PK", r.content[:2])

    def test_wrong_password_mints_nothing(self):
        r = self.client.post("/api/export/token",
                             json={"kind": "zip", "password": "nope-nope"})
        self.assertEqual(r.status_code, 401)

    def test_ticket_downloads_once_only(self):
        r = self.client.post("/api/export/token",
                             json={"kind": "zip", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        url = r.json()["url"]
        got = self.client.get(url)
        self.assertEqual(got.status_code, 200)
        self.assertIn("tenant_settings.csv",
                      zipfile.ZipFile(io.BytesIO(got.content)).namelist())
        # deleting on read is what makes a leaked URL worthless
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_the_download_names_what_a_restore_would_refuse(self):
        """The archive still downloads whole; the reasons the export found
        travel beside it, where a client can show them, and a clean
        archive carries none."""
        import json
        from oikonome.sync import export
        from oikonome.web import pages

        def _get():
            r = self.client.post("/api/export/token",
                                 json={"kind": "zip", "password": PW})
            return self.client.get(r.json()["url"])

        clean = _get()
        self.assertEqual(clean.status_code, 200)
        self.assertNotIn(pages.EXPORT_WARNINGS_HEADER, clean.headers)
        note = "notes.csv in this ZIP is too large to restore (9 MB — the cap)"
        with mock.patch.object(export, "restore_limits",
                               return_value=[note]):
            got = _get()
        self.assertEqual(got.status_code, 200)
        self.assertIn("RESTORE-NOTE.txt",
                      zipfile.ZipFile(io.BytesIO(got.content)).namelist())
        self.assertEqual(json.loads(
            got.headers[pages.EXPORT_WARNINGS_HEADER]), [note])

    def test_the_warnings_can_be_read_back_after_the_download(self):
        """A browser download is a navigation and the phone saves through
        a file API: neither reads a response header, so the export's
        findings are also answered by GET /api/export/last."""
        from oikonome.sync import export
        note = "the archive is 900 MB, over the 512 MB a restore upload takes"
        with mock.patch.object(export, "restore_limits",
                               return_value=[note]):
            r = self.client.post("/api/export/token",
                                 json={"kind": "zip", "password": PW})
            self.assertEqual(self.client.get(r.json()["url"]).status_code,
                             200)
        last = self.client.get("/api/export/last").json()
        self.assertEqual(last["warnings"], [note])
        self.assertTrue(last["at"])
        r = self.client.post("/api/export/token",
                             json={"kind": "zip", "password": PW})
        self.client.get(r.json()["url"])
        self.assertEqual(self.client.get("/api/export/last").json()
                         ["warnings"], [])

    def test_the_record_is_written_before_the_body_is_sent(self):
        """A client that starts the download by navigation polls
        /api/export/last until `at` moves past what it read before. The
        record is written once the archive is built, before its first byte
        goes out, so a poll during a long download already sees it."""
        from oikonome.db import tenancy
        from oikonome.web import pages
        admin = tenancy.admin_connect()
        try:
            row = admin.execute(
                "SELECT id, tenant_id FROM users WHERE email = %s",
                (self.email,)).fetchone()
        finally:
            admin.close()
        before = self.client.get("/api/export/last").json()["at"]
        user = {"role": "owner", "user_id": row["id"],
                "tenant_id": str(row["tenant_id"])}
        with mock.patch.object(pages, "_redeem_export_ticket"):
            resp = pages.export_data(user=user, t="oikx-test")
        try:
            after = self.client.get("/api/export/last").json()["at"]
            self.assertTrue(after)
            self.assertNotEqual(after, before)
        finally:
            # send the body now, which closes the archive's handle
            import asyncio

            async def drain():
                async for _ in resp.body_iterator:
                    pass
            asyncio.run(drain())

    def test_ticket_is_bound_to_its_kind_and_user(self):
        # a dump ticket does not open the ZIP door (nor the reverse)
        d = self.client.post("/api/export/token",
                             json={"kind": "dump", "password": PW})
        self.assertEqual(d.status_code, 200, d.text)
        self.assertEqual(
            self.client.get(f"/export?t={d.json()['token']}").status_code, 403)
        r = self.client.post("/api/export/token",
                             json={"kind": "zip", "password": PW})
        tok = r.json()["token"]
        # another account cannot redeem it either
        from fastapi.testclient import TestClient
        other = TestClient(self.app)
        other.post("/api/signup", data={
            "email": f"exo-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        self.assertEqual(other.get(f"/export?t={tok}").status_code, 403)
        # and the rightful owner still can — the failed tries burned nothing
        self.assertEqual(self.client.get(f"/export?t={tok}").status_code, 200)

    def test_ticket_expires(self):
        import datetime as dt
        from oikonome.web import pages
        with mock.patch.object(pages, "EXPORT_TICKET_TTL",
                               dt.timedelta(seconds=-1)):
            r = self.client.post("/api/export/token",
                                 json={"kind": "zip", "password": PW})
        self.assertEqual(self.client.get(r.json()["url"]).status_code, 403)

    def test_the_precheck_answers_without_spending_the_ticket(self):
        """The browser saves by navigating, so a refusal at the download
        would replace the app with a bare error page; the precheck gives
        the same answer first, and leaves the ticket for the download."""
        r = self.client.post("/api/export/token",
                             json={"kind": "zip", "password": PW})
        tok = r.json()["token"]
        pre = self.client.get(f"/api/export/precheck?kind=zip&t={tok}")
        self.assertEqual(pre.status_code, 204, pre.text)
        self.assertEqual(self.client.get(r.json()["url"]).status_code, 200)
        # spent now: the precheck says so, in JSON the app can show
        pre = self.client.get(f"/api/export/precheck?kind=zip&t={tok}")
        self.assertEqual(pre.status_code, 403)
        self.assertIn("expired or already used", pre.json()["detail"])

    def test_the_precheck_refuses_what_the_download_refuses(self):
        self.assertEqual(self.client.get(
            "/api/export/precheck?kind=zip").status_code, 403)
        d = self.client.post("/api/export/token",
                             json={"kind": "dump", "password": PW})
        tok = d.json()["token"]
        # a dump ticket does not answer for the ZIP
        self.assertEqual(self.client.get(
            f"/api/export/precheck?kind=zip&t={tok}").status_code, 403)
        # and the dump's own instance gates are asked too
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            pre = self.client.get(f"/api/export/precheck?kind=dump&t={tok}")
        self.assertEqual(pre.status_code, 403)
        self.assertIn("self-hosted", pre.json()["detail"])
        self.assertEqual(self.client.get(
            "/api/export/precheck?kind=bogus&t=x").status_code, 400)

    def test_totp_account_needs_a_live_code(self):
        from fastapi.testclient import TestClient
        c = TestClient(self.app)
        email = f"ext-{uuid.uuid4().hex[:8]}@example.dev"
        c.post("/api/signup", data={"email": email, "password": PW})
        give_totp_factor(email)
        r = c.post("/api/export/token", json={"kind": "zip", "password": PW})
        self.assertEqual(r.status_code, 401)
        self.assertIn("totp_required", r.text)


if __name__ == "__main__":
    unittest.main()
