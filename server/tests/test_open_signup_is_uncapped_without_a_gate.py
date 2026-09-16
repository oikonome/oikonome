"""Open signup admits a new address when no signup gate is configured."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from .util import _ensure_db

PW = "correct-horse-battery"


class OpenSignupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                                 "OIKONOME_OPEN_SIGNUP": "1"})
        self._env.start()
        self._dev = mock.patch.object(self.appmod, "DEV_MODE", False)
        self._dev.start()
        self._mail = mock.patch.object(self.appmod, "_deliver_verification")
        self._mail.start()
        from oikonome.web import security
        security._limiter._hits.clear()

    def tearDown(self):
        self._mail.stop(); self._dev.stop(); self._env.stop()

    def _signup(self, email, invite=""):
        return TestClient(self.appmod.app).post(
            "/api/signup", data={"email": email, "password": PW,
                                 "invite": invite})

    def test_no_gate_configured_never_refuses_a_signup(self):
        """An operator who never configured a signup limit has no cap."""
        r = self._signup(f"nocap-{uuid.uuid4().hex[:8]}@x.dev")
        self.assertEqual(r.status_code, 200, r.text)
