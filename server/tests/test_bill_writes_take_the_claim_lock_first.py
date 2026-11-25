"""Every bill write holds the household's bill-claim lock, taken first.

Two writers that each check "does another bill already claim these
charges" before the other's bill is committed would both land, and the
plan would count the charge twice. The claim lock makes them take turns,
but only if every writer takes it, and takes it before any other lock in
its transaction. So the three writers that can make a bill active or
change what it claims (save_bill, rename_bill, restore_bill) enforce it:

- on an idle connection they take the lock themselves, first;
- inside a transaction that already holds it they carry on;
- inside a transaction that does not hold it they refuse before writing,
  because taking it that late would invert the lock order. The way in is
  bills.bill_write.
"""

import os
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import bills

from .util import TEST_DB, add_bill, make_db, write_config


_HELD_BY_PID = """SELECT EXISTS (
    SELECT 1 FROM pg_locks l,
           (SELECT hashtext(%s)::bigint AS h) k
     WHERE l.locktype = 'advisory' AND l.granted AND l.pid = %s
       AND l.objsubid = 1
       AND l.classid = ((k.h >> 32) & 4294967295)::oid
       AND l.objid = (k.h & 4294967295)::oid) AS held"""


class BillWritesTakeTheClaimLockFirstTests(unittest.TestCase):
    def setUp(self):
        self.a = make_db()
        write_config(self.a)
        self.tid = self.a.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.b = tenancy.tenant_connect(self.tid, os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}"))

    def tearDown(self):
        self.a.close()
        self.b.close()

    def _a_holds_claims(self) -> bool:
        """Seen from ANOTHER session, so the answer does not lean on the
        guard's own query."""
        return self.b.execute(
            _HELD_BY_PID, (bills.CLAIM_LOCK.format(self.tid),
                           self.a.info.backend_pid)).fetchone()["held"]

    def _probe(self, seen: list):
        def probe(*a, **kw):
            seen.append(self._a_holds_claims())
        return probe

    # -- idle connection: the writer takes the lock itself ---------------

    def test_an_idle_save_holds_the_claim_lock_while_it_writes(self):
        seen: list = []
        real = bills.lock_active_bills

        def ceiling(conn):
            seen.append(self._a_holds_claims())
            real(conn)
        with mock.patch.object(bills, "lock_active_bills", ceiling):
            add_bill(self.a, "Harbor Gym", 40.0)
        self.assertEqual(seen, [True])
        self.assertFalse(self._a_holds_claims(),
                         "the lock outlived the save's transaction")
        self.assertIsNotNone(bills.get_bill(self.a, "Harbor Gym"))

    def test_an_idle_rename_holds_the_claim_lock_while_it_writes(self):
        add_bill(self.a, "Harbor Gym", 40.0)
        seen: list = []
        real = bills.move_payee_config

        def moving(cfg, old, new):
            seen.append(self._a_holds_claims())
            return real(cfg, old, new)
        with mock.patch.object(bills, "move_payee_config", moving):
            self.assertEqual(
                bills.rename_bill(self.a, "Harbor Gym", "Harbor Fitness"), 1)
        self.assertEqual(seen, [True])
        self.assertFalse(self._a_holds_claims())

    def test_an_idle_restore_holds_the_claim_lock_while_it_writes(self):
        add_bill(self.a, "Harbor Gym", 40.0)
        bills.archive_bill(self.a, "Harbor Gym")
        seen: list = []
        real = bills.lock_active_bills

        def ceiling(conn):
            seen.append(self._a_holds_claims())
            real(conn)
        with mock.patch.object(bills, "lock_active_bills", ceiling):
            self.assertEqual(bills.restore_bill(self.a, "Harbor Gym"), 1)
        self.assertEqual(seen, [True])
        self.assertFalse(self._a_holds_claims())

    # -- a transaction without the lock: refused, nothing written --------

    def test_a_save_inside_a_transaction_without_the_claim_lock_is_refused(
            self):
        with self.assertRaisesRegex(RuntimeError, "bill-claim lock"):
            with self.a.transaction():
                add_bill(self.a, "Harbor Gym", 40.0)
        self.assertIsNone(bills.get_bill(self.a, "Harbor Gym"))

    def test_a_rename_inside_a_transaction_without_the_claim_lock_is_refused(
            self):
        add_bill(self.a, "Harbor Gym", 40.0)
        with self.assertRaisesRegex(RuntimeError, "bill-claim lock"):
            with self.a.transaction():
                bills.rename_bill(self.a, "Harbor Gym", "Harbor Fitness")
        self.assertIsNotNone(bills.get_bill(self.a, "Harbor Gym"))
        self.assertIsNone(bills.get_bill(self.a, "Harbor Fitness"))

    def test_a_restore_inside_a_transaction_without_the_claim_lock_is_refused(
            self):
        add_bill(self.a, "Harbor Gym", 40.0)
        bills.archive_bill(self.a, "Harbor Gym")
        with self.assertRaisesRegex(RuntimeError, "bill-claim lock"):
            with self.a.transaction():
                bills.restore_bill(self.a, "Harbor Gym")
        self.assertEqual(self.a.execute(
            "SELECT active FROM bills WHERE payee='Harbor Gym'"
        ).fetchone()["active"], 0)

    def test_a_late_bill_write_block_is_refused_too(self):
        """A bill_write opened inside a transaction that does not hold the
        lock would take it after whatever that transaction locked: the
        same inversion, hidden in a savepoint."""
        with self.assertRaisesRegex(RuntimeError, "bill-claim lock"):
            with self.a.transaction():
                with bills.bill_write(self.a):
                    pass

    # -- bill_write: the way in -------------------------------------------

    def test_every_writer_passes_inside_bill_write(self):
        add_bill(self.a, "Lanternfish Pool", 22.0)
        bills.archive_bill(self.a, "Lanternfish Pool")
        with bills.bill_write(self.a):
            add_bill(self.a, "Harbor Gym", 40.0)
            self.assertEqual(
                bills.rename_bill(self.a, "Harbor Gym", "Harbor Fitness"), 1)
            self.assertEqual(
                bills.restore_bill(self.a, "Lanternfish Pool"), 1)
            with bills.bill_write(self.a):     # nested: a savepoint
                add_bill(self.a, "Quillcast", 9.0)
        self.assertEqual(sorted(r["payee"] for r in self.a.execute(
            "SELECT payee FROM bills WHERE active=1")),
            ["Harbor Fitness", "Lanternfish Pool", "Quillcast"])

    def test_bill_write_holds_the_lock_for_the_block_and_lets_go_after(self):
        with bills.bill_write(self.a):
            self.assertTrue(self._a_holds_claims())
            self.a.execute("SELECT 1")
            self.assertTrue(self._a_holds_claims())
        self.assertFalse(self._a_holds_claims())

    def test_bill_write_lets_go_and_writes_nothing_on_an_exception(self):
        with self.assertRaises(ZeroDivisionError):
            with bills.bill_write(self.a):
                add_bill(self.a, "Harbor Gym", 40.0)
                self.assertTrue(self._a_holds_claims())
                1 / 0
        self.assertFalse(self._a_holds_claims())
        self.assertIsNone(bills.get_bill(self.a, "Harbor Gym"))

    def test_the_held_check_reads_negative_and_positive_hashes(self):
        """pg_locks splits the widened key into two unsigned halves; a
        negative hashtext sign-extends into the high half. The check has to
        find the lock either way, so prove it on households of both
        signs."""
        signs = set()
        for i in range(64):
            tid = f"sign-probe-{i}"
            h = self.a.execute("SELECT hashtext(%s) AS h",
                               (bills.CLAIM_LOCK.format(tid),)
                               ).fetchone()["h"]
            if (h < 0) in signs:
                continue
            signs.add(h < 0)
            with self.a.transaction():
                self.a.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", (tid,))
                self.assertFalse(self.a.execute(
                    bills._CLAIMS_HELD_SQL).fetchone()["held"])
                self.a.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                               (bills.CLAIM_LOCK.format(tid),))
                self.assertTrue(self.a.execute(
                    bills._CLAIMS_HELD_SQL).fetchone()["held"], h)
            if signs == {True, False}:
                break
        self.assertEqual(signs, {True, False})


if __name__ == "__main__":
    unittest.main()
