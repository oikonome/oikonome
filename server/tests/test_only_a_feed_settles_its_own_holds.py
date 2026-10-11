"""A row can settle only a hold on its own account, and never from a file.

When a pending charge posts, the aggregator names the hold it replaces
(`raw.pending_transaction_id`), and the sync retires the hold and moves
everything a person put on it to the posted row. `raw` is whatever the
source sent, though: for a file import it is the uploaded columns, for a
collector push it is the request body. Believed from there, a push token or
a crafted column could retire any live hold in the household and pull the
notes, receipts and pairings of any row it could name onto its own.
"""
import unittest

from oikonome.sync import base as sync_base, csvimport
from oikonome.sync.base import Transaction

from .util import TODAY, make_db, write_config


class OnlyAFeedSettlesItsOwnHolds(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _row(self, tid, account, *, pending=False, raw=None, amount=40.0):
        sync_base.upsert_transactions(self.conn, [Transaction(
            id=tid, account_id=account, date=TODAY, amount=amount,
            name="CORNER CAFE", pending=pending, raw=raw or {})])

    def _note(self, tid, note):
        self.conn.execute("INSERT INTO transaction_notes (txn_id, note) "
                          "VALUES (%s,%s)", (tid, note))

    def _state(self, tid):
        r = self.conn.execute(
            "SELECT removed, (SELECT note FROM transaction_notes n "
            "WHERE n.txn_id = t.id) AS note FROM transactions t "
            "WHERE id=%s", (tid,)).fetchone()
        return r["removed"], r["note"]

    def test_a_feed_settles_its_own_hold(self):
        self._row("feed:p1", "chk", pending=True)
        self._note("feed:p1", "team lunch")
        self._row("feed:s1", "chk", raw={"pending_transaction_id": "feed:p1"})
        # retired, and the note copied onto the posted row
        self.assertEqual(self._state("feed:p1"), (1, "team lunch"))
        self.assertEqual(self._state("feed:s1"), (0, "team lunch"))

    def test_a_row_on_another_account_cannot_settle_a_hold(self):
        self._row("feed:p2", "chk", pending=True)
        self._note("feed:p2", "team lunch")
        self._row("push:x", "card", raw={"pending_transaction_id": "feed:p2"})
        self.assertEqual(self._state("feed:p2"), (0, "team lunch"))
        self.assertEqual(self._state("push:x"), (0, None))

    def test_a_posted_row_is_never_taken_as_a_hold(self):
        self._row("feed:done", "chk")
        self._note("feed:done", "receipt kept")
        self._row("push:y", "chk", raw={"pending_transaction_id": "feed:done"})
        self.assertEqual(self._state("feed:done"), (0, "receipt kept"))
        self.assertEqual(self._state("push:y"), (0, None))

    def test_an_uploaded_column_cannot_settle_a_hold(self):
        self._row("feed:p3", "chk", pending=True)
        self._note("feed:p3", "team lunch")
        csvimport.import_csv(
            self.conn, "chk",
            "Date,Amount,Description,pending_transaction_id\n"
            f"{TODAY.isoformat()},-12.00,BOOKSHOP,feed:p3\n",
            {"date": "Date", "amount": "Amount", "name": "Description"})
        self.assertEqual(self._state("feed:p3"), (0, "team lunch"))
        moved = self.conn.execute(
            "SELECT count(*) AS n FROM transaction_notes n "
            "JOIN transactions t ON t.id = n.txn_id "
            "WHERE t.id LIKE 'csv:%'").fetchone()["n"]
        self.assertEqual(moved, 0)


if __name__ == "__main__":
    unittest.main()
