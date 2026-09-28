"""A restored file import is still reversible.

Rows that came out of a file the person imported belong to a batch, and the
batch id rides in each row's `raw._batches` — which a restore carries, because
it carries `raw`. The batch RECORD is what Recent imports lists and what the
one-click undo acts on, so without it the restored rows keep claiming
membership of a batch that is not there: nothing lists the import, nothing can
take it back out, and the rows go on counting in every total. These rows come
from no feed, so re-syncing cannot correct a bad import either — the undo door
is the only way back, and a move between instances must not close it.
"""

import datetime as dt
import unittest

from oikonome.sync import base, batches, export, restore

from .util import make_db, write_config

DAY = dt.date(2026, 6, 4)


class ImportHistorySurvivesARestoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _imported(self) -> str:
        """A finished import of two rows, tagged as the importers tag."""
        bid = batches.create(self.conn, "csv", "chk", "spring-statement.csv")
        txns = batches.tag(self.conn, [
            base.Transaction(id="imp-1", account_id="chk", date=DAY,
                             name="JUNIPER MARKET", amount=18.40),
            base.Transaction(id="imp-2", account_id="chk", date=DAY,
                             name="HARBOR LIGHTS CAFE", amount=6.25),
        ], bid)
        base.upsert_transactions(self.conn, txns)
        batches.finish(self.conn, bid, len(txns))
        return bid

    def test_the_restored_import_is_listed_and_can_be_taken_back_out(self):
        bid = self._imported()
        data = export.build_zip(self.conn)
        dest = make_db()
        try:
            counts = restore.restore_zip(dest, data)
            self.assertTrue(counts.get("import_batches"))
            listed = [b for b in batches.recent(dest) if b["id"] == bid]
            self.assertEqual(len(listed), 1, "the import is not listed")
            self.assertEqual(listed[0]["filename"], "spring-statement.csv")
            self.assertEqual(listed[0]["row_count"], 2)
            # the door still works, and takes exactly this batch's rows
            self.assertEqual(int(batches.rollback(dest, bid)), 2)
            self.assertEqual(dest.execute(
                "SELECT count(*) AS n FROM transactions "
                " WHERE id LIKE 'imp-%'").fetchone()["n"], 0)
        finally:
            dest.close()

    def test_a_restore_carries_the_batch_ids_the_rows_are_tagged_with(self):
        """The tags and the record have to arrive together — either alone is
        a half-truth about where the rows came from."""
        bid = self._imported()
        data = export.build_zip(self.conn)
        dest = make_db()
        try:
            restore.restore_zip(dest, data)
            row = dest.execute(
                "SELECT raw->'_batches' AS b, raw->>'_batch' AS latest "
                "  FROM transactions WHERE id='imp-1'").fetchone()
            self.assertEqual(row["b"], [bid])
            self.assertEqual(row["latest"], bid)
        finally:
            dest.close()

    def test_re_restoring_the_same_archive_adds_no_second_record(self):
        bid = self._imported()
        data = export.build_zip(self.conn)
        dest = make_db()
        try:
            restore.restore_zip(dest, data)
            counts = restore.restore_zip(dest, data)
            self.assertFalse(counts.get("import_batches"))
            self.assertEqual(len([b for b in batches.recent(dest)
                                  if b["id"] == bid]), 1)
        finally:
            dest.close()


if __name__ == "__main__":
    unittest.main()
