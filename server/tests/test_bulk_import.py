"""Bulk import: plan analysis (type detect, mask/name account
matching, sign guessing + uncertainty), the job's run with per-file
isolation, new-account creation, batch-token expiry."""

import unittest
from unittest import mock

from oikonome.web import bulk_import

from .bulk_job_util import submit_and_run
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
        results = submit_and_run(self.conn, plan["token"], decisions)
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
        # a second run of the same token answers the same job: nothing
        # imports twice
        again = submit_and_run(self.conn, plan["token"], decisions)
        self.assertEqual([r["ok"] for r in again], [r["ok"] for r in results])
        n2 = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions t JOIN accounts a "
            "ON a.id=t.account_id WHERE a.name='Old Bank'").fetchone()["n"]
        self.assertEqual(n2, 4)

    def test_an_unexpected_failure_reports_its_kind_not_its_message(self):
        """A file that fails in a way nobody planned for is reported by the
        kind of failure and the advice to run it again; the message, which
        can carry paths or SQL, goes to the log only."""
        plan = bulk_import.analyze(self.conn, [("old bank.csv", BANK_CSV)])
        boom = RuntimeError("relation /srv/secret/path does not exist")
        with mock.patch("oikonome.web.pages._dispatch_import_unlocked",
                               side_effect=boom), \
                self.assertLogs("oikonome.import") as logs:
            results = submit_and_run(self.conn, plan["token"], plan["files"])
        self.assertFalse(results[0]["ok"])
        self.assertNotIn("/srv/secret", results[0]["error"])
        self.assertIn("RuntimeError", results[0]["error"])
        self.assertIn("again", results[0]["error"])
        self.assertIn("/srv/secret", "\n".join(logs.output))

    def test_an_importers_own_refusal_reaches_the_person_as_worded(self):
        """A refusal the importer words itself — a PDF past the size limit —
        says what is wrong and what to do; reporting it as a failure to
        retry would send the person round the same refusal again."""
        plan = bulk_import.analyze(self.conn, [("old bank.csv", BANK_CSV)])
        refusal = ValueError("this PDF expands past the size limit")
        with mock.patch("oikonome.web.pages._dispatch_import_unlocked",
                        side_effect=refusal):
            results = submit_and_run(self.conn, plan["token"], plan["files"])
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"],
                         "this PDF expands past the size limit")

    def test_a_file_with_no_account_says_so(self):
        plan = bulk_import.analyze(self.conn, [("old bank.csv", BANK_CSV)])
        plan["files"][0]["account_id"] = None
        plan["files"][0]["new_account_name"] = ""
        results = submit_and_run(self.conn, plan["token"], plan["files"])
        self.assertEqual(results[0]["error"], "needs an account")

    def test_skip_rows_do_not_import(self):
        plan = bulk_import.analyze(self.conn, [("a.csv", BANK_CSV)])
        plan["files"][0]["action"] = "skip"
        results = submit_and_run(self.conn, plan["token"], plan["files"])
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
        results = submit_and_run(self.conn, plan["token"], decisions)
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
            results = submit_and_run(self.conn, plan["token"], decisions)
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
        results = submit_and_run(self.conn, plan["token"], plan["files"])
        self.assertTrue(all(r["ok"] for r in results))


class EachStagedFileRunsOnceTests(unittest.TestCase):
    """The parse work one /run may do is bounded by what was staged. The
    row budget only counts rows a file actually imported, so a file that
    fails, needs a column mapping or yields nothing is free to it; a plan
    naming that file thousands of times would otherwise re-parse it once
    per entry while holding the household's sync lock."""

    UNMAPPED_CSV = b"Foo,Bar,Baz\n1,2,3\n4,5,6\n"

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_repeated_index_is_parsed_once(self):
        # a plan no longer than the batch, naming one file in every slot
        plan = bulk_import.analyze(self.conn, [
            ("odd.csv", self.UNMAPPED_CSV)] * 3)
        decision = dict(plan["files"][0], action="import", account_id="chk")
        from oikonome.web import pages
        with mock.patch("oikonome.web.pages._dispatch_import_unlocked",
                        wraps=pages._dispatch_import_unlocked) as spy:
            results = submit_and_run(self.conn, plan["token"],
                                      [decision] * 3)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(len(results), 1)

    def test_a_plan_longer_than_any_batch_is_refused(self):
        plan = bulk_import.analyze(self.conn, [("odd.csv", self.UNMAPPED_CSV)])
        decision = dict(plan["files"][0], action="import", account_id="chk")
        with mock.patch("oikonome.web.pages._dispatch_import_unlocked") as spy, \
                self.assertRaises(ValueError):
            submit_and_run(self.conn, plan["token"],
                            [decision] * (bulk_import.MAX_FILES + 1))
        spy.assert_not_called()
        # the refusal consumed nothing: a sane plan still runs on the token
        results = submit_and_run(self.conn, plan["token"], [decision])
        self.assertEqual(len(results), 1)


class ArchiveInAMixedBatchTests(unittest.TestCase):
    """An export ZIP in a folder drop restores as a background job that
    takes the household's sync lock on its own connection, started by the
    submitting request; the folder's other files run in the import job,
    which waits its turn for that lock rather than failing against it."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _status(self):
        from oikonome.sync import restore_job
        return restore_job.status(self.conn)

    def test_a_zip_beside_other_files_restores(self):
        import csv as _csv
        import io as _io
        import json as _json
        import time
        import zipfile

        from oikonome.jobs import bulk_import_job
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            s = _io.StringIO()
            w = _csv.DictWriter(s, fieldnames=["config"])
            w.writeheader()
            w.writerow({"config": _json.dumps({})})
            z.writestr("tenant_settings.csv", s.getvalue())
        plan = bulk_import.analyze(self.conn, [
            ("export.zip", buf.getvalue()), ("chk.csv", BANK_CSV)])
        decisions = [dict(f, action="import") for f in plan["files"]]
        decisions[1].update(account_id="chk")
        from .bulk_job_util import run_job, submit
        with mock.patch.object(bulk_import_job, "nudge"), \
                mock.patch.object(bulk_import_job, "_sleep",
                                  side_effect=lambda s: time.sleep(0.05)):
            sub = submit(self.conn, plan["token"], decisions, role="owner")
            # the folder's own files wait for the restore it started
            for _ in range(100):
                st = self._status()
                if st["state"] in ("done", "error"):
                    break
                time.sleep(0.1)
            self.assertEqual(st["state"], "done", st)
            results = bulk_import_job.compat_results(
                run_job(self.conn, sub["job_id"]))
        by = {r["name"]: r for r in results}
        self.assertTrue(by["export.zip"].get("restore_started"), results)
        self.assertTrue(by["chk.csv"]["ok"], results)
        self.assertEqual([r["index"] for r in results], [0, 1])


class SignGuessReadsTheAmountColumn(unittest.TestCase):
    """The sign convention is a property of the amount column. A running
    balance (positive on nearly every row) and a summary block above the
    header must not dilute it, or a checking export whose amounts are
    mostly debits reads as all-positive and every sign is flipped."""

    BOFA = (b"Description,,Summary Amt.\n"
            b"Beginning balance as of 01/01/2026,,1000.00\n"
            b"Total credits,,3000.00\n"
            b"Total debits,,-315.35\n"
            b"Ending balance as of 01/31/2026,,3684.65\n"
            b"\n"
            b"Date,Description,Amount,Running Bal.\n"
            b"01/02/2026,COFFEE,-4.50,995.50\n"
            b"01/03/2026,GROCERY,-88.10,907.40\n"
            b"01/04/2026,PAYCHECK,1000.00,1907.40\n"
            b"01/05/2026,GAS,-30.00,1877.40\n"
            b"01/06/2026,PHARMACY,-12.25,1865.15\n"
            b"01/07/2026,PAYCHECK,1000.00,2865.15\n"
            b"01/08/2026,BOOKS,-45.07,2820.08\n"
            b"01/09/2026,DINER,-60.43,2759.65\n"
            b"01/10/2026,PAYCHECK,1000.00,3759.65\n"
            b"01/11/2026,HARDWARE,-75.00,3684.65\n")

    def test_a_balance_column_and_summary_block_do_not_flip_the_guess(self):
        self.assertEqual(bulk_import._csv_sign_guess(self.BOFA),
                         ("bank", True))

    def test_an_all_positive_amount_column_is_still_unsure(self):
        csv = (b"Date,Description,Amount,Balance\n"
               b"2021-03-01,COFFEE,4.50,-10.00\n"
               b"2021-03-02,GROCERY,88.10,-20.00\n"
               b"2021-03-03,GAS,30.00,-30.00\n")
        self.assertEqual(bulk_import._csv_sign_guess(csv), ("plaid", False))
