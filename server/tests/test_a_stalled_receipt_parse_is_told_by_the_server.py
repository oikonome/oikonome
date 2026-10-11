"""A receipt read whose worker died is reported stalled by the server.

The clients offer "retry parse" once a read is past the reclaim window.
Measured from when a client first saw the row parsing, a read that died
yesterday would show a progress bar for another ten minutes after every
page load. So each receipt row carries when the attempt in progress began
(`parse_started_at`, null unless it is being read) and `parse_stale`, the
server's own verdict by the same rule its reclaim uses.
"""
import unittest

from oikonome.engine import receipt_match, receipts

from .util import add_txn, make_db, write_config

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class StalledParseTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _parsing(self, rid, minutes_ago):
        self.conn.execute(
            "UPDATE receipts SET status='parsing', parsed_at = now() - "
            "make_interval(mins => %s) WHERE id=%s::uuid", (minutes_ago, rid))

    def _waiting_row(self, rid):
        return next(r for r in receipt_match.waiting(self.conn)["waiting"]
                    if r["id"] == rid)

    def test_a_waiting_receipt_carries_the_parse_state(self):
        rid = receipts.add(self.conn, None, PNG, "image/png")
        self._parsing(rid, 1)
        row = self._waiting_row(rid)
        self.assertFalse(row["parse_stale"])
        self.assertTrue(row["parse_started_at"])
        self._parsing(rid, 60 * 24)
        self.assertTrue(self._waiting_row(rid)["parse_stale"])
        # exactly what claim() would now accept
        self.assertTrue(receipts.claim(self.conn, rid))

    def test_a_transactions_receipt_carries_the_parse_state(self):
        tid = add_txn(self.conn, "2026-07-01", 12.0, "CORNER GROCER")
        rid = receipts.add(self.conn, tid, PNG, "image/png")
        self._parsing(rid, 60 * 24)
        row = next(r for r in receipts.for_txn(self.conn, tid)
                   if r["id"] == rid)
        self.assertTrue(row["parse_stale"])
        self.assertIsNotNone(row["parse_started_at"])

    def test_a_read_that_is_done_has_no_attempt(self):
        rid = receipts.add(self.conn, None, PNG, "image/png")
        self.conn.execute("UPDATE receipts SET status='parsed', parsed_at="
                          "now() - interval '1 day' WHERE id=%s::uuid", (rid,))
        row = self._waiting_row(rid)
        self.assertFalse(row["parse_stale"])
        self.assertIsNone(row["parse_started_at"])


if __name__ == "__main__":
    unittest.main()
