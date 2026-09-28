"""OFX/QFX importer: parsing, sign flip, FITID idempotency, dedup."""

import unittest

from oikonome.sync import ofximport

from .util import add_txn, make_db, write_config
import datetime as dt

OFX = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>
<DTSERVER>20260712120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>USD
<BANKACCTFROM><BANKID>123456789<ACCTID>987654321<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260701<DTEND>20260712
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260710<TRNAMT>-42.50<FITID>F001<NAME>SAFEWAY STORE</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260711<TRNAMT>2000.00<FITID>F002<NAME>EMPLOYER PAYROLL</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260712<TRNAMT>-4.25<FITID>F003<NAME>COFFEE SHOP</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>1500.00<DTASOF>20260712</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


class OfxImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_import_signs_and_ids(self):
        r = ofximport.import_ofx(self.conn, "chk", OFX)
        self.assertEqual(r["imported"], 3)
        rows = {row["id"]: row for row in self.conn.execute(
            "SELECT id, amount, date FROM transactions WHERE id LIKE 'ofx:%'"
        ).fetchall()}
        self.assertEqual(rows["ofx:chk:F001"]["amount"], 42.50)   # out → +
        self.assertEqual(rows["ofx:chk:F002"]["amount"], -2000.0) # in → −
        self.assertEqual(rows["ofx:chk:F001"]["date"], dt.date(2026, 7, 10))

    def test_reimport_idempotent(self):
        ofximport.import_ofx(self.conn, "chk", OFX)
        ofximport.import_ofx(self.conn, "chk", OFX)
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE id LIKE 'ofx:%'"
        ).fetchone()["n"]
        self.assertEqual(n, 3)

    def test_cross_source_dedup(self):
        add_txn(self.conn, dt.date(2026, 7, 9), 42.50, "SAFEWAY", account="chk")
        r = ofximport.import_ofx(self.conn, "chk", OFX)
        self.assertEqual(r["skipped_duplicates"], 1)
        self.assertEqual(r["imported"], 2)

    def test_dedup_name_mismatch_does_not_suppress(self):
        """An unrelated same-amount charge a day earlier must not absorb
        the Safeway row: amount and date alone are not a match."""
        add_txn(self.conn, dt.date(2026, 7, 9), 42.50, "PETCO",
                account="chk", merchant="Petco")
        r = ofximport.import_ofx(self.conn, "chk", OFX)
        self.assertEqual(r["skipped_duplicates"], 0)
        self.assertEqual(r["imported"], 3)

    def test_xfer_trntype_maps_to_transfer(self):
        """TRNTYPE XFER → TRANSFER_OUT/IN by sign (spend-excluded)."""
        xfer = OFX.replace(
            b"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260710<TRNAMT>-42.50"
            b"<FITID>F001<NAME>SAFEWAY STORE</STMTTRN>",
            b"<STMTTRN><TRNTYPE>XFER<DTPOSTED>20260710<TRNAMT>-500.00"
            b"<FITID>F001<NAME>TO SAVINGS</STMTTRN>").replace(
            b"<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260711<TRNAMT>2000.00"
            b"<FITID>F002<NAME>EMPLOYER PAYROLL</STMTTRN>",
            b"<STMTTRN><TRNTYPE>XFER<DTPOSTED>20260711<TRNAMT>250.00"
            b"<FITID>F002<NAME>FROM SAVINGS</STMTTRN>")
        ofximport.import_ofx(self.conn, "chk", xfer)
        rows = {r["name"]: r["category_primary"] for r in self.conn.execute(
            "SELECT name, category_primary FROM transactions "
            "WHERE id LIKE 'ofx:%'").fetchall()}
        self.assertEqual(rows["TO SAVINGS"], "TRANSFER_OUT")   # money out
        self.assertEqual(rows["FROM SAVINGS"], "TRANSFER_IN")  # money in
        self.assertIsNone(rows["COFFEE SHOP"])


if __name__ == "__main__":
    unittest.main()
