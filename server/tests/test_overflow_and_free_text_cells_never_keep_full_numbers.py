"""A file row's raw keeps no full number in an overflow cell or under a
free-text header the exact key list does not spell.

raw is plaintext JSONB that leaves the instance verbatim through /export
and the portability archive, so a run of eight or more digits a statement
prints into free text keeps only its last four there, as it does in the
stored name. Two shapes slipped past a rule that read only string cells
under a handful of exact header names:

* a row with more cells than its header (an unquoted comma in the last
  free-text column) puts the extra cells under the key None, as a list,
  and the list was never looked at;
* banks name their free-text columns "Transaction Details", "Extended
  Description", "Memo/Notes", "Narrative" and so on, and only the exact
  names were masked.

The rows already stored get the same cut once from a migration, which must
agree with the live rule cell for cell and change nothing on a second run.
"""

import json
import pathlib
import re
import unittest

from oikonome.sync import base as sync_base
from oikonome.sync import csvimport, mintimport
from oikonome.sync.base import mask_file_raw

from .util import make_db, write_config

FULL = "1234567890123"          # invented, not a real account number
CARD = "4000123456789010"       # invented, not a real card number

MIGRATION = (pathlib.Path(sync_base.__file__).parent.parent / "db"
             / "migrations" / "181_file_import_raw_overflow_and_free_text_scrub.sql")


def _raws(conn, like):
    return [r["raw"] for r in conn.execute(
        "SELECT raw FROM transactions WHERE id LIKE %s", (like,)).fetchall()]


class LiveRuleTests(unittest.TestCase):
    def test_free_text_headers_are_matched_by_the_words_they_contain(self):
        raw = {"Transaction Details": f"ONLINE PMT {FULL}",
               "Extended Description": f"ACH DEBIT {CARD}",
               " Memo/Notes ": f"ref {FULL}",
               "Payee Name": f"BILLPAY {FULL}",
               "NARRATIVE": f"XFER {FULL}",
               "Particulars": f"TO {FULL}",
               "Batch": "20260105123456",
               "Ref": "R-17"}
        got = mask_file_raw(raw, "SOME OTHER NAME")
        self.assertEqual(got["Transaction Details"], "ONLINE PMT …0123")
        self.assertEqual(got["Extended Description"], "ACH DEBIT …9010")
        self.assertEqual(got[" Memo/Notes "], "ref …0123")
        self.assertEqual(got["Payee Name"], "BILLPAY …0123")
        self.assertEqual(got["NARRATIVE"], "XFER …0123")
        self.assertEqual(got["Particulars"], "TO …0123")
        # a cell that is neither free text nor the name keeps its digits:
        # a long batch stamp is not an account number
        self.assertEqual(got["Batch"], "20260105123456")
        self.assertEqual(got["Ref"], "R-17")

    def test_strings_inside_an_overflow_or_free_text_list_are_masked(self):
        raw = {None: ["TRANSFER TO CHK", f" {FULL}", 7],
               "": [f"x {CARD}"],
               "Memo": ["a", f"b {FULL}"],
               "Ref": [FULL],
               "Description": "ONLINE TRANSFER"}
        got = mask_file_raw(raw, "ONLINE TRANSFER")
        self.assertEqual(got[None], ["TRANSFER TO CHK", " …0123", 7])
        self.assertEqual(got[""], ["x …9010"])
        self.assertEqual(got["Memo"], ["a", "b …0123"])
        # a list under a header that is neither free text nor the name is
        # left as it was, exactly like a string there
        self.assertEqual(got["Ref"], [FULL])

    def test_a_list_cell_holding_the_name_is_masked(self):
        got = mask_file_raw({"Extra": [f"PAYMENT CARD {CARD}"]},
                            f"PAYMENT CARD {CARD}")
        self.assertEqual(got["Extra"], ["PAYMENT CARD …9010"])

    def test_the_identifier_scrub_masks_the_overflow_cells(self):
        got = csvimport.scrub_identifiers(
            {"Account Number": "1234567890123456",
             None: [f"TRANSFER TO CHK {FULL}"],
             "Description": "ONLINE TRANSFER"})
        self.assertEqual(got["Account Number"], "3456")
        self.assertEqual(got[None], ["TRANSFER TO CHK …0123"])
        self.assertEqual(got["Description"], "ONLINE TRANSFER")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_generic_csv_overflow_cell_keeps_no_full_number(self):
        csvimport.import_csv(
            self.conn, "chk",
            "Date,Description,Amount,Memo\n"
            f"2026-01-02,ONLINE TRANSFER,-50.00,TRANSFER TO CHK, {FULL}\n",
            {"date": "Date", "amount": "Amount", "name": "Description"})
        [raw] = _raws(self.conn, "csv:%")
        self.assertNotIn(FULL, json.dumps(raw))
        self.assertEqual(raw["Memo"], "TRANSFER TO CHK")
        self.assertEqual(raw["null"], [" …0123"])

    def test_a_mint_details_column_keeps_no_full_number(self):
        mintimport.import_mint(
            self.conn, "chk",
            "Date,Description,Original Description,Amount,Transaction Type,"
            "Category,Account Name,Labels,Notes,Transaction Details\n"
            "7/10/2026,Corner Market,CORNER MARKET,40.00,debit,Groceries,"
            f"Checking,,,CARD PURCHASE {CARD}\n")
        [raw] = _raws(self.conn, "mint:%")
        self.assertNotIn(CARD, json.dumps(raw))
        self.assertEqual(raw["Transaction Details"], "CARD PURCHASE …9010")
        self.assertEqual(raw["Account Name"], "Checking")


def _insert(conn, tid, name, raw):
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount, name,"
        " merchant_name, removed, raw)"
        " VALUES (%s, 'chk', '2026-01-05', 1, %s, %s, 0, %s::jsonb)",
        (tid, name, name, json.dumps(raw)))


class StoredRowsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _raw(self, tid):
        return self.conn.execute("SELECT raw FROM transactions WHERE id=%s",
                                 (tid,)).fetchone()["raw"]

    def test_overflow_lists_and_worded_free_text_cells_are_masked_once(self):
        csv_raw = {"Description": "ONLINE TRANSFER",
                   "Memo": "TRANSFER TO CHK",
                   None: [f" {FULL}", 3],
                   "Transaction Details": f"ACH {CARD}",
                   "Memo/Notes": [f"ref {FULL}", "plain"],
                   "Ref": [FULL], "Batch": "20260105123456",
                   "nested": {"memo": f"x {FULL}"}, "n": 12345678901,
                   "_batches": ["123456789012"]}
        _insert(self.conn, "csv:old1", "ONLINE TRANSFER", csv_raw)
        agg_raw = {"Transaction Details": f"ACH {CARD}"}
        _insert(self.conn, "agg-3", "COFFEE", agg_raw)
        sql = MIGRATION.read_text()
        self.conn.execute(sql)

        got = self._raw("csv:old1")
        self.assertNotIn(FULL, json.dumps({k: v for k, v in got.items()
                                           if k not in ("Ref", "nested")}))
        self.assertEqual(got["null"], [" …0123", 3])
        self.assertEqual(got["Transaction Details"], "ACH …9010")
        self.assertEqual(got["Memo/Notes"], ["ref …0123", "plain"])
        self.assertEqual(got["Ref"], [FULL])
        self.assertEqual(got["Batch"], "20260105123456")
        self.assertEqual(got["nested"], {"memo": f"x {FULL}"})
        self.assertEqual(got["n"], 12345678901)
        self.assertEqual(got["_batches"], ["123456789012"])
        # the backfill and the live rule cut the same cells
        stored = json.loads(json.dumps(csv_raw))
        self.assertEqual(got, mask_file_raw(stored, "ONLINE TRANSFER",
                                            "ONLINE TRANSFER"))
        self.assertEqual(self._raw("agg-3"), agg_raw)

        before = self.conn.execute(
            "SELECT xmin::text AS x FROM transactions WHERE id='csv:old1'"
        ).fetchone()["x"]
        cur = self.conn.execute(sql)
        self.assertEqual(cur.rowcount, 0)
        self.assertEqual(self.conn.execute(
            "SELECT xmin::text AS x FROM transactions WHERE id='csv:old1'"
        ).fetchone()["x"], before)

    def test_a_row_with_nothing_to_cut_is_not_written(self):
        _insert(self.conn, "csv:clean", "GROCER",
                {"Description": "GROCER", "Batch": "20260105123456",
                 "null": ["short 1234"], "Ref": [FULL]})
        cur = self.conn.execute(MIGRATION.read_text())
        self.assertEqual(cur.rowcount, 0)

    def test_migration_and_live_rule_agree_on_prefixes_keys_and_words(self):
        """The backfill and the live rule must cut the same rows and the
        same cells, or rows stored before and after the rule differ."""
        sql = MIGRATION.read_text()
        prefixes = re.search(r"split_part\(t\.id, ':', 1\) IN \((.*?)\)",
                             sql, re.S).group(1)
        self.assertEqual(
            tuple(p + ":" for p in re.findall(r"'([^']*)'", prefixes)),
            sync_base.FILE_IMPORT_ID_PREFIXES)
        key_lists = re.findall(r"IN \(('name'.*?)\)", sql, re.S)
        self.assertEqual(len(key_lists), 3)
        for keys in key_lists:
            self.assertEqual(frozenset(re.findall(r"'([^']*)'", keys)),
                             sync_base._FREE_TEXT_KEYS
                             | sync_base._OVERFLOW_KEYS)
        words = re.findall(r"~ '\((memo\|[^)]*)\)'", sql)
        self.assertEqual(len(words), 3)
        for w in words:
            self.assertEqual(tuple(w.split("|")), sync_base._FREE_TEXT_WORDS)


if __name__ == "__main__":
    unittest.main()
