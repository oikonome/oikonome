"""Importers for other apps' exports: Monarch / Copilot / Simplifi
detection + sign conventions."""

import unittest

from oikonome.sync import competitors as C

from .util import make_db, write_config

MONARCH = """Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags
2026-07-10,Safeway,Groceries,Checking,SAFEWAY STORE 123,,-42.50,
2026-07-11,Employer,Paycheck,Checking,EMPLOYER PAYROLL,,2000.00,
"""

COPILOT = """date,name,amount,status,category,parent category,excluded,tags,type,account,account mask,note,recurring
2026-07-10,Safeway,42.50,posted,Groceries,Food,,,regular,Checking,1234,,
2026-07-11,Employer Payroll,-2000.00,posted,Paycheck,Income,,,income,Checking,1234,,
"""

SIMPLIFI = """Date,Payee,Amount,Account,Category,Tags,Notes
07/10/2026,Safeway,-42.50,Checking,Groceries,,
07/11/2026,Employer Payroll,"2,000.00",Checking,Paycheck,,
"""


def _hdr(text):
    import csv, io
    return next(csv.reader(io.StringIO(text)))


class CompetitorImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_detectors_are_mutually_exclusive(self):
        for text, mine, others in (
                (MONARCH, C.looks_like_monarch,
                 (C.looks_like_copilot, C.looks_like_simplifi)),
                (COPILOT, C.looks_like_copilot,
                 (C.looks_like_monarch, C.looks_like_simplifi)),
                (SIMPLIFI, C.looks_like_simplifi,
                 (C.looks_like_monarch, C.looks_like_copilot))):
            h = _hdr(text)
            self.assertTrue(mine(h), h)
            for o in others:
                self.assertFalse(o(h), (o.__name__, h))

    def test_account_number_columns_are_scrubbed_from_raw(self):
        """A vendor export that grows an account/routing column must not
        land it verbatim in transactions.raw — raw rides /export and the
        portability archive unredacted. Same last-4 scrub the other
        CSV-family importers share."""
        text = ("Date,Merchant,Category,Account,Original Statement,Notes,"
                "Amount,Tags,Account Number\n"
                "2026-07-10,Safeway,Groceries,Checking,SAFEWAY,,-42.50,,"
                "123456789012\n")
        C.import_monarch(self.conn, "chk", text)
        raw = self.conn.execute(
            "SELECT raw FROM transactions WHERE id LIKE 'monarch:%'"
        ).fetchone()["raw"]
        self.assertEqual(raw["Account Number"], "9012")

    def _check(self, rows_by_name):
        self.assertEqual(rows_by_name["out"], 42.5)     # money out → +
        self.assertEqual(rows_by_name["in"], -2000.0)   # money in → −

    def test_monarch_signs(self):
        C.import_monarch(self.conn, "chk", MONARCH)
        rows = {("out" if r["amount"] > 0 else "in"): r["amount"]
                for r in self.conn.execute(
                    "SELECT amount FROM transactions WHERE id LIKE 'monarch:%'"
                ).fetchall()}
        self._check(rows)
        # Monarch: name = original statement, merchant = clean name
        r = self.conn.execute(
            "SELECT name, merchant_name FROM transactions "
            "WHERE id LIKE 'monarch:%' AND amount > 0").fetchone()
        self.assertEqual(r["name"], "SAFEWAY STORE 123")
        self.assertEqual(r["merchant_name"], "Safeway")

    def test_copilot_signs(self):
        C.import_copilot(self.conn, "chk", COPILOT)
        rows = {("out" if r["amount"] > 0 else "in"): r["amount"]
                for r in self.conn.execute(
                    "SELECT amount FROM transactions WHERE id LIKE 'copilot:%'"
                ).fetchall()}
        self._check(rows)

    def test_simplifi_signs_and_idempotency(self):
        C.import_simplifi(self.conn, "chk", SIMPLIFI)
        C.import_simplifi(self.conn, "chk", SIMPLIFI)
        rows = self.conn.execute(
            "SELECT amount FROM transactions WHERE id LIKE 'simplifi:%'"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self._check({("out" if r["amount"] > 0 else "in"): r["amount"]
                     for r in rows})

    def _cats(self, prefix):
        return {r["name"]: (r["category_primary"], r["category_detailed"])
                for r in self.conn.execute(
                    "SELECT name, category_primary, category_detailed "
                    f"FROM transactions WHERE id LIKE '{prefix}:%'"
                ).fetchall()}

    def test_monarch_flow_categories_mapped(self):
        """Card payments / transfers must land spend-excluded; passed
        through unmapped they would count as spend."""
        text = ("Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags\n"
                "2026-07-10,Chase,Credit Card Payment,Checking,CHASE EPAY,,-300.00,\n"
                "2026-07-11,Acme Bank,Transfer,Checking,XFER TO SAVINGS,,-500.00,\n"
                "2026-07-12,Acme Bank,Transfer,Checking,XFER BACK,,500.00,\n"
                "2026-07-13,Safeway,Groceries,Checking,SAFEWAY STORE,,-42.50,\n")
        C.import_monarch(self.conn, "chk", text)
        cats = self._cats("monarch")
        self.assertEqual(cats["CHASE EPAY"],
                         ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"))
        self.assertEqual(cats["XFER TO SAVINGS"], ("TRANSFER_OUT", None))
        self.assertEqual(cats["XFER BACK"], ("TRANSFER_IN", None))
        self.assertEqual(cats["SAFEWAY STORE"], (None, None))

    def test_simplifi_flow_categories_mapped(self):
        text = ("Date,Payee,Amount,Account,Category,Tags,Notes\n"
                "07/10/2026,Chase Card,-300.00,Checking,Credit Card Payments,,\n"
                "07/11/2026,Acme Savings,-500.00,Checking,Balance Adjustments,,\n"
                "07/12/2026,Safeway,-42.50,Checking,Groceries,,\n")
        C.import_simplifi(self.conn, "chk", text)
        cats = self._cats("simplifi")
        self.assertEqual(cats["Chase Card"],
                         ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"))
        self.assertEqual(cats["Acme Savings"], ("TRANSFER_OUT", None))
        self.assertEqual(cats["Safeway"], (None, None))

    # ---- multi-account exports must not merge --------------------
    # Monarch/Copilot/Simplifi exports are whole-household files with an
    # `account` column. Landing every vendor account on the one hub-selected
    # account — checking + card + savings as one ledger — corrupts
    # transfers, balances, and spend.

    MULTI = ("Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags\n"
             "2026-07-10,Safeway,Groceries,Checking,SAFEWAY STORE,,-42.50,\n"
             "2026-07-11,Chevron,Gas,Chase Card,CHEVRON 42,,-30.00,\n"
             "2026-07-12,Employer,Paycheck,Checking,EMPLOYER PAYROLL,,2000.00,\n"
             "2026-07-13,Acme Bank,Interest,Savings,INTEREST PAID,,1.25,\n")

    def _by_account(self, prefix):
        return {r["account_id"]: r["n"] for r in self.conn.execute(
            "SELECT account_id, COUNT(*) AS n FROM transactions "
            f"WHERE id LIKE '{prefix}:%' GROUP BY account_id").fetchall()}

    def test_multi_account_file_splits_per_vendor_account(self):
        out = C.import_monarch(self.conn, "chk", self.MULTI)
        self.assertEqual(out["imported"], 4)
        self.assertEqual(out["split_accounts"],
                         {"Checking": 2, "Chase Card": 1, "Savings": 1})
        self.assertIn("3 accounts", out["note"])
        self.assertEqual(self._by_account("monarch"), {
            "manual:monarch-checking": 2,
            "manual:monarch-chase-card": 1,
            "manual:monarch-savings": 1})
        # the split accounts exist, named after the vendor's account
        names = {r["id"]: r["name"] for r in self.conn.execute(
            "SELECT id, name FROM accounts WHERE id LIKE 'manual:monarch%'"
        ).fetchall()}
        self.assertEqual(names["manual:monarch-chase-card"], "Chase Card")

    def test_multi_account_reimport_is_idempotent(self):
        # same content-hashed ids land on the same split accounts — no
        # duplicate rows, no duplicate accounts (upsert semantics, same
        # contract the single-account simplifi test asserts)
        C.import_monarch(self.conn, "chk", self.MULTI)
        C.import_monarch(self.conn, "chk", self.MULTI)
        self.assertEqual(self._by_account("monarch"), {
            "manual:monarch-checking": 2,
            "manual:monarch-chase-card": 1,
            "manual:monarch-savings": 1})
        n = self.conn.execute("SELECT COUNT(*) AS n FROM accounts "
                              "WHERE id LIKE 'manual:monarch%'").fetchone()
        self.assertEqual(n["n"], 3)

    def test_reimport_never_clobbers_a_user_rename(self):
        C.import_monarch(self.conn, "chk", self.MULTI)
        self.conn.execute("UPDATE accounts SET name='My Card' "
                          "WHERE id='manual:monarch-chase-card'")
        C.import_monarch(self.conn, "chk", self.MULTI)
        r = self.conn.execute("SELECT name FROM accounts "
                              "WHERE id='manual:monarch-chase-card'").fetchone()
        self.assertEqual(r["name"], "My Card")

    def test_single_account_file_uses_the_selected_account(self):
        out = C.import_monarch(self.conn, "chk", MONARCH)
        self.assertNotIn("split_accounts", out)
        self.assertEqual(self._by_account("monarch"), {"chk": 2})

    def test_copilot_multi_account_splits_too(self):
        text = ("date,name,amount,status,category,parent category,excluded,"
                "tags,type,account,account mask,note,recurring\n"
                "2026-07-10,Safeway,42.50,posted,Groceries,Food,,,regular,"
                "Checking,1234,,\n"
                "2026-07-11,Chevron,30.00,posted,Gas,Auto,,,regular,"
                "Sapphire,9876,,\n")
        out = C.import_copilot(self.conn, "chk", text)
        self.assertEqual(out["split_accounts"], {"Checking": 1, "Sapphire": 1})
        self.assertEqual(self._by_account("copilot"), {
            "manual:copilot-checking": 1, "manual:copilot-sapphire": 1})

    def test_copilot_type_and_excluded_flags(self):
        """Copilot's `type` = internal transfer and its `excluded` flag both
        mean "not spend" → TRANSFER_* (spend-excluded)."""
        text = ("date,name,amount,status,category,parent category,excluded,"
                "tags,type,account,account mask,note,recurring\n"
                "2026-07-10,To Savings,500.00,posted,Misc,,,,"
                "internal transfer,Checking,1234,,\n"
                "2026-07-11,Reimbursed Dinner,80.00,posted,Restaurants,Food,"
                "true,,regular,Checking,1234,,\n"
                "2026-07-12,Card Payment,300.00,posted,Credit Card Payment,"
                ",,,regular,Checking,1234,,\n"
                "2026-07-13,Safeway,42.50,posted,Groceries,Food,,,regular,"
                "Checking,1234,,\n")
        C.import_copilot(self.conn, "chk", text)
        cats = self._cats("copilot")
        self.assertEqual(cats["To Savings"], ("TRANSFER_OUT", None))
        self.assertEqual(cats["Reimbursed Dinner"], ("TRANSFER_OUT", None))
        self.assertEqual(cats["Card Payment"],
                         ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"))
        self.assertEqual(cats["Safeway"], (None, None))


if __name__ == "__main__":
    unittest.main()
