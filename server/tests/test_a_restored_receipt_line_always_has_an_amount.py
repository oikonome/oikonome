"""A restored receipt brings back only the line items that carry an amount.

Reading a receipt drops any line without a finite amount, so the live app
never stores one, and every client renders a line's amount as a number. An
archive row with an empty amount cell would plant the one shape the app
cannot draw — the receipt panel and the expense report fail on it — so the
restore leaves that line out and the rest of the receipt comes back.
"""
import os
import tempfile
import unittest

from oikonome.sync import export, restore

from .util import make_db


def _export(conn):
    with tempfile.TemporaryFile() as fh:
        export.build_zip(conn, out=fh)
        fh.seek(0)
        return fh.read()


class RestoredReceiptLineTests(unittest.TestCase):
    def test_a_line_without_an_amount_is_left_out(self):
        src = make_db()
        self.addCleanup(src.close)
        rid = src.execute(
            "INSERT INTO receipts (image, mime, kind) VALUES "
            "(%s, 'image/jpeg', 'receipt') RETURNING id",
            (os.urandom(512),)).fetchone()["id"]
        src.execute(
            "INSERT INTO receipt_items (receipt_id, line, description, qty, "
            "amount, tag) VALUES (%s, 1, 'Bread', 1, 4.50, ''), "
            "(%s, 2, 'Unpriced', 1, NULL, '')", (rid, rid))
        data = _export(src)
        dest = make_db()
        self.addCleanup(dest.close)
        restore.restore_zip(dest, data)
        rows = dest.execute(
            "SELECT line, amount FROM receipt_items ORDER BY line").fetchall()
        self.assertEqual([(r["line"], r["amount"]) for r in rows],
                         [(1, 4.5)])


if __name__ == "__main__":
    unittest.main()
