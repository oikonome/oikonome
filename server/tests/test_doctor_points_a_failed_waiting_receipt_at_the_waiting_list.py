"""The doctor's receipt-queue line says where each failed read is retried.

A failed read on a receipt attached to a charge is retried from that
transaction's receipt panel; one still waiting for its charge has no
transaction and no panel — it is retried, or its total typed in, on the
Receipts page's waiting list. Counting both under "retry from the
transaction's receipt panel" sent a person looking for a panel that does
not exist."""

import unittest

from oikonome.engine import receipts
from oikonome.web import doctor

from .util import TODAY, add_txn, make_db, write_config


class FailedReceipts(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

    def tearDown(self):
        self.conn.close()

    def _fail(self, txn):
        rid = receipts.add(self.conn, txn, b"\x89PNG...", "image/png")
        self.conn.execute(
            "UPDATE receipts SET status='failed', error='x' "
            "WHERE id=%s::uuid", (rid,))

    def _detail(self):
        rows = [r for r in doctor.checks(self.tid)
                if r["section"] == "Smart categorization"
                and r["name"] == "receipt queue"]
        self.assertFalse(rows[0]["ok"])
        return rows[0]["detail"]

    def test_attached_and_waiting_failures_are_counted_apart(self):
        txn = add_txn(self.conn, TODAY, 10.0, "CORNER COFFEE", account="chk")
        self._fail(txn)
        self._fail(None)
        self._fail(None)
        d = self._detail()
        self.assertIn("1 failed — retry from the transaction's receipt panel",
                      d)
        self.assertIn("2 failed while waiting for its charge", d)
        self.assertIn("under Receipts → Waiting receipts", d)

    def test_only_waiting_failures_never_mention_a_receipt_panel(self):
        self._fail(None)
        d = self._detail()
        self.assertIn("1 failed while waiting", d)
        self.assertNotIn("receipt panel", d)


if __name__ == "__main__":
    unittest.main()
