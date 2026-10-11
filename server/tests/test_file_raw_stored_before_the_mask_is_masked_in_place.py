"""Rows a file import stored before raw was masked are masked in place.

The live rule (sync/base.mask_file_raw) masks a file row's raw on the way
in, and a restore masks an older archive. Rows already in the database were
written before either and still carry the full account or card number in
the cell the name was cut from, or in a memo or payee cell beside it; raw
leaves the instance verbatim through /export and the portability archive.
A migration applies the same cut once. It must agree with the live rule
cell for cell, leave aggregator rows and unrelated cells alone, and change
nothing on a second run.
"""

import json
import pathlib
import re
import unittest

from oikonome.sync import base as sync_base

from .util import make_db, write_config

MIGRATION = (pathlib.Path(sync_base.__file__).parent.parent / "db"
             / "migrations" / "178_file_import_raw_number_scrub.sql")

FULL = "4000123456789010"          # invented, not a real card number
ACCT = "000123456789"


def _insert(conn, tid, name, raw):
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount, name,"
        " merchant_name, removed, raw)"
        " VALUES (%s, 'chk', '2026-01-05', 1, %s, %s, 0, %s::jsonb)",
        (tid, name, name, json.dumps(raw)))


class FileRawMaskedInPlace(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _raw(self, tid):
        return self.conn.execute("SELECT raw FROM transactions WHERE id=%s",
                                 (tid,)).fetchone()["raw"]

    def test_memo_class_and_name_cells_are_masked_and_nothing_else(self):
        # the stored name was masked by the earlier name migration; the
        # cell it came from still holds it whole, under a header the
        # free-text list does not name
        csv_raw = {"Details": f"PAYMENT CARD {FULL}",
                   "Memo": f"ref {ACCT}",
                   " Original Statement ": f"XFER {ACCT}",
                   "Ref": "R-17", "Batch": "20260105123456",
                   "nested": {"memo": f"x {FULL}"}, "n": 12345678901}
        _insert(self.conn, "csv:old1", "PAYMENT CARD …9010", csv_raw)
        ofx_raw = {"payee": f"BILLPAY {ACCT}", "fitid": "202601050001234"}
        _insert(self.conn, "ofx:old2", "BILLPAY …6789", ofx_raw)
        # an aggregator row is the feed's own record
        agg_raw = {"memo": f"ref {ACCT}"}
        _insert(self.conn, "agg-3", "COFFEE", agg_raw)
        sql = MIGRATION.read_text()
        self.conn.execute(sql)

        got = self._raw("csv:old1")
        self.assertEqual(got["Details"], "PAYMENT CARD …9010")
        self.assertEqual(got["Memo"], "ref …6789")
        self.assertEqual(got[" Original Statement "], "XFER …6789")
        # cells that neither hold the name nor are free text keep their
        # digits: a long batch stamp is not an account number
        self.assertEqual(got["Ref"], "R-17")
        self.assertEqual(got["Batch"], "20260105123456")
        self.assertEqual(got["nested"], {"memo": f"x {FULL}"})
        self.assertEqual(got["n"], 12345678901)
        self.assertEqual(got, sync_base.mask_file_raw(
            csv_raw, "PAYMENT CARD …9010", "PAYMENT CARD …9010"))

        got = self._raw("ofx:old2")
        self.assertEqual(got, {"payee": "BILLPAY …6789",
                               "fitid": "202601050001234"})
        self.assertEqual(self._raw("agg-3"), agg_raw)

        # a second run writes nothing
        before = self.conn.execute(
            "SELECT xmin::text AS x FROM transactions WHERE id='csv:old1'"
        ).fetchone()["x"]
        cur = self.conn.execute(sql)
        self.assertEqual(cur.rowcount, 0)
        self.assertEqual(self.conn.execute(
            "SELECT xmin::text AS x FROM transactions WHERE id='csv:old1'"
        ).fetchone()["x"], before)

    def test_a_row_holding_no_long_run_in_a_covered_cell_is_not_written(self):
        _insert(self.conn, "csv:clean", "GROCER",
                {"Description": "GROCER", "Batch": "20260105123456"})
        cur = self.conn.execute(MIGRATION.read_text())
        self.assertEqual(cur.rowcount, 0)

    def test_migration_and_live_rule_agree_on_prefixes_and_keys(self):
        """The backfill and the live rule must cut the same rows and the
        same cells, or rows stored before and after the rule differ."""
        sql = MIGRATION.read_text()
        prefixes = re.search(r"split_part\(t\.id, ':', 1\) IN \((.*?)\)",
                             sql, re.S).group(1)
        self.assertEqual(
            tuple(p + ":" for p in re.findall(r"'([^']*)'", prefixes)),
            sync_base.FILE_IMPORT_ID_PREFIXES)
        key_lists = re.findall(r"IN \(('name'.*?)\)", sql, re.S)
        self.assertEqual(len(key_lists), 2)
        for keys in key_lists:
            self.assertEqual(frozenset(re.findall(r"'([^']*)'", keys)),
                             sync_base._FREE_TEXT_KEYS)


if __name__ == "__main__":
    unittest.main()
