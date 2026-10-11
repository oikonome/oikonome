"""Renaming a merchant shown for many bank lines costs one pass, not one per line.

A file-imported merchant often stands for hundreds of distinct bank lines
(a store number or order code in each). Renaming or merging it moves the
rows of every line. Done one line at a time, that read the whole ledger
once per line inside the household's locked transaction; and the rename's
journal note, copied onto each line's journal row with the full list of
lines inside it, grew with the square of the line count. The rows move in
one statement, and each journal row carries a note whose size does not
depend on how many lines the rename had — while undo still puts every
rule back.
"""

import datetime as dt
import json
import unittest
from unittest import mock

from oikonome.engine import llm_categorize, merchant_dedup

from .util import TODAY, add_txn, make_db, write_config

OLD = "Copperline Bikes"
NEW = "Copperline Cycles"


class RenameManyLinesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _lines(self, n):
        for i in range(n):
            add_txn(self.conn, TODAY - dt.timedelta(days=i), 10.0 + i,
                    f"SQ *COPPERLINE BIKES #{4100 + i * 7}", merchant=None)
        merchant_dedup.apply(self.conn)
        self.assertEqual(len(merchant_dedup.raw_strings_for(self.conn, OLD)),
                         n)

    def _ledger_updates(self, n):
        self._lines(n)
        seen = []
        real = self.conn.execute

        def spy(sql, *a, **kw):
            if " ".join(str(sql).split()).startswith("UPDATE transactions"):
                seen.append(sql)
            return real(sql, *a, **kw)

        with mock.patch.object(self.conn, "execute", side_effect=spy):
            with self.conn.transaction():
                merchant_dedup.rename(self.conn, OLD, NEW)
        return len(seen)

    def test_the_ledger_is_updated_once_whatever_the_line_count(self):
        self.assertLessEqual(self._ledger_updates(12), 1)
        rows = self.conn.execute(
            "SELECT DISTINCT m.name FROM transactions t "
            "JOIN merchants m ON m.id = t.merchant_id "
            "WHERE t.name LIKE 'SQ *COPPERLINE%%'").fetchall()
        self.assertEqual([r["name"] for r in rows], [NEW])

    def test_the_journal_note_does_not_grow_with_the_lines(self):
        def note_size(n, frm, to):
            llm_categorize.upsert_user_rule(self.conn, frm, "ENTERTAINMENT")
            with self.conn.transaction():
                merchant_dedup.rename(self.conn, frm, to)
            sizes = {len(json.dumps(r["rules"])) for r in self.conn.execute(
                "SELECT rules FROM merchant_renames WHERE to_canonical=%s",
                (to,)).fetchall()}
            self.assertEqual(len(sizes), 1)        # one note, every row
            return sizes.pop()

        self._lines(3)
        small = note_size(3, OLD, NEW)
        for i in range(40):
            add_txn(self.conn, TODAY - dt.timedelta(days=i), 5.0 + i,
                    f"SQ *QUILLWAY BOOKS #{8100 + i * 3}", merchant=None)
        merchant_dedup.apply(self.conn)
        large = note_size(40, "Quillway Books", "Quillway")
        # a different name and a random token: equal within a few bytes
        self.assertLess(abs(large - small), 40)

    def test_undo_of_every_line_still_returns_the_rule(self):
        self._lines(6)
        llm_categorize.upsert_user_rule(self.conn, OLD, "ENTERTAINMENT")
        with self.conn.transaction():
            merchant_dedup.rename(self.conn, OLD, NEW)
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM merchant_renames ORDER BY id DESC").fetchall()]
        for cid in ids[:-1]:
            with self.conn.transaction():
                merchant_dedup.undo(self.conn, cid)
            # other lines are still renamed: the new name keeps the rule
            self.assertIsNotNone(self.conn.execute(
                "SELECT 1 FROM merchant_categories WHERE merchant=%s",
                (NEW,)).fetchone())
        with self.conn.transaction():
            merchant_dedup.undo(self.conn, ids[-1])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE merchant=%s",
            (NEW,)).fetchone())
        r = self.conn.execute(
            "SELECT category_primary FROM merchant_categories WHERE merchant=%s",
            (OLD,)).fetchone()
        self.assertEqual(r["category_primary"], "ENTERTAINMENT")


if __name__ == "__main__":
    unittest.main()
