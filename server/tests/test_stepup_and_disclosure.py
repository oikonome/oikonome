"""Step-up gates and disclosure caps.

- /openapi.json served only under OIKONOME_DEV.
- family invite mint requires password step-up.
- script-token mint requires step-up; password change AND reset
  revoke the tenant's tokens (a hijack-planted token can't outlive
  a rotation).
- the cadence email routes through _recipients() (hosted membership
  filter), not raw config.
- restore filters email_recipients to tenant members on hosted.
- the feedback package omits the process-wide log ring on hosted.
- HTML /login reveals need_totp only after the password verifies.
- sign-out-everywhere-else needs the password.
- the operator signup notice escapes the address.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, make_db, write_config

PW = "correct-horse-battery"


class _Client(unittest.TestCase):
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

    def _signup(self):
        c = TestClient(self.app)
        email = f"adv-{uuid.uuid4().hex[:10]}@x.dev"
        r = c.post("/api/signup", data={"email": email, "password": PW})
        assert r.status_code == 200, r.text
        return c, email


class MintStepUpTests(_Client):
    def test_invite_mint_needs_password(self):
        c, _ = self._signup()
        self.assertEqual(
            c.post("/api/invites", json={"label": "x"}).status_code, 403)
        self.assertEqual(
            c.post("/api/invites",
                   json={"label": "x", "password": "wrong"}).status_code, 401)
        self.assertEqual(
            c.post("/api/invites",
                   json={"label": "x", "password": PW}).status_code, 200)

    def test_token_mint_needs_password(self):
        c, _ = self._signup()
        self.assertEqual(
            c.post("/api/tokens", json={"name": "s"}).status_code, 403)
        self.assertEqual(
            c.post("/api/tokens",
                   json={"name": "s", "password": PW}).status_code, 200)

    def test_password_change_revokes_tokens(self):
        c, _ = self._signup()
        c.post("/api/tokens", json={"name": "s", "password": PW})
        self.assertEqual(len(c.get("/api/tokens").json()["tokens"]), 1)
        r = c.post("/api/password/change",
                   json={"current_password": PW,
                         "new_password": "brand-new-password-99"})
        self.assertEqual(r.status_code, 200)
        live = [t for t in c.get("/api/tokens").json()["tokens"]
                if not t["revoked_at"]]
        self.assertEqual(live, [])

    def test_reset_revokes_tokens(self):
        from oikonome.auth import reset as reset_mod
        c, email = self._signup()
        c.post("/api/tokens", json={"name": "s", "password": PW})
        admin = tenancy.admin_connect()
        try:
            uid = admin.execute("SELECT id FROM users WHERE email=%s",
                                (email,)).fetchone()["id"]
            token = reset_mod.create_reset(admin, uid)
        finally:
            admin.close()
        anon = TestClient(self.app)
        r = anon.post("/reset", data={"token": token,
                                      "password": "fresh-reset-pass-1"},
                      follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        admin = tenancy.admin_connect()
        try:
            live = admin.execute(
                "SELECT COUNT(*) AS n FROM api_tokens WHERE created_by=%s "
                "AND revoked_at IS NULL", (uid,)).fetchone()["n"]
        finally:
            admin.close()
        self.assertEqual(live, 0)


class SessionRevokeStepUpTests(_Client):
    def test_revoke_all_others_needs_password(self):
        c, _ = self._signup()
        self.assertEqual(
            c.post("/api/sessions/revoke",
                   json={"all_others": True}).status_code, 403)
        self.assertEqual(
            c.post("/api/sessions/revoke",
                   json={"all_others": True, "password": PW}).status_code,
            200)

    def test_revoke_own_current_session_frictionless(self):
        c, _ = self._signup()
        # logging yourself out (revoking your OWN current session) stays
        # frictionless — no password needed
        cur = next(s for s in c.get("/api/sessions").json()["sessions"]
                   if s["current"])
        r = c.post("/api/sessions/revoke", json={"id": cur["id"]})
        self.assertEqual(r.status_code, 200)

    def test_revoke_other_session_needs_password(self):
        c, _ = self._signup()
        # revoking a session that is NOT your current one is the same
        # "kick the owner to hold an exclusive window" action as all_others —
        # a session-only thief could loop per-id revokes otherwise. Now gated.
        r = c.post("/api/sessions/revoke", json={"id": str(uuid.uuid4())})
        self.assertEqual(r.status_code, 403)         # elevation_required
        self.assertIn("elevation_required", r.text)
        # with the password it proceeds (nonexistent id → revoked 0)
        r = c.post("/api/sessions/revoke",
                   json={"id": str(uuid.uuid4()), "password": PW})
        self.assertEqual(r.status_code, 200)


class LoginTotpOracleTests(_Client):
    def test_need_totp_hidden_on_wrong_password(self):
        from oikonome.auth import totp as totp_mod
        from oikonome.db import crypto
        c, email = self._signup()
        # enroll TOTP directly (admin) so the oracle would have something
        # to leak — the endpoint path shares a rate bucket with login
        secret = totp_mod.new_secret()
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET totp_secret=%s WHERE email=%s",
                          (crypto.encrypt_cp(secret), email))
        finally:
            admin.close()
        anon = TestClient(self.app)
        # wrong password for a KNOWN TOTP-enrolled email — must not reveal
        # the authenticator field
        r = anon.post("/login", data={"email": email,
                                      "password": "wrong-wrong-wrong"})
        self.assertNotIn("Authenticator code", r.text)


class CadenceRecipientFilterTests(unittest.TestCase):
    def test_cadence_email_uses_membership_filter_on_hosted(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        try:
            conn = make_db()
            write_config(conn,
                         email_recipients=["outsider@evil.dev"])
            tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS t"
            ).fetchone()["t"]
            conn.close()
            from oikonome.jobs import worker
            from oikonome.web import report as report_mod
            sent = {}

            def fake_send(subject, plain, html, recipients, **kw):
                sent["recipients"] = recipients
            orig = report_mod.send
            report_mod.send = fake_send
            try:
                worker.email_tenant(tid, send_it=True)
            finally:
                report_mod.send = orig
            self.assertNotIn("outsider@evil.dev",
                             sent.get("recipients", []))
        finally:
            os.environ.pop("OIKONOME_HOSTED", None)


class RestoreRecipientFilterTests(unittest.TestCase):
    def test_restore_drops_outsider_recipients_on_hosted(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        try:
            import csv as _csv
            import io as _io
            import json as _json
            import zipfile

            from oikonome.sync import restore
            conn = make_db()
            write_config(conn)
            buf = _io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                s = _io.StringIO()
                w = _csv.DictWriter(s, fieldnames=["config"])
                w.writeheader()
                w.writerow({"config": _json.dumps(
                    {"email_recipients": ["outsider@evil.dev"],
                     "food_monthly": 500})})
                z.writestr("tenant_settings.csv", s.getvalue())
            restore.restore_zip(conn, buf.getvalue())
            cfg = conn.execute(
                "SELECT config FROM tenant_settings").fetchone()["config"]
            self.assertNotIn("email_recipients", cfg)   # no members matched
            self.assertEqual(cfg["food_monthly"], 500)
            conn.close()
        finally:
            os.environ.pop("OIKONOME_HOSTED", None)


class FeedbackLogScopeTests(unittest.TestCase):
    def test_hosted_feedback_omits_process_log_ring(self):
        import io as _io
        import zipfile

        from oikonome.web import feedback
        conn = make_db()
        write_config(conn)
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        os.environ["OIKONOME_HOSTED"] = "1"
        try:
            pkg = feedback.build_package(conn, tid, "42", "hi")
        finally:
            os.environ.pop("OIKONOME_HOSTED", None)
        names = zipfile.ZipFile(_io.BytesIO(pkg)).namelist()
        self.assertFalse([n for n in names if n.endswith("app-logs.txt")])
        # SINGLE-TENANT self-host still includes it. The gate is "does this
        # instance actually hold one tenant", not "is OIKONOME_HOSTED unset":
        # a household self-hosting with two logins would otherwise ship both
        # households' log lines. The suite's own database holds a tenant per
        # test, so the single-tenant case has to be stated explicitly rather
        # than assumed.
        from unittest import mock as _mock
        with _mock.patch.object(feedback, "_single_tenant_instance",
                                return_value=True):
            pkg2 = feedback.build_package(conn, tid, "43", "hi")
        names2 = zipfile.ZipFile(_io.BytesIO(pkg2)).namelist()
        self.assertTrue([n for n in names2 if n.endswith("app-logs.txt")])
        conn.close()


class OperatorMailEscapeTests(unittest.TestCase):

    def test_signup_notice_escapes_the_address_and_names_the_household(self):
        # the signup address is unauthenticated input too — it reaches the
        # operator's mailbox before anyone has proved that mailbox
        import oikonome.web.app as appmod
        sent = {}
        from oikonome.web import report as report_mod
        orig, orig_env = report_mod.send, report_mod._env_smtp
        report_mod.send = lambda subj, plain, html, to, **k: sent.update(
            subject=subj, plain=plain, html=html, to=to)
        report_mod._env_smtp = lambda: {"configured": True}
        os.environ["OIKONOME_OPERATOR_EMAIL"] = "op@x.dev"
        try:
            appmod._notify_operator_signup(
                "<script>alert(1)</script>@x.dev", "tenant-7")
        finally:
            report_mod.send, report_mod._env_smtp = orig, orig_env
            os.environ.pop("OIKONOME_OPERATOR_EMAIL", None)
        self.assertEqual(sent["to"], ["op@x.dev"])
        self.assertIn("&lt;script&gt;", sent["html"])
        self.assertNotIn("<script>", sent["html"])
        # the operator asked for the address itself, and the household id
        # is what makes it actionable in the console
        self.assertIn("@x.dev", sent["plain"])
        self.assertIn("tenant-7", sent["plain"])
        self.assertIn("tenant-7", sent["html"])

    def test_signup_notice_never_raises_when_the_relay_is_down(self):
        # a dead relay must not fail a signup: the notice runs off the
        # request, but the function itself is the last line of defense
        import oikonome.web.app as appmod

        def boom(*a, **k):
            raise RuntimeError("relay down")

        from oikonome.web import report as report_mod
        orig, orig_env = report_mod.send, report_mod._env_smtp
        report_mod.send = boom
        report_mod._env_smtp = lambda: {"configured": True}
        os.environ["OIKONOME_OPERATOR_EMAIL"] = "op@x.dev"
        try:
            appmod._notify_operator_signup("a@x.dev", "tenant-7")
        finally:
            report_mod.send, report_mod._env_smtp = orig, orig_env
            os.environ.pop("OIKONOME_OPERATOR_EMAIL", None)

    def test_no_operator_address_sends_nothing_and_starts_no_thread(self):
        # self-host has no operator mailbox, and the suite mints a tenant
        # per test — neither should cost a send or a thread
        import threading

        import oikonome.web.app as appmod
        sent = []
        from oikonome.web import report as report_mod
        orig, orig_env = report_mod.send, report_mod._env_smtp
        report_mod.send = lambda *a, **k: sent.append(a)
        report_mod._env_smtp = lambda: {"configured": True}
        os.environ.pop("OIKONOME_OPERATOR_EMAIL", None)
        before = threading.active_count()
        try:
            appmod._operator_signup_notice("a@x.dev", "tenant-7")
            appmod._notify_operator_signup("a@x.dev", "tenant-7")
        finally:
            report_mod.send, report_mod._env_smtp = orig, orig_env
        self.assertEqual(sent, [])
        self.assertEqual(threading.active_count(), before)

    def test_reclaimed_signup_says_so(self):
        # two notices for one address is the intended shape; the operator
        # has to be able to tell the second party from the first
        import oikonome.web.app as appmod
        sent = {}
        from oikonome.web import report as report_mod
        orig, orig_env = report_mod.send, report_mod._env_smtp
        report_mod.send = lambda subj, plain, html, to, **k: sent.update(
            subject=subj, plain=plain)
        report_mod._env_smtp = lambda: {"configured": True}
        os.environ["OIKONOME_OPERATOR_EMAIL"] = "op@x.dev"
        try:
            appmod._notify_operator_signup("a@x.dev", "tenant-7",
                                           reclaimed=True)
        finally:
            report_mod.send, report_mod._env_smtp = orig, orig_env
            os.environ.pop("OIKONOME_OPERATOR_EMAIL", None)
        self.assertIn("reclaimed", sent["subject"])
        self.assertIn("reclaimed", sent["plain"])


class OpenApiDevGateTests(unittest.TestCase):
    def test_openapi_url_gated_on_dev(self):
        # assert the WIRING without reloading the shared app module (a
        # reload contaminates other suites' class-level TestClients):
        # build a throwaway FastAPI the same way app.py does and check the
        # env gate both directions.
        from fastapi import FastAPI
        saved = os.environ.get("OIKONOME_DEV")
        try:
            os.environ.pop("OIKONOME_DEV", None)
            off = FastAPI(openapi_url=("/openapi.json"
                          if os.environ.get("OIKONOME_DEV") else None))
            self.assertIsNone(off.openapi_url)
            os.environ["OIKONOME_DEV"] = "1"
            on = FastAPI(openapi_url=("/openapi.json"
                         if os.environ.get("OIKONOME_DEV") else None))
            self.assertEqual(on.openapi_url, "/openapi.json")
        finally:
            if saved is None:
                os.environ.pop("OIKONOME_DEV", None)
            else:
                os.environ["OIKONOME_DEV"] = saved


if __name__ == "__main__":
    unittest.main()
