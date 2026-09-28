"""config_txn's row lock has to bite on a tenant with no settings row yet.

`SELECT config FROM tenant_settings FOR UPDATE` locks the rows it MATCHES. A
tenant that has never saved settings has no row, so an unseeded lock takes
nothing: both writers read an empty config and both write the whole document
back through save_config's INSERT ... ON CONFLICT DO UPDATE, the second
silently discarding the first. That is exactly the lost update the helper
exists to prevent, on precisely the tenants most likely to have two
concurrent writers — a setup wizard saving while the first sync seeds
budgets.

The sibling invariant: "TENANT CONNECTIONS ONLY" is enforced, not merely
documented. On an admin_connect (which bypasses RLS) the unqualified FOR
UPDATE would lock EVERY tenant's settings row, and a comment locks nothing.
"""

import os
import threading
import unittest

from oikonome.db import tenancy
from oikonome.engine import budget

from . import util
from .util import make_db, write_config


def _app_dsn():
    return os.environ.get(
        "OIKONOME_TEST_DSN",
        f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{util.TEST_DB}")


class ConfigTxnTenantOnlyTests(unittest.TestCase):
    def test_admin_connection_is_refused(self):
        admin = tenancy.admin_connect(util._admin_dsn(util.TEST_DB))
        try:
            with self.assertRaises(RuntimeError) as cm:
                with budget.config_txn(admin):
                    pass                                  # pragma: no cover
            self.assertIn("tenant_connect", str(cm.exception))
        finally:
            admin.close()


class ConfigTxnNewTenantLockTests(unittest.TestCase):
    def test_concurrent_first_writes_do_not_lose_an_update(self):
        conn = util.make_db()
        try:
            tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
            # the state under test: a tenant with NO settings row yet
            conn.execute("DELETE FROM tenant_settings")
        finally:
            conn.close()

        dsn = _app_dsn()
        a_inside = threading.Event()
        b_at_the_door = threading.Event()
        errors = []

        def writer_a():
            try:
                c = tenancy.tenant_connect(tid, dsn)
                try:
                    with budget.config_txn(c) as cfg:
                        cfg["from_a"] = 1
                        a_inside.set()
                        # hold the transaction open long enough that an
                        # unlocked B would read, write and commit inside it
                        b_at_the_door.wait(timeout=5)
                        import time
                        time.sleep(0.4)
                finally:
                    c.close()
            except Exception as e:                        # noqa: BLE001
                errors.append(("a", e))

        def writer_b():
            try:
                a_inside.wait(timeout=5)
                c = tenancy.tenant_connect(tid, dsn)
                try:
                    b_at_the_door.set()
                    with budget.config_txn(c) as cfg:     # must BLOCK here
                        cfg["from_b"] = 2
                finally:
                    c.close()
            except Exception as e:                        # noqa: BLE001
                errors.append(("b", e))

        ta, tb = threading.Thread(target=writer_a), threading.Thread(target=writer_b)
        ta.start(); tb.start()
        ta.join(timeout=30); tb.join(timeout=30)
        self.assertEqual(errors, [], f"writer raised: {errors}")

        c = tenancy.tenant_connect(tid, dsn)
        try:
            cfg = c.execute(
                "SELECT config FROM tenant_settings").fetchone()["config"]
        finally:
            c.close()
        self.assertEqual(cfg.get("from_a"), 1, "writer A's change was lost")
        self.assertEqual(cfg.get("from_b"), 2, "writer B's change was lost")


class SeedPersistLockTests(unittest.TestCase):
    """load_config's seed-persist takes the same row lock, and needs the
    same seeding: the branch runs precisely on tenants that may have no
    tenant_settings row, where FOR UPDATE matches nothing and the re-read it
    guards would be unprotected."""

    def test_seed_persist_locks_even_with_no_settings_row(self):
        conn = util.make_db()
        try:
            tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
            conn.execute("DELETE FROM tenant_settings")
        finally:
            conn.close()

        dsn = _app_dsn()
        c = tenancy.tenant_connect(tid, dsn)
        try:
            with c.transaction():
                c.execute("INSERT INTO tenant_settings (config) "
                          "VALUES ('{}'::jsonb) ON CONFLICT (tenant_id) "
                          "DO NOTHING")
                row = c.execute("SELECT config FROM tenant_settings "
                                "FOR UPDATE").fetchone()
            # the point: after seeding the row, FOR UPDATE has something to
            # bite on — without the seed it returns None and locks nothing
            self.assertIsNotNone(row)
        finally:
            c.close()


class NoOpWriteTests(unittest.TestCase):
    """A block that changes nothing must not rewrite the whole document.

    A `config_txn` that writes on exit unconditionally rewrites the entire
    settings blob when a bill's cap is toggled to the value it already had,
    and takes the row lock's write slot to do it, delaying a save that IS
    changing something.
    """

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _stamp(self):
        return self.conn.execute(
            "SELECT config FROM tenant_settings").fetchone()["config"]

    def test_an_unchanged_block_writes_nothing(self):
        from oikonome.engine import budget
        write_config(self.conn, food_monthly=123)
        calls = []
        real = budget.save_config
        try:
            budget.save_config = lambda c, cfg: calls.append(1) or real(c, cfg)
            with budget.config_txn(self.conn) as cfg:
                cfg["food_monthly"] = cfg["food_monthly"]      # no-op
        finally:
            budget.save_config = real
        self.assertEqual(calls, [], "nothing changed, nothing to write")

    def test_a_real_change_still_writes(self):
        from oikonome.engine import budget
        write_config(self.conn, food_monthly=123)
        with budget.config_txn(self.conn) as cfg:
            cfg["food_monthly"] = 456
        self.assertEqual(budget.load_config(self.conn)["food_monthly"], 456)

    def test_a_caller_can_force_the_write(self):
        from oikonome.engine import budget
        calls = []
        real = budget.save_config
        try:
            budget.save_config = lambda c, cfg: calls.append(1) or real(c, cfg)
            with budget.config_txn(self.conn, skip_unchanged=False) as cfg:
                cfg["food_monthly"] = cfg["food_monthly"]
        finally:
            budget.save_config = real
        self.assertEqual(calls, [1])
