"""Deleting a merchant nothing points at happens under the identity lock.

"Nothing points at this merchant" is only true for as long as nobody else
is resolving. A sync resolves the rows it just delivered; the nightly sweep
resolves the whole ledger; they are different single-flight domains, so the
two really do overlap. Under READ COMMITTED the other pass can already have
chosen a merchant this one is about to delete and not yet written the row
or the alias that would have held it alive — and once the delete lands, the
merchant_id it then writes names nothing at all.

The pass itself serialises on one advisory lock per household. The prune
has to take the same one, or it is the one statement in the module that
runs beside everybody.
"""

import unittest

import psycopg

from oikonome.db import tenancy
from oikonome.engine import merchant_identity

from .util import make_db


class PruningTakesTheIdentityLockTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        # a SECOND connection to the same household, so what it holds is
        # what a concurrent resolve in that household holds
        self.other = tenancy.tenant_connect(tid)
        self.conn.execute(
            "INSERT INTO merchants (name, name_source, kind) "
            "VALUES ('Harbor Lights', 'layer1', 'merchant')")

    def tearDown(self):
        self.other.close()
        self.conn.close()

    def test_the_prune_waits_while_another_pass_holds_the_lock(self):
        with self.other.transaction():
            merchant_identity._lock_identity(self.other)
            # a short fuse on the pruning side turns "waits" into
            # something a test can assert; RESET so the pooled connection
            # is handed back without one
            self.conn.execute("SET lock_timeout = '400ms'")
            try:
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    merchant_identity.prune_empty(self.conn)
            finally:
                self.conn.execute("RESET lock_timeout")
        # and once the other pass commits, the empty merchant goes
        self.assertEqual(merchant_identity.prune_empty(self.conn), 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM merchants").fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
