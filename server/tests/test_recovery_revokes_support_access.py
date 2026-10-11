"""A support-access window does not outlive the recovery of the account
that opened it.

The grant lets the operator read the household's data for up to a week.
A hijacker holding the owner's session could open one; the recovery that
evicts every other foothold must close it too — and a grant that
authenticated just before the recovery and writes just after must not
re-open it.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome import tenant_export
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class SupportAccessRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app)
        self.email = f"supp-{uuid.uuid4().hex[:10]}@example.dev"
        r = self.client.post("/api/signup", data={"email": self.email,
                                                 "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            self.uid = admin.execute(
                "SELECT id FROM users WHERE lower(email)=lower(%s)",
                (self.email,)).fetchone()["id"]
        finally:
            admin.close()

    def _live(self) -> bool:
        admin = tenancy.admin_connect()
        try:
            return tenant_export.has_live_consent(admin, self.tid)
        finally:
            admin.close()

    def _recover(self):
        from oikonome.auth import reset as reset_mod
        admin = tenancy.admin_connect()
        try:
            with admin.transaction():
                reset_mod.revoke_everything(admin, self.uid)
        finally:
            admin.close()

    def test_a_recovery_revokes_the_live_grant(self):
        r = self.client.post("/api/support-access",
                             json={"hours": 24, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self._live())
        self._recover()
        self.assertFalse(self._live())

    def test_a_grant_from_a_session_the_recovery_evicted_writes_nothing(self):
        real = self.appmod._require_elevation

        def elevation_then_recovery(user, **kw):
            real(user, **kw)
            self._recover()

        with mock.patch.object(self.appmod, "_require_elevation",
                               elevation_then_recovery):
            r = self.client.post("/api/support-access",
                                 json={"hours": 24, "password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertFalse(self._live())


if __name__ == "__main__":
    unittest.main()
