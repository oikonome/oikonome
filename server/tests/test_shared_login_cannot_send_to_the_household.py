"""The shared demonstration login cannot make the household send.

Its password is handed out, so every holder of it, from any address, could
otherwise press "send today's email now" or "send a test" over and over:
the verdict mail goes to every recipient on the list, the test text to the
household's verified phone, the test push to every member's devices. Each
of those reaches people the login is never shown. The relay test is the
one send it keeps, and that one is cut to its own address. Ordinary owners
are unaffected."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class SharedLoginHouseholdSends(unittest.TestCase):
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

    def _login(self, waived: bool):
        c = TestClient(self.app)
        email = f"send-{uuid.uuid4().hex[:10]}@example.dev"
        data = {"email": email, "password": PW}
        if os.environ.get("OIKONOME_HOSTED"):
            from oikonome.auth import signup_invites
            admin = tenancy.admin_connect()
            try:
                data["invite"] = signup_invites.mint(admin, email)
            finally:
                admin.close()
        self.assertEqual(c.post("/api/signup", data=data).status_code, 200)
        if waived:
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE users SET second_factor_waived=TRUE "
                              "WHERE email=%s", (email,))
            finally:
                admin.close()
        return c

    DOORS = [
        ("/api/jobs/email", {}),
        ("/api/notify/test", {"channel": "sms"}),
        ("/api/notify/test", {"channel": "push"}),
        ("/api/notify/test", {"channel": "email"}),
    ]

    def test_household_send_doors_refuse_the_shared_login(self):
        c = self._login(waived=True)
        with mock.patch("oikonome.jobs.worker.email_tenant") as mail, \
             mock.patch("oikonome.notify.sms.send") as text, \
             mock.patch("oikonome.notify.push.send_tenant") as push:
            for path, body in self.DOORS:
                r = c.post(path, json=body)
                self.assertEqual(r.status_code, 403, f"{path}: {r.text}")
                self.assertIn("shared demonstration login", r.text, path)
        mail.assert_not_called()
        text.assert_not_called()
        push.assert_not_called()

    def test_an_ordinary_owner_still_sends_now(self):
        c = self._login(waived=False)
        with mock.patch("oikonome.jobs.worker.email_tenant",
                        return_value="subject") as mail:
            r = c.post("/api/jobs/email", json={})
        self.assertEqual(r.status_code, 200, r.text)
        mail.assert_called_once()


if __name__ == "__main__":
    unittest.main()
