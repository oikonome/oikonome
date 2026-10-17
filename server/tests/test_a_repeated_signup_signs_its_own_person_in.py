"""The same signup form sent twice signs its person in.

A double click, or a second submit after the human check refused the
first one's spent token, reaches the server a moment after the first
request already created the account. Answered as a stranger would be —
"already has an account" plus a reclaim mail about their own account —
the person is told their address is taken in their first minute. The same
address and the same password, minutes apart, on an account that has
enrolled nothing yet, is that person; anything else is still the ordinary
409 with nothing said.
"""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class RepeatedSignupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        for p in (mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                               "OIKONOME_OPEN_SIGNUP": "1"}),
                  mock.patch.object(self.appmod, "DEV_MODE", False),
                  mock.patch.object(self.appmod, "_deliver_verification")):
            p.start()
            self.addCleanup(p.stop)
        self.reclaims: list = []
        p = mock.patch.object(self.appmod, "_deliver_reclaim",
                              side_effect=lambda *a: self.reclaims.append(a))
        p.start()
        self.addCleanup(p.stop)
        from oikonome.web import security
        security._limiter._hits.clear()
        self.email = f"rep-{uuid.uuid4().hex[:10]}@x.dev"

    def _signup(self, password=PW):
        c = TestClient(self.appmod.app)
        return c, c.post("/api/signup", data={"email": self.email,
                                              "password": password})

    def _sql(self, q, *args):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(q, args).fetchone()
        finally:
            admin.close()

    def _accounts(self):
        return self._sql("SELECT count(*) AS n FROM users WHERE email=%s",
                         self.email)["n"]

    def test_the_same_form_twice_is_one_account_and_a_session(self):
        self.assertEqual(self._signup()[1].status_code, 200)
        c, r = self._signup()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.get("/api/me").json()["email"], self.email)
        self.assertEqual(self._accounts(), 1)
        self.assertEqual(self.reclaims, [], "no reclaim mail to oneself")

    def test_the_form_page_lands_the_repeat_in_the_app(self):
        self._signup()
        r = TestClient(self.appmod.app).post(
            "/signup", data={"email": self.email, "password": PW},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(r.headers["location"], "/app/welcome")

    def test_a_different_password_is_still_a_stranger(self):
        self._signup()
        _, r = self._signup(password="someone-elses-password")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.reclaims), 1)

    def test_an_account_with_a_second_factor_goes_through_sign_in(self):
        self._signup()
        self._sql("UPDATE users SET totp_secret='enrolled' WHERE email=%s "
                  "RETURNING id", self.email)
        _, r = self._signup()
        self.assertEqual(r.status_code, 409)

    def test_an_old_unconfirmed_account_is_not_a_repeat(self):
        self._signup()
        self._sql("UPDATE users SET created_at=%s WHERE email=%s RETURNING id",
                  dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
                  self.email)
        _, r = self._signup()
        self.assertEqual(r.status_code, 409)

    def test_a_verified_account_is_not_a_repeat(self):
        self._signup()
        self._sql("UPDATE users SET verified_at=now() WHERE email=%s "
                  "RETURNING id", self.email)
        _, r = self._signup()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.reclaims, [])


class RepeatedInvitedSignupTests(RepeatedSignupTests):
    """The documented hosted default is invite-gated, and there the first
    request has already spent the invite the second one carries. The
    spent invite still recognises its own signup sent twice; it admits
    nothing else."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"OIKONOME_OPEN_SIGNUP": "0"})
        p.start()
        self.addCleanup(p.stop)
        from oikonome.auth import signup_invites
        admin = tenancy.admin_connect()
        try:
            self.invite = signup_invites.mint(admin, self.email)
        finally:
            admin.close()

    def _signup(self, password=PW, invite=None):
        c = TestClient(self.appmod.app)
        return c, c.post("/api/signup", data={
            "email": self.email, "password": password,
            "invite": self.invite if invite is None else invite})

    def test_the_form_page_lands_the_repeat_in_the_app(self):
        self._signup()
        r = TestClient(self.appmod.app).post(
            "/signup", data={"email": self.email, "password": PW,
                             "invite": self.invite},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(r.headers["location"], "/app/welcome")

    def test_a_different_password_is_still_a_stranger(self):
        self._signup()
        _, r = self._signup(password="someone-elses-password")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.reclaims, [])

    def test_a_verified_account_is_not_a_repeat(self):
        self._signup()
        self._sql("UPDATE users SET verified_at=now() WHERE email=%s "
                  "RETURNING id", self.email)
        _, r = self._signup()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.reclaims, [])

    def test_an_account_with_a_second_factor_goes_through_sign_in(self):
        self._signup()
        self._sql("UPDATE users SET totp_secret='enrolled' WHERE email=%s "
                  "RETURNING id", self.email)
        _, r = self._signup()
        self.assertEqual(r.status_code, 403)

    def test_an_old_unconfirmed_account_is_not_a_repeat(self):
        self._signup()
        self._sql("UPDATE users SET created_at=%s WHERE email=%s RETURNING id",
                  dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
                  self.email)
        _, r = self._signup()
        self.assertEqual(r.status_code, 403)

    def test_without_the_invite_the_repeat_is_a_stranger(self):
        self._signup()
        _, r = self._signup(invite="")
        self.assertEqual(r.status_code, 403)

    def test_a_link_a_resend_burned_is_not_this_signups_invite(self):
        """Re-minting burns the older link without naming a household, so
        that link never vouches for the account the newer one created."""
        from oikonome.auth import signup_invites
        burned = self.invite
        admin = tenancy.admin_connect()
        try:
            self.invite = signup_invites.mint(admin, self.email)
        finally:
            admin.close()
        self.assertEqual(self._signup()[1].status_code, 200)
        _, r = self._signup(invite=burned)
        self.assertEqual(r.status_code, 403, r.text)


class StoredHashThatIsNotAHashTests(unittest.TestCase):
    def test_a_placeholder_hash_verifies_nothing_and_raises_nothing(self):
        from oikonome.auth import passwords
        self.assertFalse(passwords.verify_password("x", PW))
        self.assertFalse(passwords.verify_password("", PW))


if __name__ == "__main__":
    unittest.main()
