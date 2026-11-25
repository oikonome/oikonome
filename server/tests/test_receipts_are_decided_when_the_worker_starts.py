"""Receipts left undecided by an upgrade are decided when the worker starts.

The migration that taught receipts to be one slip left every stored
receipt undecided, and until the next sync or nightly decided them one
photo stored twice on a charge counted twice. The worker's startup
sweeps — the place post-upgrade backfills that need each household's own
connection run — decide that backlog (bounded, outside any request), so
the first page read after a deploy already counts each slip once. A
household with nothing undecided costs one probe.
"""

import asyncio
import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import receipt_identity, receipts
from oikonome.jobs import worker

from .util import add_txn, make_db, write_config


class StartupDecidesTheBacklog(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        txn = add_txn(self.conn, dt.date.today() - dt.timedelta(days=3),
                      40.0, "HARBOR NOODLE HOUSE")
        # one photo stored twice, as an older build stored it: undecided
        self.ids = [receipts.add(self.conn, txn, b"\x89PNG same photo",
                                 "image/png") for _ in range(2)]
        self.conn.execute("UPDATE receipts SET slip_decided_at = NULL")
        self.assertTrue(receipt_identity.has_pending(self.conn))

    def test_the_sweep_decides_every_household_backlog(self):
        with mock.patch.object(worker, "_tenant_ids",
                               return_value=[self.tid]):
            self.assertEqual(worker.decide_receipt_backlogs(), 2)
        self.assertFalse(receipt_identity.has_pending(self.conn))
        # a second boot finds nothing to do
        with mock.patch.object(worker, "_tenant_ids",
                               return_value=[self.tid]):
            self.assertEqual(worker.decide_receipt_backlogs(), 0)

    def test_the_worker_startup_runs_it(self):
        with mock.patch.object(worker, "_startup_grants_ready",
                               return_value=True), \
             mock.patch("oikonome.db.migrate.reencrypt_tenant_secrets",
                        return_value=0), \
             mock.patch.object(worker, "_tenant_ids",
                               return_value=[self.tid]):
            asyncio.new_event_loop().run_until_complete(worker._startup({}))
        self.assertFalse(receipt_identity.has_pending(self.conn))


if __name__ == "__main__":
    unittest.main()
