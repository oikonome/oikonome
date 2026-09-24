"""The way out of the never-confirmed freeze has to be walkable.

Two pages send a frozen person to it in so many words: the expired-link
page says "sign in and use Resend", and the app's frozen screen offers
the button. Both promises rest on a sign-in that still completes while
the household is frozen, on the app letting that session reach its frozen
screen, and on the resend door actually posting a fresh link. If any of
the three refused, the only escape from the freeze would be a link the
person has already lost.

The door has to open for a VERIFIED caller too, which is the state a
mail scanner leaves behind: it fetched the last link, so the address is
proved while the household is still frozen, and a "nothing to do here"
answer would strand a household days from erasure.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import sessions
from oikonome.db import tenancy
from oikonome.jobs import unverified

from .util import _ensure_db

PW = "a-long-enough-password-1"


def _status(email: str) -> str:
    admin = tenancy.admin_connect()
    try:
        return admin.execute(
            "SELECT t.status FROM tenants t JOIN users u ON u.tenant_id=t.id "
            "WHERE u.email=%s", (email,)).fetchone()["status"]
    finally:
        admin.close()


def _freeze(email: str) -> None:
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "UPDATE tenants SET status=%s, status_before_delete='active', "
            "delete_after = now() + interval '7 days' WHERE id = "
            "(SELECT tenant_id FROM users WHERE email=%s)",
            (unverified.STATUS, email))
    finally:
        admin.close()


class FrozenSignInTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            "OIKONOME_HOSTED": "1", "OIKONOME_OPEN_SIGNUP": "1",
            "OIKONOME_BASE_URL": "https://app.example.test"})
        self._env.start()
        self._dev = mock.patch.object(self.appmod, "DEV_MODE", False)
        self._dev.start()
        self.mailed = mock.patch.object(self.appmod, "_deliver_verification")
        self.deliver = self.mailed.start()
        from oikonome.web import security
        security._limiter._hits.clear()
        self.email = f"frozen-{uuid.uuid4().hex[:8]}@example.dev"
        r = TestClient(self.appmod.app).post(
            "/api/signup", data={"email": self.email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        _freeze(self.email)

    def tearDown(self):
        for p in (self.mailed, self._dev, self._env):
            p.stop()

    def test_the_form_sign_in_still_hands_a_frozen_household_a_session(self):
        c = TestClient(self.appmod.app)
        r = c.post("/login", data={"email": self.email, "password": PW},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertTrue(c.cookies.get(sessions.COOKIE_NAME))
        me = c.get("/api/me")
        self.assertEqual(me.status_code, 403)
        self.assertIn("never confirmed", me.text)

    def test_the_resend_door_posts_a_fresh_link_to_a_frozen_household(self):
        """`verified: true` with no send is the dead end this door must
        not answer with while the household is still frozen."""
        c = TestClient(self.appmod.app)
        c.post("/login", data={"email": self.email, "password": PW},
               follow_redirects=False)
        self.deliver.reset_mock()
        r = c.post("/api/verify-email/resend")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "verified": False})
        self.assertEqual(self.deliver.call_count, 1)

    def test_a_verified_caller_of_a_frozen_household_is_still_mailed(self):
        """What a scanner's fetch leaves: address proved, household
        frozen. The resend door must not answer "already verified,
        nothing sent" to a household on its way to being erased."""
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET verified_at=now(), "
                          "first_verified_at=now() WHERE email=%s",
                          (self.email,))
        finally:
            admin.close()
        c = TestClient(self.appmod.app)
        c.post("/login", data={"email": self.email, "password": PW},
               follow_redirects=False)
        self.deliver.reset_mock()
        r = c.post("/api/verify-email/resend")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "verified": False})
        self.assertEqual(self.deliver.call_count, 1)

    def test_the_link_that_resend_mails_a_verified_caller_reopens(self):
        """And the whole of it works end to end: the fresh link confirms
        an address that is already confirmed, its page draws the button,
        and the button lifts the freeze."""
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET verified_at=now(), "
                          "first_verified_at=now() WHERE email=%s",
                          (self.email,))
        finally:
            admin.close()
        c = TestClient(self.appmod.app)
        c.post("/login", data={"email": self.email, "password": PW},
               follow_redirects=False)
        self.deliver.reset_mock()
        c.post("/api/verify-email/resend")
        token = self.deliver.call_args.args[1]
        browser = TestClient(self.appmod.app)
        page = browser.get(f"/verify-email?token={token}",
                           follow_redirects=False)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Reopen this account", page.text)
        r = browser.post("/verify-email", data={"token": token},
                         follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(_status(self.email), "active")

    def test_the_expired_link_page_keeps_its_promise(self):
        """"Sign in and use Resend to get a fresh one" — walked, from a
        dead link to a reopened household, which is the only path left
        to someone whose original mail is gone."""
        dead = TestClient(self.appmod.app).get(
            "/verify-email?token=this-link-is-not-a-real-one")
        self.assertEqual(dead.status_code, 400)
        self.assertIn("Resend", dead.text)
        c = TestClient(self.appmod.app)
        c.post("/login", data={"email": self.email, "password": PW},
               follow_redirects=False)
        self.deliver.reset_mock()
        self.assertEqual(c.post("/api/verify-email/resend").status_code, 200)
        token = self.deliver.call_args.args[1]
        browser = TestClient(self.appmod.app)
        browser.get(f"/verify-email?token={token}", follow_redirects=False)
        browser.post("/verify-email", data={"token": token},
                     follow_redirects=False)
        self.assertEqual(_status(self.email), "active")
        self.assertEqual(c.get("/api/me").status_code, 200)

    def test_an_unfrozen_household_is_still_told_when_nothing_is_left(self):
        """The early answer stays for everyone else: a verified address
        on a working household is not mailed another link."""
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET verified_at=now(), "
                          "first_verified_at=now() WHERE email=%s",
                          (self.email,))
            admin.execute(
                "UPDATE tenants SET status='active', delete_after=NULL, "
                "status_before_delete=NULL WHERE id = "
                "(SELECT tenant_id FROM users WHERE email=%s)",
                (self.email,))
        finally:
            admin.close()
        c = TestClient(self.appmod.app)
        c.post("/login", data={"email": self.email, "password": PW},
               follow_redirects=False)
        self.deliver.reset_mock()
        r = c.post("/api/verify-email/resend")
        self.assertEqual(r.json(), {"ok": True, "verified": True})
        self.assertEqual(self.deliver.call_count, 0)

    def test_a_frozen_session_is_taken_into_the_app_not_back_to_the_form(self):
        """This lockout leaves a door open, so the app is where its way
        out is drawn — /login hands the session on rather than parking it
        on a sign-in form it has already completed."""
        c = TestClient(self.appmod.app)
        c.post("/login", data={"email": self.email, "password": PW},
               follow_redirects=False)
        page = c.get("/login", follow_redirects=False)
        self.assertEqual(page.status_code, 303, page.text)
        self.assertEqual(page.headers["location"], "/app/")

    def test_the_json_sign_in_the_phone_uses_completes_too(self):
        bare = TestClient(self.appmod.app)
        r = bare.post("/api/login", data={"email": self.email,
                                          "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("mint_ticket"))


if __name__ == "__main__":
    unittest.main()
