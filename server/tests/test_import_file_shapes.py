"""What a person's file can look like, and what the importers must do
with it.

- A day-first (UK/EU/AU) export is dated day-first for EVERY row, decided
  once per file; otherwise 03/04 lands in March while 25/04 lands in
  April, and nothing says so.
- A two-column statement line whose amount is genuinely 0.00 keeps that
  amount; the running balance is never mistaken for it.
- A yearless statement with no period line anchors to the latest full
  date in the file, not to the day it happened to be imported.
- A QIF file is capped as it is parsed, not after.
- Non-finite money never reaches the ledger or the income spine.
- A malformed bulk decision is skipped, not a crash.
"""

import datetime as dt
import math
import unittest

from oikonome.engine import taxdocs
from oikonome.sync import base, csvimport, pdfimport, qifimport, rowcap
from oikonome.sync.base import Transaction
from oikonome.web import bulk_import

from .util import make_db, seed_accounts, write_config


class DateOrderTests(unittest.TestCase):
    def test_a_day_first_column_is_read_day_first_throughout(self):
        col = ["03/04/2024", "25/04/2024", "01/05/2024"]
        self.assertEqual(csvimport.date_order(col), "dmy")
        self.assertEqual(csvimport._parse_date("03/04/2024", "dmy"),
                         dt.date(2024, 4, 3))

    def test_a_month_first_column_stays_month_first(self):
        col = ["03/04/2024", "04/25/2024"]
        self.assertEqual(csvimport.date_order(col), "mdy")
        self.assertEqual(csvimport._parse_date("03/04/2024", "mdy"),
                         dt.date(2024, 3, 4))

    def test_a_garbage_cell_does_not_decide_the_file(self):
        # 99/01 is not a date under either order — it must not flip a
        # month-first file to day-first
        self.assertEqual(csvimport.date_order(["03/04/2020", "99/01/2020",
                                               "05/06/2020"]), "mdy")
        self.assertEqual(csvimport.date_order(["03/04/2020", "13/40/2020"]),
                         "mdy")

    def test_an_undecidable_column_reads_month_first(self):
        self.assertEqual(csvimport.date_order(["03/04/2024", "01/02/2024"]),
                         "mdy")
        self.assertEqual(csvimport.date_order(["2024-03-04"]), "mdy")

    def test_the_csv_importer_dates_a_day_first_file_consistently(self):
        conn = make_db()
        try:
            write_config(conn)
            seed_accounts(conn)
            acct = conn.execute("SELECT id FROM accounts LIMIT 1").fetchone()["id"]
            text = ("Date,Description,Amount\n"
                    "03/04/2024,Coffee,-3.50\n"
                    "25/04/2024,Groceries,-40.00\n")
            csvimport.import_csv(conn, acct, text,
                                 {"date": "Date", "name": "Description",
                                  "amount": "Amount"}, amount_sign="bank")
            dates = sorted(r["date"] for r in conn.execute(
                "SELECT date FROM transactions").fetchall())
            self.assertEqual(dates, [dt.date(2024, 4, 3), dt.date(2024, 4, 25)])
        finally:
            conn.close()

    def test_qif_dates_are_decided_per_file_too(self):
        rows = qifimport.parse_qif("D03/04/2024\nT-3.50\nPCoffee\n^\n"
                                   "D25/04/2024\nT-40\nPGroceries\n^\n")
        self.assertEqual([r["date"] for r in rows],
                         [dt.date(2024, 4, 3), dt.date(2024, 4, 25)])


class PdfAmountTests(unittest.TestCase):
    def test_a_two_column_zero_amount_is_zero_not_the_balance(self):
        out = pdfimport.parse_statement(
            "Statement period 01/01/2024 - 01/31/2024\n"
            "01/05 FEE WAIVED 0.00 1050.00\n", kind="bank")
        self.assertEqual(out["rows"][0]["amount"], 0.0)

    def test_a_three_column_zero_cell_is_still_skipped(self):
        out = pdfimport.parse_statement(
            "Statement period 01/01/2024 - 01/31/2024\n"
            "01/05 GROCERY MART 0.00 42.10 1007.90\n", kind="bank")
        self.assertEqual(abs(out["rows"][0]["amount"]), 42.10)

    def test_yearless_rows_anchor_to_the_files_own_dates(self):
        # no period line; one dated line carries a full year — every
        # yearless row lands in that year whatever today's date is
        out = pdfimport.parse_statement(
            "01/05 COFFEE 3.50 996.50\n"
            "01/20/2019 PAYROLL 1,000.00 1996.50\n"
            "01/25 RENT 900.00 1096.50\n", kind="bank")
        years = {r["date"].year for r in out["rows"]}
        self.assertEqual(years, {2019})


class QifCapTests(unittest.TestCase):
    def test_the_cap_fires_while_parsing(self):
        rec = "D01/01/2024\nT1\nPX\n^\n"
        text = rec * (rowcap.MAX_IMPORT_ROWS + 5)
        with self.assertRaises(ValueError):
            qifimport.parse_qif(text)

    def test_a_file_at_the_cap_parses_and_one_more_unterminated_does_not(self):
        rec = "D01/01/2024\nT1\nPX\n^\n"
        text = rec * rowcap.MAX_IMPORT_ROWS
        self.assertEqual(len(qifimport.parse_qif(text)), rowcap.MAX_IMPORT_ROWS)
        with self.assertRaises(ValueError):
            qifimport.parse_qif(text + "D01/02/2024\nT2\nPY\n")


class BatchBookkeepingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        seed_accounts(self.conn)
        self.acct = self.conn.execute(
            "SELECT id FROM accounts LIMIT 1").fetchone()["id"]

    def tearDown(self):
        self.conn.close()

    def test_discard_drops_an_empty_batch_and_keeps_one_that_owns_rows(self):
        from oikonome.sync import batches
        empty = batches.create(self.conn, "csv", self.acct, "a.csv")
        batches.discard(self.conn, empty)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM import_batches WHERE id=%s", (empty,)).fetchone())
        # a batch whose importer died after some rows landed keeps its
        # record — those rows must stay reachable through rollback
        partial = batches.create(self.conn, "csv", self.acct, "b.csv")
        txns = batches.tag(self.conn, [Transaction(id="bk-1", account_id=self.acct,
                                        date=dt.date(2024, 1, 5),
                                        name="landed", amount=5.0)], partial)
        base.upsert_transactions(self.conn, txns)
        batches.discard(self.conn, partial)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM import_batches WHERE id=%s", (partial,)).fetchone())

    def test_recent_counts_what_a_rollback_would_take_with_it(self):
        from oikonome.sync import batches
        bid = batches.create(self.conn, "csv", self.acct, "c.csv")
        txns = batches.tag(self.conn, [Transaction(id="bk-2", account_id=self.acct,
                                        date=dt.date(2024, 1, 5),
                                        name="noted", amount=5.0)], bid)
        base.upsert_transactions(self.conn, txns)
        batches.finish(self.conn, bid, 1)
        self.conn.execute("INSERT INTO transaction_notes (txn_id, note) "
                          "VALUES ('bk-2', 'hi')")
        self.conn.execute("INSERT INTO business_flags (txn_id) VALUES ('bk-2')")
        self.conn.execute("UPDATE transactions SET category_override='x' "
                          "WHERE id='bk-2'")
        row = [b for b in batches.recent(self.conn) if b["id"] == bid][0]
        self.assertEqual(row["annotations"], 3)
        batches.rollback(self.conn, bid)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM transaction_notes WHERE txn_id='bk-2'").fetchone())


class FiniteMoneyTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        seed_accounts(self.conn)
        self.acct = self.conn.execute(
            "SELECT id FROM accounts LIMIT 1").fetchone()["id"]

    def tearDown(self):
        self.conn.close()

    def test_upsert_drops_a_non_finite_amount(self):
        rows = [Transaction(id="fin-1", account_id=self.acct,
                            date=dt.date(2024, 1, 5), name="ok", amount=5.0),
                Transaction(id="fin-2", account_id=self.acct,
                            date=dt.date(2024, 1, 5), name="nan",
                            amount=math.nan),
                Transaction(id="fin-3", account_id=self.acct,
                            date=dt.date(2024, 1, 5), name="inf",
                            amount=math.inf)]
        base.upsert_transactions(self.conn, rows)
        ids = {r["id"] for r in self.conn.execute(
            "SELECT id FROM transactions").fetchall()}
        self.assertEqual(ids, {"fin-1"})

    def test_tax_commit_refuses_non_finite_numbers(self):
        with self.assertRaises(ValueError):
            taxdocs._commit_rows(self.conn, "irs",
                                 [{"year": 2020, "agi": math.nan}], [2020])
        with self.assertRaises(ValueError):
            taxdocs._commit_rows(self.conn, "w2",
                                 [{"year": 2020, "kind": "w2",
                                   "boxes": {"1": math.inf}}], [2020])


class BulkDecisionTests(unittest.TestCase):
    def test_malformed_decisions_are_skipped(self):
        conn = make_db()
        try:
            write_config(conn)
            from oikonome.db import staging
            token = staging.put(conn, "bulk", [("a.csv", b"Date,Name,Amount\n")],
                                {})
            out = bulk_import.run(conn, token, [None, "x", {"index": None},
                                                {"index": "q", "action": "import"}])
            self.assertEqual(out, [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
