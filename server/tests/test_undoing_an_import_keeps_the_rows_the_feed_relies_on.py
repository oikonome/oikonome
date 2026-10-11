"""Undoing a file import, or deleting the imported account, never loses a
bank-feed transaction.

When a bank is linked after its history was imported from a file, the
feed's pull sets aside every incoming row that a file-imported row already
represents, and the feed's cursor moves past it: the bank will not send
that row again. The file row is then the ledger's only copy of the charge.
So the file row is marked when it stands in, and an undo or a delete keeps
a marked row as an ordinary ledger row (no longer part of the import) and
says how many it kept. Nothing of the feed's row is stored.

Both deletes also run under the household's sync lock: a sync that read
the import rows before the delete and filtered against that stale view
would otherwise drop rows whose stand-in no longer exists.
"""
import datetime as dt
import unittest

from oikonome.db import tenancy
from oikonome.sync import base, batches

from .util import TEST_DB, _admin_dsn, make_db, write_config

DAY = dt.date(2026, 7, 10)


class MarkedRowsSurviveUndoTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES ('files', 'manual', 'Files')")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES ('filechk', 'files', 'Checking (file)', 'depository',"
            " 'checking')")
        self.batch = self._import("csv:one", 42.5, "CORNER GROCER")

    def tearDown(self):
        self.conn.close()

    def _import(self, tid, amount, name, day=DAY):
        batch = batches.create(self.conn, "csv", "filechk", "a.csv")
        rows = batches.tag(self.conn, [base.Transaction(
            id=tid, account_id="filechk", date=day, amount=amount,
            name=name)], batch)
        base.upsert_transactions(self.conn, rows)
        batches.finish(self.conn, batch, 1)
        return batch

    def _feed_row(self, tid="feed:one"):
        return base.Transaction(
            id=tid, account_id="chk", date=DAY + dt.timedelta(days=1),
            amount=42.5, name="CORNER GROCER 0042",
            merchant_name="Corner Grocer", raw={"transaction_id": tid})

    def _suppress(self, row):
        kept, skipped = base.filter_import_duplicates(self.conn, [row])
        self.assertEqual((kept, skipped), ([], 1))

    def _raw(self, tid):
        r = self.conn.execute("SELECT raw FROM transactions WHERE id = %s",
                              (tid,)).fetchone()
        return None if r is None else r["raw"]

    def test_standing_in_marks_the_file_row_and_stores_no_feed_data(self):
        self._suppress(self._feed_row())
        raw = self._raw("csv:one")
        self.assertEqual(raw[base.STOOD_IN_KEY],
                         {"source": "test", "accounts": ["chk"]})
        self.assertNotIn("_covers", raw)
        self.assertNotIn("feed:one", str(raw))
        # the next pull marks it again without rewriting it
        self.assertEqual(base.mark_stood_in(
            self.conn, {"csv:one": ["chk"]}, "test"), 0)

    def test_undo_keeps_a_row_the_feed_relies_on_and_says_so(self):
        self._suppress(self._feed_row())
        res = batches.rollback(self.conn, self.batch)
        self.assertEqual((int(res), res["kept"]), (0, 1))
        self.assertIn("1 row was kept: the bank feed already relies on it",
                      res.warning or "")
        raw = self._raw("csv:one")
        self.assertIsNotNone(raw, "the only copy of the charge was deleted")
        # an ordinary ledger row now: the batch reads as undone
        self.assertNotIn("_batches", raw)
        self.assertNotIn("_batch", raw)
        self.assertNotIn(self.batch,
                         [b["id"] for b in batches.recent(self.conn)])
        logged = self.conn.execute(
            "SELECT error FROM sync_log WHERE item_id = 'import-undo'"
        ).fetchone()
        self.assertIn("kept", logged["error"])

    def test_an_import_that_never_stood_in_is_undone_fully(self):
        res = batches.rollback(self.conn, self.batch)
        self.assertEqual((int(res), res["kept"], res.warning), (1, 0, None))
        self.assertIsNone(self._raw("csv:one"))

    def test_undo_deletes_unmarked_rows_and_keeps_marked_ones(self):
        batch = batches.create(self.conn, "csv", "filechk", "b.csv")
        rows = batches.tag(self.conn, [
            base.Transaction(id="csv:a", account_id="filechk", date=DAY,
                             amount=7.0, name="KIOSK"),
            base.Transaction(id="csv:b", account_id="filechk",
                             date=DAY + dt.timedelta(days=20), amount=8.0,
                             name="KIOSK")], batch)
        base.upsert_transactions(self.conn, rows)
        batches.finish(self.conn, batch, 2)
        feed = self._feed_row("feed:a")
        feed.amount, feed.name, feed.merchant_name = 7.0, "KIOSK 9", None
        self._suppress(feed)
        res = batches.rollback(self.conn, batch)
        self.assertEqual((int(res), res["kept"]), (1, 1))
        self.assertIsNotNone(self._raw("csv:a"))
        self.assertIsNone(self._raw("csv:b"))

    def test_importing_the_same_file_again_keeps_the_mark(self):
        self._suppress(self._feed_row())
        again = self._import("csv:one", 42.5, "CORNER GROCER")
        self.assertIn(base.STOOD_IN_KEY, self._raw("csv:one"))
        batches.rollback(self.conn, again)
        res = batches.rollback(self.conn, self.batch)
        self.assertEqual(res["kept"], 1)
        self.assertIsNotNone(self._raw("csv:one"))

    def test_deleting_the_imported_account_keeps_marked_rows(self):
        self._suppress(self._feed_row())
        self._import("csv:two", 3.0, "NEWSSTAND", DAY + dt.timedelta(days=30))
        out = base.purge_account_data(self.conn, "filechk")
        self.assertEqual(out["kept_transactions"], 1)
        self.assertEqual(out["transactions"], 1)
        self.assertIn("relies on", out["note"])
        self.assertIsNotNone(self._raw("csv:one"))
        self.assertIsNone(self._raw("csv:two"))
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM accounts WHERE id = 'filechk'").fetchone(),
            "the account holding the kept row was deleted")

    def test_deleting_an_unmarked_imported_account_deletes_it(self):
        out = base.purge_account_data(self.conn, "filechk")
        self.assertEqual((out["accounts"], out["transactions"],
                          out["kept_transactions"]), (1, 1, 0))

    def test_a_mark_whose_feed_goes_in_the_same_delete_protects_nothing(self):
        """Deleting a connection deletes its accounts together; a file row
        on one of them that stood in for that same connection's feed has
        nothing left relying on it."""
        self.conn.execute("UPDATE accounts SET item_id = 'it1'"
                          " WHERE id = 'filechk'")
        self._suppress(self._feed_row())
        out = base.purge_item_accounts(self.conn, "it1")
        self.assertEqual(out["kept_transactions"], 0)
        self.assertIsNone(self._raw("csv:one"))

    def test_a_mark_whose_feed_account_was_deleted_protects_nothing(self):
        self._suppress(self._feed_row())
        base.purge_account_data(self.conn, "chk")
        res = batches.rollback(self.conn, self.batch)
        self.assertEqual((int(res), res["kept"]), (1, 0))

    def test_a_relinked_feed_account_still_relies_on_the_row(self):
        """Re-linking the bank moves the feed account to the id the new link
        uses and records the adoption; the mark names the old id, and the
        feed lives on under the new one."""
        self._suppress(self._feed_row())
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES ('chk-new', 'it1', 'Checking', 'depository', 'checking')")
        self.conn.execute("UPDATE transactions SET account_id = 'chk-new'"
                          " WHERE account_id = 'chk'")
        self.conn.execute("DELETE FROM accounts WHERE id = 'chk'")
        self.conn.execute(
            "INSERT INTO account_adoptions (account_id, old_account_id,"
            " item_id) VALUES ('chk-new', 'chk', 'it1')")
        res = batches.rollback(self.conn, self.batch)
        self.assertEqual(res["kept"], 1)
        self.assertIsNotNone(self._raw("csv:one"))


class EarlierStoredCopiesTests(unittest.TestCase):
    """An earlier format kept a full copy of each feed row on the file row;
    the migration and the restore turn those into the mark."""

    def test_copies_become_the_mark(self):
        raw = {"_batches": ["b1"], "_covers": {
            "feed:1": {"id": "feed:1", "account_id": "chk", "amount": 4.0},
            "feed:2": {"id": "feed:2", "account_id": "card"},
            "junk": 7}}
        base.stood_in_from_legacy_copies(raw)
        self.assertEqual(raw, {"_batches": ["b1"], base.STOOD_IN_KEY: {
            "source": "feed", "accounts": ["card", "chk"]}})

    def test_no_copies_no_mark(self):
        raw = {"_covers": {}}
        base.stood_in_from_legacy_copies(raw)
        self.assertEqual(raw, {})


class DeletesWaitForTheSyncLockTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.batch = batches.create(self.conn, "csv", "card", "a.csv")
        rows = batches.tag(self.conn, [base.Transaction(
            id="csv:held", account_id="card", date=DAY, amount=9.0,
            name="KIOSK")], self.batch)
        base.upsert_transactions(self.conn, rows)
        batches.finish(self.conn, self.batch, 1)
        self.holder = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.holder.execute("SELECT pg_advisory_lock(hashtext(%s))",
                            (f"oikonome:sync:{self.tid}",))

    def tearDown(self):
        self.holder.close()
        self.conn.close()

    def _still_there(self):
        return self.conn.execute(
            "SELECT 1 FROM transactions WHERE id = 'csv:held'").fetchone()

    def test_undo_is_refused_while_a_sync_runs(self):
        with self.assertRaises(base.SyncRunning):
            batches.rollback(self.conn, self.batch)
        self.assertIsNotNone(self._still_there())

    def test_account_delete_is_refused_while_a_sync_runs(self):
        with self.assertRaises(base.SyncRunning):
            base.purge_account_data(self.conn, "card")
        self.assertIsNotNone(self._still_there())

    def test_undo_runs_once_the_sync_is_done(self):
        self.holder.execute("SELECT pg_advisory_unlock_all()")
        self.assertEqual(int(batches.rollback(self.conn, self.batch)), 1)


if __name__ == "__main__":
    unittest.main()
