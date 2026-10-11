"""A password change on a household whose only own outbound setting is its
Plaid or MX keys holds new bank links on them, and Settings says so.

Those keys decide which client receives the bank login of every new link,
and a hijacker may have saved them. The rotation holds the doors that hand
logins out (a new link, a re-auth, the MX widget) until the owner confirms
the keys in the banner or removes them; the keys keep serving the banks
already linked. With something else paused too, the same record carries
both."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.notify import outbound_pause

from .util import _ensure_db, write_config

PW = "correct-horse-battery"


class PasswordChangeHoldsNewLinks(unittest.TestCase):
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
        self.client = TestClient(self.app)
        email = f"byo-{uuid.uuid4().hex[:10]}@example.dev"
        r = self.client.post("/api/signup", data={"email": email,
                                                 "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        self.env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _set(self, **kw):
        conn = tenancy.tenant_connect(self.tid)
        try:
            write_config(conn, plaid_client_id="synthetic-client", **kw)
        finally:
            conn.close()

    def _rotate(self):
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": "another-long-phrase-9"})
        self.assertEqual(r.status_code, 200, r.text)

    def _stored(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn).get(outbound_pause.KEY)
        finally:
            conn.close()

    def test_keys_alone_arm_the_hold_and_the_banner(self):
        self._set()
        self._rotate()
        self.assertTrue(self._stored()["aggregator"])
        p = self.client.get("/api/settings").json()["outbound_paused"]
        self.assertTrue(p["aggregator"])
        self.assertEqual(p["reason"], "rotation")
        self.assertEqual([k["client_id"] for k in p["aggregator_keys"]],
                         ["synthetic-client"])
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertTrue(outbound_pause.links_held(
                budget.load_config(conn), "plaid"))
        finally:
            conn.close()

    def test_the_hold_rides_on_a_record_armed_for_the_ai_endpoint(self):
        self._set(llm_url="http://10.0.0.9:11434", llm_model="x")
        self._rotate()
        p = self.client.get("/api/settings").json()["outbound_paused"]
        self.assertTrue(p["llm"])
        self.assertTrue(p["aggregator"])


if __name__ == "__main__":
    unittest.main()
