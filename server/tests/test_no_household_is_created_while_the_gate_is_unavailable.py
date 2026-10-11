"""No household is created while the billing add-on cannot load.

A hosted instance whose add-on failed to import runs under the fail-closed
gate, and that gate refuses to provision: a household created then would
have no billing record (no trial end, none of an invite's terms), and
nothing writes one later. Some doors reach provisioning without asking the
intake cap first (an invited signup, the console's create), so the refusal
has to come back from them as "try again shortly" with nothing left
behind, not as a server error.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome import ext
from oikonome.db import tenancy

from .util import _ensure_db

PW = "a-long-enough-password-1"
TOKEN = "test-admin-token-" + "g" * 32


class _AdmitsButCannotProvision(ext._FailClosedGate):
    """The fail-closed gate as a door that skips the intake cap sees it."""

    def at_cap(self, accounts: int) -> bool:
        return False

    def intake_cap(self):
        return None


def _user_exists(email: str) -> bool:
    admin = tenancy.admin_connect()
    try:
        return admin.execute("SELECT 1 FROM users WHERE email=%s",
                             (email,)).fetchone() is not None
    finally:
        admin.close()


class GateUnavailableCreationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        ext.set_gate(_AdmitsButCannotProvision())
        self.addCleanup(ext.set_gate, None)
        from oikonome.web import security
        security._limiter._hits.clear()
        self.email = f"gate-{uuid.uuid4().hex[:8]}@example.dev"

    def test_signup_says_try_again_and_leaves_nothing(self):
        env = {"OIKONOME_HOSTED": "1", "OIKONOME_OPEN_SIGNUP": "1",
               "OIKONOME_BASE_URL": "https://app.example.test"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.appmod, "DEV_MODE", False), \
                mock.patch.object(self.appmod, "_deliver_verification"):
            r = TestClient(self.appmod.app,
                           raise_server_exceptions=False).post(
                "/api/signup", data={"email": self.email, "password": PW})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn("try again shortly", r.text)
        self.assertFalse(_user_exists(self.email))

    def test_the_console_create_refuses_and_leaves_nothing(self):
        env = {"OIKONOME_DEV": "1", "OIKONOME_ADMIN_TOKEN": TOKEN}
        from fastapi.responses import HTMLResponse

        from oikonome.web import adminconsole

        def notice_only(request, notice=None, **_kw):
            # the dashboard itself renders the add-on's fleet columns,
            # which an add-on that did not load cannot fill; the door's
            # answer is what is under test
            return HTMLResponse(notice or "")

        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.appmod, "DEV_MODE", True), \
                mock.patch.object(adminconsole, "_dashboard",
                                  side_effect=notice_only), \
                mock.patch("oikonome.web.app._deliver_welcome"):
            c = TestClient(self.appmod.app, raise_server_exceptions=False)
            r = c.post("/admin/console/login", data={"token": TOKEN},
                       follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            r = c.post("/admin/console/create-instance",
                       data={"email": self.email})
        self.assertEqual(r.status_code, 503, r.text[:300])
        self.assertIn("no instance was created", r.text)
        self.assertFalse(_user_exists(self.email))


if __name__ == "__main__":
    unittest.main()
