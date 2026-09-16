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
