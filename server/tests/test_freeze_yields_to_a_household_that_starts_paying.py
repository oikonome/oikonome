"""Money attached to a household ends the abandoned-signup question, even
when it arrives while the sweep is mid-flight.

The sweep asks that question once, per household, before it sends
anything — and then spends a whole SMTP round trip composing and mailing
the final notice. A checkout that completes in that window is the one
thing only the household's owner could have done. Freezing anyway locks a
person out of the account they have just paid for, and asks the billing
gate to pause the subscription they just started, so the question is put
again under the row lock that performs the freeze.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome import ext
from oikonome.db import tenancy
from oikonome.jobs import unverified

from .util import _ensure_db

PW = "a-long-enough-password-1"


def _age(email: str, days: int) -> None:
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "UPDATE users SET created_at = now() - make_interval(days => %s) "
            "WHERE email=%s", (days, email))
    finally:
        admin.close()


def _row(email: str):
    admin = tenancy.admin_connect()
    try:
        return admin.execute(
            "SELECT t.id, t.status, t.delete_after FROM tenants t "
            "JOIN users u ON u.tenant_id=t.id WHERE u.email=%s",
            (email,)).fetchone()
    finally:
        admin.close()


class FreezeYieldsToPaymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            "OIKONOME_HOSTED": "1", "OIKONOME_OPEN_SIGNUP": "1",
            "OIKONOME_BASE_URL": "https://app.example.test",
            "OIKONOME_UNVERIFIED_REAP_DAYS": "30"})
        self._env.start()
        self._dev = mock.patch.object(self.appmod, "DEV_MODE", False)
        self._dev.start()
        self._vmail = mock.patch.object(self.appmod, "_deliver_verification")
        self._vmail.start()
        from oikonome.web import security
        security._limiter._hits.clear()

    def tearDown(self):
        for p in (self._vmail, self._dev, self._env):
            p.stop()

    def _signup(self):
        email = f"pay-{uuid.uuid4().hex[:8]}@x.dev"
        c = TestClient(self.appmod.app)
        r = c.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return email

    def test_a_subscription_that_lands_during_the_notice_stops_the_freeze(self):
        """The notice has already gone out — the household is still not
        frozen, and its billing is not paused."""
        email = self._signup()
        _age(email, 31)
        paying = {"yes": False}
        # the checkout completes while the final notice is in the post
        with mock.patch.object(unverified.account_mail, "send",
                               side_effect=lambda *a, **kw:
                                   paying.update(yes=True) or True), \
             mock.patch.object(ext.gate._resolve(), "money_attached",
                               side_effect=lambda admin, tid: paying["yes"]), \
             mock.patch.object(ext.gate._resolve(),
                               "on_tenant_delete_scheduled") as paused:
            out = unverified.sweep()
        row = _row(email)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["delete_after"])
        self.assertNotIn(str(row["id"]), out["scheduled"])
        self.assertEqual(paused.call_count, 0)

    def test_a_household_with_no_money_is_still_frozen(self):
        """The guard must not become an excuse to never freeze anything."""
        email = self._signup()
        _age(email, 31)
        with mock.patch.object(unverified.account_mail, "send",
                               return_value=True):
            out = unverified.sweep()
        row = _row(email)
        self.assertEqual(row["status"], unverified.STATUS)
        self.assertIn(str(row["id"]), out["scheduled"])


if __name__ == "__main__":
    unittest.main()
