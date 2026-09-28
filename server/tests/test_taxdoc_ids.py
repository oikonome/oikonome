"""income_documents id allocation survives concurrent commits.

The table has no identity (original integer ids travel in restores), so
commit() computes MAX(id)+1 — and two simultaneous W-2 commits can compute
the same id. The insert is ON CONFLICT DO NOTHING with a recompute retry,
so a lost race just means "take the next id".
"""

import unittest

from oikonome.db import staging
from oikonome.engine import taxdocs

from .util import make_db

W2_ROW = {"year": 2024, "employer": "Acme Widgets", "ein": "12-3456789",
          "boxes": {"1": 50000.0, "2": 5000.0}}


def _token(conn):
    return staging.put(conn, "taxdoc",
                       meta={"kind": "w2", "rows": [dict(W2_ROW)]})


class _RaceOnce:
    """Simulates a concurrent commit: right after the first MAX(id)+1
    read, another session claims that very id before our INSERT lands."""

    def __init__(self, conn):
        self._conn = conn
        self._raced = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, params=None):
        cur = (self._conn.execute(sql, params) if params is not None
               else self._conn.execute(sql))
        if not self._raced and "MAX(id)" in sql:
            self._raced = True
            row = cur.fetchone()
            self._conn.execute(
                "INSERT INTO income_documents (id, year, form) "
                "VALUES (%s, 2019, 'W-2')", (row["id"],))

            class _Consumed:
                rowcount = 1

                def fetchone(self):
                    return row
            return _Consumed()
        return cur


class TaxdocIdTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _doc_ids(self):
        return [r["id"] for r in self.conn.execute(
            "SELECT id FROM income_documents ORDER BY id").fetchall()]

    def test_sequential_commits_get_distinct_ids(self):
        taxdocs.commit(self.conn, _token(self.conn), [dict(W2_ROW)])
        taxdocs.commit(self.conn, _token(self.conn), [dict(W2_ROW)])
        self.assertEqual(self._doc_ids(), [1, 2])

    def test_lost_race_retries_onto_the_next_id(self):
        out = taxdocs.commit(_RaceOnce(self.conn), _token(self.conn),
                             [dict(W2_ROW)])
        self.assertEqual(out["documents"], 1)
        # the racer took id 1; our commit landed intact on id 2
        self.assertEqual(self._doc_ids(), [1, 2])
        ours = self.conn.execute(
            "SELECT year, payer FROM income_documents WHERE id=2").fetchone()
        self.assertEqual((ours["year"], ours["payer"]),
                         (2024, "Acme Widgets"))
        # ...and still filled the income spine
        wages = self.conn.execute(
            "SELECT primary_wages FROM income_annual WHERE year=2024"
        ).fetchone()
        self.assertEqual(wages["primary_wages"], 50000.0)


if __name__ == "__main__":
    unittest.main()
