"""A restore takes the household's pairing and merchant-identity locks
before it writes its first row.

A restore writes the whole archive in one long transaction, and near its
end re-keys accounts and re-ids file-import rows under the pairing lock and
names merchants under the identity lock. Every other writer takes those
locks first and the rows second (a receipt attached to a row takes the
pairing lock and then that row). A restore that held a restated row while
it asked for the pairing lock would wait on an edit that, holding the
pairing lock, waits on that row: a cycle Postgres breaks by killing the
restore, after minutes of work. So with the pairing lock held elsewhere a
restore must queue before it has written anything.
"""

import datetime as dt
import unittest

from oikonome.db import tenancy
from oikonome.sync import export, restore

from .lock_order_util import Side, waits_on_a_lock
from .test_restore_after_link_merges_onto_the_live_account import (
    _account, _fresh_household, _item, _txn)
from .util import make_db

_REIMB = ("SELECT pg_advisory_xact_lock(hashtext(COALESCE("
          "current_setting('app.tenant_id', true), '')), "
          "hashtext('reimbursement-link'))")


def _archive() -> bytes:
    a = make_db()
    try:
        _item(a, "lk-item", token="tok-lk")
        _account(a, "lk-acct", "lk-item", "4321")
        _txn(a, "lk-row", "lk-acct", dt.date(2026, 4, 2), 18.0,
             "CORNER BAKERY")
        return export.build_zip(a)
    finally:
        a.close()


class ARestoreTakesTheHouseholdLocksFirst(unittest.TestCase):
    def test_restore_queues_on_the_pairing_lock_before_any_row(self):
        self._queues_before_any_row(lambda c: c.execute(_REIMB))

    def test_restore_queues_on_the_storage_lock_before_any_row(self):
        # a receipt snap holds storage, then pairing, then its row
        from oikonome.engine import storage_quota
        self._queues_before_any_row(lambda c: storage_quota.reserve(c, 1))

    def _queues_before_any_row(self, hold):
        data = _archive()
        b = _fresh_household("restore-lock-order")
        self.addCleanup(b.close)
        tid = b.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        holder = tenancy.tenant_connect(tid)
        self.addCleanup(holder.close)
        tx = holder.transaction()
        tx.__enter__()
        hold(holder)
        side = Side(tid, lambda c: restore.restore_zip(c, data))
        try:
            side.start()
            waiting = waits_on_a_lock(side.pid, "advisory", timeout=10)
            admin = tenancy.admin_connect()
            try:
                # a backend that has written a row holds its own xid
                wrote = admin.execute(
                    "SELECT 1 FROM pg_locks WHERE pid = %s"
                    " AND locktype = 'transactionid'",
                    (side.pid,)).fetchone()
            finally:
                admin.close()
        finally:
            tx.__exit__(None, None, None)
            self.assertTrue(side.finish())
        self.assertIsNone(side.error)
        self.assertTrue(waiting, "the restore never waited on the lock "
                                 "another writer held")
        self.assertIsNone(wrote, "the restore wrote rows before it took "
                                 "the lock")
        self.assertIsNotNone(b.execute(
            "SELECT 1 FROM transactions WHERE id = 'lk-row'").fetchone())


if __name__ == "__main__":
    unittest.main()
