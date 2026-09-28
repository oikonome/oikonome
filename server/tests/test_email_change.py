"""Change the login email/username. Re-auth like the
other account-security doors; the write goes through the ADMIN
connection (keeps the app role out of users.email). On hosted
the new address starts unverified and a verification token is minted.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class EmailChangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        # every _fresh() signs up — clear the 5/h signup + email_change
        # buckets so a 7-test class doesn't trip its own limiter
        from oikonome.web import security
        security._limiter._hits.clear()
        store = security._shared_store()
        if store is not None:
            try:
                store._r.flushdb()
            except Exception:
                pass

    def _fresh(self):
        c = TestClient(self.app)
        email = f"em-{uuid.uuid4().hex[:10]}@example.dev"
        data = {"email": email, "password": PW}
        if os.environ.get("OIKONOME_HOSTED"):
            from oikonome.auth import signup_invites
            admin = tenancy.admin_connect()
            try:
                data["invite"] = signup_invites.mint(admin, email)
            finally:
                admin.close()
        r = c.post("/api/signup", data=data)
        assert r.status_code == 200, r.text
        return c, email

    def test_change_succeeds_and_login_follows(self):
        c, old = self._fresh()
        new = f"new-{uuid.uuid4().hex[:8]}@example.dev"
        r = c.post("/api/email/change",
                   data={"new_email": new, "password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["email"], new)
        self.assertEqual(c.get("/api/me").json()["email"], new)
        # old address no longer authenticates; the new one does
        anon = TestClient(self.app)
        self.assertEqual(anon.post("/login", data={"email": old,
                         "password": PW}).status_code, 401)
        self.assertEqual(anon.post("/login", data={"email": new,
                         "password": PW},
                         follow_redirects=False).status_code, 303)

    def test_wrong_password_401(self):
        c, _ = self._fresh()
        r = c.post("/api/email/change",
                   data={"new_email": "x@y.dev", "password": "nope-nope-nope"})
        self.assertEqual(r.status_code, 401)

    def test_taken_email_409(self):
        c1, taken = self._fresh()
        c2, _ = self._fresh()
        r = c2.post("/api/email/change",
                    data={"new_email": taken, "password": PW})
        self.assertEqual(r.status_code, 409)

    def test_bad_email_400(self):
        c, _ = self._fresh()
        r = c.post("/api/email/change",
                   data={"new_email": "not-an-email", "password": PW})
        self.assertEqual(r.status_code, 400)

    def test_case_insensitive_and_normalized(self):
        c, _ = self._fresh()
        new = f"MixedCase-{uuid.uuid4().hex[:6]}@Example.DEV"
        r = c.post("/api/email/change",
                   data={"new_email": new, "password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["email"], new.lower())

    def test_hosted_resets_verification(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        try:
            c, _ = self._fresh()   # hosted signup starts unverified anyway
            new = f"hv-{uuid.uuid4().hex[:8]}@example.dev"
            r = c.post("/api/email/change",
                       data={"new_email": new, "password": PW})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["verify_sent"])
            self.assertFalse(c.get("/api/me").json()["verified"])
            # a verification row was minted for the user
            admin = tenancy.admin_connect()
            try:
                uid = admin.execute("SELECT id FROM users WHERE email=%s",
                                    (new,)).fetchone()["id"]
                n = admin.execute("SELECT COUNT(*) AS n FROM "
                                  "email_verifications WHERE user_id=%s AND "
                                  "used_at IS NULL", (uid,)).fetchone()["n"]
            finally:
                admin.close()
            self.assertEqual(n, 1)
        finally:
            os.environ.pop("OIKONOME_HOSTED", None)

    def test_app_role_cannot_write_email_directly(self):
        # the whole reason this endpoint uses the admin connection
        import psycopg
        with psycopg.connect(tenancy.APP_DSN, autocommit=True) as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE users SET email='hijack@x.dev'")


if __name__ == "__main__":
    unittest.main()
