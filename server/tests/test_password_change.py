"""Signed-in password change (/api/password/change) + the SPA doctor API
(/api/doctor). Style follows test_auth_hardening.py."""

import unittest
import uuid

from fastapi.testclient import TestClient

from .util import clear_totp_burn, _ensure_db


def _fresh_code(secret):
    """A code that is accepted now. Step-up doors burn the code, so
    these tests — which are about the step-up GATE,
    not replay — clear the burn first. See util.clear_totp_burn.
    """
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)

PW = "correct-horse-battery"
NEW = "brand-new-password-9"


class PasswordChangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        # each test signs up its own users; don't trip signup/login limits
        from oikonome.web import security
        security._limiter._hits.clear()

    def _signup(self):
        client = TestClient(self.app)
        email = f"pc-{uuid.uuid4().hex[:10]}@example.dev"
        r = client.post("/api/signup", data={"email": email, "password": PW})
        assert r.status_code == 200, r.text
        return client, email

    # ---- password change ----------------------------------------------------

    def test_change_password_happy_path(self):
        c, email = self._signup()
        r = c.post("/api/password/change",
                   json={"current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        # ok plus the rotation's cost (passkeys/tokens/devices removed),
        # which the Settings card shows so nothing dies unexplained
        self.assertTrue(r.json()["ok"])
        self.assertIn("passkeys_removed", r.json())
        # the calling session survives
        self.assertEqual(c.get("/api/me").status_code, 200)
        # old password dead, new one works
        anon = TestClient(self.app)
        r = anon.post("/api/login", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 401)
        r = anon.post("/api/login", data={"email": email, "password": NEW})
        self.assertEqual(r.status_code, 200)

    def test_change_password_wrong_current_is_401(self):
        c, email = self._signup()
        r = c.post("/api/password/change",
                   json={"current_password": "not-the-password",
                         "new_password": NEW})
        self.assertEqual(r.status_code, 401)
        self.assertIn("wrong password", r.json()["detail"])
        # nothing changed: the old password still signs in
        anon = TestClient(self.app)
        r = anon.post("/api/login", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200)

    def test_change_password_short_new_is_400(self):
        c, email = self._signup()
        r = c.post("/api/password/change",
                   json={"current_password": PW, "new_password": "short"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("at least 10 characters", r.json()["detail"])
        anon = TestClient(self.app)
        r = anon.post("/api/login", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200)

    def test_change_password_revokes_other_sessions(self):
        c1, email = self._signup()
        c2 = TestClient(self.app)
        self.assertEqual(c2.post("/api/login", data={
            "email": email, "password": PW}).status_code, 200)
        self.assertEqual(c2.get("/api/me").status_code, 200)

        r = c1.post("/api/password/change",
                    json={"current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        # the other session is dead, the calling one survives alone
        self.assertEqual(c2.get("/api/me").status_code, 401)
        self.assertEqual(c1.get("/api/me").status_code, 200)
        self.assertEqual(len(c1.get("/api/sessions").json()["sessions"]), 1)

    def test_change_password_requires_totp_when_enrolled(self):
        c, email = self._signup()
        secret = c.post("/api/totp/enroll",
                        data={"password": PW}).json()["secret"]
        r = c.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": PW})
        self.assertEqual(r.status_code, 200)

        # password alone isn't enough: the second factor rides the
        # elevation window, which a TOTP account opens only with a code
        r = c.post("/api/password/change",
                   json={"current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "elevation_required")
        # a wrong code is rejected
        r = c.post("/api/password/change",
                   json={"current_password": PW, "new_password": NEW,
                         "totp_code": "000000"})
        self.assertEqual(r.status_code, 401)
        # with the current code it goes through
        r = c.post("/api/password/change",
                   json={"current_password": PW, "new_password": NEW,
                         "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        anon = TestClient(self.app)
        r = anon.post("/api/login", data={
            "email": email, "password": NEW,
            "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200)

    def test_rejected_new_password_does_not_burn_the_code(self):
        """Validation runs BEFORE the factors are consumed. The other way
        round, HIBP (or a too-short candidate) rejecting the new password
        costs a real TOTP step / a one-time recovery code per attempt and
        walks a passkey-only user toward lockout; this way the SAME code
        still works on the corrected retry."""
        c, email = self._signup()
        secret = c.post("/api/totp/enroll",
                        data={"password": PW}).json()["secret"]
        r = c.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": PW})
        self.assertEqual(r.status_code, 200)
        code = _fresh_code(secret)
        # rejected candidate (too short) — must NOT consume the code
        r = c.post("/api/password/change",
                   json={"current_password": PW, "new_password": "short",
                         "totp_code": code})
        self.assertEqual(r.status_code, 400)
        # the very same code still works: nothing was burned by the 400
        r = c.post("/api/password/change",
                   json={"current_password": PW, "new_password": NEW,
                         "totp_code": code})
        self.assertEqual(r.status_code, 200, r.text)

    def test_email_change_to_taken_address_does_not_burn_the_code(self):
        """The email door: the 409 for an already-used address must land
        BEFORE the factor is consumed."""
        c1, email1 = self._signup()
        c2, email2 = self._signup()
        secret = c2.post("/api/totp/enroll",
                         data={"password": PW}).json()["secret"]
        r = c2.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": PW})
        self.assertEqual(r.status_code, 200)
        code = _fresh_code(secret)
        r = c2.post("/api/email/change",
                    data={"new_email": email1, "password": PW,
                          "totp_code": code})
        self.assertEqual(r.status_code, 409)
        fresh = f"pc-new-{uuid.uuid4().hex[:8]}@example.dev"
        r = c2.post("/api/email/change",
                    data={"new_email": fresh, "password": PW,
                          "totp_code": code})
        self.assertEqual(r.status_code, 200, r.text)

    def test_change_password_requires_auth(self):
        anon = TestClient(self.app)
        r = anon.post("/api/password/change",
                      json={"current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 401)

    def test_change_password_rate_limited(self):
        c, _ = self._signup()
        for _ in range(5):
            r = c.post("/api/password/change",
                       json={"current_password": "wrong-wrong-x",
                             "new_password": NEW})
            self.assertEqual(r.status_code, 401)
        r = c.post("/api/password/change",
                   json={"current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 429)


class DoctorApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def test_api_doctor_requires_auth(self):
        anon = TestClient(self.app)
        r = anon.get("/api/doctor")
        self.assertEqual(r.status_code, 401)   # JSON 401, no login redirect
        self.assertEqual(r.json(), {"detail": "not signed in"})

    def test_api_doctor_shape(self):
        c = TestClient(self.app)
        email = f"dr-{uuid.uuid4().hex[:10]}@example.dev"
        self.assertEqual(c.post("/api/signup", data={
            "email": email, "password": PW}).status_code, 200)
        r = c.get("/api/doctor")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertIsInstance(d["ok"], bool)
        self.assertGreater(len(d["checks"]), 0)
        for chk in d["checks"]:
            self.assertEqual(set(chk),
                             {"section", "name", "ok", "severity", "detail"})
            self.assertIsInstance(chk["ok"], bool)
            self.assertIn(chk["severity"], ("ok", "warn", "bad"))
        # the overall verdict is the conjunction of the rows
        self.assertEqual(d["ok"], all(chk["ok"] for chk in d["checks"]))
        # the named checks the Jinja page renders are present
        names = {chk["name"] for chk in d["checks"]}
        self.assertIn("database", names)
        self.assertIn("ledger", names)
        # the bundle the Doctor page links to carries the same checks
        b = c.get("/api/doctor/bundle")
        self.assertEqual(b.status_code, 200)
        self.assertEqual([chk["name"] for chk in b.json()["checks"]],
                         [chk["name"] for chk in d["checks"]])


if __name__ == "__main__":
    unittest.main()
