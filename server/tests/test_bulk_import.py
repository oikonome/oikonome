"""Bulk import: plan analysis (type detect, mask/name account
matching, sign guessing + uncertainty), run with per-file isolation,
new-account creation, batch-token expiry."""

import unittest
from unittest import mock

from oikonome.web import bulk_import

from .util import make_db, write_config

BANK_CSV = (b"Date,Description,Amount\n"
            b"2021-03-01,COFFEE SHOP,-4.50\n"
            b"2021-03-02,PAYCHECK,1500.00\n"
            b"2021-03-03,GROCERY,-88.10\n"
            b"2021-03-04,GAS,-30.00\n")
ALL_POSITIVE_CSV = (b"Date,Description,Amount\n"
                    b"2021-03-01,COFFEE,4.50\n"
                    b"2021-03-02,GROCERY,88.10\n"
                    b"2021-03-03,GAS,30.00\n")


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute("UPDATE accounts SET mask='9977' WHERE id='card'")
        self.conn.execute(
            "UPDATE accounts SET name='Wells Fargo Checking' WHERE id='chk'")

    def tearDown(self):
        self.conn.close()

    def test_plan_matching_and_signs(self):
        plan = bulk_import.analyze(self.conn, [
            ("statement_9977_2021.csv", BANK_CSV),      # mask match
            ("wells_fargo_2021_export.csv", BANK_CSV),  # name match
            ("mystery-card.csv", ALL_POSITIVE_CSV),     # no match, unsure sign
            ("notes.txt", b"hello"),                    # unsupported
            ("backup.zip", b"PK..."),                   # restore
        ])
        f = {p["name"]: p for p in plan["files"]}
        self.assertEqual(f["statement_9977_2021.csv"]["account_id"], "card")
        self.assertTrue(f["statement_9977_2021.csv"]["sign_certain"])
        self.assertEqual(f["statement_9977_2021.csv"]["amount_sign"], "bank")
        self.assertEqual(f["wells_fargo_2021_export.csv"]["account_id"],
                         "chk")
        m = f["mystery-card.csv"]
        self.assertIsNone(m["account_id"])
        self.assertEqual(m["new_account_name"], "Mystery Card")
        self.assertFalse(m["sign_certain"])
        self.assertEqual(f["notes.txt"]["action"], "skip")
        self.assertIn("restore", f["backup.zip"]["note"])

    def test_an_initials_account_name_can_still_be_matched(self):
        """An initialism account name still matches its statements. An
        account named an initialism plus filler ('ING Savings') has no word
        past the four-letter floor, so the short word is used; otherwise each
        statement for it would create a second account. The short word is
        used only because nothing longer survives — a name with a real word
        still never matches on a short one."""
        self.conn.execute("UPDATE accounts SET name='ING Savings' "
                          "WHERE id='chk'")
        plan = bulk_import.analyze(self.conn, [
            ("ing_2021_statement.csv", BANK_CSV),
            ("acme_2021_statement.csv", BANK_CSV),
        ])
        f = {p["name"]: p for p in plan["files"]}
        self.assertEqual(f["ing_2021_statement.csv"]["account_id"], "chk")
        # an unrelated bank still creates its own account
        self.assertIsNone(f["acme_2021_statement.csv"]["account_id"])

    def test_a_filename_year_is_not_a_name_match(self):
        """Digits are the mask check's business; a four-digit year in both
        strings must never read as the same account."""
        self.conn.execute("UPDATE accounts SET name='Vault 2021', mask=NULL "
                          "WHERE id='chk'")
        plan = bulk_import.analyze(self.conn, [("2021 export.csv", BANK_CSV)])
        self.assertIsNone(plan["files"][0]["account_id"])

    def test_run_imports_creates_accounts_and_isolates_failures(self):
        plan = bulk_import.analyze(self.conn, [
            ("old bank.csv", BANK_CSV),
            ("broken.ofx", b"not really ofx"),
        ])
        decisions = plan["files"]
        results = bulk_import.run(self.conn, plan["token"], decisions)
        by = {r["name"]: r for r in results}
        self.assertTrue(by["old bank.csv"]["ok"])
        self.assertEqual(by["old bank.csv"]["imported"], 4)
        self.assertFalse(by["broken.ofx"]["ok"])
        # new account was created and holds the rows
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions t JOIN accounts a "
            "ON a.id=t.account_id WHERE a.name='Old Bank'").fetchone()["n"]
        self.assertEqual(n, 4)
        # bank sign convention → spend positive, income negative (engine)
        amt = self.conn.execute(
            "SELECT amount FROM transactions WHERE name='COFFEE SHOP'"
        ).fetchone()["amount"]
        self.assertEqual(amt, 4.50)
        # token is single-use
        with self.assertRaises(ValueError):
            bulk_import.run(self.conn, plan["token"], decisions)

    def test_skip_rows_do_not_import(self):
        plan = bulk_import.analyze(self.conn, [("a.csv", BANK_CSV)])
        plan["files"][0]["action"] = "skip"
        results = bulk_import.run(self.conn, plan["token"], plan["files"])
        self.assertEqual(results, [])




class OversizedImportTests(unittest.TestCase):
    """an oversized file stages as empty bytes — run must refuse to
import it even if the client re-checks the box."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_empty_staged_file_cannot_import(self):
        # simulate the oversized staging (analyze blanks it to b"")
        plan = bulk_import.analyze(self.conn, [("huge.csv", b"")])
        # a malicious/confused client re-enables it as an import
        decisions = [{"index": 0, "action": "import",
                      "new_account_name": "X", "amount_sign": "bank"}]
        results = bulk_import.run(self.conn, plan["token"], decisions)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("too large", results[0]["error"])
        # nothing imported
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
        self.assertEqual(n, 0)


class FilenameAccountMaskTests(unittest.TestCase):
    """A real account mask in a statement filename must win over the
    year that is almost always beside it."""

    def test_real_mask_beats_the_filename_year(self):
        self.assertEqual(
            bulk_import._digits("Chase_4821_2023_statement.csv"),
            ["4821", "2023"])   # non-year first
        self.assertEqual(bulk_import._digits("acct_2023.csv"), ["2023"])


if __name__ == "__main__":
    unittest.main()


class AggregateRowBudgetTests(unittest.TestCase):
    """Per-file caps alone let one /run process MAX_FILES × 50k rows —
    every file paying its own dedup scan — in a single request. The batch
    carries an aggregate row budget: files past it are refused with a
    clear note and can be re-run in a fresh batch."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_files_past_the_batch_budget_are_refused(self):
        plan = bulk_import.analyze(self.conn, [
            ("bank-a.csv", BANK_CSV),
            ("bank-b.csv", BANK_CSV),
            ("bank-c.csv", BANK_CSV),
        ])
        decisions = plan["files"]
        # each file imports 4 rows; a budget of 6 lets two files through
        # (the budget is checked before each file, not mid-file) and
        # refuses the third
        with mock.patch.object(bulk_import, "MAX_TOTAL_ROWS", 6):
            results = bulk_import.run(self.conn, plan["token"], decisions)
        by_name = {r["name"]: r for r in results}
        self.assertTrue(by_name["bank-a.csv"]["ok"])
        self.assertTrue(by_name["bank-b.csv"]["ok"])
        third = by_name["bank-c.csv"]
        self.assertFalse(third["ok"])
        self.assertIn("row budget", third["error"])

    def test_a_batch_within_budget_is_untouched(self):
        plan = bulk_import.analyze(self.conn, [
            ("bank-a.csv", BANK_CSV),
            ("bank-b.csv", BANK_CSV),
        ])
        results = bulk_import.run(self.conn, plan["token"], plan["files"])
        self.assertTrue(all(r["ok"] for r in results))
