"""The unauthenticated entry points and the data in/out routes.

The login page signs a user in and rejects a wrong password; an anonymous
page hit redirects to /login rather than erroring; setup survives an
aggregator token the provider rejects, and the post-setup connect route
reports that failure instead of a stack trace; an export scrubs credential
keys before it leaves the instance; and a ZIP restore reports success. Each
is the only path a locked-out or migrating household has."""

import io
import json
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import setup

from .util import _ensure_db, seed_accounts, write_config
from .export_ticket import export_get


class LoginSetupAndDataTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.email = f"ux-{uuid.uuid4().hex[:8]}@example.dev"
        cls.client.post("/api/signup", data={
            "email": cls.email, "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_login_page_renders_and_signs_in(self):
        anon = TestClient(self.client.app)
        r = anon.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sign in", r.text)
        r = anon.post("/login", data={"email": self.email,
                                      "password": "correct-horse-battery"},
                      follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/app/")
        self.assertEqual(anon.get("/api/me").json()["email"], self.email)

    def test_login_page_wrong_password(self):
        anon = TestClient(self.client.app)
        r = anon.post("/login", data={"email": self.email,
                                      "password": "wrong-wrong-wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("Wrong email or password", r.text)

    def test_anonymous_page_hit_redirects_to_login(self):
        anon = TestClient(self.client.app)
        r = anon.get("/today", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")
        # API routes keep JSON 401s
        r = anon.get("/api/me")
        self.assertEqual(r.status_code, 401)

    def test_bootstrap_survives_bad_simplefin_token(self):
        email = f"badtok-{uuid.uuid4().hex[:8]}@example.dev"
        r = setup.bootstrap(email, "correct-horse-battery",
                            simplefin_token="!!!not-base64-or-a-url!!!")
        self.assertIsNotNone(r["user_id"])       # user exists — not bricked
        self.assertIsNone(r["sync"])
        self.assertIn("retry", r["sync_error"])

    def test_accounts_simplefin_retry_route_shows_error(self):
        r = self.client.post("/accounts/simplefin",
                             data={"simplefin_token": "!!!broken!!!"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/accounts?msg=", r.headers["location"])

    def test_export_scrubs_credential_keys(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute("SELECT config FROM tenant_settings").fetchone()
            cfg = row["config"] if isinstance(row["config"], dict) \
                else json.loads(row["config"])
            cfg.update({"plaid_client_id": "cid", "plaid_secret": "shh",
                        "simplefin_access_url": "https://u:p@bridge/x",
                        "food_budget": cfg.get("food_budget", 500)})
            conn.execute(
                "UPDATE tenant_settings SET config=%s::jsonb",
                (json.dumps(cfg),))
        finally:
            conn.close()
        r = export_get(self.client, "/export")
        self.assertEqual(r.status_code, 200)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        settings_csv = z.read("tenant_settings.csv").decode()
        self.assertNotIn("shh", settings_csv)
        self.assertNotIn("plaid_client_id", settings_csv)
        self.assertNotIn("bridge/x", settings_csv)
        self.assertIn("food_budget", settings_csv)

    def test_zip_restore_reports_success_not_namerror(self):
        # a minimal export ZIP: empty tables — the route must not crash on
        # the unbound-batch-id path and must render the restored notice
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for tbl in ("items", "accounts", "transactions", "bills",
                        "manual_categories", "tenant_settings"):
                z.writestr(f"{tbl}.csv", "")
        r = self.client.post(
            "/import",
            files={"file": ("oikonome-export.zip", buf.getvalue(),
                            "application/zip")},
            data={"account_id": ""})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("import failed", r.text)
        self.assertIn("restored", r.text)

    def test_non_zip_import_requires_account(self):
        r = self.client.post(
            "/import",
            files={"file": ("x.csv", b"Date,Amount,Description\n",
                            "text/csv")},
            data={"account_id": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIn("pick the account", r.text)


if __name__ == "__main__":
    unittest.main()
