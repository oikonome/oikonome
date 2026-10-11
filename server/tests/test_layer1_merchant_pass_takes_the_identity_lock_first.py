"""The nightly layer-1 merchant pass, and the door that loads a reviewed
merchant map, take the merchant-identity lock before they write an alias
row.

The pass walks every raw merchant string in the ledger and refreshes the
alias row that maps it to a display name, all in one transaction, in the
order its query hands the strings back. A rename takes the identity lock
and then locks every alias row of the merchant at once, in whatever order
its own scan finds them. Two writers taking the same set of rows in two
different orders can each hold one the other wants: Postgres kills one of
them, and either the person's rename answers with an error or the night's
alias refresh rolls back. Every other writer of alias rows already holds
the identity lock; with the pass taking it too, the two take turns. The
reviewed-map door writes its rows one at a time in the map's order and
has the same hazard.
"""

import datetime as dt
import threading
import unittest
from unittest import mock

from oikonome.engine import merchant_dedup

from .lock_order_util import Gate, Side, deadlocks, deadlocks_since, \
    tenant_of, waits_on_a_lock
from .util import TODAY, add_txn, make_db, write_config

LINES = ("LANTERN BOOKS", "LANTERN BOOKS #0412", "SQ *LANTERN BOOKS",
         "TST* LANTERN BOOKS")


class Layer1PassLockOrderTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.tid = tenant_of(self.conn)
        for i, line in enumerate(LINES):
            add_txn(self.conn, TODAY - dt.timedelta(days=10 + i), 40.0, line,
                    merchant=None)
        merchant_dedup.apply(self.conn)
        self.display = self.conn.execute(
            f"SELECT DISTINCT {merchant_dedup.DISPLAY_MERCHANT} AS d"
            f"  FROM transactions t {merchant_dedup.MC_JOIN}").fetchone()["d"]
        # Rewriting the first string's alias row moves it to the end of the
        # table, so a scan of the table meets it last while the pass, which
        # sorts the strings, meets it first: the two orders a busy ledger
        # produces on its own.
        self.conn.execute("UPDATE merchant_canonical SET as_of = now()"
                          " WHERE raw_merchant = %s", (LINES[0],))

    def test_the_pass_meeting_a_rename_waits_for_it(self):
        gate = Gate()
        real = merchant_dedup.canonical_merchant
        pass_thread: list = []
        calls = [0]

        def cleaned(m, cities=()):
            # paused after the pass wrote its first alias row, before the
            # second: only on the pass's own thread
            if threading.current_thread() in pass_thread:
                calls[0] += 1
                if calls[0] == 2:
                    gate._pause()
            return real(m, cities)

        def rename(conn):
            with conn.transaction():
                return merchant_dedup.rename(conn, self.display,
                                             "Lantern Bookshop")

        layer1 = Side(self.tid, merchant_dedup.apply)
        pass_thread.append(layer1)
        renamer = Side(self.tid, rename)
        before = deadlocks()
        with mock.patch.object(merchant_dedup, "canonical_merchant", cleaned):
            layer1.start()
            self.assertTrue(gate.reached.wait(20), "the pass never started")
            renamer.start()
            self.assertTrue(waits_on_a_lock(renamer.pid),
                            "the rename never met the pass")
            gate.go.set()
            self.assertTrue(layer1.finish(), "the pass hung")
            self.assertTrue(renamer.finish(), "the rename hung")
        self.assertIsNone(layer1.error, layer1.error)
        self.assertIsNone(renamer.error, renamer.error)
        self.assertEqual(deadlocks_since(before), 0,
                         "the layer-1 pass and the rename deadlocked")

    def test_loading_a_reviewed_map_meeting_a_rename_waits_for_it(self):
        gate = Gate()
        writes = [0]

        class Paused:
            """The loader's connection, paused before its second alias
            row is written."""

            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *a, **kw):
                if "INSERT INTO merchant_canonical" in sql:
                    writes[0] += 1
                    if writes[0] == 2:
                        gate._pause()
                return self._conn.execute(sql, *a, **kw)

        # the map's order is the sorted one, the table's the other
        mapping = {line: "Lantern Books" for line in LINES}

        def rename(conn):
            with conn.transaction():
                return merchant_dedup.rename(conn, self.display,
                                             "Lantern Bookshop")

        loader = Side(self.tid, lambda c: merchant_dedup.load_llm_map(
            Paused(c), mapping))
        renamer = Side(self.tid, rename)
        before = deadlocks()
        loader.start()
        self.assertTrue(gate.reached.wait(20), "the loader never started")
        renamer.start()
        self.assertTrue(waits_on_a_lock(renamer.pid),
                        "the rename never met the loader")
        gate.go.set()
        self.assertTrue(loader.finish(), "the loader hung")
        self.assertTrue(renamer.finish(), "the rename hung")
        self.assertIsNone(loader.error, loader.error)
        self.assertIsNone(renamer.error, renamer.error)
        self.assertEqual(deadlocks_since(before), 0,
                         "the map load and the rename deadlocked")


if __name__ == "__main__":
    unittest.main()
