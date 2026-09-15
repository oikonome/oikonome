"""Connection pool: tenant scope must never leak across checkouts of the
same physical connection."""

import unittest

from oikonome.db import tenancy

from .util import TODAY, add_txn, make_db, write_config


class PoolScopeTests(unittest.TestCase):
    def test_scope_reset_between_checkouts(self):
        c1 = make_db()
        write_config(c1)
        add_txn(c1, TODAY, 77.0, "POOL LEAK PROBE")
        t1 = c1.execute("SELECT current_setting('app.tenant_id') AS t"
                        ).fetchone()["t"]
        c1.close()                       # returns to pool, scope RESET
        # a raw pool checkout (no tenant) must see nothing
        pool = tenancy._pool(tenancy.APP_DSN)
        raw = pool.getconn()
        try:
            val = raw.execute(
                "SELECT current_setting('app.tenant_id', true) AS t"
            ).fetchone()["t"]
            self.assertIn(val, (None, ""))
            n = raw.execute("SELECT COUNT(*) AS n FROM transactions"
                            ).fetchone()["n"]
            self.assertEqual(n, 0)       # RLS: unscoped sees nothing
        finally:
            pool.putconn(raw)
        # a different tenant reusing pooled conns must not see tenant 1
        c2 = make_db()
        try:
            n = c2.execute("SELECT COUNT(*) AS n FROM transactions "
                           "WHERE name='POOL LEAK PROBE'").fetchone()["n"]
            self.assertEqual(n, 0)
            t2 = c2.execute("SELECT current_setting('app.tenant_id') AS t"
                            ).fetchone()["t"]
            self.assertNotEqual(t1, t2)
        finally:
            c2.close()

    def test_double_close_safe(self):
        c = make_db()
        c.close()
        c.close()                        # no-op, no crash


if __name__ == "__main__":
    unittest.main()
