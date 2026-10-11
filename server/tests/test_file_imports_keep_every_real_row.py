"""Every real row of an imported file lands once, on the right account,
and a malformed corner of a file never costs the rest of it.

- A row with more cells than the header (a trailing comma) is read, not a
  crash that refuses the whole file.
- A FITID is a hint, not a key: two different charges carrying one FITID
  (in one file or across two) both survive, and re-importing changes
  nothing.
- A Quicken whole-file export's list sections (securities, categories)
  are skipped, never read as amounts.
- The plan-activity door refuses numbers no account could hold, so no
  holding or balance becomes Infinity.
"""

import unittest

from oikonome.sync import (competitors, mintimport, ofximport, plan_csv,
                           qifimport, ynabimport)

from .util import make_db, write_config


def _ofx(*rows: tuple[str, str, str, str]) -> bytes:
    """A one-statement OFX; rows are (fitid, yyyymmdd, amount, name)."""
    body = "".join(
        f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}"
        f"<NAME>{n}</STMTTRN>\n" for f, d, a, n in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\n"
            "ENCODING:USASCII\nCHARSET:1252\nCOMPRESSION:NONE\n"
            "OLDFILEUID:NONE\nNEWFILEUID:NONE\n\n<OFX>\n"
            "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
            "<DTSERVER>20260712120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>\n"
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO"
            "</STATUS>\n<STMTRS><CURDEF>USD\n<BANKACCTFROM><BANKID>1"
            "<ACCTID>5555<ACCTTYPE>CHECKING</BANKACCTFROM>\n"
            "<BANKTRANLIST><DTSTART>20260101<DTEND>20260301\n" + body +
            "</BANKTRANLIST>\n<LEDGERBAL><BALAMT>0<DTASOF>20260301"
            "</LEDGERBAL>\n</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n</OFX>\n"
            ).encode()


class _Db(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rows(self, like: str):
        return sorted((r["account_id"], str(r["date"]), float(r["amount"]),
                       r["name"]) for r in self.conn.execute(
            "SELECT account_id, date, amount, name FROM transactions "
            "WHERE id LIKE %s", (like,)))


class OverflowCellTests(_Db):
    """csv.DictReader files cells past the header under the key None."""

    def test_ynab_reads_a_row_with_a_trailing_comma(self):
        text = ('"Date","Payee","Memo","Outflow","Inflow"\n'
                '"2026-07-10","Safeway","","$42.50","$0.00",\n')
        r = ynabimport.import_ynab(self.conn, "chk", text)
        self.assertEqual(r["imported"], 1)

    def test_mint_reads_a_row_with_a_trailing_comma(self):
        text = ("Date,Description,Original Description,Amount,"
                "Transaction Type,Category,Account Name,Labels,Notes\n"
                "7/10/2026,Safeway,SAFEWAY 123,42.50,debit,Groceries,"
                "Checking,,,\n")
        r = mintimport.import_mint(self.conn, "chk", text)
        self.assertEqual(r["imported"], 1)

    def test_monarch_reads_a_row_with_a_trailing_comma(self):
        text = ("Date,Merchant,Category,Account,Original Statement,Notes,"
                "Amount,Tags\n"
                "2026-07-10,Safeway,Groceries,Checking,SAFEWAY 123,,"
                "-42.50,,\n")
        r = competitors.import_monarch(self.conn, "chk", text)
        self.assertEqual(r["imported"], 1)


class FitidIsAHintTests(_Db):

    def test_two_charges_sharing_a_fitid_in_one_file_both_land(self):
        data = _ofx(("A", "20260105", "-4.50", "COFFEE ONE"),
                    ("A", "20260105", "-4.50", "COFFEE TWO"),
                    ("A", "20260106", "-9.00", "LUNCH"))
        ofximport.import_ofx(self.conn, "chk", data)
        rows = self._rows("ofx:%")
        self.assertEqual(sorted(x[3] for x in rows),
                         ["COFFEE ONE", "COFFEE TWO", "LUNCH"])
        ofximport.import_ofx(self.conn, "chk", data)
        self.assertEqual(self._rows("ofx:%"), rows)

    def test_a_later_file_reusing_fitids_never_rewrites_the_earlier(self):
        jan = _ofx(("1", "20260101", "-1200.00", "RENT"),
                   ("2", "20260115", "-60.00", "POWER"))
        feb = _ofx(("1", "20260203", "-35.17", "GROCERY"),
                   ("2", "20260115", "-60.00", "POWER"))
        ofximport.import_ofx(self.conn, "chk", jan)
        ofximport.import_ofx(self.conn, "chk", feb)
        names = [x[3] for x in self._rows("ofx:%")]
        self.assertEqual(sorted(names), ["GROCERY", "POWER", "RENT"])
        before = self._rows("ofx:%")
        ofximport.import_ofx(self.conn, "chk", jan)
        ofximport.import_ofx(self.conn, "chk", feb)
        self.assertEqual(self._rows("ofx:%"), before)

    def test_free_text_numbers_are_masked_in_raw(self):
        data = _ofx(("Z", "20260105", "-50.00",
                     "ONLINE TRANSFER TO CHK 1234567890123"))
        ofximport.import_ofx(self.conn, "chk", data)
        raw = self.conn.execute(
            "SELECT raw::text AS r FROM transactions WHERE id LIKE 'ofx:%'"
        ).fetchone()["r"]
        self.assertNotIn("1234567890123", raw)
        self.assertNotIn("12345678", raw)


class QifListSectionTests(_Db):

    def test_a_whole_file_export_with_lists_imports_its_transactions(self):
        text = ("!Type:Cat\nNGroceries\nE\nT\n^\n"
                "!Type:Security\nNApple Inc\nSAAPL\nTStock\n^\n"
                "!Option:AutoSwitch\n!Account\nNChecking\nTBank\n^\n"
                "!Clear:AutoSwitch\n"
                "!Account\nNChecking\nTBank\n^\n"
                "!Type:Bank\nD01/15/2024\nT-42.50\nPSafeway\n^\n")
        rows = qifimport.parse_qif(text)
        self.assertEqual([(r["payee"], r["amount"]) for r in rows],
                         [("Safeway", -42.5)])
        r = qifimport.import_qif(self.conn, "chk", text)
        self.assertEqual(r["imported"], 1)


class PlanActivityBoundsTests(_Db):
    HEAD = ("Trade Date,Investments,Ticker,Transaction,Transaction Amount,"
            "Share Price,Total Shares\n")

    def test_numbers_past_any_real_account_are_refused(self):
        for row in ("01/02/2026,Fund A,ABC,Contribution,5,1e200,1e200\n",
                    "01/02/2026,Fund A,ABC,Contribution,1e307,10,1\n",
                    "01/02/2026,Fund A,ABC,Contribution,5,inf,1\n"):
            with self.subTest(row=row), self.assertRaises(ValueError):
                plan_csv.import_csv(self.conn, "401k", self.HEAD + row)
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM holdings").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_an_ordinary_row_still_imports(self):
        r = plan_csv.import_csv(
            self.conn, "401k",
            self.HEAD + "01/02/2026,Fund A,ABC,Contribution,500,25.00,20\n")
        self.assertEqual(r["holdings_value"], 500.0)


if __name__ == "__main__":
    unittest.main()
