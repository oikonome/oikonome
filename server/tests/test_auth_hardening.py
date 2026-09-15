"""Auth hardening: password reset (web + CLI), session
management API, security headers, account deletion (CCPA path)."""

import contextlib
import io
import re
import sys
import unittest
import uuid

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from oikonome.auth import reset as reset_mod
from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config

PW = "correct-horse-battery"


def _control():
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


class AuthHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        # each test signs up its own users; don't trip signup/forgot limits
        from oikonome.web import security
        security._limiter._hits.clear()

    def _signup(self, seed=False):
        client = TestClient(self.app)
        email = f"ah-{uuid.uuid4().hex[:10]}@example.dev"
        r = client.post("/api/signup", data={"email": email, "password": PW})
        assert r.status_code == 200, r.text
        tid = client.get("/api/me").json()["tenant_id"]
        if seed:
            conn = tenancy.tenant_connect(tid)
            try:
                seed_accounts(conn)
                write_config(conn)
                add_txn(conn, "2026-07-14", 42.0, "Coffee")
            finally:
                conn.close()
        return client, email, tid

    # ---- password reset ----------------------------------------------------

    def test_reset_happy_path(self):
        client, email, _ = self._signup()
        anon = TestClient(self.app)
        with self.assertLogs("oikonome.auth", level="INFO") as logs:
            r = anon.post("/forgot", data={"email": email})
        self.assertEqual(r.status_code, 200)
        self.assertIn("if that address exists", r.text.lower())
        m = re.search(r"/reset\?token=([\w\-]+)", "\n".join(logs.output))
        self.assertIsNotNone(m, "reset link not logged")
        token = m.group(1)

        r = anon.get(f"/reset?token={token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("New password", r.text)

        r = anon.post("/reset", data={"token": token,
                                      "password": "brand-new-password-1"},
                      follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login?reset=1")
        self.assertIn("Password updated",
                      anon.get("/login?reset=1").text)

        # every pre-reset session is revoked
        self.assertEqual(client.get("/api/me").status_code, 401)
        # old password dead, new one works
        r = anon.post("/api/login", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 401)
        r = anon.post("/api/login", data={"email": email,
                                          "password": "brand-new-password-1"})
        self.assertEqual(r.status_code, 200)

    def test_forgot_is_enumeration_safe(self):
        _, email, _ = self._signup()
        anon = TestClient(self.app)
        with _control() as conn:
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM password_resets").fetchone()["n"]
        r_known = anon.post("/forgot", data={"email": email})
        r_unknown = anon.post(
            "/forgot", data={"email": f"nobody-{uuid.uuid4().hex}@example.dev"})
        self.assertEqual(r_known.status_code, 200)
        self.assertEqual(r_unknown.status_code, 200)
        self.assertEqual(r_known.text, r_unknown.text)   # byte-identical page
        with _control() as conn:
            after = conn.execute(
                "SELECT COUNT(*) AS n FROM password_resets").fetchone()["n"]
        self.assertEqual(after, before + 1)   # known minted one, unknown none

    def test_forgot_rate_limited(self):
        anon = TestClient(self.app)
        for _ in range(5):
            self.assertEqual(anon.post("/forgot", data={
                "email": "x@example.dev"}).status_code, 200)
        self.assertEqual(anon.post("/forgot", data={
            "email": "x@example.dev"}).status_code, 429)

    def test_reset_token_is_single_use(self):
        _, email, _ = self._signup()
        with _control() as conn:
            u = conn.execute("SELECT id FROM users WHERE email=%s",
                             (email,)).fetchone()
            token = reset_mod.create_reset(conn, u["id"])
        anon = TestClient(self.app)
        r = anon.post("/reset", data={"token": token,
                                      "password": "first-use-password"},
                      follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        r = anon.post("/reset", data={"token": token,
                                      "password": "second-use-password"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("invalid, expired, or already used", r.text)

    def test_reset_token_expired(self):
        _, email, _ = self._signup()
        with _control() as conn:
            u = conn.execute("SELECT id FROM users WHERE email=%s",
                             (email,)).fetchone()
            token = reset_mod.create_reset(conn, u["id"])
        # backdate via the ADMIN role: column-scoped the app
        # role's password_resets UPDATE to used_at only (expires_at is
        # exactly what injected SQL must not extend)
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                """UPDATE password_resets SET expires_at = now() - interval '1 minute'
                   WHERE user_id = %s""", (u["id"],))
        finally:
            admin.close()
        anon = TestClient(self.app)
        self.assertEqual(anon.get(f"/reset?token={token}").status_code, 400)
        r = anon.post("/reset", data={"token": token,
                                      "password": "never-mind-password"})
        self.assertEqual(r.status_code, 400)

    def test_reset_garbage_token_and_short_password(self):
        anon = TestClient(self.app)
        self.assertEqual(anon.get("/reset?token=not-a-token").status_code, 400)
        _, email, _ = self._signup()
        with _control() as conn:
            u = conn.execute("SELECT id FROM users WHERE email=%s",
                             (email,)).fetchone()
            token = reset_mod.create_reset(conn, u["id"])
        r = anon.post("/reset", data={"token": token, "password": "short"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("at least 10 characters", r.text)
        # short password did NOT burn the token
        r = anon.post("/reset", data={"token": token,
                                      "password": "long-enough-now-ok"},
                      follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    def test_login_page_skips_the_form_when_already_signed_in(self):
        """Browsers learn /login from repeated sign-ins and autocomplete
        later visits straight to it; serving the form to a valid session
        asked signed-in people to sign in again, which read as sessions
        not sticking while the abandoned ones sat valid in the DB."""
        client, _, _ = self._signup()
        r = client.get("/login", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/app/")
        # and signed OUT still gets the form
        r = TestClient(self.app).get("/login", follow_redirects=False)
        self.assertEqual(r.status_code, 200)

    def test_signing_out_from_a_link_lands_on_the_form(self):
        """GET /logout is the door current_user carves out three times
        for frozen accounts (suspended, pending-delete, an add-on's own) —
        and, since /login redirects a live session into the app, the only
        way to reach the sign-in form to sign in as someone else. Without
        it a frozen browser is stuck holding its dead session."""
        client, _, _ = self._signup()
        r = client.get("/logout", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")
        # the session is really gone, so /login now serves the form
        self.assertEqual(client.get("/api/me").status_code, 401)
        r = client.get("/login", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("password", r.text)

    # a real browser's fetch metadata for a top-level navigation someone
    # typed or bookmarked: the shape a genuine sign-out arrives in
    _NAV = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none"}

    def test_a_navigation_signs_out_but_a_cross_site_subresource_cannot(self):
        """Sign-out by GET is a state change (session revoked, every
        unspent mint ticket burnt) outside the write-only Origin check,
        and SameSite=Lax still sends the cookie on cross-site GETs — so
        `<img src="https://…/logout">` on any page would sign a visitor
        out and drop them on a sign-in form a phishing page can imitate.
        Fetch metadata separates the two: a person's navigation carries
        mode=navigate/dest=document, a sub-resource never does."""
        client, _, _ = self._signup()
        # an image tag on someone else's page: cookie attached, no
        # navigation — must not touch the session
        r = client.get("/logout", follow_redirects=False,
                       headers={"Sec-Fetch-Dest": "image",
                                "Sec-Fetch-Mode": "no-cors",
                                "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(client.get("/api/me").status_code, 200,
                         "a cross-site sub-resource signed the user out")
        # nor may that page force the navigation instead
        r = client.get("/logout", follow_redirects=False,
                       headers={"Sec-Fetch-Dest": "document",
                                "Sec-Fetch-Mode": "navigate",
                                "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(client.get("/api/me").status_code, 200)
        # a browser too old for fetch metadata is judged by the same
        # Origin/Referer rule writes get
        r = client.get("/logout", follow_redirects=False,
                       headers={"Referer": "https://evil.example/"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(client.get("/api/me").status_code, 200)
        # and the door still opens for the person who typed the URL
        r = client.get("/logout", follow_redirects=False, headers=self._NAV)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_a_frozen_account_can_still_sign_itself_out(self):
        """The route exists so a suspended / pending-delete household is
        not stuck holding a dead session — whatever guards it must never
        close that door."""
        from oikonome.db import tenancy
        for status in ("suspended", "pending_delete"):
            client, _, tid = self._signup()
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE tenants SET status=%s WHERE id=%s",
                              (status, tid))
            finally:
                admin.close()
            r = client.get("/logout", follow_redirects=False,
                           headers=self._NAV)
            self.assertEqual(r.status_code, 303, status)
            self.assertEqual(r.headers["location"], "/login", status)

    def test_a_frozen_session_still_gets_the_sign_in_form(self):
        """A suspended or pending-delete household is locked out of the
        whole app, so redirecting it into /app/ would only bounce it back.
        (A lockout that leaves doors open inside the app — an add-on's
        own frozen state with its way out — is the exception; the add-on's
        own suite covers it.)"""
        for status in ("suspended", "pending_delete"):
            with self.subTest(status=status):
                client, _, tid = self._signup()
                self._freeze(tid, status)
                r = client.get("/login", follow_redirects=False)
                self.assertEqual(r.status_code, 200, status)

    @staticmethod
    def _freeze(tid, status):
        from oikonome.db import tenancy
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET status=%s WHERE id=%s",
                          (status, tid))
        finally:
            admin.close()

    def test_every_status_that_locks_a_tenant_out_is_in_one_table(self):
        """The set is consulted by more than one door — the API gate and
        the /login shortcut — so it lives in one place. A status enforced
        by one and not the other is the drift this table exists to stop.
        The product's own statuses are two; an installed add-on may extend
        the table, so the check is containment, not equality."""
        from oikonome.db import tenancy
        from oikonome.web import app as appmod
        self.assertLessEqual({"suspended", "pending_delete"},
                             set(appmod.LOCKED_OUT_STATUSES))
        for status in appmod.LOCKED_OUT_STATUSES:
            client, _, tid = self._signup()
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE tenants SET status=%s WHERE id=%s",
                              (status, tid))
            finally:
                admin.close()
            self.assertIn(client.get("/api/me").status_code, (402, 403),
                          f"{status} did not lock the tenant out of the API")

    def test_an_email_flow_arrival_always_gets_the_form(self):
        """A ?reset/?verified/?unlocked link may belong to a DIFFERENT
        account than the machine's cookie — on a shared machine, bouncing
        into the stale signed-in account would hide exactly the thing the
        person came to do."""
        client, _, _ = self._signup()
        for q in ("reset=1", "verified=1", "unlocked=1"):
            r = client.get(f"/login?{q}", follow_redirects=False)
            self.assertEqual(r.status_code, 200, q)

    def test_login_page_links_forgot(self):
        anon = TestClient(self.app)
        r = anon.get("/login")
        self.assertIn('href="/forgot"', r.text)
        r = anon.get("/forgot")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Send reset link", r.text)

    # ---- CLI reset ----------------------------------------------------------

    def test_cli_reset_password_prints_link(self):
        from oikonome import cli
        _, email, _ = self._signup()
        argv, sys.argv = sys.argv, ["oikonome", "reset-password", email]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cli.main()
        finally:
            sys.argv = argv
        m = re.search(r"^/reset\?token=([\w\-]+)$", out.getvalue(), re.M)
        self.assertIsNotNone(m, f"no link in: {out.getvalue()!r}")
        with _control() as conn:                 # token is real and live
            self.assertIsNotNone(reset_mod.lookup_reset(conn, m.group(1)))

    def test_cli_reset_password_unknown_email_exits_1(self):
        from oikonome import cli
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.reset_password(f"ghost-{uuid.uuid4().hex}@example.dev")
        self.assertEqual(cm.exception.code, 1)

    # ---- session management --------------------------------------------------

    def test_sessions_list_and_revoke(self):
        c1, email, _ = self._signup()
        c2 = TestClient(self.app)
        self.assertEqual(c2.post("/api/login", data={
            "email": email, "password": PW}).status_code, 200)
        c2.get("/api/me")                        # stamp last_seen

        r = c1.get("/api/sessions")
        self.assertEqual(r.status_code, 200)
        sess = r.json()["sessions"]
        self.assertEqual(len(sess), 2)
        self.assertEqual(sum(s["current"] for s in sess), 1)
        for s in sess:
            self.assertIn("created_at", s)
            self.assertIn("user_agent", s)
            # every row prints its expiry (web + native); a row without
            # it crashed the native Security screen
            self.assertTrue(s.get("expires_at"))
            self.assertIn("ip", s)
            self.assertNotIn("token_hash", s)
            uuid.UUID(s["id"])                   # ids are UUIDs, not hashes
        other = next(s for s in sess if not s["current"])
        self.assertIsNotNone(other["last_seen"])

        # revoking ANOTHER session is destructive (kick the owner) —
        # gated like all_others; unelevated and without the password
        # it's refused (elevation_required — the client opens the sheet)…
        r = c1.post("/api/sessions/revoke", json={"id": other["id"]})
        self.assertEqual(r.status_code, 403)
        self.assertIn("elevation_required", r.text)
        self.assertEqual(c2.get("/api/me").status_code, 200)   # still alive
        # …with the password → only that session dies
        r = c1.post("/api/sessions/revoke",
                    json={"id": other["id"], "password": PW})
        self.assertEqual(r.json(), {"ok": True, "revoked": 1})
        self.assertEqual(c2.get("/api/me").status_code, 401)
        self.assertEqual(c1.get("/api/me").status_code, 200)

        # revoke all others → current survives
        c3 = TestClient(self.app)
        c3.post("/api/login", data={"email": email, "password": PW})
        r = c1.post("/api/sessions/revoke", json={"all_others": True, "password": PW})
        self.assertEqual(r.json(), {"ok": True, "revoked": 1})
        self.assertEqual(c3.get("/api/me").status_code, 401)
        self.assertEqual(c1.get("/api/me").status_code, 200)
        self.assertEqual(len(c1.get("/api/sessions").json()["sessions"]), 1)

    def test_revoke_all_others_kills_device_tokens(self):
        """The Sessions card lists phones next to browsers; the bulk
        button is 'everywhere else' and must revoke oikd_ tokens too."""
        c1, _, _ = self._signup()
        r = c1.post("/api/devices", json={
            "device_name": "Pixel", "platform": "android"})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        c_dev = TestClient(self.app)
        me = c_dev.get("/api/me",
                       headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me.status_code, 200, me.text)
        r = c1.post("/api/sessions/revoke",
                    json={"all_others": True, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        dead = c_dev.get("/api/me",
                         headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(dead.status_code, 401)

    def test_sessions_revoke_rejects_junk(self):
        c1, _, _ = self._signup()
        self.assertEqual(c1.post("/api/sessions/revoke", json={}).status_code, 400)
        self.assertEqual(c1.post("/api/sessions/revoke",
                                 json={"id": "not-a-uuid"}).status_code, 400)

    def test_sessions_revoke_cannot_touch_other_users(self):
        c1, _, _ = self._signup()
        c2, _, _ = self._signup()
        victim = c2.get("/api/sessions").json()["sessions"][0]["id"]
        # even with c1's own password (step-up passes), the revoke is
        # user-scoped, so c2's session is untouched
        r = c1.post("/api/sessions/revoke", json={"id": victim, "password": PW})
        self.assertEqual(r.json(), {"ok": True, "revoked": 0})
        self.assertEqual(c2.get("/api/me").status_code, 200)

    # ---- security headers ----------------------------------------------------

    def test_security_headers_on_pages_and_spa(self):
        anon = TestClient(self.app)
        for path in ("/", "/login", "/setup"):
            r = anon.get(path, follow_redirects=False)
            self.assertEqual(r.headers["x-content-type-options"], "nosniff", path)
            self.assertEqual(r.headers["x-frame-options"], "DENY", path)
            self.assertEqual(r.headers["referrer-policy"],
                             "strict-origin-when-cross-origin", path)
            csp = r.headers["content-security-policy"]
            self.assertIn("style-src 'self' 'unsafe-inline'", csp, path)
            self.assertIn("default-src 'self'", csp, path)
            # the pages lost 'unsafe-inline' for scripts too —
            # all page JS is external (static/pages.js)
            self.assertIn("script-src 'self'", csp, path)
            self.assertNotIn("script-src 'self' 'unsafe-inline'", csp, path)
            self.assertNotIn("http://", csp)     # no external hosts
        # SPA gets the strict script policy: no inline script
        r = anon.get("/app", follow_redirects=False)
        csp = r.headers["content-security-policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("img-src 'self' data:", csp)
        # pages still render with the headers on
        self.assertEqual(anon.get("/login").status_code, 200)

    def test_spa_shell_never_cached_blind(self):
        """The unhashed shell must always revalidate: with no
        Cache-Control, browsers heuristically cache index.html across
        deploys and keep loading last week's bundle. Hashed assets keep
        long-lived immutable caching."""
        anon = TestClient(self.app)
        r = anon.get("/app/transactions", follow_redirects=False)
        if r.status_code == 404:
            self.skipTest("webapp/dist not built in this environment")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-cache", r.headers.get("cache-control", ""))
        # a hashed asset (if any exist) is immutable for a year
        import re
        m = re.search(r'assets/index-[\w-]+\.js', r.text)
        if m:
            ra = anon.get(f"/app/{m.group(0)}")
            self.assertEqual(ra.status_code, 200)
            self.assertIn("immutable", ra.headers.get("cache-control", ""))

    # ---- account deletion -----------------------------------------------------

    def test_account_delete_erases_tenant_but_not_others(self):
        ca, email_a, tid_a = self._signup(seed=True)
        cb, email_b, tid_b = self._signup(seed=True)

        r = ca.post("/api/account/delete", data={"password": "wrong-wrong-x"})
        self.assertEqual(r.status_code, 401)     # re-auth required

        r = ca.post("/api/account/delete", data={"password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(ca.get("/api/me").status_code, 401)  # cookie dead

        admin = tenancy.admin_connect()
        try:
            for tbl in ("transactions", "accounts", "items", "tenant_settings"):
                n = admin.execute(
                    f"SELECT COUNT(*) AS n FROM {tbl} WHERE tenant_id=%s",  # noqa: S608
                    (tid_a,)).fetchone()["n"]
                self.assertEqual(n, 0, f"{tbl} rows survived deletion")
            self.assertIsNone(admin.execute(
                "SELECT 1 FROM users WHERE email=%s", (email_a,)).fetchone())
            self.assertIsNone(admin.execute(
                "SELECT 1 FROM tenants WHERE id=%s", (tid_a,)).fetchone())
            # the OTHER tenant is untouched
            n = admin.execute(
                "SELECT COUNT(*) AS n FROM transactions WHERE tenant_id=%s",
                (tid_b,)).fetchone()["n"]
            self.assertGreater(n, 0)
            self.assertIsNotNone(admin.execute(
                "SELECT 1 FROM users WHERE email=%s", (email_b,)).fetchone())
        finally:
            admin.close()
        self.assertEqual(cb.get("/api/me").status_code, 200)  # B still alive

    def test_account_delete_requires_totp_when_enrolled(self):
        from oikonome.auth import totp as totp_mod
        c, _, _ = self._signup()
        secret = c.post("/api/totp/enroll",
                        data={"password": PW}).json()["secret"]
        r = c.post("/api/totp/confirm", data={
            "secret": secret, "code": totp_mod.code_now(secret),
            "password": PW})
        self.assertEqual(r.status_code, 200)

        r = c.post("/api/account/delete", data={"password": PW})
        self.assertEqual(r.status_code, 401)     # password alone isn't enough
        # /totp/confirm above BURNED this step's code (`_totp_consume`), so present the next step's, which is inside
        # verify_used's +1 tolerance. Reusing the confirm code here is the
        # replay the fix refuses; test_totp_code_is_single_use_across_doors
        # asserts that directly.
        import time as _t
        r = c.post("/api/account/delete", data={
            "password": PW,
            "totp_code": totp_mod.code_now(secret, at=_t.time() + totp_mod.STEP)})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(c.get("/api/me").status_code, 401)


    def test_totp_code_is_single_use_across_doors(self):
        """A TOTP code is single-use ACCOUNT-WIDE. Login burns its code,
        but a step-up / posture-change door calling the bare `verify()`
        consumes nothing — one observed code then opens the
        credential-changing endpoints repeatedly inside its ~90s window,
        and a code already spent at login opens all of them."""
        from oikonome.auth import totp as totp_mod
        c, _, _ = self._signup()
        # unique per run: `users` is control-plane and the test DB persists
        # across runs, so a fixed address 409s the second time through
        moved = f"moved-{uuid.uuid4().hex[:8]}@example.dev"
        secret = c.post("/api/totp/enroll",
                        data={"password": PW}).json()["secret"]
        code = totp_mod.code_now(secret)
        self.assertEqual(c.post("/api/totp/confirm", data={
            "secret": secret, "code": code, "password": PW}).status_code, 200)

        # the SAME code must not now open a different posture-change door
        r = c.post("/api/email/change", data={
            "password": PW, "new_email": moved,
            "totp_code": code})
        self.assertEqual(r.status_code, 401,
                         "a TOTP code already spent at one door still opened "
                         "another — codes must be single-use account-wide")

        # a fresh code from the next step still works (not a lockout)
        import time as _t
        r = c.post("/api/email/change", data={
            "password": PW, "new_email": moved,
            "totp_code": totp_mod.code_now(secret, at=_t.time() + totp_mod.STEP)})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
