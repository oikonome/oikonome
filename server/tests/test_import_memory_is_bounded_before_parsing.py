"""An uploaded file cannot cost the shared process more memory than its
format's cap allows.

Upload parsing runs in the web process (and in the folder-import worker)
that every household shares, inside a container with a fixed memory
limit, so a refusal that comes only after the parse has built the whole
file in memory comes too late: the container is killed first. Each guard
here refuses, or bounds, BEFORE the expensive structure exists:

* OFX/QFX: a byte ceiling and a row count read from the raw bytes run
  before ofxtools is called, and the parse itself runs in a child under an
  address-space limit, so a file the estimate misses costs only the child;
* QIF: lines are read one at a time, never materialized as a list;
* Mint/YNAB/Monarch/Copilot/Simplifi and the generic column-mapped CSV:
  a header far wider than any real export is refused before rows become
  dicts.
"""

import tracemalloc
import unittest
from unittest import mock

from ofxtools.Parser import OFXTree

from oikonome.sync import competitors, csvimport, mintimport, ofximport, \
    qifimport, rowcap, ynabimport
from oikonome.web import bulk_import

from .bulk_job_util import submit_and_run
from .test_ofximport import OFX
from .test_competitors import MONARCH
from .util import make_db, write_config

_HEAD = OFX.split(b"<STMTTRN>", 1)[0]
_TAIL = b"</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n"
_ROW = (b"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260710<TRNAMT>-1"
        b"<FITID>%d<NAME>a</STMTTRN>")


def _ofx(rows: int, pad: int = 0) -> bytes:
    return (_HEAD + b"".join(_ROW % i for i in range(rows))
            + b" " * pad + _TAIL)


def _never(*_a, **_k):
    raise AssertionError("ofxtools was called on a file it must not see")


class OfxIsRefusedBeforeItIsParsed(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def test_one_row_past_the_cap_is_refused_without_parsing(self):
        data = _ofx(rowcap.MAX_IMPORT_ROWS + 1)
        with mock.patch.object(OFXTree, "parse", _never), \
                mock.patch.object(ofximport, "IN_PROCESS", True), \
                self.assertRaises(ValueError) as caught:
            ofximport.import_ofx(self.conn, "chk", data)
        self.assertIn("too many transactions", str(caught.exception))

    def test_investment_lines_count_against_the_cap(self):
        inv = (b"<BUYSTOCK><INVBUY><INVTRAN><FITID>x<DTTRADE>20260710"
               b"</INVTRAN></INVBUY></BUYSTOCK>")
        data = _HEAD + inv * (rowcap.MAX_IMPORT_ROWS + 1) + _TAIL
        with mock.patch.object(OFXTree, "parse", _never), \
                mock.patch.object(ofximport, "IN_PROCESS", True), \
                self.assertRaises(ValueError):
            ofximport.parse_ofx(data)

    def test_a_file_past_the_byte_ceiling_is_refused_without_parsing(self):
        data = _ofx(3, pad=ofximport.MAX_OFX_BYTES)
        with mock.patch.object(OFXTree, "parse", _never), \
                mock.patch.object(ofximport, "IN_PROCESS", True), \
                self.assertRaises(ValueError) as caught:
            ofximport.import_ofx(self.conn, "chk", data)
        self.assertIn("too large", str(caught.exception))

    def test_a_parse_past_the_child_memory_limit_is_refused(self):
        """What the estimate misses is contained by the child: a file
        under both caps that still blows up the parse ends in a refusal,
        and the calling process lives."""
        with mock.patch.object(ofximport, "CHILD_MEMORY", 8 * 1024 * 1024):
            with self.assertRaises(ValueError) as caught, \
                    self.assertLogs(ofximport.log, "WARNING"):
                ofximport.parse_ofx(_ofx(20_000))
        self.assertIn("within the limits", str(caught.exception))

    def test_an_ordinary_file_still_imports_through_the_child(self):
        r = ofximport.import_ofx(self.conn, "chk", OFX)
        self.assertEqual(r["imported"], 3)
        rows = ofximport.parse_ofx(OFX)
        self.assertEqual([x["fitid"] for x in rows], ["F001", "F002", "F003"])
        self.assertEqual(rows[0]["window"][0].isoformat(), "2026-07-01")
        self.assertEqual(rows[0]["acct"], "987654321")

    def test_a_folder_import_takes_the_same_guarded_path(self):
        plan = bulk_import.analyze(self.conn, [
            ("big.ofx", _ofx(rowcap.MAX_IMPORT_ROWS + 1)),
            ("small.ofx", OFX)])
        for f in plan["files"]:
            f["account_id"] = "chk"
        seen = []
        real = ofximport.parse_ofx

        def spy(data, tenant=None):
            seen.append(len(data))
            return real(data, tenant=tenant)
        with mock.patch.object(OFXTree, "parse", _never), \
                mock.patch.object(ofximport, "parse_ofx", spy):
            results = submit_and_run(self.conn, plan["token"], plan["files"])
        by_name = {r["name"]: r for r in results}
        self.assertFalse(by_name["big.ofx"]["ok"])
        self.assertIn("too many transactions", by_name["big.ofx"]["error"])
        self.assertTrue(by_name["small.ofx"]["ok"], by_name["small.ofx"])
        self.assertEqual(len(seen), 2)


class TextImportersStayBounded(unittest.TestCase):
    def test_qif_reads_lines_without_building_them_all(self):
        text = "a\n" * 500_000                       # no record ever closes
        tracemalloc.start()
        try:
            qifimport.parse_qif(text)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, len(text))

    def test_qif_still_splits_every_kind_of_line_end(self):
        rec = ["!Type:Bank", "D01/02/2024", "T-4.00", "PCOFFEE", "^"]
        for sep in ("\n", "\r\n", "\r"):
            rows = qifimport.parse_qif(sep.join(rec * 2) + sep)
            self.assertEqual([r["payee"] for r in rows],
                             ["COFFEE", "COFFEE"], repr(sep))

    def test_a_register_with_a_runaway_header_is_refused(self):
        extra = ",".join(f"c{i}" for i in range(1000))
        ynab = ("Account,Date,Payee,Memo,Outflow,Inflow," + extra + "\n"
                + "A,01/02/2024,P,,1.00,," + "," * 999 + "\n")
        with self.assertRaises(ValueError) as caught:
            ynabimport.parse(ynab)
        self.assertIn("columns", str(caught.exception))
        mint = ("Date,Description,Original Description,Amount,"
                "Transaction Type,Category,Account Name," + extra + "\n"
                + "01/02/2024,P,P,1.00,debit,Food,Card," + "," * 999 + "\n")
        with self.assertRaises(ValueError):
            mintimport.import_mint(None, "chk", mint)

    def test_an_ordinary_register_is_not_refused(self):
        ynab = ("Account,Flag,Date,Payee,Category Group/Category,"
                "Category Group,Category,Memo,Outflow,Inflow,Cleared\n"
                "Card,,01/02/2024,P,,,,,1.00,,Cleared\n")
        self.assertEqual(len(ynabimport.parse(ynab)), 1)


class CsvHeadersStayBounded(unittest.TestCase):
    """The column-mapped CSV and the other-app exports build a dict over
    the whole header for every row; a thousand-column header must be
    refused before the first row is read."""

    WIDE = ",".join(f"c{i}" for i in range(1000))
    MAP = {"date": "Date", "name": "Description", "amount": "Amount"}

    def _rows_never_read(self):
        def boom(*_a, **_k):
            raise AssertionError("a row was read past a refused header")
        return mock.patch.object(csvimport, "capped", boom)

    def test_a_generic_csv_with_a_runaway_header_is_refused(self):
        text = ("Date,Description,Amount," + self.WIDE + "\n"
                + "01/02/2024,P,-1.00\n")
        with self._rows_never_read(), \
                self.assertRaises(ValueError) as caught:
            csvimport.import_csv(None, "chk", text, self.MAP)
        self.assertIn("1,003 columns", str(caught.exception))

    def test_other_app_exports_with_a_runaway_header_are_refused(self):
        heads = {competitors.import_monarch:
                     "Date,Merchant,Category,Account,Original Statement,"
                     "Notes,Amount,Tags",
                 competitors.import_copilot:
                     "date,name,amount,status,category,parent category,"
                     "excluded,tags,type,account,account mask,note,recurring",
                 competitors.import_simplifi:
                     "Date,Payee,Amount,Account,Category"}
        for imp, head in heads.items():
            text = head + "," + self.WIDE + "\n" + "2024-01-02,P\n"
            with mock.patch.object(competitors, "capped",
                                   side_effect=AssertionError("row read")), \
                    self.assertRaises(ValueError, msg=imp.__name__) as caught:
                imp(None, "chk", text)
            self.assertIn("columns", str(caught.exception))

    def test_ordinary_files_still_import(self):
        conn = make_db()
        write_config(conn)
        self.addCleanup(conn.close)
        text = "Date,Description,Amount\n01/02/2024,CORNER SHOP,-4.00\n"
        self.assertEqual(
            csvimport.import_csv(conn, "chk", text, self.MAP)["imported"], 1)
        self.assertGreater(
            competitors.import_monarch(conn, "chk", MONARCH)["imported"], 0)


if __name__ == "__main__":
    unittest.main()
