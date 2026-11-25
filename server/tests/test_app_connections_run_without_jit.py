"""Every connection the app opens runs with PostgreSQL's JIT off: the report
aggregates are priced high enough to be compiled, and the compile costs more
than the query it speeds up. Set at connect, so a pooled connection keeps it
after the pool resets it."""
import unittest

from oikonome.db import tenancy

from .util import TEST_DB, _admin_dsn, _ensure_db, make_db


class AppConnectionsRunWithoutJit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_the_tenant_connection_has_jit_off(self):
        conn = make_db()
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SHOW jit").fetchone()["jit"], "off")

    def test_the_admin_connection_has_jit_off(self):
        conn = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SHOW jit").fetchone()["jit"], "off")

    def test_the_control_options_carry_it(self):
        self.assertIn("jit=off", tenancy._control_options())


if __name__ == "__main__":
    unittest.main()
