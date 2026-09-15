"""TOTP: RFC 6238 test vector, drift window, full enroll→login→disable
flow over HTTP."""

import base64
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import totp

from .util import clear_totp_burn, _ensure_db


def _fresh_code(secret):
    """A code that is accepted now. Step-up doors burn the code, so
    these tests — which are about the step-up GATE,
    not replay — clear the burn first. See util.clear_totp_burn.
    """
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)

# RFC 6238 Appendix B secret (SHA-1): ASCII "12345678901234567890"
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


class TotpUnitTests(unittest.TestCase):
    def test_rfc6238_vector(self):
        # t=59s → counter 1 → RFC 8-digit code 94287082 → 6-digit 287082
        self.assertEqual(totp.code_now(RFC_SECRET, at=59), "287082")
        # t=1111111109 → 07081804 → 081804
        self.assertEqual(totp.code_now(RFC_SECRET, at=1111111109), "081804")

    def test_drift_window(self):
        now = 1111111109.0
        prev_code = totp.code_now(RFC_SECRET, at=now - 30)
        self.assertTrue(totp.verify(RFC_SECRET, prev_code, at=now))
        old_code = totp.code_now(RFC_SECRET, at=now - 90)
        self.assertFalse(totp.verify(RFC_SECRET, old_code, at=now))

    def test_provisioning_uri(self):
        uri = totp.provisioning_uri("ABC234", "me@x.dev")
        self.assertTrue(uri.startswith("otpauth://totp/Oikonome:me%40x.dev"))
        self.assertIn("secret=ABC234", uri)

    def test_garbage_secret_is_no_match_not_an_error(self):
        """A secret that isn't decodable base32 can never match, so
        verification answers no — it must not raise. The enrollment
        confirm endpoint feeds a client-supplied secret straight into
        verify_used, and an exception there turned a plain bad input
        into a 500."""
        self.assertIsNone(totp.verify_used("not-valid-base32!!!", "123456",
                                           None))
        self.assertIsNone(totp.verify_used("not-valid-base32!!!", "123456", 5))
        self.assertFalse(totp.verify("not-valid-base32!!!", "123456"))

    def test_qr_svg_data_uri(self):
        uri = totp.provisioning_uri("ABC234", "me@x.dev")
        qr = totp.qr_svg_data_uri(uri)
        # an <img>-ready SVG data URI, white background so it scans on the
        # dark theme — and it must NOT leak the raw otpauth secret as text
        self.assertTrue(qr.startswith("data:image/svg+xml"))
        self.assertIn("svg", qr)
        self.assertNotIn("otpauth", qr)
        self.assertNotIn("ABC234", qr)


class TotpFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app

    def test_enroll_login_disable(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        c = TestClient(self.app)
        email = f"totp-{uuid.uuid4().hex[:8]}@example.dev"
        pw = "correct-horse-battery"
        c.post("/api/signup", data={"email": email, "password": pw})
        # the first enrol re-verifies the password
        r = c.post("/api/totp/enroll", data={"password": pw}).json()
        secret = r["secret"]
        # enroll returns a scannable QR (SVG data URI), never the raw otpauth
        self.assertTrue(r["qr"].startswith("data:image/svg+xml"))
        self.assertNotIn("otpauth", r)
        # confirm with a live code activates 2FA
        r = c.post("/api/totp/confirm",
                   data={"secret": secret, "code": _fresh_code(secret),
                         "password": pw})
        self.assertEqual(r.status_code, 200)
        c.post("/api/logout")
        # password alone now fails with the totp_required signal
        r = c.post("/api/login", data={"email": email, "password": pw})
        self.assertEqual(r.status_code, 401)
        self.assertIn("totp_required", r.text)
        # wrong code rejected
        r = c.post("/api/login", data={"email": email, "password": pw,
                                       "totp_code": "000000"})
        self.assertEqual(r.status_code, 401)
        # correct code passes
        r = c.post("/api/login", data={"email": email, "password": pw,
                                       "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200)
        # disable requires the password AND a valid code, then
        # password-only login works
        r = c.post("/api/totp/disable",
                   data={"code": _fresh_code(secret), "password": pw})
        self.assertEqual(r.status_code, 200)
        c.post("/api/logout")
        security._limiter._hits.clear()
        r = c.post("/api/login", data={"email": email, "password": pw})
        self.assertEqual(r.status_code, 200)


    def test_reenroll_requires_current_code_and_never_downgrades(self):
        """Enroll must not strip an active
        second factor; re-enrollment needs the current code."""
        from oikonome.web import security
        security._limiter._hits.clear()
        c = TestClient(self.app)
        email = f"totp2-{uuid.uuid4().hex[:8]}@example.dev"
        pw = "correct-horse-battery"
        c.post("/api/signup", data={"email": email, "password": pw})
        s1 = c.post("/api/totp/enroll", data={"password": pw}).json()["secret"]
        c.post("/api/totp/confirm", data={"secret": s1,
                                          "code": _fresh_code(s1),
                                          "password": pw})
        # enrolling again WITHOUT the current code (and unelevated) is
        # refused…
        r = c.post("/api/totp/enroll")
        self.assertEqual(r.status_code, 403)         # elevation_required
        # …and the ORIGINAL factor still works at login (never downgraded)
        c.post("/api/logout")
        security._limiter._hits.clear()
        r = c.post("/api/login", data={"email": email, "password": pw,
                                       "totp_code": _fresh_code(s1)})
        self.assertEqual(r.status_code, 200)
        # with the current code, re-enrollment proceeds and swap works
        s2 = c.post("/api/totp/enroll",
                    data={"current_code": _fresh_code(s1)}).json()["secret"]
        # confirm ALSO demands the current factor when rebinding (without
        # this, confirm bypassed enroll's guard)
        r = c.post("/api/totp/confirm", data={"secret": s2,
                                              "code": _fresh_code(s2)})
        self.assertEqual(r.status_code, 403)         # elevation_required
        r = c.post("/api/totp/confirm",
                   data={"secret": s2, "code": _fresh_code(s2),
                         "current_code": _fresh_code(s1)})
        self.assertEqual(r.status_code, 200)
        c.post("/api/logout")
        security._limiter._hits.clear()
        r = c.post("/api/login", data={"email": email, "password": pw,
                                       "totp_code": _fresh_code(s2)})
        self.assertEqual(r.status_code, 200)

    def test_confirm_answers_400_on_undecodable_secret(self):
        """The confirm door binds a client-supplied secret; garbage that
        isn't base32 gets the normal "code doesn't match" 400, never a
        500 from the decode."""
        from oikonome.web import security
        security._limiter._hits.clear()
        c = TestClient(self.app)
        email = f"totp4-{uuid.uuid4().hex[:8]}@example.dev"
        pw = "correct-horse-battery"
        c.post("/api/signup", data={"email": email, "password": pw})
        r = c.post("/api/totp/confirm", data={
            "secret": "not-valid-base32!!!", "code": "123456",
            "password": pw})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("doesn't match", r.text)

    def test_me_reports_totp_enabled(self):
        """/api/me carries the additive totp_enabled flag the SPA Settings
        card keys off — false at signup, true after confirm, false after
        disable."""
        from oikonome.web import security
        security._limiter._hits.clear()
        c = TestClient(self.app)
        email = f"totp3-{uuid.uuid4().hex[:8]}@example.dev"
        c.post("/api/signup", data={"email": email,
                                    "password": "correct-horse-battery"})
        self.assertFalse(c.get("/api/me").json()["totp_enabled"])
        s = c.post("/api/totp/enroll",
                   data={"password": "correct-horse-battery"}).json()["secret"]
        # enrollment alone doesn't flip it — only confirm binds the secret
        self.assertFalse(c.get("/api/me").json()["totp_enabled"])
        c.post("/api/totp/confirm", data={"secret": s,
                                          "code": _fresh_code(s),
                                          "password": "correct-horse-battery"})
        self.assertTrue(c.get("/api/me").json()["totp_enabled"])
        c.post("/api/totp/disable", data={"code": _fresh_code(s),
                                          "password": "correct-horse-battery"})
        self.assertFalse(c.get("/api/me").json()["totp_enabled"])


if __name__ == "__main__":
    unittest.main()
