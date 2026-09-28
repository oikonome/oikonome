"""The email sweep's single-flight lock is PER TENANT.

`_email_if_due` takes `pg_try_advisory_lock('oikonome:email:<tenant>')`
because the due-check and the heartbeat that suppresses it are two
statements — two overlapping hourly sweeps could both read "due" and the
household gets its daily verdict twice.

Nothing asserted the key was per tenant. A key that accidentally became
instance-wide would still pass a single-tenant test while quietly
serialising every tenant behind the first one, so on a busy hosted box most
households would simply not get their mail that hour.
"""

import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.jobs import worker

from .util import make_db


class EmailSweepLockTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()
        cls.tid = cls.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _hold(self, key: str):
        c = tenancy.tenant_connect(self.tid)
        got = c.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                        (key,)).fetchone()["ok"]
        self.assertTrue(got)
        return c

    def _release(self, c, key: str):
        c.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        c.close()

    def test_this_tenants_lock_blocks_this_tenants_sweep(self):
        key = f"oikonome:email:{self.tid}"
        held = self._hold(key)
        try:
            with mock.patch("oikonome.jobs.worker.emails_due") as due:
                worker._email_if_due(self.tid)
                due.assert_not_called()
        finally:
            self._release(held, key)

    def test_another_tenants_lock_does_not(self):
        """The point of the key carrying the tenant id."""
        other = f"oikonome:email:{'0' * 8}-0000-0000-0000-000000000000"
        held = self._hold(other)
        try:
            with mock.patch("oikonome.jobs.worker.emails_due",
                            return_value=[]) as due:
                worker._email_if_due(self.tid)
                due.assert_called_once()
        finally:
            self._release(held, other)

    def test_the_key_is_built_from_the_tenant_id(self):
        import inspect
        src = inspect.getsource(worker._email_if_due)
        self.assertIn('f"oikonome:email:{tenant_id}"', src)
