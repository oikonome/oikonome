"""Passkeys: options/challenge lifecycle, register + login with
the WebAuthn verifier mocked (no real authenticator in CI), sign-count
bump, own-only delete. Plus the session-lifetime model (sliding idle +
absolute cap)."""

import datetime as dt
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"   # enroll/delete re-verify it


class _FakeReg:
    credential_id = b"\x01\x02\x03\x04"
    credential_public_key = b"\x05\x06\x07\x08"
    sign_count = 0


class _FakeAuth:
    new_sign_count = 7


class PasskeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        os.environ["OIKONOME_HOSTED"] = "1"   # passkeys are hosted-only now
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app
        cls.client = TestClient(app)
        cls.email = f"pk-{uuid.uuid4().hex[:8]}@example.dev"
        # hosted signup is invite-gated — mint one, as the operator would
        from oikonome.auth import signup_invites
        admin = tenancy.admin_connect()
        try:
            invite = signup_invites.mint(admin, cls.email)
        finally:
            admin.close()
        r = cls.client.post("/api/signup", data={
            "email": cls.email, "password": PW, "invite": invite})
        assert r.status_code == 200, r.text
        # These are the *password-gating* tests. A separate layer covers
        # a passkey-ONLY account (no TOTP), which must present a recovery
        # code for posture changes. To keep these focused on the
        # password gate (and not on that recovery layer, which has its own
        # tests), give the user a TOTP factor so the account is never
        # passkey-only — _require_passkey_stepup then no-ops here.
        from oikonome.auth import totp as _totp
        from oikonome.db import crypto as _crypto
        cls.totp_secret = _totp.new_secret()   # kept so tests can mint codes
        admin2 = tenancy.admin_connect()   # users is control-plane (no RLS)
        try:
            admin2.execute(
                "UPDATE users SET totp_secret=%s WHERE email=%s",
                (_crypto.encrypt_cp(cls.totp_secret), cls.email))
        finally:
            admin2.close()

    @classmethod
    def _totp_now(cls):
        """A usable code, every time.

        Step-up doors BURN the code now (`_totp_consume`), so two
        calls inside one 30s step would be a replay — correctly refused. These
        tests exercise the step-up gate, not replay protection (which has its
        own test in test_auth_hardening), so clear the burn rather than
        contort them around the clock."""
        from oikonome.auth import totp as _totp
        from oikonome.db import tenancy
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE users SET totp_last_counter=NULL WHERE email=%s",
                (cls.email,))
        finally:
            admin.close()
        return _totp.code_now(cls.totp_secret)

    def setUp(self):
        # the step-up tests deliberately burn failed attempts against the
        # passkey_manage bucket (10/h per IP, in-process store) — clear it
        # between tests so they can't 429 each other
        from oikonome.web import security
        security._limiter._hits.clear()

    @classmethod
    def tearDownClass(cls):
        import os
        os.environ.pop("OIKONOME_HOSTED", None)
        # `passkeys.credential_id` is globally UNIQUE on a control-plane
        # table (no RLS) and _FakeReg's id is a CONSTANT, so leaving the row
        # behind would make this suite pass once per database and then
        # fail forever with "passkey rejected: UniqueViolation" — a fresh
        # tenant per test does not save us here, because the constraint is
        # not tenant-scoped. Clean up what we inserted.
        import psycopg
        with psycopg.connect(tenancy.APP_DSN, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM passkeys WHERE user_id IN "
                "(SELECT id FROM users WHERE email = %s)", (cls.email,))

    def _options(self, **over):
        """Mint a registration challenge the way the SPA does: password
        (and, since this account has TOTP, a live code) go up front."""
        body = {"password": PW, "totp_code": self._totp_now(), **over}
        return self.client.post("/api/passkeys/options", json=body)

    def test_options_refuse_to_start_the_ceremony_on_a_wrong_password(self):
        # The authenticator SAVES the credential during the ceremony, so a
        # wrong password caught only at persist time leaves the person with
        # a passkey in their vault the server never kept. The challenge must
        # not be minted at all.
        # no proof at all (unelevated) → elevation_required; a wrong
        # password → password_required. Neither mints the challenge.
        for bad, status, want in (
                ({"password": ""}, 403, "elevation_required"),
                ({"password": "not-the-password"}, 401,
                 "password_required")):
            r = self._options(**bad)
            self.assertEqual(r.status_code, status, r.text)
            self.assertIn(want, r.text)
            self.assertNotIn("challenge_id", r.text)

    def test_options_refuse_to_start_the_ceremony_on_a_wrong_code(self):
        # same reason: a TOTP account must present a live code to bind a
        # new passkey, and finding that out after the ceremony is too late.
        for bad in ({"totp_code": ""}, {"totp_code": "000000"}):
            r = self._options(**bad)
            self.assertEqual(r.status_code, 401, r.text)
            self.assertNotIn("challenge_id", r.text)
        # the pre-check consumes nothing: the same code that opened options
        # still binds the key at persist time
        o = self._options().json()
        cred = {"id": "AQIDBA", "response": {"transports": ["internal"]}}
        with mock.patch("oikonome.auth.passkeys.verify_registration_response",
                        return_value=_FakeReg()):
            r = self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"], "credential": cred,
                "password": PW, "totp_code": self._totp_now()})
        self.assertEqual(r.status_code, 200, r.text)
        # _FakeReg's credential id is a constant and globally unique — take
        # the row back out so the lifecycle test can enrol it again
        import psycopg
        with psycopg.connect(tenancy.APP_DSN, autocommit=True) as conn:
            conn.execute("DELETE FROM passkeys WHERE id = %s",
                         (r.json()["id"],))

    def test_self_host_routes_are_404(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OIKONOME_HOSTED", None)
            self.assertEqual(
                self._options().status_code, 404)

    def test_uv_required_in_options(self):
        # passkey login stands in for password AND TOTP, so the
        # authenticator must verify the user (PIN/biometric) — REQUIRED,
        # not PREFERRED, in both registration and authentication options.
        o = self._options().json()
        self.assertEqual(
            o["options"]["authenticatorSelection"]["userVerification"],
            "required")
        lo = self.client.post("/api/login/passkey/options",
                              json={"email": self.email}).json()
        self.assertEqual(lo["options"]["userVerification"], "required")

    def test_enroll_requires_step_up(self):
        # a stolen session must not be able to enroll its own key.
        # No password / wrong password → 401 before any WebAuthn work.
        o = self._options().json()
        cred = {"id": "AQIDBA", "response": {"transports": ["internal"]}}
        for bad, status, want in (
                ({}, 403, "elevation_required"),
                ({"password": "not-the-password"}, 401,
                 "password_required")):
            with mock.patch(
                    "oikonome.auth.passkeys.verify_registration_response",
                    return_value=_FakeReg()) as vrr:
                r = self.client.post("/api/passkeys", json={
                    "challenge_id": o["challenge_id"], "credential": cred,
                    **bad})
            self.assertEqual(r.status_code, status, r.text)
            self.assertIn(want, r.text)
            vrr.assert_not_called()
        # the challenge must survive the rejection — step-up runs first,
        # so the good-password retry below still has a live challenge
        with mock.patch("oikonome.auth.passkeys.verify_registration_response",
                        return_value=_FakeReg()) as vrr:
            r = self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"], "credential": cred,
                "password": PW, "totp_code": self._totp_now()})
        self.assertEqual(r.status_code, 200, r.text)
        # …and the server demands UV on the signed response, not just in
        # the (client-tamperable) options
        self.assertTrue(
            vrr.call_args.kwargs.get("require_user_verification"))
        pk = self.client.get("/api/passkeys").json()["passkeys"][0]
        self.client.request("DELETE", f"/api/passkeys/{pk['id']}",
                            json={"password": PW,
                                  "totp_code": self._totp_now()})

    def test_delete_requires_step_up(self):
        # enroll one key legitimately…
        o = self._options().json()
        cred = {"id": "AQIDBA", "response": {"transports": ["internal"]}}
        with mock.patch("oikonome.auth.passkeys.verify_registration_response",
                        return_value=_FakeReg()):
            r = self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"], "credential": cred,
                "password": PW, "totp_code": self._totp_now()})
        self.assertEqual(r.status_code, 200, r.text)
        pk = self.client.get("/api/passkeys").json()["passkeys"][0]
        # …then a stolen session must not be able to strip it off
        for bad, status, want in (
                (None, 403, "elevation_required"),
                ({"password": "not-the-password"}, 401,
                 "password_required")):
            r = self.client.request("DELETE", f"/api/passkeys/{pk['id']}",
                                    json=bad)
            self.assertEqual(r.status_code, status, r.text)
            self.assertIn(want, r.text)
        self.assertEqual(
            len(self.client.get("/api/passkeys").json()["passkeys"]), 1)
        r = self.client.request("DELETE", f"/api/passkeys/{pk['id']}",
                                json={"password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("totp_required", r.text)
        r = self.client.request("DELETE", f"/api/passkeys/{pk['id']}",
                                json={"password": PW,
                                      "totp_code": self._totp_now()})
        self.assertEqual(r.status_code, 200, r.text)

    def test_register_login_lifecycle(self):
        o = self._options().json()
        self.assertIn("challenge", str(o["options"]))
        cred = {"id": "AQIDBA", "response": {"transports": ["internal"]}}
        with mock.patch("oikonome.auth.passkeys.verify_registration_response",
                        return_value=_FakeReg()):
            r = self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"], "credential": cred,
                "label": "laptop", "password": PW,
                "totp_code": self._totp_now()})
        self.assertEqual(r.status_code, 200)
        # challenge is single-use
        with mock.patch("oikonome.auth.passkeys.verify_registration_response",
                        return_value=_FakeReg()):
            r2 = self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"], "credential": cred,
                "password": PW, "totp_code": self._totp_now()})
        self.assertEqual(r2.status_code, 400)

        lst = self.client.get("/api/passkeys").json()["passkeys"]
        self.assertEqual([p["label"] for p in lst], ["laptop"])

        # passkey login from a fresh client
        anon = TestClient(self.app)
        lo = anon.post("/api/login/passkey/options",
                       json={"email": self.email}).json()
        with mock.patch(
                "oikonome.auth.passkeys.verify_authentication_response",
                return_value=_FakeAuth()) as var:
            r = anon.post("/api/login/passkey", json={
                "challenge_id": lo["challenge_id"],
                "credential": {"id": "AQIDBA"}})
        self.assertEqual(r.status_code, 200)
        # login enforces UV on the assertion itself — passkey
        # sign-in skips TOTP, so UV is the second factor
        self.assertTrue(
            var.call_args.kwargs.get("require_user_verification"))
        self.assertEqual(anon.get("/api/me").json()["email"], self.email)
        # sign count bumped + last_used stamped
        lst = self.client.get("/api/passkeys").json()["passkeys"]
        self.assertIsNotNone(lst[0]["last_used"])

        # unknown credential → 401, not a hint
        lo = anon.post("/api/login/passkey/options", json={}).json()
        r = anon.post("/api/login/passkey", json={
            "challenge_id": lo["challenge_id"],
            "credential": {"id": "bm9wZQ"}})
        self.assertEqual(r.status_code, 401)

        # delete own passkey — password + live TOTP
        r = self.client.request(
            "DELETE", f"/api/passkeys/{lst[0]['id']}",
            json={"password": PW, "totp_code": self._totp_now()})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/passkeys").json()["passkeys"],
                         [])


class ExpectedOriginTests(unittest.TestCase):
    """The set of page origins an assertion may claim. https is the rule:
    browsers refuse WebAuthn on insecure origins (and passkeys are a
    hosted-HTTPS feature), so a server-side http:// accept for a public
    host can never admit a real login — it only weakens downgrade
    resistance. localhost/127.0.0.1 keep http for dev; the https twin of
    a derived http origin stays accepted because a proxy that loses
    X-Forwarded-Proto reports http for a page really served over https."""

    def _origins(self, origin):
        # the Android app origins (android:apk-key-hash:…) ride every
        # list by design — these tests protect the WEB-origin rules, so
        # they look at the web subset
        from oikonome.auth.passkeys import _expected_origins
        return [o for o in _expected_origins(origin)
                if not o.startswith("android:")]

    def test_registration_accepts_the_same_origin_set_as_login(self):
        """Enrollment used a single literal origin while login/step-up
        accept the http/https twin. On a proxy that drops X-Forwarded-Proto
        that meant a passkey could sign in but could never be added — the
        register path must be exactly as tolerant as login."""
        from oikonome.auth import passkeys
        seen = {}

        def _fake_verify(**kw):
            seen["origin"] = kw.get("expected_origin")
            return _FakeReg()

        with mock.patch.object(passkeys, "verify_registration_response",
                               _fake_verify), \
             mock.patch.object(passkeys, "_take_challenge",
                               return_value="chal"):
            passkeys.register_verify(
                mock.MagicMock(), "uid", "cid",
                {"id": "AQIDBA", "response": {}},
                rp_id="app.oikonome.example",
                origin="http://app.oikonome.example")
        self.assertEqual(
            seen["origin"],
            passkeys._expected_origins("http://app.oikonome.example"))

    def test_public_host_never_accepts_plain_http(self):
        got = self._origins("https://app.oikonome.example")
        self.assertIn("https://app.oikonome.example", got)
        self.assertNotIn("http://app.oikonome.example", got)
        for o in got:
            self.assertTrue(o.startswith("https://"), o)

    def test_derived_http_origin_still_verifies_via_its_https_twin(self):
        # the proxy-header-gap case: config/derivation says http, the
        # browser signed https — logins must keep working
        got = self._origins("http://app.oikonome.example")
        self.assertIn("https://app.oikonome.example", got)
        self.assertNotIn("http://app.oikonome.example", got)

    def test_localhost_keeps_http_for_dev(self):
        for origin in ("http://localhost:8042", "http://127.0.0.1:8042"):
            self.assertIn(origin, self._origins(origin))

    def test_ports_ride_along(self):
        got = self._origins("http://app.oikonome.example:8443")
        self.assertEqual(got, ["https://app.oikonome.example:8443"])


class SessionLifetimeTests(unittest.TestCase):
    def test_sliding_idle_capped_by_absolute(self):
        from oikonome.auth import sessions
        _ensure_db()
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"sess-{uuid.uuid4().hex[:8]}")
            u = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (%s,%s,'x') RETURNING id",
                (tid, f"s-{uuid.uuid4().hex[:6]}@example.dev")).fetchone()
            token = sessions.create_session(admin, u["id"], tid, "ua")
            h = sessions._h(token)
            # fresh session: expires ≈ now + IDLE_TTL
            row = admin.execute("SELECT created_at, expires_at FROM sessions "
                                "WHERE token_hash=%s", (h,)).fetchone()
            idle_days = (row["expires_at"] - row["created_at"]).days
            self.assertAlmostEqual(idle_days, sessions.IDLE_TTL.days, delta=1)
            # activity slides the deadline forward…
            admin.execute("UPDATE sessions SET last_seen = now() - interval "
                          "'2 minutes', expires_at = now() + interval '1 day'"
                          " WHERE token_hash=%s", (h,))
            self.assertIsNotNone(sessions.lookup_session(admin, token))
            row = admin.execute("SELECT expires_at FROM sessions "
                                "WHERE token_hash=%s", (h,)).fetchone()
            self.assertGreater(
                row["expires_at"],
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=25))
            # …but never past created_at + ABSOLUTE_TTL
            admin.execute("UPDATE sessions SET created_at = now() - %s, "
                          "last_seen = now() - interval '2 minutes' "
                          "WHERE token_hash=%s",
                          (sessions.ABSOLUTE_TTL - dt.timedelta(days=1), h))
            sessions.lookup_session(admin, token)
            row = admin.execute("SELECT expires_at FROM sessions "
                                "WHERE token_hash=%s", (h,)).fetchone()
            self.assertLess(
                row["expires_at"],
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2))
        finally:
            admin.close()


if __name__ == "__main__":
    unittest.main()
