"""Every import refuses a date whose year the app cannot read.

A year like 0001 or 9999 is a valid calendar date, so strptime and
fromisoformat accept it and Postgres stores it — and then every later reader
that goes through as_date (the Today payload's month walk, the ledger
search) raises on that row, turning one bad import line into a page that
500s. The importers now parse through the same range check, so such a line
is refused at the door (a bad file or payload) or skipped (a PDF line)
instead of stored. The bank feeds (OFX, MX, SimpleFIN) skip that one row and
keep the rest, since a whole pull must not fail on one bad line.
"""

import copy
import unittest

from oikonome.sync import (coinbase_push, competitors, csvimport, mintimport,
                           mx, ofximport, pdfimport, qifimport, simplefin,
                           ynabimport)

from .test_mx import PAYLOAD as MX_PAYLOAD
from .test_mx import _save_creds as _save_mx_creds
from .test_mx import _transport as _mx_transport
from .test_ofximport import OFX
from .test_simplefin import PAYLOAD as SFIN_PAYLOAD
from .test_simplefin import _transport as _sfin_transport
from .util import make_db, write_config

MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}


class ImportDatesInRangeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _stored_out_of_range(self) -> int:
        return self.conn.execute(
            "SELECT count(*) n FROM transactions "
            "WHERE date < '1800-01-01' OR date > '2200-12-31'").fetchone()["n"]

    def test_csv_refuses_a_year_the_app_cannot_read(self):
        csv_text = "Date,Description,Amount\n0001-01-01,CORNER BAKERY,-12.00\n"
        with self.assertRaises(ValueError):
            csvimport.import_csv(self.conn, "chk", csv_text, MAPPING)
        self.assertEqual(self._stored_out_of_range(), 0)

    def test_qif_refuses_a_year_the_app_cannot_read(self):
        with self.assertRaises(ValueError):
            qifimport._date("01/01/9999")
        self.assertEqual(str(qifimport._date("01/02/2024")), "2024-01-02")

    def test_coinbase_push_refuses_a_year_the_app_cannot_read(self):
        payload = {
            "items": [{"id": "cb-dates", "institution_name": "Coinbase"}],
            "accounts": [{"id": "cb-dates", "item_id": "cb-dates",
                          "name": "Coinbase (dates)", "type": "investment",
                          "subtype": "crypto", "balance_current": 10.0}],
            "transactions": [{"id": "coinbase:far", "account_id": "cb-dates",
                              "date": "0001-01-01", "amount": 5.0,
                              "name": "BUY BTC"}]}
        with self.assertRaises(ValueError):
            coinbase_push.import_payload(self.conn, payload)
        self.assertEqual(self._stored_out_of_range(), 0)

    def test_pdf_skips_a_line_in_a_year_the_app_cannot_read(self):
        self.assertIsNone(pdfimport._date_or_none(1, 1, 1))
        self.assertIsNone(pdfimport._date_or_none(9999, 12, 31))
        self.assertEqual(str(pdfimport._date_or_none(2026, 6, 3)), "2026-06-03")

    def test_mint_refuses_a_year_the_app_cannot_read(self):
        with self.assertRaises(ValueError):
            mintimport._date("06/30/9999")
        with self.assertRaises(ValueError):
            mintimport._date("0001-01-01")
        self.assertEqual(str(mintimport._date("01/02/2024")), "2024-01-02")

    def test_ynab_refuses_a_year_the_app_cannot_read(self):
        with self.assertRaises(ValueError):
            ynabimport._date("9999-06-30")
        with self.assertRaises(ValueError):
            ynabimport._date("01/01/0001")
        self.assertEqual(str(ynabimport._date("2024-01-02")), "2024-01-02")

    def test_monarch_copilot_simplifi_refuse_a_year_the_app_cannot_read(self):
        text = ("Date,Merchant,Category,Account,Original Statement,Notes,"
                "Amount,Tags\n"
                "9999-06-30,Corner Bakery,Groceries,Checking,CORNER BAKERY,,"
                "-12.00,\n")
        with self.assertRaises(ValueError):
            competitors.import_monarch(self.conn, "chk", text)
        self.assertEqual(self._stored_out_of_range(), 0)
        with self.assertRaises(ValueError):
            competitors._date("01/01/0001")
        self.assertEqual(str(competitors._date("07/10/2026")), "2026-07-10")

    def test_ofx_skips_a_row_in_a_year_the_app_cannot_read(self):
        data = (OFX.replace(b"<DTPOSTED>20260710", b"<DTPOSTED>00010101")
                   .replace(b"<DTPOSTED>20260711", b"<DTPOSTED>99990630"))
        r = ofximport.import_ofx(self.conn, "chk", data)
        self.assertEqual(r["imported"], 1)
        self.assertEqual(self._stored_out_of_range(), 0)

    def test_mx_skips_a_row_in_a_year_the_app_cannot_read(self):
        payload = copy.deepcopy(MX_PAYLOAD)
        payload["GET /users/USR-1/transactions"]["transactions"][0][
            "transacted_at"] = "9999-06-30T08:00:00Z"
        _save_mx_creds(self.conn)
        mx.sync(self.conn, transport=_mx_transport(payload))
        ids = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'mx:%'").fetchall()}
        self.assertEqual(ids, {"mx:TRN-2"})
        self.assertEqual(self._stored_out_of_range(), 0)

    def test_simplefin_skips_a_row_whose_timestamp_is_out_of_range(self):
        payload = copy.deepcopy(SFIN_PAYLOAD)
        txns = payload["accounts"][0]["transactions"]
        txns.append({"id": "far-future", "posted": 253386489600,  # year 9999
                     "amount": "-1.00", "description": "CORNER BAKERY"})
        txns.append({"id": "overflow", "posted": 10 ** 20,
                     "amount": "-1.00", "description": "CORNER BAKERY"})
        txns.append({"id": "far-past", "posted": -10 ** 12,
                     "amount": "-1.00", "description": "CORNER BAKERY"})
        simplefin.sync(self.conn, "sfin-demo",
                       "https://u:p@bridge.test/simplefin",
                       transport=_sfin_transport(payload))
        ids = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'sfin:%'").fetchall()}
        self.assertEqual(ids, {"sfin:acc-chk:t1", "sfin:acc-chk:t2",
                               "sfin:acc-card:t3"})
        self.assertEqual(self._stored_out_of_range(), 0)


if __name__ == "__main__":
    unittest.main()
