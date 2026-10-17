"""Every member a restore reads is bounded per record before csv builds
it, as every CSV importer's data rows are.

A restore archive is accepted from any household owner, and a member's
declared size can sit exactly at the per-member limit while the ZIP is a
few hundred KB on the wire: one line of commas deflates to almost nothing.
csv limits each field, not the number of fields, so a short header over
one such line becomes tens of millions of empty cells — gigabytes in the
web process every household shares — before the row cap has counted it as
one row. A header made of commas does the same through the field names.
So the header is read under a small character budget and a column limit,
each data record (every line of it, since a quoted field may run across
many) under a budget sized to the largest honest cell, and a record whose
separators alone would make it absurdly wide is refused before it is
parsed. An ordinary archive, receipt photos included, still restores.
"""

import io
import time
import tracemalloc
import unittest
import zipfile

from oikonome.sync import restore

from .util import make_db, write_config


def _zip(files: dict[str, str]) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def _read(z, name="transactions.csv"):
    return list(restore._rows(z, name))


class ArchiveRowBoundTests(unittest.TestCase):
    # One record may be as long as the largest honest cell, and csv holds
    # a field being built at four bytes a character, so a refused record
    # can cost a few tens of MB; what it may never cost is the member's
    # width, which at the member limit is gigabytes.
    def _refused_cheaply(self, z, name="transactions.csv",
                         under=128 * 1024 * 1024):
        tracemalloc.start()
        try:
            t0 = time.monotonic()
            with self.assertRaises(ValueError) as e:
                _read(z, name)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(time.monotonic() - t0, 5)
        self.assertLess(peak, under, f"peak {peak:,} bytes")
        self.assertIn(name, str(e.exception))
        return str(e.exception)

    def test_a_line_of_commas_under_a_short_header_is_refused(self):
        z = _zip({"transactions.csv":
                  "id,account_id,amount\n" + "," * (4 * 1024 * 1024)
                  + "\nt1,a1,1.00\n"})
        # refused on its separators, before csv builds a cell of it
        self._refused_cheaply(z, under=32 * 1024 * 1024)

    def test_a_line_longer_than_any_honest_record_is_refused(self):
        z = _zip({"transactions.csv":
                  "id,account_id,amount\n"
                  + "x" * (restore.MAX_RECORD_CHARS + 10) + "\n"})
        self._refused_cheaply(z)

    def test_a_quoted_record_spanning_many_short_lines_is_refused(self):
        body = '"' + "x\n" * (restore.MAX_RECORD_CHARS // 2 + 10) + '"'
        z = _zip({"transactions.csv": "id,account_id,amount\nt1,a1," + body})
        self._refused_cheaply(z)

    def test_a_header_made_of_commas_is_refused(self):
        z = _zip({"tenant_settings.csv": "," * (4 * 1024 * 1024) + "\n"})
        self._refused_cheaply(z, "tenant_settings.csv",
                              under=32 * 1024 * 1024)

    def test_a_short_but_too_wide_header_is_refused(self):
        z = _zip({"accounts.csv": ",".join(f"c{i}" for i in range(1000))
                  + "\n"})
        self._refused_cheaply(z, "accounts.csv")

    def test_a_row_far_wider_than_its_header_is_refused(self):
        z = _zip({"accounts.csv": "id,name\n" + "a1" + ",x" * 5000 + "\n"})
        self._refused_cheaply(z, "accounts.csv")

    def test_an_honest_archive_with_a_photo_sized_cell_still_reads(self):
        photo = "QUJD" * (7 * 1024 * 1024 // 4)        # base64, ~7 MB
        z = _zip({"receipts.csv":
                  'id,image,parsed\nr1,' + photo
                  + ',"{""a"": 1, ""b"": [1, 2, 3]}"\n'
                  + 'r2,,"line one\nline two"\n'})
        rows = _read(z, "receipts.csv")
        self.assertEqual([r["id"] for r in rows], ["r1", "r2"])
        self.assertEqual(len(rows[0]["image"]), len(photo))
        self.assertEqual(rows[1]["parsed"], "line one\nline two")

    def test_a_row_with_a_stray_comma_or_two_still_reads(self):
        z = _zip({"accounts.csv": "id,name\na1,Checking,extra,more\n"})
        rows = _read(z, "accounts.csv")
        self.assertEqual(rows[0]["id"], "a1")


class RestoreDoorRefusesAWideArchive(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_restore_reports_a_bad_archive(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("transactions.csv",
                       "id,account_id,amount\n" + "," * (4 * 1024 * 1024))
        with self.assertRaises(ValueError):
            restore.restore_zip(self.conn, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
