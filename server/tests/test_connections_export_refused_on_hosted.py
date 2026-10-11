"""In hosted mode the connections bundle is refused on the server.

Every bank link on a hosted instance was made under the operator's provider
keys, so its access tokens must never leave the server — and the import side
drops them anyway. The apps hide the card; this checks a direct request is
refused too, before any credential is decrypted, while self-host keeps the
door.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import base as sync_base

from .util import _ensure_db

PW = "correct-horse-battery"
PASSPHRASE = "open-sesame-open-sesame"


class HostedExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"hx-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            sync_base.upsert_item(conn, "it-hx", "plaid", "Test Bank",
                                  "access-platform-minted")
        finally:
            conn.close()

    def _export(self):
        return self.client.post("/api/connections/export", json={
            "password": PW, "passphrase": PASSPHRASE})

    def test_hosted_export_is_refused(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            r = self._export()
        self.assertEqual(r.status_code, 403, r.text)
        self.assertNotIn(b"access-platform-minted", r.content)

    def test_self_host_export_still_works(self):
        env = dict(os.environ)
        env.pop("OIKONOME_HOSTED", None)
        with mock.patch.dict(os.environ, env, clear=True):
            r = self._export()
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
