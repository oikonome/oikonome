"""A bulk recategorize takes its rows in one order — the row before its
pin, and the rows by id — so it waits for another category writer rather
than deadlocking with it.

Every single-row category writer (set_category, clear_category) locks the
`transactions` row before it touches the row's `manual_categories` pin.
A bulk write that upserted each pin first and then updated the row, in
whatever order the client listed the ids, held those locks the other way
round, across a whole selection and until its commit. A writer holding
one of the rows and wanting another the bulk already held formed a
cycle; Postgres broke it by killing one side, and when that was the bulk
the whole selection was lost to a server error."""

import threading
import time
import unittest

from oikonome.db import tenancy
from oikonome.web import data

from .util import TODAY, add_txn, make_db, write_config

PIN = """INSERT INTO manual_categories (transaction_id, category, set_at,
                                        bill_id)
         VALUES (%s, %s, now(), NULL)
         ON CONFLICT (tenant_id, transaction_id)
         DO UPDATE SET category = EXCLUDED.category"""


class BulkLockOrderTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        a = add_txn(self.conn, TODAY.replace(day=5), 10.0, "ACME")
        b = add_txn(self.conn, TODAY.replace(day=6), 20.0, "ACME")
        self.low, self.high = sorted([a, b])
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def _bulk_while_peer_holds(self, held: str, ids: list[str], then):
        """A peer locks `held` the way set_category does, a bulk over `ids`
        starts, and once the bulk has had time to take whatever it takes
        first the peer runs `then` and commits. Held open past Postgres's
        deadlock_timeout (1 s by default) so a crossed order is caught."""
        errors, threads = {}, []
        peer = tenancy.tenant_connect(self.tid)
        self.addCleanup(peer.close)

        def bulk():
            other = tenancy.tenant_connect(self.tid)
            try:
                data.bulk_apply(other, ids, "category", "GENERAL_MERCHANDISE")
            except Exception as e:                       # noqa: BLE001
                errors["bulk"] = type(e).__name__
            finally:
                other.close()

        try:
            with peer.transaction():
                peer.execute("SELECT 1 FROM transactions WHERE id=%s "
                             "FOR UPDATE", (held,))
                th = threading.Thread(target=bulk)
                threads.append(th)
                th.start()
                time.sleep(0.5)
                then(peer)
                time.sleep(1.5)
        except Exception as e:                           # noqa: BLE001
            errors["peer"] = type(e).__name__
        for th in threads:
            th.join(30)
        return errors

    def _override(self, tid):
        return self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_override"]

    def test_a_bulk_waits_for_a_single_row_edit_of_the_same_row(self):
        errors = self._bulk_while_peer_holds(
            self.low, [self.low],
            lambda p: p.execute(PIN, (self.low, "Groceries")))
        self.assertEqual(errors, {})
        self.assertEqual(self._override(self.low), "GENERAL_MERCHANDISE")

    def test_a_bulk_listed_newest_first_does_not_cross_an_id_ordered_writer(self):
        # The peer holds the lower id and next wants the higher one, the
        # order a set-based writer walking ids takes; the client listed the
        # selection highest first.
        errors = self._bulk_while_peer_holds(
            self.low, [self.high, self.low],
            lambda p: p.execute(
                "SELECT 1 FROM transactions WHERE id=%s FOR UPDATE",
                (self.high,)))
        self.assertEqual(errors, {})
        self.assertEqual(self._override(self.low), "GENERAL_MERCHANDISE")
        self.assertEqual(self._override(self.high), "GENERAL_MERCHANDISE")


if __name__ == "__main__":
    unittest.main()
