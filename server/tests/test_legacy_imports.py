"""Import-hub sources: Mint export (sign from Transaction Type,
category mapping, transfer direction) and Quicken QIF (line format, date
quirks). Both idempotent with cross-source dedup."""

import datetime as dt
import unittest

from oikonome.sync import mintimport, qifimport

from .util import make_db, write_config

MINT = """Date,Description,Original Description,Amount,Transaction Type,Category,Account Name,Labels,Notes
7/10/2026,Safeway,SAFEWAY STORE 123,42.50,debit,Groceries,Checking,,
7/11/2026,Paycheck,EMPLOYER PAYROLL,2000.00,credit,Paycheck,Checking,,
7/12/2026,Transfer to Savings,ONLINE TRANSFER,500.00,debit,Transfer,Checking,,
7/12/2026,Refund,ONLINE TRANSFER BACK,500.00,credit,Transfer,Checking,,
7/13/2026,Card Payment,CHASE EPAY,300.00,debit,Credit Card Payment,Checking,,
"""

QIF = """!Type:Bank
D07/10'26
T-42.50
PSAFEWAY STORE
LGroceries
^
D07/11/2026
T2000.00
PEMPLOYER PAYROLL
^
D07/12/2026
T-4.25
PCOFFEE SHOP
MDouble shot
^
"""


class MintImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_detects_and_imports_with_categories(self):
        header = MINT.splitlines()[0].split(",")
        self.assertTrue(mintimport.looks_like_mint(header))
        r = mintimport.import_mint(self.conn, "chk", MINT)
        self.assertEqual(r["imported"], 5)
        rows = {r["name"]: r for r in self.conn.execute(
            "SELECT name, amount, category_primary, category_detailed "
            "FROM transactions WHERE id LIKE 'mint:%'").fetchall()}
        # debit → +42.50 out; credit → −2000 in
        self.assertEqual(rows["SAFEWAY STORE 123"]["amount"], 42.5)
        self.assertEqual(rows["SAFEWAY STORE 123"]["category_primary"],
                         "FOOD_AND_DRINK")
        self.assertEqual(rows["EMPLOYER PAYROLL"]["amount"], -2000.0)
        self.assertEqual(rows["EMPLOYER PAYROLL"]["category_primary"], "INCOME")
        # Transfer direction decided by sign
        self.assertEqual(rows["ONLINE TRANSFER"]["category_primary"],
                         "TRANSFER_OUT")
        self.assertEqual(rows["ONLINE TRANSFER BACK"]["category_primary"],
                         "TRANSFER_IN")
        # CC payment gets the detailed code the spend definition excludes
        self.assertEqual(rows["CHASE EPAY"]["category_detailed"],
                         "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")

    def test_idempotent(self):
        mintimport.import_mint(self.conn, "chk", MINT)
        mintimport.import_mint(self.conn, "chk", MINT)
        n = self.conn.execute("SELECT COUNT(*) AS n FROM transactions "
                              "WHERE id LIKE 'mint:%'").fetchone()["n"]
        self.assertEqual(n, 5)


class QifImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_parse_and_import(self):
        recs = qifimport.parse_qif(QIF)
        self.assertEqual(len(recs), 3)
        # the '26 apostrophe-year quirk
        self.assertEqual(recs[0]["date"], dt.date(2026, 7, 10))
        r = qifimport.import_qif(self.conn, "chk", QIF)
        self.assertEqual(r["imported"], 3)
        rows = {r["name"]: r["amount"] for r in self.conn.execute(
            "SELECT name, amount FROM transactions WHERE id LIKE 'qif:%'"
        ).fetchall()}
        self.assertEqual(rows["SAFEWAY STORE"], 42.5)     # bank − → engine +
        self.assertEqual(rows["EMPLOYER PAYROLL"], -2000.0)
        self.assertEqual(rows["COFFEE SHOP"], 4.25)

    def test_idempotent_and_cross_source_dedup(self):
        from .util import add_txn
        add_txn(self.conn, dt.date(2026, 7, 9), 42.50, "SAFEWAY",
                account="chk")
        r = qifimport.import_qif(self.conn, "chk", QIF)
        self.assertEqual(r["skipped_duplicates"], 1)      # aggregator wins
        qifimport.import_qif(self.conn, "chk", QIF)
        n = self.conn.execute("SELECT COUNT(*) AS n FROM transactions "
                              "WHERE id LIKE 'qif:%'").fetchone()["n"]
        self.assertEqual(n, 2)

    def test_bracket_category_is_quicken_transfer_marker(self):
        """L[AccountName] → TRANSFER_OUT/IN by sign; plain categories
        untouched."""
        qif = ("!Type:Bank\n"
               "D07/10/2026\nT-500.00\nPTRANSFER TO SAVINGS\nL[Savings]\n^\n"
               "D07/11/2026\nT500.00\nPTRANSFER FROM SAVINGS\nL[Savings]\n^\n"
               "D07/12/2026\nT-42.50\nPSAFEWAY\nLGroceries\n^\n")
        qifimport.import_qif(self.conn, "chk", qif)
        rows = {r["name"]: r["category_primary"] for r in self.conn.execute(
            "SELECT name, category_primary FROM transactions "
            "WHERE id LIKE 'qif:%'").fetchall()}
        self.assertEqual(rows["TRANSFER TO SAVINGS"], "TRANSFER_OUT")
        self.assertEqual(rows["TRANSFER FROM SAVINGS"], "TRANSFER_IN")
        self.assertIsNone(rows["SAFEWAY"])


YNAB = """"Account","Flag","Date","Payee","Category Group/Category","Category Group","Category","Memo","Outflow","Inflow","Cleared"
"Checking","","2026-07-10","Safeway","Everyday: Groceries","Everyday","Groceries","","$42.50","$0.00","Cleared"
"Checking","","2026-07-11","Employer","Inflow: Ready to Assign","Inflow","Ready to Assign","","$0.00","$2,000.00","Cleared"
"""


class YnabImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_detect_and_signs(self):
        from oikonome.sync import ynabimport
        import csv as _csv, io as _io
        header = next(_csv.reader(_io.StringIO(YNAB)))
        self.assertTrue(ynabimport.looks_like_ynab(header))
        r = ynabimport.import_ynab(self.conn, "chk", YNAB)
        self.assertEqual(r["imported"], 2)
        rows = {r["name"]: r["amount"] for r in self.conn.execute(
            "SELECT name, amount FROM transactions WHERE id LIKE 'ynab:%'"
        ).fetchall()}
        self.assertEqual(rows["Safeway"], 42.5)       # outflow → +
        self.assertEqual(rows["Employer"], -2000.0)   # inflow → −

    def test_transfer_and_starting_balance_payees_map_to_transfer(self):
        """YNAB's transfer marker payee ("Transfer : <Account>") and its
        "Starting Balance" seed row are flows, not spend/income."""
        from oikonome.sync import ynabimport
        ynab = ('"Date","Payee","Memo","Outflow","Inflow"\n'
                '"2026-07-09","Starting Balance","","$0.00","$5,000.00"\n'
                '"2026-07-10","Transfer : Savings","","$500.00","$0.00"\n'
                '"2026-07-11","Transfer : Savings","","$0.00","$250.00"\n'
                '"2026-07-12","Safeway","","$42.50","$0.00"\n')
        ynabimport.import_ynab(self.conn, "chk", ynab)
        rows = {(r["name"], float(r["amount"])): r["category_primary"]
                for r in self.conn.execute(
                    "SELECT name, amount, category_primary FROM transactions "
                    "WHERE id LIKE 'ynab:%'").fetchall()}
        self.assertEqual(rows[("Starting Balance", -5000.0)], "TRANSFER_IN")
        self.assertEqual(rows[("Transfer : Savings", 500.0)], "TRANSFER_OUT")
        self.assertEqual(rows[("Transfer : Savings", -250.0)], "TRANSFER_IN")
        self.assertIsNone(rows[("Safeway", 42.5)])


class RawIdentifierScrubTests(unittest.TestCase):
    """transactions.raw leaves the instance verbatim (/export + the
    data-portability archive), so no importer in the CSV family may store a
    full account or routing number in it — the rule ofximport already
    applies to the OFX ACCTID. Mint and YNAB detect their formats by column
    SUBSET, so a bank re-export or converter output carrying an account
    column imports as one of them — and must not store it whole."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _raw(self, prefix):
        return self.conn.execute(
            f"SELECT raw FROM transactions WHERE id LIKE '{prefix}:%'"
        ).fetchone()["raw"]

    def test_mint_row_keeps_names_but_not_numbers(self):
        text = (
            "Date,Description,Original Description,Amount,Transaction Type,"
            "Category,Account Name,Labels,Notes,Account Number\n"
            "7/10/2026,Safeway,SAFEWAY STORE 123,42.50,debit,Groceries,"
            "Checking,,,1234567890123456\n")
        mintimport.import_mint(self.conn, "chk", text)
        raw = self._raw("mint")
        self.assertEqual(raw["Account Number"], "3456")
        # Mint's "Account Name" is a nickname the per-account split reads —
        # scrubbing it would break the split and protect nothing
        self.assertEqual(raw["Account Name"], "Checking")
        self.assertEqual(raw["Original Description"], "SAFEWAY STORE 123")

    def test_ynab_row_keeps_names_but_not_numbers(self):
        from oikonome.sync import ynabimport
        text = ('"Date","Payee","Memo","Outflow","Inflow","Account",'
                '"Routing Number"\n'
                '"2026-07-10","Safeway","","$42.50","$0.00","Checking",'
                '"021000021"\n')
        ynabimport.import_ynab(self.conn, "chk", text)
        raw = self._raw("ynab")
        self.assertEqual(raw["Routing Number"], "0021")
        self.assertEqual(raw["Account"], "Checking")

    def test_qif_check_number_and_account_name_are_kept(self):
        """The QIF record goes through the same scrub, and must come out
        untouched: N inside a transaction is the CHECK number and `account`
        is Quicken's account NAME (the split accounts are named from it).
        Neither is an account number — an over-broad scrub would corrupt
        both."""
        qif = ("!Type:Bank\n"
               "D07/10/2026\nT-42.50\nPSAFEWAY STORE\nN1042\nLGroceries\n^\n")
        qifimport.import_qif(self.conn, "chk", qif)
        raw = self._raw("qif")
        self.assertEqual(raw["number"], "1042")
        self.assertEqual(raw["payee"], "SAFEWAY STORE")


class BatchRollbackTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_batch_tagged_and_rolled_back(self):
        from oikonome.sync import batches
        bid = batches.create(self.conn, "qif", "chk", "old.qif")
        r = qifimport.import_qif(self.conn, "chk", QIF, batch_id=bid)
        batches.finish(self.conn, bid, r["imported"])
        rec = batches.recent(self.conn)
        self.assertEqual(rec[0]["row_count"], 3)
        n = batches.rollback(self.conn, bid)
        self.assertEqual(n, 3)
        left = self.conn.execute("SELECT COUNT(*) AS n FROM transactions "
                                 "WHERE id LIKE 'qif:%'").fetchone()["n"]
        self.assertEqual(left, 0)
        self.assertEqual(batches.recent(self.conn), [])

    def test_rollback_scoped_to_batch(self):
        from oikonome.sync import batches
        b1 = batches.create(self.conn, "qif", "chk", "one.qif")
        qifimport.import_qif(self.conn, "chk", QIF, batch_id=b1)
        b2 = batches.create(self.conn, "mint", "chk", "mint.csv")
        mintimport.import_mint(self.conn, "chk", MINT, batch_id=b2)
        batches.rollback(self.conn, b1)
        mint_left = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE id LIKE 'mint:%'"
        ).fetchone()["n"]
        self.assertGreater(mint_left, 0)         # other batch untouched

    def _qif_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE id LIKE 'qif:%'"
        ).fetchone()["n"]

    def test_reimport_appends_batches_and_rollback_preserves_first(self):
        """Content-hashed ids mean an overlapping re-import UPSERTS the same
        rows; the batch id must APPEND to raw->'_batches', and rolling back
        the re-import must NOT delete rows the first batch created."""
        from oikonome.sync import batches
        b1 = batches.create(self.conn, "qif", "chk", "one.qif")
        qifimport.import_qif(self.conn, "chk", QIF, batch_id=b1)
        b2 = batches.create(self.conn, "qif", "chk", "one-again.qif")
        qifimport.import_qif(self.conn, "chk", QIF, batch_id=b2)
        row = self.conn.execute(
            "SELECT raw->'_batches' AS b, raw->>'_batch' AS latest "
            "FROM transactions WHERE id LIKE 'qif:%' LIMIT 1").fetchone()
        self.assertEqual(row["b"], [b1, b2])     # appended, not overwritten
        self.assertEqual(row["latest"], b2)      # display = latest
        res = batches.rollback(self.conn, b2)
        self.assertEqual(int(res), 0)            # nothing deleted…
        self.assertEqual(self._qif_count(), 3)   # …b1's rows survive
        row = self.conn.execute(
            "SELECT raw->'_batches' AS b, raw->>'_batch' AS latest "
            "FROM transactions WHERE id LIKE 'qif:%' LIMIT 1").fetchone()
        self.assertEqual(row["b"], [b1])
        self.assertEqual(row["latest"], b1)
        res = batches.rollback(self.conn, b1)    # last owner → hard delete
        self.assertEqual(int(res), 3)
        self.assertEqual(res["deleted"], 3)      # dict-style access
        self.assertIsNone(res.warning)           # no later batches remain
        self.assertEqual(self._qif_count(), 0)

    def test_rollback_warns_about_later_overlapping_import(self):
        """A later batch over the same window may have skipped rows as
        duplicates of the rolled-back batch's rows — rollback can't
        resurrect those, so it must warn."""
        from oikonome.sync import batches
        b1 = batches.create(self.conn, "qif", "chk", "one.qif")
        qifimport.import_qif(self.conn, "chk", QIF, batch_id=b1)
        b2 = batches.create(self.conn, "mint", "chk", "mint.csv")
        r2 = mintimport.import_mint(self.conn, "chk", MINT, batch_id=b2)
        self.assertGreater(r2["skipped_duplicates"], 0)  # overlap is real
        res = batches.rollback(self.conn, b1)
        self.assertEqual(int(res), 3)
        self.assertIsNotNone(res.warning)
        self.assertIn("re-import", res.warning)
        # rolling back the LATEST batch warns about nothing
        res2 = batches.rollback(self.conn, b2)
        self.assertIsNone(res2.warning)


if __name__ == "__main__":
    unittest.main()
