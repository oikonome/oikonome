"""The Amazon/Costco chain lock and the merchant-classify lock are session
advisory locks on a pooled tenant connection, and the store push doors take
them inside an outer transaction. When a statement under the lock fails,
that transaction is aborted and a bare unlock cannot run: the failure it
raises would replace the real one, and the connection would go back to the
pool still holding the household's lock — every later chain for that
household skipped, the nightly waiting and giving up, until the backend
died. Released through tenancy.release_lock, the real error surfaces and
the lock never outlives the failure.
"""
import time
import unittest

import psycopg

from oikonome.db import tenancy
from oikonome.engine import llm_categorize

from .util import make_db

_HELD = ("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory'"
         " AND objid::bigint = (hashtext(%s)::bigint & 4294967295)")


class FailedChainNeverPoolsItsLockTests(unittest.TestCase):
    def setUp(self):
        self.probe = make_db()
        self.addCleanup(self.probe.close)
        self.tid = self.probe.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def _held(self, key) -> int:
        c = tenancy.control_connect()
        try:
            return c.execute(_HELD, (key,)).fetchone()["n"]
        finally:
            c.close()

    def _fail_under(self, lock, key):
        conn = tenancy.tenant_connect(self.tid)
        try:
            with self.assertRaises(psycopg.errors.DivisionByZero):
                with conn.transaction():
                    with lock(conn) as got:
                        self.assertTrue(got)
                        self.assertEqual(self._held(key), 1)
                        conn.execute("SELECT 1 / 0")
        finally:
            conn.close()
        # a dropped backend exits asynchronously: the invariant is that the
        # lock dies with it, not that it is gone before the next statement
        deadline = time.monotonic() + 5
        while self._held(key):
            self.assertLess(time.monotonic(), deadline,
                            f"{key} outlived the failed chain")
            time.sleep(0.05)

    def test_the_store_chain_lock(self):
        self._fail_under(llm_categorize.amazon_chain_lock,
                         f"oikonome:amazon:{self.tid}")

    def test_the_merchant_classify_lock(self):
        self._fail_under(llm_categorize.merchant_classify_lock,
                         f"oikonome:llmclassify:{self.tid}")

    def test_a_clean_run_releases_and_keeps_the_connection(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            with llm_categorize.amazon_chain_lock(conn) as got:
                self.assertTrue(got)
            self.assertEqual(conn.execute("SELECT 1 AS x").fetchone()["x"], 1)
            self.assertEqual(self._held(f"oikonome:amazon:{self.tid}"), 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
