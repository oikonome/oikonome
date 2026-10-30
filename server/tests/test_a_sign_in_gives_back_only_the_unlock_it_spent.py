"""A sign-in that gives its emailed unlock back gives back only its own.

Two doors re-arm a spent unlock: a sign-in whose hasher was busy (it
verified nothing, so the attempt the unlock paid for never happened), and
the password step of a two-step sign-in that still needs its second factor.
Both used to re-arm whatever the account had spent in the last few
seconds. A concurrent attempt from somewhere else — one that spent the
unlock and reached a wrong password — was then handed its attempt back by
a request that had passed the check with a token and spent nothing.

Each request records the unlock ids it spent at its own human check, and
both doors re-arm exactly those: none when it spent none.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import login_unlock, passwords

from .util import _ensure_db

PW = "correct-horse-battery"


class UnlockGiveBackTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security, turnstile
        security._limiter._hits.clear()
        for p in (mock.patch.object(security, "_redis_limiter", False),
                  mock.patch.dict(os.environ, {
                      "OIKONOME_TURNSTILE_SITE_KEY": "site",
                      "OIKONOME_TURNSTILE_SECRET": "secret"}),
                  # the check is armed, as after a burst of mistyped sign-ins
                  mock.patch.object(turnstile, "required_for_login",
                                    return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        turnstile.forget_spent_unlocks()
        self.uid, self.email = self._user()
        with self._control() as conn:
            login_unlock.follow(conn, login_unlock.create(conn, self.uid))

    def _user(self):
        from oikonome.db import tenancy
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"own-{uuid.uuid4().hex[:8]}")
            email = f"own-{uuid.uuid4().hex[:8]}@example.dev"
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s,%s,%s,now()) RETURNING id",
                (tid, email, passwords.hash_password(PW))).fetchone()["id"]
            admin.commit()
        finally:
            admin.close()
        return uid, email

    def _control(self):
        from oikonome.db import tenancy
        return tenancy.control_connect()

    def _unlocked(self):
        with self._control() as conn:
            return login_unlock.is_unlocked(conn, self.email)

    def _concurrent_spend(self):
        """Another attempt, mid-request, spends the unlock."""
        with self._control() as conn:
            self.assertTrue(login_unlock.consume(conn, self.email))

    def test_a_busy_sign_in_by_token_leaves_a_concurrent_spend_spent(self):
        from oikonome.web import turnstile

        def busy(*_a, **_k):
            self._concurrent_spend()
            raise passwords.HasherBusy("full")
        c = TestClient(self.appmod.app, raise_server_exceptions=False)
        with mock.patch.object(turnstile, "verify", return_value=True), \
                mock.patch.object(passwords, "_gated", side_effect=busy):
            r = c.post("/api/login", data={"email": self.email,
                                           "password": PW,
                                           "cf-turnstile-response": "tok"})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertFalse(self._unlocked(),
                         "a request that spent nothing re-armed another "
                         "attempt's unlock")

    def test_a_busy_sign_in_that_spent_the_unlock_gets_it_back(self):
        c = TestClient(self.appmod.app, raise_server_exceptions=False)
        with mock.patch.object(passwords, "_gated",
                               side_effect=passwords.HasherBusy("full")):
            r = c.post("/api/login", data={"email": self.email,
                                           "password": PW})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertTrue(self._unlocked())

    def test_the_spend_is_recorded_by_id_and_re_armed_by_id(self):
        from oikonome.web import turnstile
        self.assertTrue(turnstile.verify_login("", "198.51.100.7",
                                               self.email))
        ids = turnstile.spent_unlocks()
        self.assertEqual(len(ids), 1)
        self.assertFalse(self._unlocked())
        self.appmod._unspend_unlock(self.email)
        self.assertTrue(self._unlocked())
        # given back once; the record is cleared
        self.assertEqual(turnstile.spent_unlocks(), ())

    def test_a_new_check_starts_the_record_afresh(self):
        from oikonome.web import turnstile
        self.assertTrue(turnstile.verify_login("", "198.51.100.7",
                                               self.email))
        with mock.patch.object(turnstile, "verify", return_value=True):
            self.assertTrue(turnstile.verify_login("tok", "198.51.100.7",
                                                   self.email))
        self.assertEqual(turnstile.spent_unlocks(), ())

    def test_a_two_step_refund_leaves_a_concurrent_spend_spent(self):
        from oikonome.web import turnstile
        with mock.patch.object(turnstile, "verify", return_value=True):
            self.assertTrue(turnstile.verify_login("tok", "198.51.100.7",
                                                   self.email))
        self._concurrent_spend()
        turnstile.refund_unlock(self.email)
        self.assertFalse(self._unlocked(),
                         "the password step refunded an unlock it never "
                         "spent")

    def test_a_two_step_refund_gives_back_the_unlock_it_spent(self):
        from oikonome.web import turnstile
        self.assertTrue(turnstile.verify_login("", "198.51.100.7",
                                               self.email))
        self.assertFalse(self._unlocked())
        turnstile.refund_unlock(self.email)
        self.assertTrue(self._unlocked())


if __name__ == "__main__":
    unittest.main()
