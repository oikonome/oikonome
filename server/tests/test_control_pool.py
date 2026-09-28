"""Control-plane connection pool + load-shed 503.

Under load, a per-request unpooled TLS connect in web/app._control_conn
would contend with a managed Postgres connection cap. These tests pin the
shape that avoids it: control-plane queries come from a
shared psycopg_pool pool (reuse, no per-request connect), the pool sizes
respect the documented connection budget, and a starved control pool sheds
load with a 503 + Retry-After instead of hanging — the signal an edge
maintenance page understands.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db


class ControlPoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.client = TestClient(appmod.app)
        email = f"cpool-{uuid.uuid4().hex[:8]}@example.dev"
        r = cls.client.post("/api/signup",
                            data={"email": email,
                                  "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text

    # ---- pooled reuse ------------------------------------------------------

    def test_control_conn_is_pooled_and_reused(self):
        """Sequential checkouts reuse pooled connections instead of
        connecting per request. Asserted via the pool's own
        connection counter: repeated checkouts open ZERO new connections
        once the pool is warm."""
        c = self.appmod._control_conn()      # warm the pool
        pool = tenancy._control_pool(tenancy.APP_DSN)
        c.close()
        before = pool.get_stats().get("connections_num", 0)
        for _ in range(5):
            with self.appmod._control_conn() as conn:
                conn.execute("SELECT 1")
        self.assertEqual(pool.get_stats().get("connections_num", 0), before)

    def test_close_returns_not_disconnects(self):
        conn = tenancy.control_connect()
        raw = conn._conn
        conn.close()
        self.assertFalse(raw.closed)      # alive in the pool, not torn down
        conn.close()                      # double-close is a no-op
        # and the wrapper still context-manages like the old raw conn
        with tenancy.control_connect() as c:
            row = c.execute("SELECT 1 AS one").fetchone()
        self.assertEqual(row["one"], 1)

    def test_requests_share_pool_connections(self):
        """A burst of authenticated requests must not grow the control pool
        past its cap — pooled checkouts, not one connect per request."""
        pool = tenancy._control_pool(tenancy.APP_DSN)
        for _ in range(8):
            self.assertEqual(self.client.get("/api/me").status_code, 200)
        self.assertLessEqual(pool.get_stats()["pool_size"], pool.max_size)

    # ---- budget ------------------------------------------------------------

    def test_pool_sizes_respect_the_connection_budget(self):
        """Defaults per the budget comment in tenancy.py: 10 tenant + 4
        control = 14 for the web process, leaving room under a small
        managed-database connection limit — which also serves the
        provider's own agents — for the worker and some headroom."""
        tenant_max = tenancy._pool(tenancy.APP_DSN).max_size
        control_max = tenancy._control_pool(tenancy.APP_DSN).max_size
        self.assertEqual(tenant_max, 10)
        self.assertEqual(control_max, 4)
        self.assertLessEqual(tenant_max + control_max, 16)

    def test_control_pool_max_env_tunable(self):
        import os
        dsn = tenancy.APP_DSN + "?application_name=cpool-env-test" \
            if "?" not in tenancy.APP_DSN else \
            tenancy.APP_DSN + "&application_name=cpool-env-test"
        os.environ["OIKONOME_CONTROL_POOL_MAX"] = "2"
        try:
            p = tenancy._control_pool(dsn)
            self.assertEqual(p.max_size, 2)
        finally:
            os.environ.pop("OIKONOME_CONTROL_POOL_MAX", None)
            p = tenancy._control_pools.pop(dsn, None)
            if p is not None:
                p.close(timeout=1)

    # ---- metrics -----------------------------------------------------------

    def test_reqmetrics_summary_includes_control_pool(self):
        from oikonome.web import reqmetrics
        tenancy._control_pool(tenancy.APP_DSN)     # ensure it exists
        stats = reqmetrics._pool_stats()
        self.assertIn("cpool_size", stats)
        self.assertIn("cpool_timeouts", stats)
        # the aggregate pool_* columns fold the control pool in
        self.assertGreaterEqual(stats["pool_size"] or 0,
                                stats["cpool_size"] or 0)

    # ---- tenant pool fast-fail ---------------------------------------------

    def test_tenant_pool_timeout_default(self):
        self.assertEqual(tenancy.TENANT_POOL_TIMEOUT, 5.0)

    def test_starved_tenant_pool_fast_fails(self):
        """tenant_connect uses a SHORT checkout timeout (symmetric with
        the control pool), not psycopg_pool's 30 s default — a starved tenant
        pool raises PoolTimeout fast so the web layer can shed load instead of
        pinning a threadpool worker for 30 s."""
        import time

        from psycopg_pool import PoolTimeout
        pool = tenancy._pool(tenancy.APP_DSN)
        # taken one at a time inside the try: a connection some earlier test
        # never gave back makes this loop time out, and what it did take
        # must still go back or every later test starves with it
        held = []
        old = tenancy.TENANT_POOL_TIMEOUT
        try:
            for _ in range(pool.max_size):
                held.append(pool.getconn(timeout=10))
            tenancy.TENANT_POOL_TIMEOUT = 0.2
            t0 = time.perf_counter()
            with self.assertRaises(PoolTimeout):
                tenancy.tenant_connect(uuid.uuid4())
            self.assertLess(time.perf_counter() - t0, 5.0)   # nowhere near 30 s
        finally:
            tenancy.TENANT_POOL_TIMEOUT = old
            for c in held:
                pool.putconn(c)

    # ---- load shed ---------------------------------------------------------

    def test_starved_control_pool_sheds_503(self):
        """Every control conn checked out + a short timeout → requests get
        an immediate-ish 503 with Retry-After (API JSON, page HTML), not a
        30 s hang into a 500."""
        pool = tenancy._control_pool(tenancy.APP_DSN)
        held = []
        old_timeout = tenancy.CONTROL_POOL_TIMEOUT
        try:
            for _ in range(pool.max_size):
                held.append(pool.getconn(timeout=10))
            tenancy.CONTROL_POOL_TIMEOUT = 0.2
            r = self.client.get("/api/me")
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.headers.get("Retry-After"), "15")
            self.assertIn("busy", r.json()["detail"])
            r = self.client.get("/", follow_redirects=False)
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.headers.get("Retry-After"), "15")
            self.assertIn("text/html", r.headers.get("content-type", ""))
        finally:
            tenancy.CONTROL_POOL_TIMEOUT = old_timeout
            for c in held:
                pool.putconn(c)
        # pool recovers once connections return
        self.assertEqual(self.client.get("/api/me").status_code, 200)


if __name__ == "__main__":
    unittest.main()
