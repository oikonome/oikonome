"""A reconnect backfill dropped against an imported row keeps that row
through an import undo.

After a restore or a re-link, the adoption guard drops the part of the new
link's backfill the account already holds: everything well before the
cutoff on the date alone, and the seam through the matcher, carrying the
incoming id onto the row it matched. When what holds the charge is a
file-imported row, the feed's cursor has moved past its own copy, so the
imported row is the only one left; it carries the same mark the
import-overlap guard leaves, and undoing the import keeps it.
"""
import datetime as dt
import unittest

from oikonome.sync import adopt, base, batches

from .test_reconnect_onto_restored import _Txn, _account, _item
from .util import make_db


class BackfillMarksFileRowsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", "restored", "ins_1", "Acme")
        _account(self.conn, "old-acct", "old-item", mask="1234")
        self.batch = batches.create(self.conn, "csv", "old-acct", "a.csv")
        rows = batches.tag(self.conn, [
            base.Transaction(id="csv:old", account_id="old-acct",
                             date=dt.date(2026, 1, 5), amount=10.0,
                             name="POS DEBIT 4417"),
            base.Transaction(id="csv:seam", account_id="old-acct",
                             date=dt.date(2026, 6, 1), amount=20.0,
                             name="SAFEWAY STORE 123"),
            base.Transaction(id="csv:other", account_id="old-acct",
                             date=dt.date(2026, 3, 1), amount=99.0,
                             name="UNRELATED")], self.batch)
        base.upsert_transactions(self.conn, rows)
        batches.finish(self.conn, self.batch, 3)
        _item(self.conn, "new-item", "ok", "ins_1", "Acme",
              token="live-token")
        adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_1",
            institution_name="Acme",
            incoming=[{"account_id": "new-acct", "mask": "1234",
                       "type": "depository"}])

    def _marked(self, tid):
        r = self.conn.execute("SELECT raw FROM transactions WHERE id = %s",
                              (tid,)).fetchone()
        return r is not None and base.STOOD_IN_KEY in (r["raw"] or {})

    def test_a_date_dropped_backfill_row_marks_the_file_row(self):
        kept, stats = adopt.filter_incoming(self.conn, "new-acct", [
            _Txn("new-acct", dt.date(2026, 1, 6), 10.0, "COFFEE",
                 id="plaid-old")])
        self.assertEqual((kept, stats["dropped_before_cutoff"]), ([], 1))
        self.assertTrue(self._marked("csv:old"))
        self.assertFalse(self._marked("csv:other"))
        res = batches.rollback(self.conn, self.batch)
        self.assertEqual(res["kept"], 1)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id = 'csv:old'").fetchone())

    def test_a_seam_row_carried_onto_a_file_row_keeps_it_on_undo(self):
        kept, stats = adopt.filter_incoming(self.conn, "new-acct", [
            _Txn("new-acct", dt.date(2026, 6, 1), 20.0, "SAFEWAY",
                 id="plaid-safeway")])
        self.assertEqual((kept, stats["carried_plaid_ids"]), ([], 1))
        self.assertTrue(self._marked("plaid-safeway"))
        batches.rollback(self.conn, self.batch)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id = 'plaid-safeway'"
        ).fetchone(), "the feed's own identity went with the undo")

    def test_a_backfill_that_drops_nothing_marks_nothing(self):
        adopt.filter_incoming(self.conn, "new-acct", [
            _Txn("new-acct", dt.date(2026, 7, 20), 4.0, "LATER",
                 id="plaid-later")])
        for tid in ("csv:old", "csv:seam", "csv:other"):
            self.assertFalse(self._marked(tid))


if __name__ == "__main__":
    unittest.main()
