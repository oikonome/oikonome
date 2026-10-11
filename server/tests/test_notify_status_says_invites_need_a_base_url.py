"""The notify status says when mail is on but invitations cannot go.

A recipient added under Email recipients only receives the daily email
after accepting an emailed invitation, and the invitation is a link built
under OIKONOME_BASE_URL (never from request headers). With a relay and no
base URL the invitation is never mailed, the recipient never accepts, and
the only fix is the server's config, so the status the clients read says
so (`invites_need_base_url`) and they can name it.
"""
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from .util import _ensure_db


class NotifyStatusNamesTheMissingBaseUrl(unittest.TestCase):
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
        r = self.client.post("/api/signup", data={
            "email": f"nsb-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)

    def _status(self, smtp, base):
        env = {"OIKONOME_BASE_URL": base}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.appmod, "_tenant_smtp",
                                  return_value=smtp):
            r = self.client.get("/api/notify")
        self.assertEqual(200, r.status_code, r.text)
        return r.json()["invites_need_base_url"]

    def test_a_relay_without_a_base_url_is_named(self):
        self.assertTrue(self._status({"configured": True}, ""))

    def test_a_base_url_or_no_relay_is_fine(self):
        self.assertFalse(self._status({"configured": True},
                                      "https://money.example.com"))
        self.assertFalse(self._status(None, ""))
        self.assertFalse(self._status({"configured": False}, ""))


if __name__ == "__main__":
    unittest.main()
