"""A household's export restores however many receipts it keeps.

Every receipt travels in receipts.csv as base64 of its photo and its
optimised copy, so a few dozen phone photos are more than the restore's
per-file cap, and a household that keeps its receipts could not restore its
own export at all. The receipts member is held to how much photos can
expand instead (they are already compressed), with every row still held to
a live upload's limit; a member that expands like a zip bomb is refused.
And the export says so, at export time, when what it wrote is something a
restore would refuse.

The limits are shrunk here so a few small receipts stand in for dozens of
real photos.
"""
import io
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from oikonome.engine import receipts
from oikonome.sync import export, restore

from .util import TODAY, add_txn, make_db

IMG = 48 * 1024
COUNT = 8


def _export(conn, **kw):
    with tempfile.TemporaryFile() as fh:
        export.build_zip(conn, out=fh, **kw)
        fh.seek(0)
        return fh.read()


class ReceiptsRoundTrip(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        for _ in range(COUNT):
            self.conn.execute(
                "INSERT INTO receipts (image, mime, kind, image_optimized) "
                "VALUES (%s, 'image/jpeg', 'receipt', %s)",
                (os.urandom(IMG), os.urandom(IMG // 2)))
        caps = [mock.patch.object(receipts, "MAX_IMAGE", IMG),
                mock.patch.object(restore, "MAX_MEMBER_BYTES", 256 * 1024),
                mock.patch.object(restore, "MAX_TOTAL_BYTES", 1024 * 1024)]
        for c in caps:
            c.start()
            self.addCleanup(c.stop)

    def test_every_receipt_comes_back(self):
        warnings: list[str] = []
        data = _export(self.conn, warnings=warnings)
        size = zipfile.ZipFile(io.BytesIO(data)).getinfo(
            "receipts.csv").file_size
        self.assertGreater(size, restore.MAX_MEMBER_BYTES)
        self.assertEqual(warnings, [])
        dest = make_db()
        self.addCleanup(dest.close)
        counts = restore.restore_zip(dest, data)
        self.assertEqual(counts.get("receipts"), COUNT)
        self.assertEqual(dest.execute(
            "SELECT count(*) AS n FROM receipts").fetchone()["n"], COUNT)

    def test_a_receipts_member_that_expands_like_a_bomb_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("receipts.csv", "id,image\n"
                       + "x," + "A" * (restore.MAX_MEMBER_BYTES + 1) + "\n")
        dest = make_db()
        self.addCleanup(dest.close)
        with self.assertRaisesRegex(ValueError, "expands"):
            restore.restore_zip(dest, buf.getvalue())

    def test_the_export_says_when_a_restore_would_refuse_it(self):
        tid = add_txn(self.conn, TODAY, 12.0, "STATIONER")
        self.conn.execute(
            "INSERT INTO transaction_notes (txn_id, note) VALUES (%s, %s)",
            (tid, os.urandom(200 * 1024).hex()))
        warnings: list[str] = []
        data = _export(self.conn, warnings=warnings)
        self.assertEqual(len(warnings), 1)
        self.assertIn("transaction_notes.csv", warnings[0])
        self.assertIn("RESTORE-NOTE.txt",
                      zipfile.ZipFile(io.BytesIO(data)).namelist())


if __name__ == "__main__":
    unittest.main()
