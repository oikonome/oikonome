"""A household that is a demo must read as one in every process, from the
first moment its shared login can sign in.

The demo seed and the hourly reset run in the worker or the CLI, while the
doors that consult the flag run in the web process; a web process that
answered "not a demo" once must not keep that answer after another process
turned the household into a demo, and the seed must not create a login
that can sign in to a household not yet marked as a demo."""
import unittest
import uuid
from unittest import mock

from oikonome import demo
from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.web import demoguard

from .util import _ensure_db


class _FreshInstanceView:
    """An admin connection that reports no tenants or users, so the seed's
    fresh-instance check passes on the shared test database."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *a, **k):
        if sql.strip() in ("SELECT 1 FROM tenants LIMIT 1",
                           "SELECT 1 FROM users LIMIT 1"):
            return mock.Mock(fetchone=lambda: None)
        return self._conn.execute(sql, *a, **k)


class _Stop(Exception):
    pass


class DemoLockReachesEveryProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        demoguard.invalidate()

    def tearDown(self):
        demoguard.invalidate()

    def _tenant(self):
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"t-{uuid.uuid4().hex[:8]}")
        finally:
            admin.close()
        self.addCleanup(self._drop, tid)
        return tid

    @staticmethod
    def _drop(tid):
        admin = tenancy.admin_connect()
        try:
            tenancy.delete_tenant_rows(admin, tid)
        finally:
            admin.close()

    def test_a_flip_written_by_another_process_is_seen(self):
        tid = self._tenant()
        self.assertFalse(demoguard.is_demo(tid))
        # another process writes the flag: nothing here hears an invalidate
        conn = tenancy.tenant_connect(tid)
        try:
            with mock.patch.object(demoguard, "invalidate"):
                with budget.config_txn(conn) as cfg:
                    cfg["demo_mode"] = True
        finally:
            conn.close()
        later = demoguard.time.monotonic() + demoguard._NOT_DEMO_TTL + 1
        with mock.patch.object(demoguard.time, "monotonic",
                               return_value=later):
            self.assertTrue(demoguard.is_demo(tid))

    def test_the_seed_marks_the_demo_before_its_login_exists(self):
        email = f"demo-{uuid.uuid4().hex[:8]}@example.com"
        seen = {}

        def at_write(conn, *a, **k):
            tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS t"
            ).fetchone()["t"]
            seen["tid"] = tid
            seen["demo"] = bool(budget.load_config(conn).get("demo_mode"))
            raise _Stop

        admin = tenancy.admin_connect()
        try:
            with mock.patch.object(demo, "_write", side_effect=at_write):
                with self.assertRaises(_Stop):
                    demo._seed_locked(_FreshInstanceView(admin), seed=7,
                                      email=email, password="pw-demo-12345",
                                      years=1)
            owner = admin.execute(
                "SELECT tenant_id FROM users WHERE email=%s",
                (email,)).fetchone()
        finally:
            admin.close()
        self.addCleanup(self._drop, seen["tid"])
        self.assertIsNotNone(owner)
        self.assertTrue(seen["demo"])


if __name__ == "__main__":
    unittest.main()
