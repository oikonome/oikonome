"""Every file importer caps ROWS, not just bytes.

The 50 MB upload cap bounds nothing that matters — ~12-byte rows fit about
four million to a file, and each one is parsed, dedup-scanned O(candidates)
and INSERTed synchronously inside the request handler. The collector doors
(plan_csv, coinbase_push) carry MAX_ROWS for that reason; every importer a
user can reach needs it too.
"""
import datetime as dt
import unittest
from unittest import mock

from oikonome.sync import (base, competitors, csvimport, mintimport,
                           ofximport, pdfimport, qifimport, rowcap,
                           ynabimport)

from .test_reporting import add_account
from .util import _ensure_db, make_db


def _rows(n, header="Date,Amount,Description\n", row="1/1/24,1,a\n"):
    return header + row * n


class RowCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        add_account(self.conn, "acct-1", "Checking", "depository",
                    "checking", 100)

    def tearDown(self):
        self.conn.close()

    def test_generator_refuses_past_the_cap_without_materializing(self):
        seen = []

        def endless():
            i = 0
            while True:
                i += 1
                seen.append(i)
                yield {"n": i}

        with self.assertRaises(ValueError) as e:
            list(rowcap.capped(endless(), limit=10))
        self.assertIn("too many", str(e.exception))
        # it stopped AT the cap rather than reading an unbounded input
        self.assertEqual(len(seen), 11)

    def test_csv_import_refuses_an_oversized_file(self):
        text = _rows(rowcap.MAX_IMPORT_ROWS + 1)
        with self.assertRaises(ValueError) as e:
            csvimport.import_csv(self.conn, "acct-1", text,
                                 {"date": "Date", "amount": "Amount",
                                  "name": "Description"})
        self.assertIn("max", str(e.exception))

    def test_a_normal_file_still_imports(self):
        """The cap must not touch a realistic export."""
        out = csvimport.import_csv(self.conn, "acct-1", _rows(50),
                                   {"date": "Date", "amount": "Amount",
                                    "name": "Description"})
        self.assertEqual(out["rows"], 50)

    def test_every_importer_family_carries_the_cap(self):
        """One row cap, every door — mint/ynab/monarch/copilot/simplifi and
        the OFX/QIF parsers."""
        big = _rows(rowcap.MAX_IMPORT_ROWS + 1,
                    header="Date,Amount,Description,Category\n",
                    row="1/1/24,1,a,x\n")
        for fn in (mintimport.import_mint, ynabimport.import_ynab,
                   competitors.import_monarch, competitors.import_copilot,
                   competitors.import_simplifi):
            with self.subTest(fn=fn.__name__):
                with self.assertRaises(ValueError):
                    fn(self.conn, "acct-1", big)
        qif = "!Type:Bank\n" + ("D1/1/24\nT-1.00\nPa\n^\n"
                                * (rowcap.MAX_IMPORT_ROWS + 1))
        with self.assertRaises(ValueError):
            qifimport.import_qif(self.conn, "acct-1", qif)
        # OFX arrives as a parsed list, so the cap sits on check_len — assert
        # the guard itself rather than hand-rolling a valid OFX document.
        with self.assertRaises(ValueError):
            ofximport.check_len([{"n": i}
                                 for i in range(rowcap.MAX_IMPORT_ROWS + 1)],
                                what="transactions")
        # PDF: the parser's own ceilings (pages, extracted-text bytes) are
        # far looser than the row cap — dense statement text packs well
        # past 50k transaction lines into 2 MB. Stub the parse so the test
        # exercises the import wiring, not pypdf.
        row = {"date": dt.date(2024, 1, 1), "amount": 1.0, "name": "a",
               "line": "x", "flow": None}
        parsed = {"rows": [dict(row)
                           for _ in range(rowcap.MAX_IMPORT_ROWS + 1)],
                  "confidence": 1.0, "period": None, "warnings": []}
        with mock.patch.object(pdfimport, "extract_text", return_value=""), \
                mock.patch.object(pdfimport, "parse_statement",
                                  return_value=parsed):
            with self.assertRaises(ValueError):
                pdfimport.import_pdf(self.conn, "acct-1", b"%PDF-")

    def test_aggregator_payloads_are_capped_at_the_upsert_funnel(self):
        """File uploads are capped at the door, but aggregator pulls
        (plaid/mx/simplefin) arrive through base.upsert_transactions —
        and one upstream (a tenant-supplied SimpleFIN bridge URL) is
        hostile-controllable, so an unbounded provider payload was an
        inline DoS. The funnel refuses an implausibly large batch."""
        txns = [base.Transaction(
            id=f"sfin:{i}", account_id="acct-1",
            date=dt.date(2024, 1, 1), amount=1.0, name="a")
            for i in range(101)]
        with mock.patch.object(rowcap, "MAX_SYNC_ROWS", 100):
            with self.assertRaises(ValueError) as e:
                base.upsert_transactions(self.conn, txns)
        self.assertIn("implausibly large", str(e.exception))
        # a batch at the ceiling still stores
        with mock.patch.object(rowcap, "MAX_SYNC_ROWS", 101):
            self.assertEqual(base.upsert_transactions(self.conn, txns), 101)


if __name__ == "__main__":
    unittest.main()
