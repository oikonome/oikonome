"""An export's memory follows its largest row, not its row count.

Both export doors stream through a server-side cursor, and the cursor's
prefetch decides how many rows sit in the process at once. For a ledger
row that is a few hundred bytes, a large batch is cheap; for a receipt it
is a phone photo and its optimised copy, and a batch of hundreds of those
is more memory than a small app container has. A household that keeps its
receipts would then fail its own export every time, and take every other
request on the process down with it.

So the invariant: adding more receipts (or other binary rows) to a
household must not raise the export's peak memory beyond what one such row
needs. The operator's portable archive omits binary columns altogether,
so it must not read them into the process merely to write their size.
"""

import os
import tempfile
import tracemalloc
import unittest

from oikonome import tenant_export
from oikonome.sync import export

from .util import _ensure_db, make_db

IMG = 2 * 1024 * 1024          # one synthetic photo
MANY = 10


def _tid(conn) -> str:
    return conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]


def _add_receipts(conn, n: int) -> None:
    for _ in range(n):
        conn.execute(
            "INSERT INTO receipts (image, mime, kind, image_optimized) "
            "VALUES (%s, 'image/jpeg', 'receipt', %s)",
            (os.urandom(IMG), os.urandom(IMG // 2)))


def _peak(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


class FullExportMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # one export first, so the measured ones pay for no module imports
        conn = make_db()
        try:
            with tempfile.TemporaryFile() as fh:
                export.build_zip(conn, out=fh)
        finally:
            conn.close()

    def _export_peak(self, conn) -> int:
        with tempfile.TemporaryFile() as fh:
            return _peak(lambda: export.build_zip(conn, out=fh))

    def test_many_receipts_cost_no_more_than_one(self):
        one = make_db()
        many = make_db()
        try:
            _add_receipts(one, 1)
            _add_receipts(many, MANY)
            p1 = self._export_peak(one)
            pn = self._export_peak(many)
        finally:
            one.close()
            many.close()
        # one receipt row (its photo, the optimised copy and their base64
        # cells) is the unit; ten of them prefetched at once would be ten
        # units, and a handful of rows' slack is all that is allowed
        self.assertLess(pn, p1 + 3 * IMG,
                        f"peak {pn} for {MANY} receipts vs {p1} for one")

    def test_the_receipts_still_travel_whole(self):
        conn = make_db()
        try:
            _add_receipts(conn, 2)
            data = export.build_zip(conn)
        finally:
            conn.close()
        import csv
        import io
        import sys
        import zipfile
        csv.field_size_limit(sys.maxsize)
        rows = list(csv.DictReader(io.StringIO(zipfile.ZipFile(
            io.BytesIO(data)).read("receipts.csv").decode())))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(r["image"]) > IMG for r in rows))


class PortableArchiveMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        conn = make_db()
        tid = _tid(conn)
        conn.close()
        tenant_export.build_archive(tid, operator="op", consented=True)

    def test_binary_columns_are_never_read_into_the_process(self):
        conn = make_db()
        tid = _tid(conn)
        try:
            # staged uploads and the learned model are binary columns the
            # archive writes only the size of
            for i in range(MANY):
                conn.execute(
                    "INSERT INTO import_staging (token, idx, kind, data) "
                    "VALUES ('tok-mem', %s, 'csv', %s)",
                    (i, os.urandom(IMG)))
            conn.execute(
                "INSERT INTO categorizer_model (artifact, samples, classes) "
                "VALUES (%s, 1, 1)", (os.urandom(IMG),))
        finally:
            conn.close()
        out = {}
        # the archive leaves these two tables out altogether now; they are
        # let back in here because they are the binary columns there are,
        # and the size-only read must hold for any table that has one
        from unittest import mock
        skip = {k: v for k, v in tenant_export.ARCHIVE_TABLE_SKIP.items()
                if k not in ("import_staging", "categorizer_model")}
        with mock.patch.object(tenant_export, "ARCHIVE_TABLE_SKIP", skip):
            peak = _peak(lambda: out.setdefault(
                "zip", tenant_export.build_archive(
                    tid, operator="op", consented=True)))
        self.assertLess(peak, 2 * IMG,
                        "the archive read binary columns it only sizes")
        import io
        import json
        import zipfile
        z = zipfile.ZipFile(io.BytesIO(out["zip"]))
        staged = json.loads(z.read("data/import_staging.json"))
        self.assertEqual(len(staged), MANY)
        # the size note is still written, so the archive says what it left out
        self.assertEqual(staged[0]["data"], f"<{IMG} bytes omitted>")
        model = json.loads(z.read("data/categorizer_model.json"))
        self.assertEqual(model[0]["artifact"], f"<{IMG} bytes omitted>")


if __name__ == "__main__":
    unittest.main()
