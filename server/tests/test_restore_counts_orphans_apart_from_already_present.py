"""A restored row whose parent the archive does not carry is left out and
counted as orphaned — not as "already present", which it never was. An
archive written before the child tables had real foreign keys can carry
such rows; the person restoring is told what they were, and a second
restore of the same archive still counts what was already here apart."""

import unittest

from oikonome.sync import export, restore

from .test_export_is_one_snapshot import _with_rows
from .util import make_db


class RestoreOrphanCountTests(unittest.TestCase):

    def test_orphans_are_counted_apart(self):
        src = make_db()
        try:
            src.execute(
                "INSERT INTO transactions (id, account_id, date, amount, "
                "name, pending, removed) VALUES ('kept-1', 'chk', "
                "'2026-06-01', 4.0, 'HARBOR LIGHTS CAFE', 0, 0)")
            src.execute("INSERT INTO transaction_notes (txn_id, note) "
                        "VALUES ('kept-1', 'kept')")
            data = export.build_zip(src)
        finally:
            src.close()
        data = _with_rows(data, {
            "transaction_notes.csv": [
                {"txn_id": "no-such-txn", "note": "stray"}],
            "business_flags.csv": [{"txn_id": "no-such-txn"}],
        })
        dest = make_db()
        try:
            first = restore.restore_zip(dest, data)
            self.assertEqual(first.get("orphaned"), 2)
            self.assertTrue(any("does not carry" in n
                                for n in first.get("_notes", [])))
            second = restore.restore_zip(dest, data)
            self.assertEqual(second.get("orphaned"), 2,
                             "an orphan is an orphan on every restore")
            self.assertGreaterEqual(second.get("already_present", 0), 1,
                                    "the rows that did land are what a "
                                    "second restore finds already here")
            self.assertEqual(dest.execute(
                "SELECT count(*) AS n FROM transaction_notes").fetchone()["n"],
                1)
        finally:
            dest.close()


if __name__ == "__main__":
    unittest.main()
