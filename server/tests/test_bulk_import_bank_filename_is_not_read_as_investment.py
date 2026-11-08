"""A folder import guesses an investment export from words in the
filename. Those words must stand on their own: a date stamp holding the
digits 401, or a bank named after a place containing "ira", is an
ordinary bank file. Typing it as an investment would create a brokerage
account and file every deposit and withdrawal as a transfer, dropping
paychecks out of income and bills out of spending."""

import unittest

from oikonome.web import bulk_import

from .util import make_db, write_config

BANK_CSV = (b"Date,Description,Amount\n"
            b"2024-01-02,COFFEE SHOP,-4.50\n"
            b"2024-01-03,PAYCHECK,1500.00\n"
            b"2024-01-04,GROCERY,-88.10\n"
            b"2024-01-05,GAS,-30.00\n")


class BankFilenameIsNotInvestment(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _signs(self, names, data=BANK_CSV):
        plan = bulk_import.analyze(self.conn, [(n, data) for n in names])
        return {f["name"]: f["amount_sign"] for f in plan["files"]}

    def test_date_stamps_and_ordinary_words_stay_bank_files(self):
        names = ["Acct1234_Activity20240101_20241231_20250105.CSV",
                 "checking_20250401.csv",
                 "Admiral Savings Bank.csv",
                 "Spiral Valley CU checking.csv",
                 "ADMIRAL FEDERAL.csv",
                 "Smith Brothers CU.csv"]
        for name, sign in self._signs(names).items():
            self.assertNotEqual(sign, "investment", name)

    def test_investment_words_still_mark_an_investment_file(self):
        names = ["Roth_IRA.csv", "my 401k.csv", "401(k) 2024.csv",
                 "brokerage-2024.csv", "retirement_20240401.csv",
                 "403b plan.csv"]
        for name, sign in self._signs(names).items():
            self.assertEqual(sign, "investment", name)

    def test_camelcase_investment_names_still_mark_an_investment_file(self):
        # brand and wrapper words run straight into each other in many
        # exports; a CamelCase "IRA" or "Roth" is its own word
        names = ["VanguardIRA.csv", "FidelityInvestments.csv",
                 "RothIRA_2025.csv", "SchwabBrokerage.csv",
                 "ira_2025.csv", "VanguardRoth.csv"]
        for name, sign in self._signs(names).items():
            self.assertEqual(sign, "investment", name)

    def test_a_dated_bank_statement_pdf_is_not_an_investment(self):
        plan = bulk_import.analyze(
            self.conn, [("statement_20240115.pdf", b"%PDF-1.4\n")])
        self.assertEqual(plan["files"][0]["amount_sign"], "statement")


if __name__ == "__main__":
    unittest.main()
