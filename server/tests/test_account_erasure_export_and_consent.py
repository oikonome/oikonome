"""Ending an account and taking the data with you: delete-with-grace,
restore, the scheduled purge, the portable data export, and consented
support access."""

import io
import json
import os
import unittest
import uuid
import zipfile
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db


def _seed_tenant(with_data=True):
    admin = tenancy.admin_connect()
    try:
        email = f"ga-{uuid.uuid4().hex[:8]}@x.dev"
        tid = tenancy.create_tenant(admin, email)
        from oikonome.auth import passwords
        admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, verified_at) "
            "VALUES (%s, %s, %s, now())",
            (tid, email, passwords.hash_password("correct-horse-battery")))
    finally:
        admin.close()
    if with_data:
        conn = tenancy.tenant_connect(tid)
        try:
            conn.execute(
                "INSERT INTO accounts (id, tenant_id, name, type) "
                "VALUES (%s, %s, %s, %s)",
                (str(uuid.uuid4()), tid, "Everyday Checking", "depository"))
        finally:
            conn.close()
    return tid, email


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_archive_is_zip_of_json_with_manifest(self):
        from oikonome import tenant_export
        tid, email = _seed_tenant()
        data = tenant_export.build_archive(tid, operator="op", consented=True)
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        self.assertIn("manifest.json", names)
        self.assertIn("data/accounts.json", names)
        man = json.loads(z.read("manifest.json"))
        self.assertEqual(man["export_format"], "oikonome-tenant-export/1")
        self.assertTrue(man["tenant_consent_on_file"])
        self.assertEqual(man["tenant"]["id"], tid)
        accts = json.loads(z.read("data/accounts.json"))
        self.assertEqual(len(accts), 1)
        self.assertEqual(accts[0]["name"], "Everyday Checking")

    def test_secrets_and_control_tables_excluded(self):
        from oikonome import tenant_export
        tid, email = _seed_tenant()
        # give the account a would-be-secret-bearing item row
        data = tenant_export.build_archive(tid, operator="op", consented=False)
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        self.assertNotIn("data/sessions.json", names)
        # no exported json may contain an access_token / password_hash column
        for n in names:
            if n.startswith("data/"):
                rows = json.loads(z.read(n))
                for r in rows:
                    self.assertNotIn("access_token", r)
                    self.assertNotIn("password_hash", r)

    def test_device_token_metadata_travels_without_the_credentials(self):
        """device_tokens is control-plane (tenant_id, no RLS), so the
        RLS-driven table discovery can never include it — yet a phone
        enrolled against the tenant is personal data a portability
        request must cover. The manifest carries its METADATA (name,
        platform, lifecycle timestamps) hand-scoped by tenant, and never
        the token hash or the push token — both are credentials."""
        from oikonome import tenant_export
        from oikonome.auth import device_tokens as dt_mod
        tid, email = _seed_tenant()
        admin = tenancy.admin_connect()
        try:
            uid = admin.execute(
                "SELECT id FROM users WHERE tenant_id=%s",
                (tid,)).fetchone()["id"]
            raw, row = dt_mod.mint(admin, uid, tid, "Casey's Pixel",
                                   platform="android")
            dt_mod.set_push(admin, uid, row["id"],
                            f"pushtok-{uuid.uuid4().hex}", "fcm")
        finally:
            admin.close()
        data = tenant_export.build_archive(tid, operator="op",
                                           consented=False)
        z = zipfile.ZipFile(io.BytesIO(data))
        man = json.loads(z.read("manifest.json"))
        devices = man.get("mobile_devices")
        self.assertTrue(devices, "enrolled device missing from the export")
        self.assertEqual(devices[0]["device_name"], "Casey's Pixel")
        self.assertEqual(devices[0]["platform"], "android")
        self.assertIn("created_at", devices[0])
        # the credentials never travel: not the raw token, not its hash,
        # not the push routing token
        blob = data if isinstance(data, bytes) else bytes(data)
        self.assertNotIn(raw.encode(), blob)
        self.assertNotIn(b"token_hash", z.read("manifest.json"))
        self.assertNotIn(b"pushtok-", z.read("manifest.json"))

    def test_tenant_settings_config_is_scrubbed(self):
        """_EXCLUDE_COLS is a column filter, and tenant_settings hides
        everything in one config JSONB — encrypted smtp/plaid/llm secret
        blobs, cleartext provider identifiers and demo_login's plaintext
        password would ride an archive whose own manifest asserts 'secrets
        are NEVER exported'."""
        from oikonome import tenant_export
        tid, email = _seed_tenant()
        conn = tenancy.tenant_connect(tid)
        try:
            from oikonome.engine.compat import jsonb
            conn.execute(
                """INSERT INTO tenant_settings (tenant_id, config)
                   VALUES (%s, %s)
                   ON CONFLICT (tenant_id) DO UPDATE
                     SET config = EXCLUDED.config""",
                (tid, jsonb({
                    "food_monthly": 800,
                    "smtp_password": "enc:v1:ciphertext",
                    "plaid_secret": "enc:v1:ciphertext2",
                    "llm_api_key": "enc:v1:ciphertext3",
                    "demo_login": {"email": "d@x.test",
                                   "password": "plaintext-pw"}})))
        finally:
            conn.close()
        data = tenant_export.build_archive(tid, operator="op",
                                           consented=True)
        z = zipfile.ZipFile(io.BytesIO(data))
        raw = z.read("data/tenant_settings.json").decode()
        self.assertNotIn("enc:v1:", raw, "encrypted secret blobs exported")
        self.assertNotIn("plaintext-pw", raw, "demo_login password exported")
        self.assertNotIn("smtp_password", raw)
        rows = json.loads(raw)
        cfg = rows[0]["config"]
        cfg = json.loads(cfg) if isinstance(cfg, str) else cfg
        self.assertEqual(cfg.get("food_monthly"), 800,
                         "the scrub must keep ordinary settings")

    def test_no_such_tenant_raises(self):
        from oikonome import tenant_export
        with self.assertRaises(ValueError):
            tenant_export.build_archive(str(uuid.uuid4()), operator="op",
                                        consented=False)

    def test_export_never_crosses_tenants(self):
        """The export must contain
        ZERO rows belonging to any OTHER tenant — no-RLS control-plane
        tables (users/invites/api_tokens) carry tenant_id and a naive
        RLS-reliant SELECT would return every tenant's rows."""
        from oikonome import tenant_export
        a_tid, a_email = _seed_tenant()
        b_tid, b_email = _seed_tenant()
        data = tenant_export.build_archive(a_tid, operator="op",
                                           consented=False)
        z = zipfile.ZipFile(io.BytesIO(data))
        for n in z.namelist():
            if not n.startswith("data/"):
                continue
            blob = z.read(n).decode()
            self.assertNotIn(b_tid, blob, f"{n} leaked tenant B id")
            self.assertNotIn(b_email, blob, f"{n} leaked tenant B email")
        # and the export must NOT bulk-dump the no-RLS control tables
        self.assertNotIn("data/users.json", z.namelist())
        self.assertNotIn("data/api_tokens.json", z.namelist())

    def test_wrapped_key_never_exported(self):
        """The tenant's envelope key (tenant_keys.wrapped_key, wrapped under
        the master key) must not ride a portable archive."""
        from oikonome import tenant_export
        tid, _ = _seed_tenant(with_data=False)
        # give the tenant an envelope key row
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO tenant_keys (tenant_id, wrapped_key) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (tid, "SECRET-WRAPPED-KEY-MATERIAL"))
        finally:
            admin.close()
        data = tenant_export.build_archive(tid, operator="op", consented=False)
        z = zipfile.ZipFile(io.BytesIO(data))
        for n in z.namelist():
            self.assertNotIn("SECRET-WRAPPED-KEY-MATERIAL", z.read(n).decode())

    def test_encrypted_ein_never_exported(self):
        """business_entity is tenant-scoped + RLS so it
        rides the portable archive, but the encrypted EIN blob (ein_enc) must
        not — entities.py guarantees the raw EIN only leaves via a separate
        gated reveal, and this bulk path must honor the same invariant."""
        from oikonome import tenant_export
        from oikonome.engine import entities
        tid, _ = _seed_tenant(with_data=False)
        conn = tenancy.tenant_connect(tid)
        try:
            entities.create_entity(conn, name="Acme LLC",
                                   structure="single_member_llc",
                                   # EIN synthetic, not a real one
                                   ein="00-0001234")
        finally:
            conn.close()
        data = tenant_export.build_archive(tid, operator="op", consented=False)
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertIn("data/business_entity.json", z.namelist())  # DID export
        blob = z.read("data/business_entity.json").decode()
        self.assertNotIn("ein_enc", blob)


class ConsentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_live_consent_lifecycle(self):
        from oikonome import tenant_export
        tid, _ = _seed_tenant(with_data=False)
        admin = tenancy.admin_connect()
        try:
            self.assertFalse(tenant_export.has_live_consent(admin, tid))
            admin.execute(
                "INSERT INTO support_consents (tenant_id, expires_at, reason) "
                "VALUES (%s, now() + interval '1 hour', %s)", (tid, "debug"))
            self.assertTrue(tenant_export.has_live_consent(admin, tid))
            self.assertEqual(
                tenant_export.consent_status(admin, tid)["reason"], "debug")
            # expired grant doesn't count
            admin.execute("UPDATE support_consents SET expires_at = "
                          "now() - interval '1 minute' WHERE tenant_id=%s",
                          (tid,))
            self.assertFalse(tenant_export.has_live_consent(admin, tid))
        finally:
            admin.close()


class DeleteGraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_scheduled_purge_only_after_window(self):
        from oikonome.jobs import worker
        tid, _ = _seed_tenant(with_data=False)
        admin = tenancy.admin_connect()
        try:
            # schedule with the window still in the FUTURE
            admin.execute(
                "UPDATE tenants SET status='pending_delete', "
                "delete_after = now() + interval '7 days' WHERE id=%s", (tid,))
        finally:
            admin.close()
        # nothing due yet
        with mock.patch("oikonome.erasure.release_external",
                        return_value={"plaid_items": 0, "released": "none",
                                      "mx_user": "none"}):
            self.assertEqual(worker.purge_scheduled_deletions(), [])
            self._still_exists(tid, True)
            # move the window into the PAST → now it purges
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE tenants SET delete_after = "
                              "now() - interval '1 minute' WHERE id=%s", (tid,))
            finally:
                admin.close()
            purged = worker.purge_scheduled_deletions()
            self.assertIn(tid, purged)
            self._still_exists(tid, False)

    def _still_exists(self, tid, expected):
        admin = tenancy.admin_connect()
        try:
            row = admin.execute("SELECT 1 FROM tenants WHERE id=%s",
                               (tid,)).fetchone()
        finally:
            admin.close()
        self.assertEqual(row is not None, expected)

    def test_active_tenant_never_purged(self):
        from oikonome.jobs import worker
        tid, _ = _seed_tenant(with_data=False)
        with mock.patch("oikonome.erasure.release_external",
                        return_value={"plaid_items": 0, "released": "none",
                                      "mx_user": "none"}):
            self.assertNotIn(tid, worker.purge_scheduled_deletions())
        self._still_exists(tid, True)

    def test_restored_tenant_skipped_by_purge(self):
        """A restore between the candidate scan and the wipe wins: the
        row-lock re-check sees status != pending_delete and skips."""
        from oikonome.jobs import worker
        tid, _ = _seed_tenant(with_data=False)
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE tenants SET status='pending_delete', "
                "delete_after = now() - interval '1 minute' WHERE id=%s",
                (tid,))
            # simulate the restore having already landed
            admin.execute(
                "UPDATE tenants SET status='active', delete_after=NULL "
                "WHERE id=%s", (tid,))
        finally:
            admin.close()
        with mock.patch("oikonome.erasure.release_external") as rel:
            self.assertNotIn(tid, worker.purge_scheduled_deletions())
        rel.assert_not_called()          # never even attempted the release
        self._still_exists(tid, True)


class RestoreStatusTests(unittest.TestCase):
    """A suspended tenant scheduled for deletion must restore to SUSPENDED,
    not silently un-suspend to active."""
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def _console(self):
        token = "test-admin-token-" + "r" * 32
        os.environ["OIKONOME_ADMIN_TOKEN"] = token
        client = TestClient(self.appmod.app)
        r = client.post("/admin/console/login", data={"token": token},
                        follow_redirects=False)
        assert r.status_code == 303
        return client

    def _status(self, tid):
        admin = tenancy.admin_connect()
        try:
            return admin.execute("SELECT status FROM tenants WHERE id=%s",
                                (tid,)).fetchone()["status"]
        finally:
            admin.close()

    def test_suspended_then_scheduled_then_restored_returns_suspended(self):
        tid, email = _seed_tenant(with_data=False)
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET status='suspended' WHERE id=%s",
                          (tid,))
        finally:
            admin.close()
        client = self._console()
        try:
            r = client.post("/admin/console/tenant-delete",
                            data={"tenant_id": tid, "confirm": email,
                                  "mode": "grace"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self._status(tid), "pending_delete")
            r = client.post("/admin/console/tenant-restore",
                            data={"tenant_id": tid})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self._status(tid), "suspended")   # not active
        finally:
            os.environ.pop("OIKONOME_ADMIN_TOKEN", None)

    def test_active_scheduled_restore_returns_active(self):
        tid, email = _seed_tenant(with_data=False)
        client = self._console()
        try:
            client.post("/admin/console/tenant-delete",
                        data={"tenant_id": tid, "confirm": email,
                              "mode": "grace"})
            client.post("/admin/console/tenant-restore",
                        data={"tenant_id": tid})
            self.assertEqual(self._status(tid), "active")
        finally:
            os.environ.pop("OIKONOME_ADMIN_TOKEN", None)


class SupportAccessApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.tid, self.email = _seed_tenant(with_data=False)
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={
            "email": self.email, "password": "correct-horse-battery"},
            follow_redirects=False)
        assert r.status_code == 303, r.text

    def test_grant_and_revoke(self):
        self.assertFalse(self.client.get("/api/support-access")
                         .json()["granted"])
        # granting is step-up gated (password)
        r = self.client.post("/api/support-access",
                             json={"hours": 24, "reason": "help",
                                   "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        st = self.client.get("/api/support-access").json()
        self.assertTrue(st["granted"])
        self.assertEqual(st["reason"], "help")
        r = self.client.post("/api/support-access/revoke")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.client.get("/api/support-access")
                         .json()["granted"])

    def test_grant_requires_password(self):
        r = self.client.post("/api/support-access",
                             json={"hours": 24, "reason": "help"})
        self.assertEqual(r.status_code, 403, r.text)   # elevation_required
        self.assertEqual(r.json()["error"], "elevation_required")

    def test_hours_bounds(self):
        for bad in (0, 200, -5):
            r = self.client.post("/api/support-access",
                                 json={"hours": bad,
                                       "password": "correct-horse-battery"})
            self.assertEqual(r.status_code, 400, bad)


if __name__ == "__main__":
    unittest.main()
