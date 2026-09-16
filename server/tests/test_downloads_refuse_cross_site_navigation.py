"""Cookie-authenticated downloads refuse a cross-site initiator.

SameSite=Lax cookies ride a top-level cross-site GET, so a link on any
page a signed-in person visits could drop their Schedule C worksheet, a
CPA package or a receipt image into Downloads. The browser names a
cross-site initiator in fetch metadata (and in Referer without it); the
native app and same-origin pages send neither and pass.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db

PATHS = ["/api/business/export.csv", "/api/receipts/report?format=csv"]


class CrossSiteDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def _client(self):
        c = TestClient(self.app)
        r = c.post("/api/signup", data={
            "email": f"dl-{uuid.uuid4().hex[:8]}@x.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        return c

    def test_cross_site_fetch_metadata_is_refused(self):
        c = self._client()
        for p in PATHS:
            r = c.get(p, headers={"Sec-Fetch-Site": "cross-site",
                                  "Sec-Fetch-Mode": "navigate"})
            self.assertEqual(r.status_code, 403, p)

    def test_foreign_referer_without_metadata_is_refused(self):
        c = self._client()
        r = c.get(PATHS[0], headers={"Referer": "https://evil.example/x"})
        self.assertEqual(r.status_code, 403)

    def test_same_origin_and_bare_requests_pass(self):
        c = self._client()
        for hdrs in ({}, {"Sec-Fetch-Site": "same-origin"}):
            r = c.get(PATHS[0], headers=hdrs)
            self.assertNotEqual(r.status_code, 403, hdrs)
