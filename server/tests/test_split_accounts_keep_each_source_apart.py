"""A file holding several accounts splits into stable accounts, names
none by its full number, and splits into a bounded number of them.

Each source account gets its own account, and a re-import lands on the
accounts it made before. Where an earlier release's slug mapped two sources
onto one id, that account keeps both (test_split_account_ids_follow_where_
the_rows_came_from covers it): moving one off would insert its history a
second time.

An account named by its number keeps only the last four digits in its name
and in the import result, the rule every hand-made account follows.

One file splits into a bounded number of accounts: every split is an
account every page of the app then lists.
"""
import re
import unittest

from oikonome.sync import base, mintimport, qifimport

from .util import make_db, write_config

HEADER = ("Date,Description,Original Description,Amount,"
          "Transaction Type,Category,Account Name,Labels,Notes\n")


def _mint(rows):
    return HEADER + "".join(
        f"7/{1 + i % 28}/2026,Shop {i},SHOP {i},{amt:.2f},debit,Groceries,"
        f"{acct},,\n" for i, (acct, amt) in enumerate(rows))


class SplitAccountReimportTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _split_rows(self):
        return {r["account_id"]: r["n"] for r in self.conn.execute(
            "SELECT account_id, count(*) AS n FROM transactions "
            "WHERE account_id LIKE 'manual:mint-%' GROUP BY account_id")}

    def test_a_reimport_lands_on_the_same_accounts(self):
        text = _mint([("Savings Reserve", 10.0), ("Everyday Checking", 11.0)])
        mintimport.import_mint(self.conn, "chk", text)
        before = self._split_rows()
        mintimport.import_mint(self.conn, "chk", text)
        self.assertEqual(self._split_rows(), before)
        self.assertEqual(len(before), 2, before)

    def test_two_names_the_slug_cannot_spell_split_and_reimport_cleanly(self):
        """Both names slug to nothing a plain id can hold. Each gets its own
        account, and re-importing the file adds nothing to either."""
        text = _mint([("Сбережения", 10.0), ("Текущий счёт", 23.0)])
        mintimport.import_mint(self.conn, "chk", text)
        before = self._split_rows()
        self.assertEqual(len(before), 2, before)
        self.assertNotIn("manual:mint-x", before)
        mintimport.import_mint(self.conn, "chk", text)
        self.assertEqual(self._split_rows(), before)

    def test_two_names_alike_past_the_cut_split_and_reimport_cleanly(self):
        stem = "Springfield Credit Union Shared Household"   # past 40
        text = _mint([(stem + " Checking A", 10.0),
                      (stem + " Checking B", 23.0)])
        mintimport.import_mint(self.conn, "chk", text)
        before = self._split_rows()
        self.assertEqual(len(before), 2, before)
        mintimport.import_mint(self.conn, "chk", text)
        self.assertEqual(self._split_rows(), before)


QIF_NUMBERED = """!Account
N1234567890123
TBank
^
!Type:Bank
D03/04/2026
T-61.20
PCORNER GROCER
^
!Account
NHarbor Savings
TBank
^
!Type:Bank
D03/05/2026
T-5.00
PSERVICE FEE
^
"""

LONG_DIGITS = re.compile(r"\d{8,}")


class SplitAccountNumberTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_an_account_named_by_its_number_keeps_only_the_last_four(self):
        out = qifimport.import_qif(self.conn, "chk", QIF_NUMBERED)
        rows = self.conn.execute(
            "SELECT name FROM accounts WHERE id LIKE 'manual:qif-%'"
        ).fetchall()
        self.assertEqual(len(rows), 2, rows)
        for r in rows:
            self.assertIsNone(LONG_DIGITS.search(r["name"]), r["name"])
        for k in out["split_accounts"]:
            self.assertIsNone(LONG_DIGITS.search(k), k)
        self.assertTrue(any(r["name"].endswith("0123") for r in rows))


class SplitAccountLimitTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_file_naming_too_many_accounts_is_refused_before_any_is_made(self):
        text = _mint([(f"Account {i}", 1.0)
                      for i in range(base.MAX_SPLIT_ACCOUNTS + 1)])
        with self.assertRaises(ValueError):
            mintimport.import_mint(self.conn, "chk", text)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM accounts WHERE id LIKE 'manual:mint-%'"
        ).fetchone()["n"], 0)

    def test_a_household_sized_file_still_splits(self):
        out = mintimport.import_mint(self.conn, "chk", _mint(
            [(f"Account {i}", 1.0) for i in range(12)]))
        self.assertEqual(len(out["split_accounts"]), 12)


if __name__ == "__main__":
    unittest.main()
