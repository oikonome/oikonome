"""Demo instance: demo.reset regenerates the household in place (same
seed, same login), OIKONOME_DEMO closes the pre-auth doors, tenant
suspension is total, and the console's per-tenant suspend/sync actions
audit what they did."""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import TEST_DB, _admin_dsn, _ensure_db


ADMIN_TOKEN = "test-admin-token-" + "y" * 32


class DemoDoorTests(unittest.TestCase):
    """OIKONOME_DEMO=1 → signup / forgot / reset are 404 (cloaked): the
    tenant-level demoguard can't reach pre-auth paths."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        os.environ["OIKONOME_DEV"] = "1"
        from oikonome.web.app import app
        cls.client = TestClient(app)

    def test_doors_cloaked_with_env(self):
        os.environ["OIKONOME_DEMO"] = "1"
        try:
            for method, path in (("get", "/signup"), ("post", "/signup"),
                                 ("get", "/forgot"), ("post", "/forgot"),
                                 ("get", "/reset"), ("post", "/reset"),
                                 ("post", "/api/signup")):
                r = getattr(self.client, method)(path)
                self.assertEqual(r.status_code, 404, f"{method} {path}")
        finally:
            os.environ.pop("OIKONOME_DEMO", None)


class SuspensionTests(unittest.TestCase):
    """tenants.status='suspended' blocks EVERYTHING but logout."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        os.environ["OIKONOME_DEV"] = "1"
        from oikonome.auth import sessions
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        cls.tid = str(tenancy.create_tenant(
            cls.admin, f"susp-{uuid.uuid4().hex[:8]}"))
        u = cls.admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s, %s, 'x') RETURNING id",
            (cls.tid, f"susp-{uuid.uuid4().hex[:6]}@example.dev")).fetchone()
        cls.client.cookies.set(
            sessions.COOKIE_NAME,
            sessions.create_session(cls.admin, u["id"], cls.tid, "ua"))

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()

    def _set(self, status):
        self.admin.execute("UPDATE tenants SET status=%s WHERE id=%s",
                           (status, self.tid))
        self.admin.commit()

    def test_active_serves(self):
        self._set("active")
        self.assertEqual(self.client.get("/api/me").status_code, 200)

    def test_suspended_blocks_reads_and_writes(self):
        self._set("suspended")
        try:
            self.assertEqual(self.client.get("/api/me").status_code, 403)
            self.assertEqual(
                self.client.post("/api/accounts/plaid/keys",
                                 json={}).status_code, 403)
        finally:
            self._set("active")

    def test_suspended_logout_still_works(self):
        self._set("suspended")
        try:
            r = self.client.post("/api/logout")
            self.assertNotEqual(r.status_code, 403)
        finally:
            self._set("active")


class ConsoleActionTests(unittest.TestCase):
    """Console actions: tenant-status flips + audits; sync-now
    degrades to a notice when the queue is unreachable; the fleet query
    carries the billing columns."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        os.environ["OIKONOME_ADMIN_TOKEN"] = ADMIN_TOKEN
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app)
        self.client.post("/admin/console/login", data={"token": ADMIN_TOKEN},
                         follow_redirects=False)
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            admin, f"cact-{uuid.uuid4().hex[:8]}"))
        admin.close()

    def tearDown(self):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)

    def _tenant_status(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            return admin.execute("SELECT status FROM tenants WHERE id=%s",
                                 (self.tid,)).fetchone()["status"]
        finally:
            admin.close()

    def test_suspend_and_reactivate_audited(self):
        r = self.client.post("/admin/console/tenant-status",
                             data={"tenant_id": self.tid, "to": "suspended"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._tenant_status(), "suspended")
        r = self.client.post("/admin/console/tenant-status",
                             data={"tenant_id": self.tid, "to": "active"})
        self.assertEqual(self._tenant_status(), "active")
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            acts = [a["action"] for a in admin.execute(
                "SELECT action FROM admin_audit WHERE target=%s "
                "ORDER BY id", (self.tid,)).fetchall()]
        finally:
            admin.close()
        self.assertIn("tenant_suspend", acts)
        self.assertIn("tenant_activate", acts)

    def test_bad_target_status_rejected(self):
        r = self.client.post("/admin/console/tenant-status",
                             data={"tenant_id": self.tid, "to": "deleted"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._tenant_status(), "active")


class DemoResetTests(unittest.TestCase):
    """reset() = erase + reseed in place, deterministically. These run
    against a THROWAWAY database because reset() operates on 'the single
    tenant of this instance' — the shared test DB has thousands."""

    DB = f"{TEST_DB}_demo_reset"

    @classmethod
    def setUpClass(cls):
        import psycopg

        from oikonome.db import migrate

        from .util import _admin_dsn as dsn
        _ensure_db()
        with psycopg.connect(dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {cls.DB}")
            c.execute(f"CREATE DATABASE {cls.DB}")
        migrate.run(dsn(cls.DB))
        # redirect BOTH module DSNs at the throwaway DB (reset() opens
        # admin AND tenant connections); restored in tearDownClass
        cls._saved = (tenancy.ADMIN_DSN, tenancy.APP_DSN)
        tenancy.ADMIN_DSN = dsn(cls.DB)
        tenancy.APP_DSN = (f"postgresql://oikonome_app:apppass@"
                           f"127.0.0.1:5433/{cls.DB}")

    @classmethod
    def tearDownClass(cls):
        tenancy.ADMIN_DSN, tenancy.APP_DSN = cls._saved

    def test_reset_replays_the_household_and_keeps_login(self):
        from oikonome import demo
        first = demo.seed(seed=4242, years=2)
        # simulate visitor damage: recategorize + delete rows
        conn = tenancy.tenant_connect(first["tenant_id"])
        try:
            conn.execute("UPDATE transactions SET category_override='Vandal'")
            conn.execute("DELETE FROM bills")
            conn.commit()
        finally:
            conn.close()
        r = demo.reset()
        self.assertTrue(r["reset"])
        self.assertEqual(r["seed"], 4242)
        self.assertEqual(r["email"], first["email"])
        self.assertEqual(r["password"], first["password"])
        self.assertEqual(r["transactions"], first["transactions"])
        self.assertNotEqual(r["tenant_id"], first["tenant_id"])
        # single pristine tenant, damage gone
        admin = tenancy.admin_connect()
        try:
            n = admin.execute("SELECT count(*) AS n FROM tenants").fetchone()
            self.assertEqual(n["n"], 1)
            v = admin.execute(
                "SELECT count(*) AS n FROM transactions "
                "WHERE category_override='Vandal'").fetchone()
            self.assertEqual(v["n"], 0)
            rec = admin.execute(
                "SELECT count(*) AS n FROM bills").fetchone()
            self.assertGreater(rec["n"], 0)
        finally:
            admin.close()

    def test_reset_refuses_non_demo(self):
        from oikonome import demo
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenant_settings SET config = "
                          "config - 'demo_mode'")
            admin.commit()
            with self.assertRaises(SystemExit):
                demo.reset()
            admin.execute("""UPDATE tenant_settings SET config =
                jsonb_set(config, '{demo_mode}', 'true')""")
            admin.commit()
        finally:
            admin.close()


class DemoSeedSingleFlightTests(unittest.TestCase):
    """seed() and reset() are the two things that can create the demo
    tenant, and they can overlap: the hourly cron, a slow seed and an
    operator at the CLI. Two seeds that both saw an empty instance each
    create a tenant, after which every reset refuses ("needs exactly one
    tenant") and the public demo stays vandalized for good."""

    DB = f"{TEST_DB}_demo_seed"

    @classmethod
    def setUpClass(cls):
        import psycopg

        from oikonome.db import migrate

        from .util import _admin_dsn as dsn
        _ensure_db()
        with psycopg.connect(dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {cls.DB} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {cls.DB}")
        migrate.run(dsn(cls.DB))
        cls._saved = (tenancy.ADMIN_DSN, tenancy.APP_DSN)
        tenancy.ADMIN_DSN = dsn(cls.DB)
        tenancy.APP_DSN = (f"postgresql://oikonome_app:apppass@"
                           f"127.0.0.1:5433/{cls.DB}")

    @classmethod
    def tearDownClass(cls):
        tenancy.ADMIN_DSN, tenancy.APP_DSN = cls._saved

    def setUp(self):
        admin = tenancy.admin_connect()
        try:
            for r in admin.execute("SELECT id FROM tenants").fetchall():
                tenancy.delete_tenant_rows(admin, str(r["id"]))
        finally:
            admin.close()

    def _tenant_count(self):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT count(*) AS n FROM tenants").fetchone()["n"]
        finally:
            admin.close()

    def test_seed_refuses_when_a_tenant_exists_even_without_users(self):
        from oikonome import demo
        admin = tenancy.admin_connect()
        try:
            tenancy.create_tenant(admin, "half-made")
        finally:
            admin.close()
        with self.assertRaises(SystemExit):
            demo.seed(seed=1, years=1)
        self.assertEqual(self._tenant_count(), 1)

    def test_two_concurrent_seeds_make_exactly_one_tenant(self):
        import threading

        from oikonome import demo
        gate = threading.Barrier(2)
        outcomes = []

        def run():
            gate.wait()
            try:
                outcomes.append(demo.seed(seed=7, years=1)["tenant_id"])
            except SystemExit as e:
                outcomes.append(e)

        ts = [threading.Thread(target=run) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=300)
        self.assertEqual(self._tenant_count(), 1,
                         f"parallel seeds produced {self._tenant_count()} "
                         "tenants — the demo can never reset again")
        self.assertEqual(1, sum(isinstance(o, str) for o in outcomes))
        self.assertEqual(1, sum(isinstance(o, SystemExit) for o in outcomes))

    def test_reset_racing_a_reset_leaves_one_tenant(self):
        import threading

        from oikonome import demo
        demo.seed(seed=9, years=1)
        gate = threading.Barrier(2)
        errors = []

        def run():
            gate.wait()
            try:
                demo.reset()
            except SystemExit as e:      # a loser that saw 0 or 2 tenants
                errors.append(e)

        ts = [threading.Thread(target=run) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=300)
        self.assertEqual(self._tenant_count(), 1)
        self.assertEqual([], errors, "a serialized reset never sees the "
                                     "other one's half-done state")


class DeploymentHistoryTests(unittest.TestCase):
    """migrate stamps one deployments row per NEW version; re-running the
    same version (compose restarts re-run migrate) never duplicates."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _count(self, admin, v):
        return admin.execute("SELECT count(*) AS n FROM deployments "
                             "WHERE version=%s", (v,)).fetchone()["n"]

    def test_stamp_once_per_version(self):
        from oikonome.db import migrate
        v = f"testver-{uuid.uuid4().hex[:8]}"
        os.environ["OIKONOME_VERSION"] = v
        try:
            migrate.run(_admin_dsn(TEST_DB))
            migrate.run(_admin_dsn(TEST_DB))     # same version → no dup
        finally:
            os.environ.pop("OIKONOME_VERSION", None)
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            self.assertEqual(self._count(admin, v), 1)
        finally:
            admin.close()

    def test_migrate_repairs_stale_summaries(self):
        # a row carrying a hand-set summary is corrected once a manifest
        # that knows the version's real subject is deployed
        from oikonome.db import migrate

        from .test_admin_console import _manifest_versions
        known = _manifest_versions()[0]
        real = migrate._manifest_subject(known)
        self.assertTrue(real)
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute("DELETE FROM deployments WHERE version=%s", (known,))
            admin.execute(
                "INSERT INTO deployments (version, migrations, summary) "
                "VALUES (%s, 0, 'Stale hand-set deploy note')", (known,))
        finally:
            admin.close()
        migrate.run(_admin_dsn(TEST_DB))
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            row = admin.execute("SELECT summary FROM deployments "
                                "WHERE version=%s", (known,)).fetchone()
            self.assertEqual(row["summary"], real)
        finally:
            admin.close()

    def test_console_shows_build_and_history(self):
        os.environ["OIKONOME_ADMIN_TOKEN"] = ADMIN_TOKEN
        os.environ["OIKONOME_DEV"] = "1"
        try:
            import oikonome.web.app as appmod
            appmod.DEV_MODE = True
            from oikonome.web import security
            security._limiter._hits.clear()
            client = TestClient(appmod.app)
            client.post("/admin/console/login", data={"token": ADMIN_TOKEN},
                        follow_redirects=False)
            r = client.get("/admin/console")
            self.assertEqual(r.status_code, 200)
            self.assertIn("<th>Build</th>", r.text)
            self.assertIn("Deployments", r.text)
        finally:
            os.environ.pop("OIKONOME_ADMIN_TOKEN", None)


class ConsoleBroadcastAndDrilldownTests(unittest.TestCase):
    """Broadcast set/clear (+ /api/me carries it), run-job validation,
    tenant drill-down page, host-state cards render without mounts."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        os.environ["OIKONOME_DEV"] = "1"
        os.environ["OIKONOME_ADMIN_TOKEN"] = ADMIN_TOKEN
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        cls.client.post("/admin/console/login", data={"token": ADMIN_TOKEN},
                        follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)

    def test_broadcast_set_reaches_me_and_clears(self):
        from oikonome.auth import sessions
        r = self.client.post("/admin/console/broadcast",
                             data={"message": "Maintenance at 9pm",
                                   "severity": "warn"})
        self.assertEqual(r.status_code, 200)
        # a tenant session sees it on /api/me
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        tid = str(tenancy.create_tenant(admin, f"bc-{uuid.uuid4().hex[:8]}"))
        u = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s,%s,'x') RETURNING id",
            (tid, f"bc-{uuid.uuid4().hex[:6]}@example.dev")).fetchone()
        tok = sessions.create_session(admin, u["id"], tid, "ua")
        admin.close()
        from fastapi.testclient import TestClient as TC

        import oikonome.web.app as appmod
        tenant_client = TC(appmod.app)
        tenant_client.cookies.set(sessions.COOKIE_NAME, tok)
        me = tenant_client.get("/api/me").json()
        self.assertEqual(me["broadcast"]["message"], "Maintenance at 9pm")
        self.assertEqual(me["broadcast"]["severity"], "warn")
        self.client.post("/admin/console/broadcast", data={"message": ""})
        self.assertIsNone(tenant_client.get("/api/me").json()["broadcast"])

    def test_run_job_rejects_unknown(self):
        r = self.client.post("/admin/console/run-job",
                             data={"job": "rm_rf_everything"})
        self.assertEqual(r.status_code, 400)

    def test_tenant_drilldown_renders(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        tid = str(tenancy.create_tenant(admin, f"dd-{uuid.uuid4().hex[:8]}"))
        admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s,%s,'x')", (tid, f"dd-{uuid.uuid4().hex[:6]}@e.dev"))
        admin.close()
        r = self.client.get(f"/admin/console/tenant/{tid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sync log", r.text)
        self.assertIn("Active sessions", r.text)
        r404 = self.client.get(f"/admin/console/tenant/{uuid.uuid4()}")
        self.assertEqual(r404.status_code, 404)

    def test_dashboard_renders_host_cards_without_mounts(self):
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Host &amp; backups", r.text)
        self.assertIn("Broadcast", r.text)
        self.assertIn("run sync sweep now", r.text)


class TenantDeleteTests(unittest.TestCase):
    """Console tenant-delete: typed-confirmation guard, full erasure,
    audited."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        os.environ["OIKONOME_DEV"] = "1"
        os.environ["OIKONOME_ADMIN_TOKEN"] = ADMIN_TOKEN
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        cls.client.post("/admin/console/login", data={"token": ADMIN_TOKEN},
                        follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)

    def _mk(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        tid = str(tenancy.create_tenant(admin, f"del-{uuid.uuid4().hex[:8]}"))
        email = f"del-{uuid.uuid4().hex[:6]}@example.dev"
        admin.execute("INSERT INTO users (tenant_id, email, password_hash) "
                      "VALUES (%s,%s,'x')", (tid, email))
        admin.close()
        return tid, email

    def _exists(self, tid):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            return admin.execute("SELECT 1 FROM tenants WHERE id=%s",
                                 (tid,)).fetchone() is not None
        finally:
            admin.close()

    def test_wrong_confirmation_refuses(self):
        tid, _ = self._mk()
        r = self.client.post("/admin/console/tenant-delete",
                             data={"tenant_id": tid, "confirm": "nope"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self._exists(tid))

    def test_correct_confirmation_deletes_and_audits(self):
        tid, email = self._mk()
        # immediate mode = the erase-now contract this test covers (the
        # default is the grace/scheduled path)
        r = self.client.post("/admin/console/tenant-delete",
                             data={"tenant_id": tid, "confirm": email,
                                   "mode": "immediate"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self._exists(tid))
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            row = admin.execute(
                "SELECT 1 FROM admin_audit WHERE action='tenant_delete' "
                "AND target=%s", (tid,)).fetchone()
        finally:
            admin.close()
        self.assertIsNotNone(row)


class HostStateCardTests(unittest.TestCase):
    """The Host card renders container status dots from host-health.json
    (OIKONOME_STATE_DIR override for tests)."""

    def test_containers_render_with_indicators(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "ops").mkdir()
            (Path(td) / "backups").mkdir()
            (Path(td) / "ops" / "host-health.json").write_text(json.dumps({
                "generated_at": "2026-07-18T00:00:00Z",
                "updates_pending": 2, "security_pending": 1,
                "reboot_required": True, "reboot_pkgs": "linux-image",
                "uptime_seconds": 3600, "disk_used_pct": 40,
                "containers": [
                    {"name": "oikonome-alpha-app-1", "state": "running",
                     "status": "Up 2 hours (healthy)"},
                    # clean exit (migrate one-shot / external-db postgres):
                    # STOPPED, must NOT be red
                    {"name": "oikonome-alpha-postgres-1", "state": "exited",
                     "status": "Exited (0) 2 hours ago"},
                    {"name": "oikonome-beta-worker-1", "state": "exited",
                     "status": "Exited (1) 5 minutes ago"}]}))
            os.environ["OIKONOME_STATE_DIR"] = td
            os.environ["OIKONOME_DEV"] = "1"
            os.environ["OIKONOME_ADMIN_TOKEN"] = ADMIN_TOKEN
            try:
                _ensure_db()
                import oikonome.web.app as appmod
                appmod.DEV_MODE = True
                from oikonome.web import security
                security._limiter._hits.clear()
                client = TestClient(appmod.app)
                client.post("/admin/console/login",
                            data={"token": ADMIN_TOKEN},
                            follow_redirects=False)
                r = client.get("/admin/console")
                self.assertEqual(r.status_code, 200)
                # rollup: alpha shows "1 up · 1 stopped" (app up, postgres
                # cleanly exited — NOT red); beta's crashed worker (Exited 1)
                # is the only one listed individually in red
                self.assertIn("oikonome-alpha", r.text)
                self.assertIn("1 stopped", r.text)
                self.assertNotIn("oikonome-alpha-postgres-1</b>", r.text)
                self.assertNotIn("oikonome-alpha-app-1</b>", r.text)
                self.assertIn("oikonome-beta-worker-1", r.text)
                self.assertIn("Exited (1)", r.text)
                self.assertIn("reboot required", r.text)
                self.assertIn("1 security", r.text)
            finally:
                os.environ.pop("OIKONOME_STATE_DIR", None)
                os.environ.pop("OIKONOME_ADMIN_TOKEN", None)


class HostRebootTests(unittest.TestCase):
    """Console reboot request: typed confirm, flag file + broadcast when
    configured, honest notice when the watcher dir is absent."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        os.environ["OIKONOME_DEV"] = "1"
        os.environ["OIKONOME_ADMIN_TOKEN"] = ADMIN_TOKEN
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        cls.client.post("/admin/console/login", data={"token": ADMIN_TOKEN},
                        follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)
        os.environ.pop("OIKONOME_STATE_DIR", None)

    def setUp(self):
        # host-reboot shares a tight admin_action bucket (5/h) — reset per
        # test so the class's own posts don't 429 each other
        from oikonome.web import security
        security._limiter._hits.clear()

    def _arm(self):
        import re
        r = self.client.post("/admin/console/host-reboot",
                             data={"stage": "arm"})
        self.assertIn("Yes — reboot the host now", r.text)
        m = re.search(r'name="nonce" value="([^"]+)"', r.text)
        self.assertIsNotNone(m, "armed page must carry a nonce")
        return m.group(1)

    def test_first_click_arms_only(self):
        self._arm()

    def test_go_without_valid_nonce_never_reboots(self):
        r = self.client.post("/admin/console/host-reboot",
                             data={"stage": "go", "nonce": "replayed"})
        self.assertIn("already used or expired", r.text)

    def test_unconfigured_notice(self):
        n = self._arm()
        os.environ["OIKONOME_STATE_DIR"] = "/nonexistent-state"
        try:
            r = self.client.post("/admin/console/host-reboot",
                                 data={"stage": "go", "nonce": n,
                                       "confirm": "reboot"})
            self.assertIn("Not configured", r.text)
        finally:
            os.environ.pop("OIKONOME_STATE_DIR", None)

    def test_writes_flag_redirects_and_nonce_is_single_use(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "cmd").mkdir()
            os.environ["OIKONOME_STATE_DIR"] = td
            try:
                n = self._arm()
                # PRG: success is a redirect — a refreshed/restored tab
                # replays a GET, never the action
                r = self.client.post("/admin/console/host-reboot",
                                     data={"stage": "go", "nonce": n,
                                           "confirm": "reboot"},
                                     follow_redirects=False)
                self.assertEqual(r.status_code, 303)
                self.assertIn("notice=rebooting", r.headers["location"])
                flag = Path(td) / "cmd" / "reboot-requested"
                self.assertTrue(flag.is_file())
                # replaying the SAME go post must refuse, not re-flag
                flag.unlink()
                r2 = self.client.post("/admin/console/host-reboot",
                                      data={"stage": "go", "nonce": n,
                                            "confirm": "reboot"})
                self.assertIn("already used or expired", r2.text)
                self.assertFalse(flag.exists())
            finally:
                os.environ.pop("OIKONOME_STATE_DIR", None)
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            bc = admin.execute("SELECT message FROM broadcast "
                               "WHERE id=1").fetchone()
            self.assertIn("Maintenance restart", bc["message"])
            admin.execute("DELETE FROM broadcast WHERE id=1")
            admin.commit()
        finally:
            admin.close()


class StaleBroadcastTests(unittest.TestCase):
    """/api/me never serves a reboot broadcast whose deadline is long
    past — the banner must not depend on the worker sweep winning its race
    against the reboot."""

    def test_me_hides_expired_reboot_broadcast(self):
        from oikonome.auth import sessions
        _ensure_db()
        os.environ["OIKONOME_DEV"] = "1"
        from oikonome.web.app import app
        client = TestClient(app)
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        tid = str(tenancy.create_tenant(admin, f"sb-{uuid.uuid4().hex[:8]}"))
        u = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s,%s,'x') RETURNING id",
            (tid, f"sb-{uuid.uuid4().hex[:6]}@example.dev")).fetchone()
        client.cookies.set(sessions.COOKIE_NAME,
                           sessions.create_session(admin, u["id"], tid, "ua"))
        try:
            admin.execute(
                """INSERT INTO broadcast (id, message, deadline)
                   VALUES (1, 'restart soon', now() - interval '10 minutes')
                   ON CONFLICT (id) DO UPDATE SET
                     message='restart soon',
                     deadline=now() - interval '10 minutes'""")
            admin.commit()
            self.assertIsNone(client.get("/api/me").json()["broadcast"])
            admin.execute("""UPDATE broadcast SET
                deadline = now() + interval '50 seconds'""")
            admin.commit()
            self.assertIsNotNone(client.get("/api/me").json()["broadcast"])
        finally:
            admin.execute("DELETE FROM broadcast WHERE id=1")
            admin.commit()
            admin.close()


class NightlyRebootTests(unittest.TestCase):
    """Graceful automated maintenance reboot: only with the env flag AND
    a pending host reboot; sets the countdown broadcast + drops the flag."""

    def _run(self, td, reboot_required, env_on=True):
        import json
        from pathlib import Path

        from oikonome.jobs.worker import _nightly_reboot_check
        (Path(td) / "ops").mkdir(exist_ok=True)
        (Path(td) / "cmd").mkdir(exist_ok=True)
        (Path(td) / "ops" / "host-health.json").write_text(json.dumps(
            {"reboot_required": reboot_required}))
        os.environ["OIKONOME_STATE_DIR"] = td
        if env_on:
            os.environ["OIKONOME_AUTO_REBOOT"] = "1"
        try:
            return _nightly_reboot_check()
        finally:
            os.environ.pop("OIKONOME_STATE_DIR", None)
            os.environ.pop("OIKONOME_AUTO_REBOOT", None)

    def test_noop_without_env(self):
        import tempfile
        _ensure_db()
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._run(td, True, env_on=False), "disabled")

    def test_noop_when_no_reboot_pending(self):
        import tempfile
        _ensure_db()
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._run(td, False), "not-needed")

    def test_fires_with_broadcast_and_flag(self):
        import tempfile
        from pathlib import Path
        _ensure_db()
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._run(td, True), "reboot-requested")
            self.assertTrue((Path(td) / "cmd" / "reboot-requested").is_file())
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            bc = admin.execute("SELECT message, deadline FROM broadcast "
                               "WHERE id=1").fetchone()
            self.assertIn("Nightly maintenance", bc["message"])
            self.assertIsNotNone(bc["deadline"])
            admin.execute("DELETE FROM broadcast WHERE id=1")
            admin.commit()
        finally:
            admin.close()


class AuthLockoutAndOpsCmdTests(unittest.TestCase):
    """Account-lockout scoping, TOTP replay, and ops-cmd directory
    permissions."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_account_login_lockout(self):
        """An account-scoped failure counter locks the account
        independent of source IP (the distributed-brute defense), and the
        login handler 429s a locked account before verifying."""
        import os as _os

        from oikonome.web import security
        email = f"lock-{uuid.uuid4().hex[:8]}@example.dev"
        security.clear_login_failures(email)
        # the account counter is IP-independent: below threshold → open
        for _ in range(security._ACCT_FAIL_MAX - 1):
            security.record_login_failure(email)
        self.assertFalse(security.account_login_locked(email))
        security.record_login_failure(email)          # crosses threshold
        self.assertTrue(security.account_login_locked(email))
        # the handler refuses a pre-locked account with 429 (not a 401 that
        # would let the brute keep sampling codes)
        _os.environ["OIKONOME_DEV"] = "1"
        from fastapi.testclient import TestClient

        from oikonome.web.app import app
        r = TestClient(app).post(
            "/api/login", data={"email": email, "password": "x"})
        self.assertEqual(r.status_code, 429)
        # a successful login clears the counter (legit user not penalized)
        security.clear_login_failures(email)
        self.assertFalse(security.account_login_locked(email))

    def test_wrong_password_cannot_lock_account_dos(self):
        """A WRONG PASSWORD must not feed the account lock —
        otherwise anyone who knows the email could DoS the owner. Only
        post-password second-factor failures count."""
        import os as _os

        from oikonome.auth import passwords
        from oikonome.web import security
        _os.environ["OIKONOME_DEV"] = "1"
        from fastapi.testclient import TestClient

        from oikonome.web.app import app
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        email = f"dos-{uuid.uuid4().hex[:8]}@example.dev"
        tid = str(tenancy.create_tenant(admin, "dos"))
        # account WITH totp enrolled (so the lock is even in play)
        admin.execute("INSERT INTO users (tenant_id, email, password_hash, "
                      "totp_secret) VALUES (%s,%s,%s,'enc:cp:x')",
                      (tid, email, passwords.hash_password("right-pw")))
        admin.close()
        security.clear_login_failures(email)
        c = TestClient(app)
        # 30 wrong-PASSWORD attempts (attacker knows only the email)
        for _ in range(30):
            security._limiter._hits.clear()   # bypass the per-IP limit
            c.post("/api/login", data={"email": email, "password": "wrong"})
        # the account is NOT locked — a wrong password never counted
        self.assertFalse(security.account_login_locked(email))
        security.clear_login_failures(email)

    def test_totp_replay_rejected(self):
        """verify_used refuses an already-consumed counter."""
        from oikonome.auth import totp
        secret = totp.new_secret()
        code = totp.code_now(secret)
        c = totp.verify_used(secret, code, None)
        self.assertIsNotNone(c)
        # same code, now that counter is "last used" → replay rejected
        self.assertIsNone(totp.verify_used(secret, code, c))

    def test_ops_cmd_not_world_writable(self):
        """install.sh defaults ops-cmd closed, and the root watcher
        installer chowns it 0700 (not 1777)."""
        import pathlib
        inst = pathlib.Path(__file__).resolve().parents[2] / "install.sh"
        self.assertNotIn("chmod 1777 ../ops-cmd", inst.read_text())
        self.assertIn("chmod 0700 ../ops-cmd", inst.read_text())
        w = (pathlib.Path(__file__).resolve().parents[2]
             / "scripts" / "install-host-reboot-watcher.sh").read_text()
        self.assertIn("chmod 0700", w)
        self.assertNotIn("chmod 1777", w)
        self.assertIn("last-console-reboot", w)   # loop-DoS guard


class SecretReencryptSweepTests(unittest.TestCase):
    """The boot sweep re-encrypts tenant secrets (items.access_token +
    config keys) written in plaintext while no master key was configured.
    Runs in a THROWAWAY DB so it can control the master-key env without
    disturbing the shared suite."""

    DB = "oikonome_test_reencrypt"

    @classmethod
    def setUpClass(cls):
        import os as _os

        import psycopg

        from oikonome.db import migrate

        from .util import _admin_dsn as dsn
        _ensure_db()
        with psycopg.connect(dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {cls.DB}")
            c.execute(f"CREATE DATABASE {cls.DB}")
        migrate.run(dsn(cls.DB))
        cls._saved = (tenancy.ADMIN_DSN, tenancy.APP_DSN,
                      _os.environ.get("OIKONOME_MASTER_KEY"))
        tenancy.ADMIN_DSN = dsn(cls.DB)
        tenancy.APP_DSN = (f"postgresql://oikonome_app:apppass@"
                           f"127.0.0.1:5433/{cls.DB}")

    @classmethod
    def tearDownClass(cls):
        import os as _os
        tenancy.ADMIN_DSN, tenancy.APP_DSN, mk = cls._saved
        if mk is None:
            _os.environ.pop("OIKONOME_MASTER_KEY", None)
        else:
            _os.environ["OIKONOME_MASTER_KEY"] = mk

    def test_plaintext_secrets_get_encrypted(self):
        import os as _os

        from cryptography.fernet import Fernet

        from oikonome.db import crypto, migrate
        from oikonome.engine import budget
        # a tenant with PLAINTEXT secrets (as if seeded before a key existed)
        _os.environ.pop("OIKONOME_MASTER_KEY", None)
        admin = tenancy.admin_connect()
        tid = str(tenancy.create_tenant(admin, "reenc"))
        admin.close()
        conn = tenancy.tenant_connect(tid)
        try:
            conn.execute("INSERT INTO items (id, aggregator, access_token) "
                         "VALUES ('it1','plaid','PLAINTEXT-TOKEN')")
            budget.save_config(conn, {"plaid_secret": "PLAINTEXT-SECRET",
                                      "food_monthly": 100})
        finally:
            conn.close()
        # now a master key is configured and the sweep runs
        _os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        n = migrate.reencrypt_tenant_secrets()
        self.assertGreaterEqual(n, 2)
        conn = tenancy.tenant_connect(tid)
        try:
            tok = conn.execute("SELECT access_token FROM items WHERE "
                               "id='it1'").fetchone()["access_token"]
            self.assertTrue(tok.startswith(crypto.PREFIX))
            self.assertEqual(crypto.decrypt(conn, tok), "PLAINTEXT-TOKEN")
            cfg = budget.load_config(conn)
            self.assertTrue(cfg["plaid_secret"].startswith(crypto.PREFIX))
            self.assertEqual(crypto.decrypt(conn, cfg["plaid_secret"]),
                             "PLAINTEXT-SECRET")
            # idempotent: a second run touches nothing
            self.assertEqual(migrate.reencrypt_tenant_secrets(), 0)
        finally:
            conn.close()


class PasskeyLoginTests(unittest.TestCase):
    """Passkey-only accounts block password-only login on hosted, and an
    empty TOTP code never feeds the account lock."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _user(self, totp=False, passkey=False):
        from oikonome.auth import passwords
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        email = f"pk-{uuid.uuid4().hex[:8]}@example.dev"
        tid = str(tenancy.create_tenant(admin, "pk"))
        uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, totp_secret) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (tid, email, passwords.hash_password("correct-pw"),
             "enc:cp:x" if totp else None)).fetchone()["id"]
        if passkey:
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, "
                "public_key, sign_count) VALUES (%s,%s,%s,0)",
                (uid, f"cred-{uuid.uuid4().hex}", "pk-cose"))
        admin.close()
        return email

    def test_passkey_only_password_login_blocked_hosted(self):
        """A passkey-only hosted account cannot complete password-only
        login — it must present the passkey (WebAuthn)."""
        import os as _os

        from oikonome.web import security
        email = self._user(totp=False, passkey=True)
        _os.environ["OIKONOME_DEV"] = "1"
        _os.environ["OIKONOME_HOSTED"] = "1"
        from fastapi.testclient import TestClient

        from oikonome.web.app import app
        try:
            security._limiter._hits.clear()
            r = TestClient(app).post("/api/login",
                                     data={"email": email,
                                           "password": "correct-pw"})
            self.assertEqual(r.status_code, 401)
            self.assertIn("passkey_required", r.text)
        finally:
            _os.environ.pop("OIKONOME_HOSTED", None)

    def test_passkey_only_login_allowed_selfhost(self):
        """Self-host (no forced-2FA): password login still works even with a
        passkey enrolled — a LAN install is its own trust domain."""
        import os as _os

        from oikonome.web import security
        email = self._user(totp=False, passkey=True)
        _os.environ["OIKONOME_DEV"] = "1"
        _os.environ.pop("OIKONOME_HOSTED", None)
        from fastapi.testclient import TestClient

        from oikonome.web.app import app
        security._limiter._hits.clear()
        r = TestClient(app).post("/api/login",
                                 data={"email": email, "password": "correct-pw"})
        self.assertEqual(r.status_code, 200)

    def test_empty_code_does_not_lock_account(self):
        """The two-phase form's first step (password, no code) must
        NOT count toward the account lock."""
        import os as _os

        from oikonome.web import security
        email = self._user(totp=True)
        security.clear_login_failures(email)
        _os.environ["OIKONOME_DEV"] = "1"
        from fastapi.testclient import TestClient

        from oikonome.web.app import app
        c = TestClient(app)
        for _ in range(20):
            security._limiter._hits.clear()
            c.post("/login", data={"email": email, "password": "correct-pw"})
        self.assertFalse(security.account_login_locked(email))
        security.clear_login_failures(email)


class CreateInstanceTests(unittest.TestCase):
    """The operator's create-instance action (tenant + owner + one-time
    set-password link) — the create half of the create/delete pair."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        os.environ["OIKONOME_DEV"] = "1"
        os.environ["OIKONOME_ADMIN_TOKEN"] = ADMIN_TOKEN
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        cls.client.post("/admin/console/login", data={"token": ADMIN_TOKEN},
                        follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)

    def test_creates_tenant_owner_and_reset_link(self):
        email = f"prov-{uuid.uuid4().hex[:8]}@example.dev"
        r = self.client.post("/admin/console/create-instance",
                             data={"email": email})
        self.assertEqual(r.status_code, 200)
        self.assertIn("/reset?token=", r.text)   # set-password link shown
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            row = admin.execute(
                "SELECT u.id, u.verified_at, u.tenant_id FROM users u "
                "WHERE u.email=%s", (email,)).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNotNone(row["verified_at"])   # operator-provisioned
            # a live set-password token exists
            n = admin.execute("SELECT count(*) AS n FROM password_resets "
                              "WHERE user_id=%s AND used_at IS NULL",
                              (row["id"],)).fetchone()["n"]
            self.assertEqual(n, 1)
            acts = [a["action"] for a in admin.execute(
                "SELECT action FROM admin_audit WHERE target=%s",
                (email,)).fetchall()]
            self.assertIn("create_instance", acts)
            # WELCOME mechanics — a provisioned user gets days, not the
            # 1-hour reset TTL (the link is their only way in)
            exp = admin.execute(
                "SELECT expires_at FROM password_resets "
                "WHERE user_id=%s AND used_at IS NULL",
                (row["id"],)).fetchone()["expires_at"]
            import datetime as _dt
            self.assertGreater(
                exp, _dt.datetime.now(_dt.timezone.utc)
                + _dt.timedelta(days=6))
        finally:
            admin.close()

    def test_duplicate_email_refused(self):
        email = f"dup-{uuid.uuid4().hex[:8]}@example.dev"
        self.client.post("/admin/console/create-instance",
                         data={"email": email})
        r = self.client.post("/admin/console/create-instance",
                             data={"email": email})
        self.assertIn("already has an account", r.text)


if __name__ == "__main__":
    unittest.main()
