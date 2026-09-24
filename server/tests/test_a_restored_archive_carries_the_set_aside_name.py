"""A set-aside aggregator name is a decision, and a restore reproduces it.

When the resolver files one charge under the merchant its bank line means,
the aggregator's name for that charge moves to `merchant_name_set_aside` and
the row's own `merchant_name` is emptied — so every reader of
`COALESCE(merchant_name, name)` reads the line, by construction.

Three shapes of archive reach this door, and each has to land meaning what it
meant where it was written:

  * the current one, which names the set-aside column;
  * one written while the same decision was a boolean, which leaves the stray
    name in `merchant_name` and marks it — the name has to MOVE on the way in,
    or the restore re-keys the row on a name the household had already judged
    a misreading;
  * one older than either, which says nothing about any of this and must come
    back untouched.
"""

import csv
import io
import unittest
import zipfile

from oikonome.sync import restore

from .util import TODAY, make_db

LINE = "TAPPAY JUNIPERS MKT SPRINGFIELD OR"
USUAL = "Juniper's Mkt"
STRAY = "Harbor Lights"

ACCT_COLS = ["id", "item_id", "name", "type", "subtype"]
ACCT_ROW = {"id": "chk", "item_id": "it1", "name": "Test Checking",
            "type": "depository", "subtype": "checking"}


def _zip(tables: dict[str, tuple[list[str], list[dict]]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for tbl, (cols, rows) in tables.items():
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: ("" if r.get(c) is None else r.get(c))
                            for c in cols})
            z.writestr(f"{tbl}.csv", s.getvalue())
    return buf.getvalue()


def _archive(cols: list[str], row: dict) -> bytes:
    base = {"id": "stray", "account_id": "chk", "date": str(TODAY),
            "amount": "9.25", "name": LINE}
    return _zip({"accounts": (ACCT_COLS, [ACCT_ROW]),
                 "transactions": (["id", "account_id", "date", "amount",
                                   "name"] + cols, [{**base, **row}])})


class RestoredSetAsideNameTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _row(self):
        return self.conn.execute(
            "SELECT merchant_name, merchant_name_set_aside AS aside "
            "  FROM transactions WHERE id = 'stray'").fetchone()

    def test_the_set_aside_name_comes_back_on_the_row(self):
        restore.restore_zip(self.conn, _archive(
            ["merchant_name", "merchant_name_set_aside"],
            {"merchant_name": "", "merchant_name_set_aside": STRAY}))
        row = self._row()
        self.assertEqual(row["aside"], STRAY)
        self.assertIsNone(row["merchant_name"])

    def test_a_boolean_era_archive_has_the_name_moved_on_the_way_in(self):
        """The mark said "this name does not count"; the name leaving
        `merchant_name` is how the row says that now."""
        restore.restore_zip(self.conn, _archive(
            ["merchant_name", "merchant_name_overruled"],
            {"merchant_name": STRAY, "merchant_name_overruled": "True"}))
        row = self._row()
        self.assertEqual(row["aside"], STRAY)
        self.assertIsNone(row["merchant_name"])

    def test_a_boolean_era_row_that_was_never_marked_keeps_its_name(self):
        restore.restore_zip(self.conn, _archive(
            ["merchant_name", "merchant_name_overruled"],
            {"merchant_name": USUAL, "merchant_name_overruled": "False"}))
        row = self._row()
        self.assertEqual(row["merchant_name"], USUAL)
        self.assertIsNone(row["aside"])

    def test_an_archive_older_than_both_columns_restores_unchanged(self):
        restore.restore_zip(self.conn,
                            _archive(["merchant_name"],
                                     {"merchant_name": USUAL}))
        row = self._row()
        self.assertEqual(row["merchant_name"], USUAL)
        self.assertIsNone(row["aside"])

    def test_a_row_the_aggregator_never_named_stays_unnamed(self):
        restore.restore_zip(self.conn, _archive([], {}))
        row = self._row()
        self.assertIsNone(row["merchant_name"])
        self.assertIsNone(row["aside"])

    def test_a_set_aside_row_pointing_at_a_merchant_that_is_not_here_loses_it(self):
        """The name being set aside changes nothing about the pointer rule:
        a merchant_id naming a merchant the archive did not carry is worse
        than none — identity reads go to a row that is not there — so it
        lands NULL and the next pass reads the row's key afresh. The
        judgement itself still arrives."""
        import uuid
        ghost = str(uuid.uuid4())
        restore.restore_zip(self.conn, _archive(
            ["merchant_name", "merchant_name_set_aside", "merchant_id"],
            {"merchant_name": "", "merchant_name_set_aside": STRAY,
             "merchant_id": ghost}))
        row = self.conn.execute(
            "SELECT merchant_id::text AS mid, "
            "       merchant_name_set_aside AS aside "
            "  FROM transactions WHERE id='stray'").fetchone()
        # whatever the row keys on afterwards, it is not the merchant the
        # archive named and did not carry
        self.assertNotEqual(row["mid"], ghost)
        self.assertEqual(row["aside"], STRAY)

    def test_a_set_aside_row_keeps_a_pointer_the_archive_carried(self):
        import uuid
        mid = str(uuid.uuid4())
        data = _zip({
            "merchants": (["id", "name", "name_source", "kind"],
                          [{"id": mid, "name": USUAL,
                            "name_source": "plaid", "kind": "merchant"}]),
            "accounts": (ACCT_COLS, [ACCT_ROW]),
            "transactions": (["id", "account_id", "date", "amount", "name",
                              "merchant_name", "merchant_name_set_aside",
                              "merchant_id"],
                             [{"id": "stray", "account_id": "chk",
                               "date": str(TODAY), "amount": "9.25",
                               "name": LINE, "merchant_name": "",
                               "merchant_name_set_aside": STRAY,
                               "merchant_id": mid}]),
            "merchant_canonical": (["raw_merchant", "canonical", "method",
                                    "merchant_id"],
                                   [{"raw_merchant": LINE, "canonical": USUAL,
                                     "method": "manual", "merchant_id": mid}]),
        })
        restore.restore_zip(self.conn, data)
        row = self.conn.execute(
            "SELECT merchant_id::text AS mid, merchant_name_set_aside AS aside"
            "  FROM transactions WHERE id='stray'").fetchone()
        self.assertEqual(row["mid"], mid)
        self.assertEqual(row["aside"], STRAY)


if __name__ == "__main__":
    unittest.main()
