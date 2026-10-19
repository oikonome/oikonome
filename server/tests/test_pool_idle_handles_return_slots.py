"""An idle pooled connection still occupies one of the database server's
connection slots. After a burst, the pools must give those slots back
within a couple of minutes rather than the library's default ten, so a
quiet instance does not sit on slots it is not using."""
import unittest

from oikonome.db import tenancy

from .util import _admin_dsn, _ensure_db, TEST_DB


class PoolIdleTests(unittest.TestCase):
    def test_pools_close_idle_handles_within_two_minutes(self):
        self.assertLessEqual(tenancy.POOL_MAX_IDLE_S, 120.0)
        self.assertGreaterEqual(tenancy.POOL_MAX_IDLE_S, 30.0)

    def test_the_tenant_pool_carries_the_idle_clock(self):
        _ensure_db()
        p = tenancy._pool(_admin_dsn(TEST_DB))
        self.assertEqual(p.max_idle, tenancy.POOL_MAX_IDLE_S)
        c = tenancy._control_pool(_admin_dsn(TEST_DB))
        self.assertEqual(c.max_idle, tenancy.POOL_MAX_IDLE_S)


if __name__ == "__main__":
    unittest.main()
