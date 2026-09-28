"""Free-text per-transaction notes: set/clear + surfaced on the ledger row."""
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.web import data

from .util import (_admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts,
                   write_config, TODAY)


class TxnNoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"note-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        write_config(self.conn)
        self.t = add_txn(self.conn, TODAY, 42.0, "SAFEWAY")

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def test_set_get_clear(self):
        self.assertEqual(data.set_note(self.conn, self.t, "  split w/ Alex "),
                         "split w/ Alex")
        row = self.conn.execute(
            "SELECT note FROM transaction_notes WHERE txn_id=%s",
            (self.t,)).fetchone()
        self.assertEqual(row["note"], "split w/ Alex")
        # update
        data.set_note(self.conn, self.t, "reimbursable")
        # clear (empty)
        self.assertEqual(data.set_note(self.conn, self.t, "   "), "")
        self.assertIsNone(self.conn.execute(
            "SELECT note FROM transaction_notes WHERE txn_id=%s",
            (self.t,)).fetchone())

    def test_note_appears_on_ledger_row(self):
        data.set_note(self.conn, self.t, "warranty until 2028")
        rows = data.transactions(self.conn, TODAY.year, TODAY.month)
        r = next(x for x in rows if x["id"] == self.t)
        self.assertEqual(r["note"], "warranty until 2028")
        # a note-less transaction reports None
        t2 = add_txn(self.conn, TODAY, 9.0, "COFFEE")
        rows = data.transactions(self.conn, TODAY.year, TODAY.month)
        self.assertIsNone(next(x for x in rows if x["id"] == t2)["note"])

    def test_note_capped(self):
        stored = data.set_note(self.conn, self.t, "x" * 5000)
        self.assertEqual(len(stored), data.NOTE_MAX)


if __name__ == "__main__":
    unittest.main()
