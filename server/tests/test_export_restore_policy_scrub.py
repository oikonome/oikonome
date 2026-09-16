"""An export carries a household's data; it never carries policy.

Three invariants, each with a silent failure mode:

* The export scrub is DEEP. A top-level key-substring match leaves
  `demo_login` (a nested {email, password}) in tenant_settings.csv, so a
  demo instance's export ZIP carries its shared plaintext password. Export
  and restore share one scrub (restore.scrub_config) that drops policy keys
  and recurses into nested dicts.
* A restore MERGES data, never decisions. Merging every non-credential key
  from a ZIP lets a crafted export set smtp_starttls:false (silent
  cleartext SMTP), demo_mode, or demo_login on the destination. Those
  policy keys are denylisted; the destination's own values always win.
* The demo guard's per-tenant cache is invalidated on every config write.
  Cached and never invalidated, a runtime flip (restore, settings write)
  leaves the data doors in their pre-flip state until process restart.
"""

import csv
import io
import json
import unittest
import uuid
import zipfile

from oikonome.sync import restore
from oikonome.web import demoguard

from .util import _ensure_db, make_db, write_config
from .export_ticket import export_get


def _settings_zip(cfg: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(cfg)})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


class ConfigScrubTests(unittest.TestCase):
    """scrub_config drops demo_login/demo_mode and nested passwords while
    leaving user data (savings-goal token lists) alone."""

    def test_scrub_drops_demo_login_and_nested_password(self):
        cfg = {"food_monthly": 500,
               "demo_login": {"email": "demo@x", "password": "shared-pw"},
               "demo_mode": True,
               "smtp_starttls": False,
               "portal": {"password": "deep-shh", "note": "keep"},
               "plaid_secret": "shh"}
        out = restore.scrub_config(cfg)
        self.assertEqual(out["food_monthly"], 500)
        self.assertNotIn("demo_login", out)
        self.assertNotIn("demo_mode", out)
        self.assertNotIn("smtp_starttls", out)
        self.assertNotIn("plaid_secret", out)
        self.assertNotIn("password", out["portal"])
        self.assertEqual(out["portal"]["note"], "keep")

    def test_scrub_keeps_savings_goal_tokens(self):
        # goal match tokens are user data in a LIST — the deep scrub must
        # not eat them (they'd be unreachable by the substring match today,
        # but the list boundary is the guarantee)
        cfg = {"savings_goals": [{"name": "Trip", "account_id": "sav",
                                  "tokens": ["vacation"]}]}
        out = restore.scrub_config(cfg)
        self.assertEqual(out["savings_goals"][0]["tokens"], ["vacation"])


class ExportOmitsDemoLoginTests(unittest.TestCase):
    """A tenant with demo_login exports: the ZIP's tenant_settings.csv
    must not carry the plaintext password."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        from fastapi.testclient import TestClient

        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"scrub-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def test_export_zip_has_no_demo_login_password(self):
        from oikonome.db import tenancy
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT config FROM tenant_settings").fetchone()
            cfg = dict(row["config"]) if row else {}
            cfg.update({"demo_login": {"email": "demo@example.dev",
                                       "password": "topsecret-demo-pw"},
                        "food_monthly": 500})
            conn.execute(
                """INSERT INTO tenant_settings (config) VALUES (%s::jsonb)
                   ON CONFLICT (tenant_id) DO UPDATE
                   SET config = EXCLUDED.config""", (json.dumps(cfg),))
        finally:
            conn.close()
        r = export_get(self.client, "/export")
        self.assertEqual(r.status_code, 200)
        settings_csv = zipfile.ZipFile(io.BytesIO(r.content)).read(
            "tenant_settings.csv").decode()
        self.assertNotIn("topsecret-demo-pw", settings_csv)
        self.assertNotIn("demo_login", settings_csv)
        self.assertIn("food_monthly", settings_csv)


class RestorePolicyKeyTests(unittest.TestCase):
    """A crafted ZIP must not flip demo lockdown or downgrade SMTP."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, smtp_starttls=True)

    def tearDown(self):
        self.conn.close()

    def _cfg(self):
        row = self.conn.execute(
            "SELECT config FROM tenant_settings").fetchone()
        return dict(row["config"])

    def test_restore_drops_policy_keys(self):
        data = _settings_zip({
            "food_monthly": 777,
            "smtp_starttls": False,
            "demo_mode": True,
            "demo_login": {"email": "a@b", "password": "pw"}})
        restore.restore_zip(self.conn, data)
        cfg = self._cfg()
        self.assertEqual(cfg["food_monthly"], 777)   # benign keys merge
        self.assertIs(cfg["smtp_starttls"], True)    # destination wins
        self.assertNotIn("demo_mode", cfg)
        self.assertNotIn("demo_login", cfg)


class DemoguardCacheInvalidationTests(unittest.TestCase):
    """A runtime demo_mode flip takes effect without a process restart."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS tid"
        ).fetchone()["tid"]

    def tearDown(self):
        self.conn.close()

    def test_save_config_invalidates_cache(self):
        self.assertFalse(demoguard.is_demo(self.tid))   # cache primed False
        write_config(self.conn, demo_mode=True)         # budget.save_config
        self.assertTrue(demoguard.is_demo(self.tid))
        write_config(self.conn, demo_mode=False)
        self.assertFalse(demoguard.is_demo(self.tid))

    def test_restore_settings_merge_invalidates_cache(self):
        self.assertFalse(demoguard.is_demo(self.tid))
        # restore can't SET demo_mode — but its settings merge
        # must still refresh the cache (belt for any future merged flag)
        demoguard._cache[self.tid] = True               # simulate stale
        restore.restore_zip(self.conn, _settings_zip({"food_monthly": 1}))
        self.assertFalse(demoguard.is_demo(self.tid))


if __name__ == "__main__":
    unittest.main()
