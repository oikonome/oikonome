"""Auth and tenant-isolation invariants, plus credential handling: the
self-host signup gate, encrypted aggregator secrets, export scrubbing,
single-winner invite claims, session eviction, and the SSRF policy."""

import os
import threading
import unittest
import uuid
import zipfile
import io

import psycopg
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from oikonome.auth import invites
from oikonome.db import tenancy
from oikonome.engine import budget

from .util import clear_totp_burn, _ensure_db
from .export_ticket import export_get


def _fresh_code(secret):
    """A code that is accepted now. Step-up doors burn the code, so
    these tests — which are about the step-up GATE,
    not replay — clear the burn first. See util.clear_totp_burn.
    """
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)

PW = "correct-horse-battery"


def _control():
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


class AuthIsolationTests(unittest.TestCase):
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

    def _signup(self):
        c = TestClient(self.app)
        email = f"iso-{uuid.uuid4().hex[:10]}@example.dev"
        r = c.post("/api/signup", data={"email": email, "password": PW})
        assert r.status_code == 200, r.text
        return c, email, c.get("/api/me").json()["tenant_id"]

    # ---- self-host signup gate ---------------------------------------------
    def test_signup_gated_on_a_claimed_selfhost_instance(self):
        # ensure at least one user exists on the shared control plane
        self._signup()
        # simulate a real self-host box: not hosted, not dev
        self.appmod.DEV_MODE = False
        try:
            c = TestClient(self.app)
            r = c.post("/api/signup", data={
                "email": f"attacker-{uuid.uuid4().hex[:8]}@evil.dev",
                "password": PW})
            self.assertEqual(r.status_code, 403, r.text)
        finally:
            self.appmod.DEV_MODE = True
        # dev/hosted path still open
        c2 = TestClient(self.app)
        r2 = c2.post("/api/signup", data={
            "email": f"ok-{uuid.uuid4().hex[:8]}@example.dev", "password": PW})
        self.assertEqual(r2.status_code, 200, r2.text)

    # ---- Plaid secret encrypt-on-write, decrypt-on-read --------------------
    def test_plaid_secret_roundtrips_with_master_key(self):
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        try:
            from oikonome.web import setup as setup_mod
            from oikonome.sync import plaid
            r = setup_mod.bootstrap(
                f"h1-{uuid.uuid4().hex[:8]}@example.dev", PW,
                plaid_client_id="cid-abc", plaid_secret="sec-xyz",
                plaid_env="sandbox")
            conn = tenancy.tenant_connect(r["tenant_id"])
            try:
                stored = budget.load_config(conn)["plaid_secret"]
                # stored encrypted at rest…
                self.assertTrue(stored.startswith("enc:v1:"))
                self.assertNotIn("sec-xyz", stored)
                # …and credentials() hands Plaid the DECRYPTED secret
                cid, secret, _url = plaid.credentials(conn)
                self.assertEqual(cid, "cid-abc")
                self.assertEqual(secret, "sec-xyz")
            finally:
                conn.close()
        finally:
            os.environ.pop("OIKONOME_MASTER_KEY", None)

    # ---- CSV export never emits *_api_key ----------------------------------
    def test_export_scrubs_api_keys(self):
        c, _, tid = self._signup()
        conn = tenancy.tenant_connect(tid)
        try:
            cfg = budget.load_config(conn)
            cfg["llm_api_key"] = "llmsecret123"
            cfg["mx_api_key"] = "mxsecret456"
            cfg["llm_url"] = "http://127.0.0.1:11434"
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        body = export_get(c, "/export").content
        z = zipfile.ZipFile(io.BytesIO(body))
        settings_csv = z.read("tenant_settings.csv").decode()
        self.assertNotIn("llmsecret123", settings_csv)
        self.assertNotIn("mxsecret456", settings_csv)
        self.assertNotIn("api_key", settings_csv)

    # ---- one-time invite survives a concurrent claim -----------------------
    def test_concurrent_invite_claim_single_winner(self):
        _, _, tid = self._signup()
        cc = _control()
        try:
            owner = cc.execute("SELECT id FROM users WHERE tenant_id=%s LIMIT 1",
                               (tid,)).fetchone()["id"]
            inv = invites.create(cc, tid, owner, role="viewer")
        finally:
            cc.close()
        token = inv["token"]
        results, lock = [], threading.Lock()

        def claim(email):
            conn = _control()
            try:
                r = invites.claim(conn, token, email, PW)
                with lock:
                    results.append(("ok", r))
            except Exception as e:                    # noqa: BLE001
                with lock:
                    results.append(("err", str(e)))
            finally:
                conn.close()

        ts = [threading.Thread(target=claim,
                               args=(f"v{i}-{uuid.uuid4().hex[:6]}@ex.dev",))
              for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        wins = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(wins), 1, f"expected exactly one winner: {results}")

    # ---- TOTP disable revokes other sessions -------------------------------
    def test_totp_disable_revokes_other_sessions(self):
        c, email, tid = self._signup()
        # enroll TOTP
        opts = c.post("/api/totp/enroll", data={"password": PW}).json()
        secret = opts["secret"]
        cr = c.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": PW})
        self.assertEqual(cr.status_code, 200, cr.text)
        # a second, independent session for the same account
        other = TestClient(self.app)
        other.post("/api/login", data={
            "email": email, "password": PW,
            "totp_code": _fresh_code(secret)})
        self.assertEqual(other.get("/api/me").status_code, 200)
        # disable 2FA from the first session…
        r = c.post("/api/totp/disable",
                   data={"code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        # …the other session is now dead, the acting one survives
        self.assertEqual(other.get("/api/me").status_code, 401)
        self.assertEqual(c.get("/api/me").status_code, 200)


    # ---- SSRF guard (hosted blocks private, self-host allows) --------------
    def test_ssrf_guard_hosted_vs_selfhost(self):
        from oikonome.web import netguard
        # self-host (default in tests): local LLM allowed, metadata blocked
        netguard.check_url("http://127.0.0.1:11434", what="llm")   # no raise
        with self.assertRaises(netguard.BlockedURL):
            netguard.check_url("http://169.254.169.254/latest/meta-data")
        # hosted: private/loopback rejected
        os.environ["OIKONOME_HOSTED"] = "1"
        try:
            with self.assertRaises(netguard.BlockedURL):
                netguard.check_url("http://127.0.0.1:11434", what="llm")
            with self.assertRaises(netguard.BlockedURL):
                netguard.check_url("http://10.0.0.5:6379")
            netguard.check_url("https://api.openai.com/v1")   # public: ok
        finally:
            os.environ.pop("OIKONOME_HOSTED", None)

    def test_settings_rejects_a_metadata_llm_url(self):
        c, _, _ = self._signup()
        r = c.post("/api/settings", json={
            "llm_url": "http://169.254.169.254/", "llm_model": "x"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("metadata", r.text.lower())

    # ---- generic import refuses to guess a workplace plan ------------------
    def test_generic_import_refuses_a_plan_csv(self):
        from oikonome.web import pages
        from oikonome.sync import plan_csv as lin
        c, _, tid = self._signup()
        conn = tenancy.tenant_connect(tid)
        try:
            # a plan-activity CSV header (the column signature portals share)
            sample = ("Trade Date,Investments,Ticker,Transaction,"
                      "Transaction Amount,Share Price,Total Shares\n"
                      "07/01/2026,S&P 500,FXAIX,Buy,1000,100,10\n")
            assert lin.looks_like_plan_activity(
                sample.split("\n", 1)[0].split(",")), "header must match"
            result, _ = pages.dispatch_import(
                conn, "chk", "bank_neg", "plan.csv", sample.encode())
            self.assertIn("error", result)
            self.assertIn("plan-activity", result["error"].lower())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
