"""The full bank account number must never land in transactions.raw.

An OFX/QFX statement's ACCTID is the account's true, unmasked number. The
importer keeps it on each parsed row only to route rows in a
multi-statement file; raw is plaintext JSONB that rides /export and the
data-portability archive, so anything past the last 4 digits stored there
is a durable PII leak in every imported row.
"""

import unittest

from oikonome.sync import ofximport

from .util import make_db, write_config

ACCTID = b"987654321"

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
</BANKTRANLIST>
<LEDGERBAL><BALAMT>1500.00<DTASOF>20260712</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


class OfxRawMinimizationTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_raw_carries_only_the_last_four(self):
        r = ofximport.import_ofx(self.conn, "chk", OFX)
        self.assertEqual(r["imported"], 2)
        rows = self.conn.execute(
            "SELECT raw FROM transactions WHERE id LIKE 'ofx:%'").fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            raw = dict(row["raw"])
            self.assertEqual(raw.get("acct"), ACCTID.decode()[-4:])

    def test_full_number_appears_nowhere_in_stored_raw(self):
        """Belt over the key check: however the dict is shaped, the full
        digit string must be absent from the serialized JSONB."""
        ofximport.import_ofx(self.conn, "chk", OFX)
        hits = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions "
            "WHERE raw::text LIKE %s", (f"%{ACCTID.decode()}%",)).fetchone()
        self.assertEqual(hits["n"], 0)

    def test_parser_still_sees_the_full_number_for_routing(self):
        """The split router keys statements by ACCTID suffix — parsing must
        keep the value; only the STORED raw is minimized."""
        rows = ofximport.parse_ofx(OFX)
        self.assertTrue(rows)
        self.assertTrue(all(r["acct"] == ACCTID.decode() for r in rows))
