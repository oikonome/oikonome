"""Every writer that retires a row does it under the lock every
reimbursement pairing write takes, taken before it writes any row.

A link reads its two rows as live, then commits its pair. Unlocked, a
retire landing between the two leaves the pair on a removed row: the
deposit leaves income as a transfer while the charge's live twin still
counts as full spend, and the pair drops out of every list, so nobody can
see it to unlink it. Under the lock a link in flight either commits first
or waits and finds the row gone. The twin retirement, the reconnect's
pending sweep and the re-anchor carry are held to that here; the
pending→posted settlement is covered beside the sync code."""

import os
import threading
import time
import unittest

from oikonome.db import tenancy
from oikonome.sync import adopt, reanchor
from oikonome.web.data import _REIMB_LOCK

from . import util
from .util import _ensure_db, add_txn, make_db


def _app_dsn():
    return os.environ.get(
        "OIKONOME_TEST_DSN",
        f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{util.TEST_DB}")


class RetirementsWaitForPairingLockTests(unittest.TestCase):
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

    def _race(self, retire, write):
        """Run `retire` on the main connection while `write` holds the
        pairing lock in an open transaction on a second connection, then
        commit the write. Returns whether the retire had to wait."""
        errors = []
        done = threading.Event()

        def run():
            try:
                retire()
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

    def _deposit(self):
        return add_txn(self.conn, "2026-07-03", -40.0, "FRIEND PAYBACK",
                       account="chk", primary="INCOME")

    @staticmethod
    def _link(expense, deposit):
        def write(c):
            c.execute("INSERT INTO reimbursements (expense_id, reimburse_id,"
                      " partial) VALUES (%s,%s,0)", (expense, deposit))
        return write

    def _removed(self, txn_id):
        return self.conn.execute(
            "SELECT removed FROM transactions WHERE id=%s", (txn_id,)
        ).fetchone()["removed"]

    def test_twin_retirement_waits_for_a_link_in_flight(self):
        hold = add_txn(self.conn, "2026-07-01", 40.0, "CAFE", pending=1)
        posted = add_txn(self.conn, "2026-07-01", 40.0, "CAFE")
        dep = self._deposit()
        waited = self._race(lambda: adopt.dedupe_twins(self.conn, "card"),
                            self._link(hold, dep))
        self.assertTrue(waited)
        self.assertEqual(self._removed(hold), 1)
        self.assertEqual(self._removed(posted), 0)

    def test_reconnect_pending_sweep_waits_for_a_link_in_flight(self):
        hold = add_txn(self.conn, "2026-07-01", 40.0, "CAFE", pending=1)
        dep = self._deposit()

        def sweep():
            with self.conn.transaction():
                adopt._retire_orphaned_pending(self.conn, "card")

        waited = self._race(sweep, self._link(hold, dep))
        self.assertTrue(waited)
        self.assertEqual(self._removed(hold), 1)

    def test_a_retire_already_holding_the_lock_takes_it_again(self):
        """The lock is re-entrant within a session: a retire nested inside
        another locked write proceeds instead of waiting on itself."""
        hold = add_txn(self.conn, "2026-07-01", 40.0, "CAFE", pending=1)
        with self.conn.transaction():
            self.conn.execute(_REIMB_LOCK)
            self.assertEqual(
                adopt._retire_orphaned_pending(self.conn, "card"), 1)
        self.assertEqual(self._removed(hold), 1)

    def test_reanchor_carry_waits_for_the_pairing_lock(self):
        """The carry reads the retired row's pairs and moves them, so it
        waits for any pairing write holding the lock — not only one that
        happens to touch the same rows (a row lock would already queue
        that one) — and a pair written meanwhile is there when it looks."""
        old = add_txn(self.conn, "2026-07-01", 40.0, "CAFE")
        new = add_txn(self.conn, "2026-07-01", 40.0, "CAFE")
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s",
                          (old,))
        waited = self._race(lambda: reanchor.carry(self.conn, old, new),
                            lambda c: None)
        self.assertTrue(waited)
        self.assertEqual(self._removed(old), 1)

if __name__ == "__main__":
    unittest.main()
