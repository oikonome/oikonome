"""The worker's startup sweeps wait for the admin role's table grants.

After a restore or a Postgres major-version migration the worker can boot
while the app container's migrate step — which re-applies the grants — is
still running. The startup hook must poll until the sweeps' own privileges
(DELETE on broadcast, SELECT on tenants) are in place, run the sweeps once
they are, and skip them — not crash, not run them anyway — when they never
arrive within the wait.
"""

import asyncio
import unittest
from unittest import mock

from oikonome.jobs import worker

from .util import _ensure_db


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class StartupGrantWaitTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        # shrink the wait so the timeout case doesn't sleep for a minute
        self._p = mock.patch.multiple(worker, STARTUP_GRANT_TRIES=3,
                                      STARTUP_GRANT_SLEEP=0.0)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_predicate_is_true_on_a_migrated_database(self):
        self.assertTrue(worker._startup_grants_ready())

    def test_predicate_false_when_the_tables_are_not_there(self):
        # a table the admin role can't even see (not yet migrated) must read
        # as "not ready", not raise out of the poll loop
        with mock.patch.object(worker.tenancy, "admin_connect") as ac:
            ac.return_value.execute.side_effect = RuntimeError("no such table")
            self.assertFalse(worker._startup_grants_ready())

    def test_sweeps_run_once_grants_arrive(self):
        ready = iter([False, False, True])
        with mock.patch.object(worker, "_startup_grants_ready",
                               side_effect=lambda: next(ready)) as g, \
             mock.patch("oikonome.db.migrate.reencrypt_tenant_secrets",
                        return_value=0) as reenc:
            _run(worker._startup({}))
        self.assertEqual(g.call_count, 3)
        reenc.assert_called_once()

    def test_sweeps_skipped_when_grants_never_arrive(self):
        with mock.patch.object(worker, "_startup_grants_ready",
                               return_value=False) as g, \
             mock.patch("oikonome.db.migrate.reencrypt_tenant_secrets") as reenc, \
             self.assertLogs(worker.log, level="WARNING") as cm:
            _run(worker._startup({}))
        self.assertEqual(g.call_count, 3)
        reenc.assert_not_called()
        self.assertTrue(any("startup sweeps skipped" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()
