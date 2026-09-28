"""Credential and session doors hold under encrypted TOTP.

Account delete must work against an encrypted TOTP secret and must be
throttled; TOTP disable requires a password step-up; the reset email
builds its link from configured base URL, never from a client-supplied
Host header; and the forwarded-proto header is honoured only behind a
trusted proxy.

Each class states the invariant it protects in its own docstring."""

import os
import threading
import types
import unittest
import uuid
from unittest import mock

from cryptography.fernet import Fernet
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


def _fresh_client(app):
    client = TestClient(app)
    email = f"rb-{uuid.uuid4().hex[:10]}@example.dev"
    r = client.post("/api/signup", data={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return client, email


def _enroll(client):
    """Enroll + confirm 2FA the way the SPA does (password step-up on the
    first factor), returning the plaintext seed for minting codes."""
    secret = client.post("/api/totp/enroll",
                         data={"password": PW}).json()["secret"]
    r = client.post("/api/totp/confirm", data={
        "secret": secret, "code": _fresh_code(secret), "password": PW})
    assert r.status_code == 200, r.text
    return secret


class DeleteWithEncryptedSeedTests(unittest.TestCase):
    """/api/account/delete must verify the TOTP code against the DECRYPTED
    seed. Verifying against the stored ciphertext makes erasure permanently
    impossible for 2FA users on any install with a master key set — which
    is every real install."""

    def setUp(self):
        os.environ["OIKONOME_DEV"] = "1"
        # the bug only bites when the seed actually encrypts — run this
        # whole class WITH a master key, unlike most of the suite
        self._prev = os.environ.get("OIKONOME_MASTER_KEY")
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        self.app = appmod.app

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("OIKONOME_MASTER_KEY", None)
        else:
            os.environ["OIKONOME_MASTER_KEY"] = self._prev

    def test_valid_code_deletes_the_account(self):
        import psycopg
        from psycopg.rows import dict_row

        from oikonome.db import crypto, tenancy
        client, email = _fresh_client(self.app)
        secret = _enroll(client)
        # sanity: the seed really is ciphertext at rest — otherwise this
        # test silently degrades to the plaintext passthrough path and
        # would pass even with the bug present
        with psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                             autocommit=True) as conn:
            raw = conn.execute("SELECT totp_secret FROM users WHERE email=%s",
                               (email,)).fetchone()["totp_secret"]
        self.assertTrue(raw.startswith(crypto.CP_PREFIX), raw[:20])
        r = client.post("/api/account/delete", data={
            "password": PW, "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_wrong_code_is_401_and_account_survives(self):
        client, _ = _fresh_client(self.app)
        _enroll(client)
        r = client.post("/api/account/delete", data={
            "password": PW, "totp_code": "000000"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(client.get("/api/me").status_code, 200)


class ResetEmailPinnedBaseTests(unittest.TestCase):
    """The reset email never derives its absolute URL from the request.
    Host / X-Forwarded-Proto are attacker-controlled, so a forged /forgot
    would mail a REAL token on an attacker-domain link. Policy mirrors
    invites: with SMTP configured but OIKONOME_BASE_URL unset, never
    email — log the reset path instead, and name the knob to turn."""

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

    def _post_forgot(self, base_url: str | None):
        """POST /forgot with a forged Host, SMTP mocked as configured, and
        delivery captured. Returns (sent_bodies, logged_warnings) after the
        background delivery thread signals completion — either by sending
        (the pinned-base-URL path) or by logging the reset link (refusal
        path)."""
        import oikonome.web.app as appmod
        from oikonome.web import report
        client, email = _fresh_client(self.app)
        sent: list[str] = []
        logged: list[str] = []
        done = threading.Event()

        def fake_send(subject, plain, html, recipients, smtp=None, **kw):
            sent.append(plain + html)
            done.set()

        def spy_warning(msg, *args, **kw):
            logged.append((msg % args) if args else str(msg))
            # the "password reset link" line is the delivery of last resort —
            # once it fires, the thread is past any send() it could do
            if "password reset link" in str(msg):
                done.set()

        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(appmod, "DEV_MODE", False), \
                mock.patch.object(report, "resolve_smtp",
                                  return_value={"configured": True}), \
                mock.patch.object(report, "send", new=fake_send), \
                mock.patch.object(appmod.log, "warning", new=spy_warning):
            if base_url is None:
                os.environ.pop("OIKONOME_BASE_URL", None)
            else:
                os.environ["OIKONOME_BASE_URL"] = base_url
            r = client.post("/forgot", data={"email": email},
                            headers={"host": "evil.example",
                                     "x-forwarded-proto": "https"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(done.wait(10), "reset delivery never completed")
        return sent, logged

    def test_unpinned_base_refuses_email_despite_forged_host(self):
        sent, logged = self._post_forgot(base_url=None)
        self.assertEqual(sent, [],
                         f"reset email sent with no pinned base: {sent!r}")
        joined = "\n".join(logged)
        # the operator can still recover: the path is server-logged…
        self.assertIn("/reset?token=", joined)
        # …and the refusal names the knob to turn
        self.assertIn("OIKONOME_BASE_URL", joined)

    def test_pinned_base_sends_the_pinned_origin_not_the_host(self):
        sent, _ = self._post_forgot(base_url="https://good.example")
        body = "".join(sent)
        self.assertIn("https://good.example/reset?token=", body)
        self.assertNotIn("evil.example", body)


class DeleteThrottleAndDisableStepUpTests(unittest.TestCase):
    """(a) /api/account/delete takes a password guess, so it carries the
    same 5/h rate limit password_change does: the 6th wrong attempt is a
    429, not another 401. (b) /api/totp/disable needs a password step-up
    as well as a TOTP code — a code alone would let a stolen session plus
    one observed 6-digit code strip 2FA and keep the session."""

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

    def test_account_delete_is_rate_limited(self):
        client, _ = _fresh_client(self.app)
        for _ in range(5):
            r = client.post("/api/account/delete",
                            data={"password": "wrong-wrong-wrong"})
            self.assertEqual(r.status_code, 401)
        r = client.post("/api/account/delete",
                        data={"password": "wrong-wrong-wrong"})
        self.assertEqual(r.status_code, 429)
        # …and the throttle didn't break the real thing: a fresh window
        # with the right password still erases
        from oikonome.web import security
        security._limiter._hits.clear()
        r = client.post("/api/account/delete", data={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_totp_disable_needs_password_and_code(self):
        client, _ = _fresh_client(self.app)
        secret = _enroll(client)
        # code alone: refused
        r = client.post("/api/totp/disable",
                        data={"code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 403)         # elevation_required
        self.assertIn("elevation_required", r.text)
        # wrong password: refused
        r = client.post("/api/totp/disable",
                        data={"code": _fresh_code(secret),
                              "password": "not-the-password"})
        self.assertEqual(r.status_code, 401)
        # password without a valid code: refused (still both factors)
        r = client.post("/api/totp/disable",
                        data={"code": "000000", "password": PW})
        self.assertEqual(r.status_code, 401)
        # 2FA is still on after all three rejects
        self.assertTrue(client.get("/api/me").json()["totp_enabled"])
        # password + live code: off
        r = client.post("/api/totp/disable",
                        data={"code": _fresh_code(secret),
                              "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(client.get("/api/me").json()["totp_enabled"])


class ForwardedProtoTrustTests(unittest.TestCase):
    """HSTS and the Secure-cookie flag believe X-Forwarded-Proto only
    behind OIKONOME_TRUSTED_PROXIES, the same gate the rate limiter puts
    on X-Forwarded-For. Believed from anyone, one forged header on a
    cleartext LAN install pins browsers to https for a year (self-DoS) or
    mints a Secure cookie the client's own http connection then drops."""

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

    @staticmethod
    def _req(peer, proto=None, scheme="http"):
        headers = {} if proto is None else {"x-forwarded-proto": proto}
        return types.SimpleNamespace(
            client=types.SimpleNamespace(host=peer),
            headers=headers,
            url=types.SimpleNamespace(scheme=scheme))

    def test_request_scheme_trust_matrix(self):
        from oikonome.web import security
        # no trusted proxies configured → the header never counts
        with mock.patch.dict(os.environ, {"OIKONOME_TRUSTED_PROXIES": ""}):
            self.assertEqual(
                security.request_scheme(self._req("203.0.113.9", "https")),
                "http")
            # …but a REAL tls connection keeps its scheme without any proxy
            self.assertEqual(
                security.request_scheme(self._req("203.0.113.9", None,
                                                  scheme="https")),
                "https")
        with mock.patch.dict(os.environ,
                             {"OIKONOME_TRUSTED_PROXIES": "10.0.0.0/24"}):
            # trusted peer's word is believed
            self.assertEqual(
                security.request_scheme(self._req("10.0.0.1", "https")),
                "https")
            # untrusted peer's forgery is not
            self.assertEqual(
                security.request_scheme(self._req("203.0.113.9", "https")),
                "http")
            # trusted peer, no header → socket scheme
            self.assertEqual(
                security.request_scheme(self._req("10.0.0.1", None)), "http")
            # appended chain: the rightmost (nearest-trusted-hop) entry wins
            # over a client-forged left entry
            self.assertEqual(
                security.request_scheme(self._req("10.0.0.1", "https, http")),
                "http")

    def test_secure_cookie_honors_the_trust_gate(self):
        import oikonome.web.app as appmod
        client, email = _fresh_client(self.app)
        client.post("/api/logout")
        # DEV_MODE forces secure=False outright — lift it so the scheme
        # decision is what's under test
        with mock.patch.object(appmod, "DEV_MODE", False):
            # forged proto from a DIRECT http client: cookie must not be
            # Secure (the browser would drop it and brick the LAN install)
            r = client.post("/api/login",
                            data={"email": email, "password": PW},
                            headers={"x-forwarded-proto": "https"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertNotIn("secure", r.headers.get("set-cookie", "").lower())
            # same header from a TRUSTED proxy peer: Secure
            proxied = TestClient(self.app, client=("10.9.9.9", 4711))
            with mock.patch.dict(os.environ,
                                 {"OIKONOME_TRUSTED_PROXIES": "10.9.9.9"}):
                r = proxied.post("/api/login",
                                 data={"email": email, "password": PW},
                                 headers={"x-forwarded-proto": "https"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("secure", r.headers.get("set-cookie", "").lower())
            # direct TLS (no proxy, no header): real scheme → Secure
            tls = TestClient(self.app, base_url="https://testserver")
            r = tls.post("/api/login",
                         data={"email": email, "password": PW})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("secure", r.headers.get("set-cookie", "").lower())

    def test_hsts_needs_a_trusted_proxy_or_real_tls(self):
        anon = TestClient(self.app)
        r = anon.get("/login", headers={"x-forwarded-proto": "https"})
        self.assertNotIn("strict-transport-security",
                         {k.lower() for k in r.headers})
        proxied = TestClient(self.app, client=("10.9.9.9", 4711))
        with mock.patch.dict(os.environ,
                             {"OIKONOME_TRUSTED_PROXIES": "10.9.9.9"}):
            r = proxied.get("/login", headers={"x-forwarded-proto": "https"})
        self.assertIn("max-age=31536000",
                      r.headers.get("strict-transport-security", ""))
        # real TLS needs no proxy at all
        tls = TestClient(self.app, base_url="https://testserver")
        r = tls.get("/login")
        self.assertIn("max-age=31536000",
                      r.headers.get("strict-transport-security", ""))


class PlainHttpHstsTests(unittest.TestCase):
    """A cleartext install must never send HSTS — pinning an
    http-only LAN box to https for a year is a self-DoS."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def test_hsts_https_only(self):
        from unittest import mock
        anon = TestClient(self.app)
        # plain http (dev): no HSTS (would pin http-only LAN installs)
        r = anon.get("/login")
        self.assertNotIn("strict-transport-security", {k.lower() for k in r.headers})
        # a DIRECT client's forged X-Forwarded-Proto no longer buys
        # HSTS — only a peer in OIKONOME_TRUSTED_PROXIES is believed
        r2 = anon.get("/login", headers={"x-forwarded-proto": "https"})
        self.assertNotIn("strict-transport-security",
                         {k.lower() for k in r2.headers})
        # simulated TLS terminated at a TRUSTED proxy: HSTS is on
        proxied = TestClient(self.app, client=("10.9.9.9", 4711))
        with mock.patch.dict(os.environ,
                             {"OIKONOME_TRUSTED_PROXIES": "10.9.9.9"}):
            r3 = proxied.get("/login", headers={"x-forwarded-proto": "https"})
        self.assertIn("max-age=31536000",
                      r3.headers.get("strict-transport-security", ""))


if __name__ == "__main__":
    unittest.main()
