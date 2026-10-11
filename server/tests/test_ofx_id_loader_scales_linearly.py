"""Re-importing a large OFX file costs work in proportion to its size.

Every re-import loads the account's stored OFX rows to decide which row
each line already is. Matching those rows against the file's FITIDs with
a scan of a list made the work stored-rows × file-lines: a multi-year
download of a busy card spun the CPU for tens of seconds per upload while
holding the household's sync lock. Each stored row must be matched with
a constant number of comparisons, and a full re-import must add nothing.
"""

import unittest

from oikonome.sync import ofximport

from .test_file_imports_keep_every_real_row import _ofx
from .util import make_db, write_config

N = 5000


class _CountingId(str):
    """A row id that counts how often it is compared for equality."""
    compares = 0

    def __eq__(self, other):
        _CountingId.compares += 1
        return str.__eq__(self, other)

    __hash__ = str.__hash__


class _Rows:
    def __init__(self, cur):
        self._cur = cur

    def fetchall(self):
        out = []
        for r in self._cur.fetchall():
            r = dict(r)
            if isinstance(r.get("id"), str):
                r["id"] = _CountingId(r["id"])
            out.append(r)
        return out

    def fetchone(self):
        return self._cur.fetchone()

    def __iter__(self):
        return iter(self.fetchall())


class _CountingConn:
    """The real connection, with every fetched id wrapped to count."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *a, **kw):
        return _Rows(self._conn.execute(*a, **kw))


class IdLoaderScale(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _file(self):
        # invented card activity: one round charge a day over ~14 years
        import datetime as dt
        day0 = dt.date(2012, 1, 1)
        return _ofx(*[(f"T{i:05d}",
                       (day0 + dt.timedelta(days=i)).strftime("%Y%m%d"),
                       f"-{10 + i % 50}.00", f"SHOP {i % 40}")
                      for i in range(N)])

    def test_reimport_work_grows_linearly_with_the_stored_rows(self):
        data = self._file()
        ofximport.import_ofx(self.conn, "chk", data)
        rows = [r for r in ofximport.parse_ofx(data)
                if r["date"] is not None and r["amount"] is not None]
        _CountingId.compares = 0
        ofximport._FitidIds(_CountingConn(self.conn), "chk", rows)
        self.assertLess(_CountingId.compares, 4 * N)
        # and the re-import itself lands every line on its own row
        ofximport.import_ofx(self.conn, "chk", data)
        n = self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE id LIKE 'ofx:%%'"
        ).fetchone()["n"]
        self.assertEqual(n, N)

if __name__ == "__main__":
    unittest.main()
