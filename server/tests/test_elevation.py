"""Session elevation ("sudo mode"): prove identity ONCE, then a 10-minute
window on THAT session in which every re-auth door just works.

The doors still exist for the same reason step-up did: a session thief
holds the cookie, not the password / passkey / authenticator, and must not
be able to change the account's security posture. Elevation moves the
proof from "every request" to "once per window", stored on the session
row — so the thief still cannot elevate, and cannot borrow an elevation
from the owner's OTHER session either. Two doors (account delete, the
download-everything export) demand a FRESH elevation (60 s) because a
ten-minute-old proof is too stale for an irreversible act.

Legacy: older store app builds still send passwords in the body, and that
path must keep working unchanged.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import (clear_totp_burn, _ensure_db, handler_source,
                   mutating_routes, route_key)

PW = "correct-horse-battery"
GUARDED = "/api/totp/recovery-regenerate"      # a plain re-auth door


def _fresh_code(secret):
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)


def _set_elevated_age(email: str, seconds: int) -> None:
    """Age every session of this user's elevation by `seconds` — the
    clock the server consults is the database's, so the window is
    exercised by moving the stored stamp, not by patching time."""
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "UPDATE sessions SET elevated_at = now() - make_interval(secs => %s) "
            "WHERE user_id = (SELECT id FROM users WHERE email = %s)",
            (seconds, email))
    finally:
        admin.close()


def _age_session(email: str, seconds: int) -> None:
    """Make the session look signed-in `seconds` ago (the device-mint
    door waives re-auth inside its first minutes)."""
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "UPDATE sessions SET created_at = now() - make_interval(secs => %s) "
            "WHERE user_id = (SELECT id FROM users WHERE email = %s)",
            (seconds, email))
    finally:
        admin.close()


def _calls_elevation(route) -> bool:
    """Does this route's handler call _require_elevation — directly, or
    through ONE module-level helper (the device mint dispatches to
    _device_mint_session; a handler-only grep would report it open)?"""
    import inspect
    import re
    import sys
    src = handler_source(route)
    if "_require_elevation(" in src:
        return True
    module = sys.modules.get(route.endpoint.__module__)
    for name in set(re.findall(r"\b(_[A-Za-z0-9_]+)\s*\(", src)):
        fn = getattr(module, name, None)
        if fn is None or not callable(fn):
            continue
        try:
            if "_require_elevation(" in inspect.getsource(fn):
                return True
        except (OSError, TypeError):
            continue
    return False


def _add_viewer(email: str) -> str:
    """A second member in this owner's household (no login needed)."""
    from oikonome.auth import passwords
    admin = tenancy.admin_connect()
    try:
        return str(admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role, "
            "verified_at) SELECT tenant_id, %s, %s, 'viewer', now() "
            "FROM users WHERE email = %s RETURNING id",
            (f"v-{uuid.uuid4().hex[:8]}@example.dev",
             passwords.hash_password(PW), email)).fetchone()["id"])
    finally:
        admin.close()


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _signup(self, prefix="elev"):
        client = TestClient(self.app)
        email = f"{prefix}-{uuid.uuid4().hex[:10]}@example.dev"
        r = client.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return client, email

    def _login(self, email):
        client = TestClient(self.app)
        r = client.post("/api/login", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return client


class ElevationWindowTests(_Base):

    def test_unelevated_call_to_a_guarded_route_is_refused(self):
        c, _ = self._signup()
        r = c.post(GUARDED)
        self.assertEqual(r.status_code, 403, r.text)
        body = r.json()
        self.assertEqual(body["detail"]["error"], "elevation_required")
        self.assertFalse(body["detail"]["fresh"])
        # the SPA's interceptor keys on a top-level `error` too
        self.assertEqual(body.get("error"), "elevation_required")

    def test_status_reports_unelevated_password_account(self):
        c, _ = self._signup()
        r = c.get("/api/auth/elevation")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"elevated": False, "until": None,
                                    "methods": ["password"], "totp": False})

    def test_elevate_with_password_then_the_same_call_succeeds(self):
        c, _ = self._signup()
        r = c.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        st = r.json()
        self.assertTrue(st["elevated"])
        self.assertTrue(st["until"])
        self.assertEqual(st["methods"], ["password"])
        self.assertFalse(st["totp"])
        self.assertTrue(c.get("/api/auth/elevation").json()["elevated"])
        r = c.post(GUARDED)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["recovery_codes"]), 8)

    def test_wrong_password_does_not_elevate(self):
        c, _ = self._signup()
        r = c.post("/api/auth/elevate", json={"password": "not-it-at-all"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])
        self.assertEqual(c.post(GUARDED).status_code, 403)

    def test_empty_proof_does_not_elevate(self):
        c, _ = self._signup()
        r = c.post("/api/auth/elevate", json={})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])

    def test_elevation_expires_after_ten_minutes(self):
        c, email = self._signup()
        self.assertEqual(
            c.post("/api/auth/elevate", json={"password": PW}).status_code,
            200)
        _set_elevated_age(email, 9 * 60)
        self.assertEqual(c.post(GUARDED).status_code, 200)
        _set_elevated_age(email, 11 * 60)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])
        r = c.post(GUARDED)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"]["error"], "elevation_required")

    def test_fresh_door_refuses_a_five_minute_old_elevation(self):
        c, email = self._signup()
        c.post("/api/auth/elevate", json={"password": PW})
        _set_elevated_age(email, 5 * 60)
        # an ordinary door is still open at five minutes...
        self.assertEqual(c.post(GUARDED).status_code, 200)
        # ...the irreversible one is not
        r = c.post("/api/account/delete")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"],
                         {"error": "elevation_required", "fresh": True})
        # nothing was deleted
        self.assertEqual(c.get("/api/me").status_code, 200)

    def test_fresh_door_accepts_a_ten_second_old_elevation(self):
        c, email = self._signup()
        c.post("/api/auth/elevate", json={"password": PW})
        _set_elevated_age(email, 10)
        r = c.post("/api/account/delete")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.get("/api/me").status_code, 401)

    def test_elevation_is_per_session(self):
        a, email = self._signup()
        b = self._login(email)
        self.assertEqual(
            a.post("/api/auth/elevate", json={"password": PW}).status_code,
            200)
        # same user, different cookie: the second session proved nothing
        self.assertFalse(b.get("/api/auth/elevation").json()["elevated"])
        r = b.post(GUARDED)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(a.post(GUARDED).status_code, 200)

    def test_legacy_password_in_body_is_still_accepted(self):
        c, _ = self._signup()
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])
        r = c.post(GUARDED, data={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        r = c.post("/api/tokens", json={"name": "cron", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        # a legacy call proves the password but does NOT open the window
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])
        r = c.post("/api/account/delete", data={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_legacy_wrong_password_is_still_refused(self):
        c, _ = self._signup()
        r = c.post(GUARDED, data={"password": "nope-nope-nope"})
        self.assertEqual(r.status_code, 401, r.text)
        r = c.post("/api/account/delete", data={"password": "nope-nope-nope"})
        self.assertEqual(r.status_code, 401, r.text)

    def test_elevate_is_rate_limited_like_login(self):
        c, _ = self._signup()
        for _ in range(10):
            r = c.post("/api/auth/elevate", json={"password": "wrong-x-y-z"})
            self.assertEqual(r.status_code, 401, r.text)
        r = c.post("/api/auth/elevate", json={"password": "wrong-x-y-z"})
        self.assertEqual(r.status_code, 429, r.text)

    def test_every_guarded_door_refuses_an_unelevated_session(self):
        """The whole set, not one representative — and the set is read
        out of the SOURCE: every route whose handler calls
        _require_elevation must appear in the probes below, so a door
        added later fails this test until someone proves it refuses.
        Probed as a hosted owner with TOTP (so the passkey doors are open
        and password_change owes a second proof) plus a viewer for the
        member-only door."""
        from oikonome.auth import totp as totp_mod
        c, email = self._signup("doors")
        secret = c.post("/api/totp/enroll",
                        data={"password": PW}).json()["secret"]
        r = c.post("/api/totp/confirm", data={
            "secret": secret, "code": totp_mod.code_now(secret),
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        viewer_id = _add_viewer(email)
        _age_session(email, 10 * 60)              # past the device-mint waiver
        # a second factor on file, so totp/disable gets past its
        # last-factor refusal and reaches the elevation check
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) SELECT id, %s, %s, 0 FROM users WHERE email=%s",
                (f"cred{uuid.uuid4().hex}", "cHVibGljLWtleQ", email))
        finally:
            admin.close()
        os.environ["OIKONOME_HOSTED"] = "1"
        self.addCleanup(os.environ.pop, "OIKONOME_HOSTED", None)
        v = TestClient(self.app)
        admin = tenancy.admin_connect()
        try:
            v_email = admin.execute("SELECT email FROM users WHERE id=%s",
                                    (viewer_id,)).fetchone()["email"]
        finally:
            admin.close()
        r = v.post("/login", data={"email": v_email, "password": PW},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        garbage = {"data": {"passphrase": "a-long-passphrase"},
                   "files": {"file": ("x.oikx", b"garbage")}}
        probes = {
            # route key: (client, method, path, kwargs, fresh?)
            "POST /api/totp/enroll": (c, "post", "/api/totp/enroll", {}, False),
            "POST /api/totp/confirm": (
                c, "post", "/api/totp/confirm",
                {"data": {"secret": secret, "code": "000000"}}, False),
            "POST /api/totp/recovery-regenerate": (
                c, "post", "/api/totp/recovery-regenerate", {}, False),
            "POST /api/totp/disable": (
                c, "post", "/api/totp/disable",
                {"data": {"code": _fresh_code(secret)}}, False),
            "POST /api/password/change": (
                c, "post", "/api/password/change",
                {"json": {"current_password": PW,
                          "new_password": "brand-new-passphrase"}}, False),
            "POST /api/email/change": (
                c, "post", "/api/email/change",
                {"data": {"new_email": "new@example.dev"}}, False),
            "POST /api/account/leave": (
                v, "post", "/api/account/leave", {}, False),
            "POST /api/account/delete": (
                c, "post", "/api/account/delete", {}, True),
            "POST /api/invites": (
                c, "post", "/api/invites", {"json": {"label": "x"}}, False),
            "POST /api/users/{user_id}/role": (
                c, "post", f"/api/users/{viewer_id}/role",
                {"json": {"role": "member"}}, False),
            "DELETE /api/users/{user_id}": (
                c, "request", f"/api/users/{viewer_id}", {"json": {}}, False),
            "POST /api/passkeys/options": (
                c, "post", "/api/passkeys/options", {"json": {}}, False),
            "POST /api/passkeys": (
                c, "post", "/api/passkeys",
                {"json": {"challenge_id": "x", "credential": {}}}, False),
            "DELETE /api/passkeys/{pk_id}": (
                c, "request", f"/api/passkeys/{uuid.uuid4()}",
                {"json": {}}, False),
            "POST /api/sessions/revoke": (
                c, "post", "/api/sessions/revoke",
                {"json": {"id": str(uuid.uuid4())}}, False),
            "POST /api/devices": (
                c, "post", "/api/devices",
                {"json": {"device_name": "x", "platform": "android"}}, False),
            "POST /api/devices/revoke": (
                c, "post", "/api/devices/revoke",
                {"json": {"id": str(uuid.uuid4())}}, False),
            "POST /api/tokens": (
                c, "post", "/api/tokens", {"json": {"name": "x"}}, False),
            "POST /api/tokens/revoke": (
                c, "post", "/api/tokens/revoke",
                {"json": {"id": str(uuid.uuid4())}}, False),
            "POST /api/support-access": (
                c, "post", "/api/support-access", {"json": {"hours": 1}},
                False),
            "POST /api/connections/export": (
                c, "post", "/api/connections/export",
                {"json": {"passphrase": "a-long-passphrase"}}, True),
            "POST /api/connections/import": (
                c, "post", "/api/connections/import", garbage, True),
            "POST /api/export/token": (
                c, "post", "/api/export/token", {"json": {"kind": "zip"}},
                True),
            # the continuity packet by mail is the whole shape of the
            # household's money leaving the instance — FRESH, like the
            # download it mirrors
            "POST /api/continuity/email": (
                c, "post", "/api/continuity/email",
                {"json": {"to": "sam@example.dev"}}, True),
            # a webhook points the ledger at a URL: creating one, moving
            # one, and re-keying one step up; toggling and deleting do not
            "POST /api/webhooks": (
                c, "post", "/api/webhooks",
                {"json": {"url": "https://receiver.example/hook",
                          "events": ["test.ping"]}}, False),
            "POST /api/webhooks/{hook_id}": (
                c, "post", f"/api/webhooks/{uuid.uuid4()}",
                {"json": {"url": "https://receiver.example/hook"}}, False),
            "POST /api/webhooks/{hook_id}/rotate": (
                c, "post", f"/api/webhooks/{uuid.uuid4()}/rotate",
                {"json": {}}, False),
        }
        guarded = sorted(route_key(r) for r in mutating_routes(self.app)
                         if _calls_elevation(r))
        self.assertEqual(guarded, sorted(probes),
                         "a door calls _require_elevation but is not probed "
                         "here (or a probe names a door that no longer "
                         "checks) — add it, with whether it wants FRESH")
        # the all_others branch of sessions/revoke shares its route key
        probes["POST /api/sessions/revoke (all_others)"] = (
            c, "post", "/api/sessions/revoke", {"json": {"all_others": True}},
            False)
        for key, (client, method, path, kw, fresh) in probes.items():
            if method == "request":
                r = client.request("DELETE", path, **kw)
            else:
                r = getattr(client, method)(path, **kw)
            self.assertEqual(r.status_code, 403, f"{key}: {r.text}")
            self.assertEqual(r.json()["detail"],
                             {"error": "elevation_required", "fresh": fresh},
                             key)
        self.assertEqual(c.get("/api/me").status_code, 200)
        self.assertEqual(v.get("/api/me").status_code, 200)

    def test_export_token_wants_a_fresh_elevation(self):
        """The data downloads mint a one-shot ticket here; every dump
        walks out through it, so it is the export door's equal."""
        c, email = self._signup("exp")
        r = c.post("/api/export/token", json={"kind": "zip"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"],
                         {"error": "elevation_required", "fresh": True})
        c.post("/api/auth/elevate", json={"password": PW})
        _set_elevated_age(email, 5 * 60)
        self.assertEqual(
            c.post("/api/export/token", json={"kind": "zip"}).status_code, 403)
        _set_elevated_age(email, 10)
        r = c.post("/api/export/token", json={"kind": "zip"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.get(r.json()["url"]).status_code, 200)
        # legacy body still mints
        _set_elevated_age(email, 20 * 60)
        r = c.post("/api/export/token", json={"kind": "zip", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_connections_import_wants_a_fresh_elevation(self):
        """The export door's declared twin: importing PLANTS credentials."""
        c, email = self._signup("imp")
        kw = {"data": {"passphrase": "a-long-passphrase"},
              "files": {"file": ("x.oikx", b"garbage")}}
        c.post("/api/auth/elevate", json={"password": PW})
        _set_elevated_age(email, 5 * 60)
        r = c.post("/api/connections/import", **kw)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"],
                         {"error": "elevation_required", "fresh": True})
        _set_elevated_age(email, 10)
        # past the guard: the garbage bundle is the refusal now
        self.assertEqual(c.post("/api/connections/import", **kw).status_code,
                         400)

    def test_bare_recovery_code_does_not_elevate_a_password_account(self):
        c, _ = self._signup("rc")
        r = c.post("/api/auth/elevate", json={"recovery_code": "abcd-efgh-ijkl"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])


class TotpAccountTests(_Base):

    def _enroll_totp(self, c):
        from oikonome.auth import totp as totp_mod
        secret = c.post("/api/totp/enroll",
                        data={"password": PW}).json()["secret"]
        r = c.post("/api/totp/confirm", data={
            "secret": secret, "code": totp_mod.code_now(secret),
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return secret

    def test_totp_account_needs_the_code_at_elevate_time(self):
        c, _ = self._signup("totp")
        secret = self._enroll_totp(c)
        st = c.get("/api/auth/elevation").json()
        self.assertEqual(st["methods"], ["password"])
        self.assertTrue(st["totp"])
        r = c.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("totp_required", r.text)
        r = c.post("/api/auth/elevate", json={"password": PW,
                                              "totp_code": "000000"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])
        r = c.post("/api/auth/elevate", json={
            "password": PW, "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["elevated"])
        # inside the window no door asks for a live code again
        self.assertEqual(c.post(GUARDED).status_code, 200)
        r = c.post("/api/tokens", json={"name": "cron"})
        self.assertEqual(r.status_code, 200, r.text)
        # the elevate code was burnt like any other: it cannot be replayed
        # at a legacy door
        r = c.post(GUARDED, data={"password": PW,
                                  "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)

    def test_totp_disable_always_wants_the_live_code(self):
        """Turning 2FA OFF is a downgrade: the window covers the password
        half, but the live code from the current authenticator is owed
        every time — elevated or not."""
        c, _ = self._signup("totp")
        secret = self._enroll_totp(c)
        r = c.post("/api/auth/elevate", json={
            "password": PW, "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        r = c.post("/api/totp/disable")
        self.assertIn(r.status_code, (401, 422), r.text)
        r = c.post("/api/totp/disable", data={"code": "000000"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertTrue(c.get("/api/auth/elevation").json()["totp"])
        r = c.post("/api/totp/disable", data={"code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["totp"])

    def test_recovery_code_stands_in_for_the_totp_code_beside_the_password(
            self):
        """As at login: password + recovery code is a valid pair on a TOTP
        account; a recovery code ALONE is not — it is a stand-in for the
        second factor, never for the first."""
        c, _ = self._signup("totp")
        secret = self._enroll_totp(c)
        codes = c.post("/api/totp/recovery-regenerate", data={
            "password": PW, "totp_code": _fresh_code(secret)}
            ).json()["recovery_codes"]
        r = c.post("/api/auth/elevate", json={"recovery_code": codes[0]})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])
        r = c.post("/api/auth/elevate",
                   json={"password": PW, "recovery_code": "abcd-efgh-ijkl"})
        self.assertEqual(r.status_code, 401, r.text)
        # the refused bare attempt did not burn the code
        r = c.post("/api/auth/elevate",
                   json={"password": PW, "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["elevated"])
        # ...and now it is spent
        r = c.post("/api/auth/elevate",
                   json={"password": PW, "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 401, r.text)

    def test_wrong_totp_code_at_elevate_counts_as_an_auth_failure(self):
        c, _ = self._signup("totp")
        self._enroll_totp(c)
        with mock.patch("oikonome.web.security.record_auth_failure") as raf:
            r = c.post("/api/auth/elevate",
                       json={"password": PW, "totp_code": "000000"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertTrue(raf.called)
        self.assertEqual(raf.call_args.kwargs.get("route"), "elevate")

    def test_legacy_bodies_on_a_totp_account_still_get_the_old_401s(self):
        """Older app builds send the password and, on a TOTP account,
        prompt for the code when the server says 401 totp_required — that
        prompt must keep firing; a 403 would show them an error instead."""
        c, _ = self._signup("totp")
        secret = self._enroll_totp(c)
        r = c.post("/api/totp/enroll", data={"password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("current one-time code", r.text)
        r = c.post("/api/totp/confirm", data={
            "secret": secret, "code": "000000", "password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("totp_required", r.text)
        # password_change: the old app's body carries the code key (empty
        # until the prompt fills it); a body WITHOUT the key is the new
        # clients' and reads the window instead
        r = c.post("/api/password/change", json={
            "current_password": PW, "new_password": "brand-new-passphrase",
            "totp_code": ""})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()["detail"], "totp_required")
        r = c.post("/api/password/change", json={
            "current_password": PW, "new_password": "brand-new-passphrase"})
        self.assertEqual(r.status_code, 403, r.text)

    def test_totp_disable_unelevated_still_needs_password_and_code(self):
        c, _ = self._signup("totp")
        secret = self._enroll_totp(c)
        r = c.post("/api/totp/disable", data={"code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 403, r.text)
        r = c.post("/api/totp/disable",
                   data={"code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)


class PasskeyOnlyTests(_Base):

    def setUp(self):
        super().setUp()
        os.environ["OIKONOME_HOSTED"] = "1"

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def _passkey_only_user(self, with_codes=True, with_totp=False):
        from oikonome.auth import passwords, recovery
        email = f"pk-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, email)
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (tid, email, passwords.hash_password(PW))).fetchone()["id"]
        finally:
            admin.close()
        client = TestClient(self.app)
        r = client.post("/login", data={"email": email, "password": PW},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.email = email
        self.secret = self._add_totp(client) if with_totp else None
        cred_id = f"cred{uuid.uuid4().hex}"
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, %s, 0)",
                (uid, cred_id, "cHVibGljLWtleQ"))
            codes = recovery.issue(admin, uid) if with_codes else []
        finally:
            admin.close()
        return client, cred_id, codes

    def _ticket(self, client, cred_id):
        o = client.post("/api/stepup/passkey/options", json={})
        self.assertEqual(o.status_code, 200, o.text)

        class _FakeAuth:
            new_sign_count = 3
        with mock.patch(
                "oikonome.auth.passkeys.verify_authentication_response",
                return_value=_FakeAuth()):
            r = client.post("/api/stepup/passkey", json={
                "challenge_id": o.json()["challenge_id"],
                "credential": {"id": cred_id}})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["stepup_ticket"]

    def test_passkey_account_elevates_via_ticket_not_password(self):
        c, cred_id, _ = self._passkey_only_user(with_codes=False)
        st = c.get("/api/auth/elevation").json()
        self.assertEqual(st["methods"], ["passkey"])
        self.assertFalse(st["elevated"])
        # the password is not the factor this account signs in with
        r = c.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("recovery_required", r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])
        r = c.post("/api/auth/elevate",
                   json={"stepup_ticket": self._ticket(c, cred_id)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["elevated"])
        self.assertEqual(c.post(GUARDED).status_code, 200)

    def _add_totp(self, client):
        from oikonome.auth import totp as totp_mod
        secret = client.post("/api/totp/enroll",
                             data={"password": PW}).json()["secret"]
        r = client.post("/api/totp/confirm", data={
            "secret": secret, "code": totp_mod.code_now(secret),
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return secret

    def test_totp_and_passkey_account_is_offered_both_methods(self):
        """A device without the key falls back to password + code rather
        than burning a recovery code; the passkey still comes first."""
        c, cred_id, _ = self._passkey_only_user(with_codes=False,
                                                with_totp=True)
        secret = self.secret
        st = c.get("/api/auth/elevation").json()
        self.assertEqual(st["methods"], ["passkey", "password"])
        self.assertTrue(st["totp"])
        r = c.post("/api/auth/elevate", json={
            "password": PW, "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        _set_elevated_age(self.email, 20 * 60)
        r = c.post("/api/auth/elevate",
                   json={"stepup_ticket": self._ticket(c, cred_id)})
        self.assertEqual(r.status_code, 200, r.text)
        # elevated by the passkey, but turning TOTP off still owes a code
        r = c.post("/api/totp/disable", data={"code": "000000"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertTrue(c.get("/api/auth/elevation").json()["totp"])
        r = c.post("/api/totp/disable", data={"code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)

    def test_stepup_ticket_is_single_use(self):
        c, cred_id, _ = self._passkey_only_user(with_codes=False)
        t = self._ticket(c, cred_id)
        self.assertEqual(
            c.post("/api/auth/elevate", json={"stepup_ticket": t}).status_code,
            200)
        r = c.post("/api/auth/elevate", json={"stepup_ticket": t})
        self.assertEqual(r.status_code, 401, r.text)

    def test_forged_ticket_does_not_elevate(self):
        c, _, _ = self._passkey_only_user(with_codes=False)
        r = c.post("/api/auth/elevate",
                   json={"stepup_ticket": "pkstep-" + "A" * 32})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])

    def test_recovery_code_is_the_lost_key_fallback(self):
        c, _, codes = self._passkey_only_user()
        r = c.post("/api/auth/elevate", json={"recovery_code": "wrong-code-x"})
        self.assertEqual(r.status_code, 401, r.text)
        r = c.post("/api/auth/elevate", json={"recovery_code": codes[0]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["elevated"])
        self.assertEqual(c.post(GUARDED).status_code, 200)


class NonCookiePrincipalTests(_Base):

    def test_script_token_cannot_elevate(self):
        c, _ = self._signup("tok")
        r = c.post("/api/tokens", json={"name": "cron", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        bare = TestClient(self.app)
        hdr = {"Authorization": f"Bearer {r.json()['token']}"}
        self.assertEqual(
            bare.post("/api/auth/elevate", headers=hdr,
                      json={"password": PW}).status_code, 403)
        self.assertEqual(
            bare.get("/api/auth/elevation", headers=hdr).status_code, 403)

    def test_device_token_elevates_its_own_device_only(self):
        """The mobile app is a device token, not a cookie — it needs the
        same window, on ITS row: the phone's elevation must not leak to
        the browser session that minted it, nor the reverse."""
        c, _ = self._signup("dev")
        r = c.post("/api/devices", json={"device_name": "Pixel",
                                         "platform": "android"})
        self.assertEqual(r.status_code, 200, r.text)
        bare = TestClient(self.app)
        hdr = {"Authorization": f"Bearer {r.json()['token']}"}
        self.assertFalse(
            bare.get("/api/auth/elevation", headers=hdr).json()["elevated"])
        self.assertEqual(
            bare.post(GUARDED, headers=hdr).status_code, 403)
        r = bare.post("/api/auth/elevate", headers=hdr,
                      json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(bare.post(GUARDED, headers=hdr).status_code, 200)
        # the browser that minted the phone proved nothing
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])
        self.assertEqual(c.post(GUARDED).status_code, 403)


if __name__ == "__main__":
    unittest.main()
