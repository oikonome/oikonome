"""The pending→posted settlement moves a held charge's reimbursement pair
and awaiting flag onto the posted row and retires the pending one under
the same lock every pairing write takes.

Without it, a link made while the sync is mid-settlement commits its pair
onto the pending row after the carry has already looked for pairs to move
and found none; the sync then retires that row. The pair ends up attached
to a removed transaction: the deposit is taken out of income as a transfer
while the posted charge still counts as full spend, and the pair drops
out of every list, so nobody can see it to unlink it. The same holds for
an awaiting-reimbursement flag, and for the rows a removal delta or a
vanished-pending sweep retires."""

import datetime as dt
import os
import threading
import time
import unittest

from oikonome.db import tenancy
from oikonome.sync import base
from oikonome.web.data import _REIMB_LOCK

from . import util
from .util import _ensure_db, add_txn, make_db


def _app_dsn():
    return os.environ.get(
        "OIKONOME_TEST_DSN",
        f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{util.TEST_DB}")


class SettlementWaitsForPairingLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.other = tenancy.tenant_connect(self.tid, _app_dsn())

    def tearDown(self):
        self.other.close()
        self.conn.close()

    def _race(self, sync, write):
        """Run `sync` on the main connection while `write` holds the
        pairing lock in an open transaction on a second connection, then
        commit the write. Returns whether the sync had to wait."""
        errors = []
        done = threading.Event()

        def run():
            try:
                sync()
            except Exception as e:                    # noqa: BLE001
                errors.append(e)
            finally:
                done.set()

        with self.other.transaction():
            self.other.execute(_REIMB_LOCK)
            write(self.other)
            t = threading.Thread(target=run)
            t.start()
            time.sleep(1.0)
            waited = not done.is_set()
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        return waited

    def test_link_committed_mid_settlement_follows_the_posted_row(self):
        pend = add_txn(self.conn, "2026-07-01", 40.0, "CAFE", pending=1)
        dep = add_txn(self.conn, "2026-07-03", -40.0, "FRIEND PAYBACK",
                      account="chk", primary="INCOME")
        posted = base.Transaction(
            id="posted-1", account_id="card", date=dt.date(2026, 7, 2),
            amount=40.0, name="CAFE",
            raw={"pending_transaction_id": pend})

        def link(c):
            c.execute("INSERT INTO reimbursements (expense_id, reimburse_id,"
                      " partial) VALUES (%s,%s,0)", (pend, dep))
            c.execute("INSERT INTO reimburse_flags (txn_id, partial)"
                      " VALUES (%s,0)", (pend,))

        waited = self._race(
            lambda: base.upsert_transactions(self.conn, [posted]), link)
        self.assertTrue(waited)
        self.assertEqual(
            [r["expense_id"] for r in self.conn.execute(
                "SELECT expense_id FROM reimbursements").fetchall()],
            ["posted-1"])
        self.assertEqual(
            [r["txn_id"] for r in self.conn.execute(
                "SELECT txn_id FROM reimburse_flags").fetchall()],
            ["posted-1"])
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id=%s", (pend,)
        ).fetchone()["removed"], 1)

    def test_removal_delta_waits_for_a_link_in_flight(self):
        """A removal delta retires the row and re-anchors what was written
        on it under the pairing lock, so a link in flight on that row
        commits before the re-anchor looks, or finds the row gone."""
        pend = add_txn(self.conn, "2026-07-01", 40.0, "CAFE", pending=1)
        dep = add_txn(self.conn, "2026-07-03", -40.0, "FRIEND PAYBACK",
                      account="chk", primary="INCOME")

        def link(c):
            c.execute("INSERT INTO reimbursements (expense_id, reimburse_id,"
                      " partial) VALUES (%s,%s,0)", (pend, dep))

        waited = self._race(lambda: base.mark_removed(self.conn, [pend]),
                            link)
        self.assertTrue(waited)

    def test_vanished_pending_sweep_waits_for_a_link_in_flight(self):
        pend = add_txn(self.conn, dt.date.today(), 40.0, "CAFE", pending=1,
                       txn_id="sf:pend-1")
        dep = add_txn(self.conn, dt.date.today(), -40.0, "FRIEND PAYBACK",
                      account="chk", primary="INCOME")

        def link(c):
            c.execute("INSERT INTO reimbursements (expense_id, reimburse_id,"
                      " partial) VALUES (%s,%s,0)", (pend, dep))

        waited = self._race(
            lambda: base.reconcile_vanished_pendings(
                self.conn, ["card"], set(), "sf:",
                dt.date.today() - dt.timedelta(days=30)), link)
        self.assertTrue(waited)


if __name__ == "__main__":
    unittest.main()
