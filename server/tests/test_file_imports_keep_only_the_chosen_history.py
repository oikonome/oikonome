"""A file import keeps only the history the household chose.

With `history_months` set, every file importer leaves out the rows dated
before that many calendar months ago and reports how many as
`skipped_older` — on the importer's own answer, on the single-file door and
on each file of a folder import. Rows inside the window import exactly as
before; with nothing chosen nothing is left out.
"""

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.sync import base, csvimport, ofximport, qifimport, ynabimport
from oikonome.web import bulk_import

from .bulk_job_util import submit_and_run
from .util import _ensure_db, make_db, seed_accounts, write_config

TODAY = dt.date.today()
RECENT = TODAY - dt.timedelta(days=10)
OLD = TODAY - dt.timedelta(days=200)      # outside 3 months, inside 12


def _csv(rows) -> str:
    return "Date,Description,Amount\n" + "".join(
        f"{d.isoformat()},{name},{amt}\n" for d, name, amt in rows)


CSV_ROWS = [(RECENT, "TUNEBOX MUSIC", "-9.99"),
            (RECENT, "ACME PAYROLL", "2000.00"),
            (OLD, "NORTHWIND GROCERY", "-41.20"),
            (OLD - dt.timedelta(days=1), "NORTHWIND GROCERY", "-12.80")]
MAPPING = {"date": "Date", "amount": "Amount", "name": "Description"}


def _choose(conn, months):
    with budget.config_txn(conn) as cfg:
        cfg["history_months"] = months


class MonthsBeforeTests(unittest.TestCase):
    def test_calendar_months_clamp_to_a_shorter_month(self):
        self.assertEqual(base.months_before(dt.date(2026, 5, 31), 3),
                         dt.date(2026, 2, 28))
        self.assertEqual(base.months_before(dt.date(2026, 1, 15), 1),
                         dt.date(2025, 12, 15))
        self.assertEqual(base.months_before(dt.date(2026, 3, 31), 12),
                         dt.date(2025, 3, 31))


class ImporterCutoffTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _names(self, prefix):
        return sorted(r["name"] for r in self.conn.execute(
            "SELECT name FROM transactions WHERE id LIKE %s",
            (prefix + "%",)).fetchall())

    def test_no_choice_keeps_every_row(self):
        r = csvimport.import_csv(self.conn, "chk", _csv(CSV_ROWS), MAPPING)
        self.assertEqual(r["imported"], 4)
        self.assertEqual(r["skipped_older"], 0)

    def test_csv_rows_before_the_window_are_left_out_and_counted(self):
        _choose(self.conn, 3)
        r = csvimport.import_csv(self.conn, "chk", _csv(CSV_ROWS), MAPPING)
        self.assertEqual(r["imported"], 2)
        self.assertEqual(r["skipped_older"], 2)
        self.assertEqual(self._names("csv:"),
                         ["ACME PAYROLL", "TUNEBOX MUSIC"])

    def test_a_wider_window_keeps_the_older_rows(self):
        _choose(self.conn, 12)
        r = csvimport.import_csv(self.conn, "chk", _csv(CSV_ROWS), MAPPING)
        self.assertEqual((r["imported"], r["skipped_older"]), (4, 0))

    def test_a_reimport_with_the_window_adds_nothing_twice(self):
        _choose(self.conn, 3)
        csvimport.import_csv(self.conn, "chk", _csv(CSV_ROWS), MAPPING)
        r = csvimport.import_csv(self.conn, "chk", _csv(CSV_ROWS), MAPPING)
        self.assertEqual(r["skipped_older"], 2)
        self.assertEqual(len(self._names("csv:")), 2)

    def test_ofx_rows_before_the_window_are_left_out(self):
        _choose(self.conn, 3)
        from .test_ofximport import OFX

        def d(x):
            return x.strftime("%Y%m%d").encode()
        # the statement's three lines: two recent, the payroll one old
        ofx = (OFX.replace(b"<DTPOSTED>20260710", b"<DTPOSTED>" + d(RECENT))
               .replace(b"<DTPOSTED>20260711", b"<DTPOSTED>" + d(OLD))
               .replace(b"<DTPOSTED>20260712", b"<DTPOSTED>" + d(RECENT)))
        r = ofximport.import_ofx(self.conn, "chk", ofx)
        self.assertEqual((r["imported"], r["skipped_older"]), (2, 1))
        self.assertEqual(self._names("ofx:"),
                         ["COFFEE SHOP", "SAFEWAY STORE"])

    def test_qif_rows_before_the_window_are_left_out(self):
        _choose(self.conn, 3)
        qif = ("!Type:Bank\n"
               f"D{RECENT.strftime('%m/%d/%Y')}\nT-9.99\nPTUNEBOX MUSIC\n^\n"
               f"D{OLD.strftime('%m/%d/%Y')}\nT-41.20\nPNORTHWIND GROCERY\n^\n")
        r = qifimport.import_qif(self.conn, "chk", qif)
        self.assertEqual((r["imported"], r["skipped_older"]), (1, 1))
        self.assertEqual(self._names("qif:"), ["TUNEBOX MUSIC"])

    def test_ynab_lines_before_the_window_are_left_out(self):
        _choose(self.conn, 3)
        head = ('"Account","Flag","Date","Payee","Category Group/Category",'
                '"Category Group","Category","Memo","Outflow","Inflow",'
                '"Cleared"\n')
        text = head + "".join(
            f'"Checking","","{x.isoformat()}","{p}","Bills: Music","Bills",'
            f'"Music","","${amt}","$0.00","Cleared"\n'
            for x, p, amt in ((RECENT, "Tunebox Music", "9.99"),
                              (OLD, "Northwind Grocery", "41.20")))
        r = ynabimport.import_ynab(self.conn, "chk", text)
        self.assertEqual((r["imported"], r["skipped_older"]), (1, 1))
        self.assertEqual(self._names("ynab:"), ["Tunebox Music"])


class ACorrectionStillLandsBeforeTheWindowTests(unittest.TestCase):
    """A re-import that corrects rows an earlier import stored under an old
    reading (a sign printed inside a direction column, a statement read
    before its columns signed it) must correct them even when they fall
    before the history window chosen since. They are the household's own
    rows, not new old ones: left out of the write, they keep the wrong
    sign under the corrected id, and every later import leaves them out
    again."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _live(self, prefix):
        return {r["name"]: float(r["amount"]) for r in self.conn.execute(
            "SELECT name, amount FROM transactions"
            " WHERE account_id='chk' AND removed=0 AND id LIKE %s",
            (prefix + "%",))}

    def test_a_csv_sign_rule_correction_lands_before_the_window(self):
        from .test_csvimport import ReimportAfterTheColumnSignRuleTests as R
        case = R("test_reimport_corrects_a_row_stored_under_the_printed_sign")
        case.conn = self.conn
        case._store_the_old_reading()
        self.assertEqual(self._live("csv:")["REFUND HARDWARE STORE"], 150.0)
        _choose(self.conn, 1)              # the July rows are all older
        r = csvimport.import_csv(self.conn, "chk", R.TEXT, R.MAPPING)
        self.assertEqual(self._live("csv:"),
                         {"HARDWARE STORE": 60.0, "CORNER GROCER": 30.0,
                          "REFUND HARDWARE STORE": -150.0})
        self.assertEqual(r["corrected"], 2)
        self.assertEqual(r["skipped_older"], 0)
        # and nothing the household did not already have was added
        r = csvimport.import_csv(self.conn, "chk", R.TEXT, R.MAPPING)
        self.assertEqual(len(self._live("csv:")), 3)

    def test_a_new_old_row_beside_a_corrected_one_is_still_left_out(self):
        from .test_csvimport import ReimportAfterTheColumnSignRuleTests as R
        case = R("test_reimport_corrects_a_row_stored_under_the_printed_sign")
        case.conn = self.conn
        case._store_the_old_reading()
        _choose(self.conn, 1)
        text = R.TEXT + "2026-07-13,PAINT SUPPLY,25.00,\n"
        r = csvimport.import_csv(self.conn, "chk", text, R.MAPPING)
        self.assertEqual(r["skipped_older"], 1)
        self.assertNotIn("PAINT SUPPLY", self._live("csv:"))
        self.assertEqual(self._live("csv:")["REFUND HARDWARE STORE"], -150.0)

    def test_a_ynab_sign_rule_correction_lands_before_the_window(self):
        head = ('"Account","Flag","Date","Payee","Category Group/Category",'
                '"Category Group","Category","Memo","Outflow","Inflow",'
                '"Cleared"\n')
        text = head + (
            '"Checking","","2026-07-11","Hardware Store Refund",'
            '"Shopping: Home","Shopping","Home","","$0.00","-$150.00",'
            '"Cleared"\n')
        ynabimport.import_ynab(self.conn, "chk", text)
        # the row as the rule that let the printed sign flip it stored it
        old = base.FileImportIds("ynab", "chk").id_for(
            dt.date(2026, 7, 11), 150.0, "Hardware Store Refund")
        self.conn.execute(
            "UPDATE transactions SET id=%s, amount=150 WHERE id LIKE 'ynab:%%'",
            (old,))
        _choose(self.conn, 1)
        r = ynabimport.import_ynab(self.conn, "chk", text)
        self.assertEqual(self._live("ynab:"), {"Hardware Store Refund": -150.0})
        self.assertEqual(r["corrected"], 1)
        self.assertEqual(r["skipped_older"], 0)

    def test_a_pdf_read_before_column_signs_is_corrected_before_the_window(self):
        from unittest import mock

        from oikonome.sync import pdfimport

        from .test_pdf_reimport_after_column_signs import _statement
        pdf = _statement()
        plain = pdfimport.extract_text(pdf)
        with mock.patch.object(pdfimport, "extract_statement",
                               return_value=(plain, None)), \
                mock.patch.object(pdfimport, "_balance_hints",
                                  return_value={}):
            pdfimport.import_pdf(self.conn, "chk", pdf, amount_sign="bank")
        self.assertEqual(self._live("pdf:")["IRA DISTRIBUTION"], 1000.0)
        _choose(self.conn, 1)              # the June statement is older
        pdfimport.import_pdf(self.conn, "chk", pdf, amount_sign="bank")
        self.assertEqual(self._live("pdf:"),
                         {"GROCERY MART SPRINGFIELD": 42.50,
                          "IRA DISTRIBUTION": -1000.00,
                          "CHECK 1024": 500.00})


class AMovedRegisterRowIsNotLostToTheWindowTests(unittest.TestCase):
    """A register an earlier import put whole on checking is imported again
    with its card account sent to the card and the earlier card rows on
    checking removed. Those rows are the household's own, changing
    account: the history window must not leave them out at the card while
    the removal takes them off checking, or they vanish from the ledger."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rows(self, account_id):
        return sorted((r["name"], r["acct"]) for r in self.conn.execute(
            "SELECT name, raw->>'Account' AS acct FROM transactions"
            " WHERE account_id = %s AND removed = 0 AND id LIKE 'ynab:%%'",
            (account_id,)))

    def test_pre_window_card_rows_move_to_the_card(self):
        from .test_ynab_register_rows_land_on_the_account_they_came_from \
            import REGISTER, seed_whole_register_on
        seed_whole_register_on(self.conn, "chk", REGISTER)
        _choose(self.conn, 1)            # every July line is older
        r = ynabimport.import_ynab(
            self.conn, "chk", REGISTER,
            accounts={"Checking": "chk", "Visa": "card"},
            remove_earlier={"Visa": True})
        self.assertEqual(r["removed_earlier"], 3)
        self.assertEqual(r["skipped_older"], 0)
        self.assertEqual(self._rows("card"),
                         [("Gas Station", "Visa"), ("Interest", "Visa"),
                          ("Transfer : Checking", "Visa")])
        self.assertEqual({a for _, a in self._rows("chk")}, {"Checking"})

    def test_a_card_line_never_stored_is_still_left_out(self):
        from .test_ynab_register_rows_land_on_the_account_they_came_from \
            import REGISTER, seed_whole_register_on
        seed_whole_register_on(self.conn, "chk", REGISTER)
        _choose(self.conn, 1)
        text = REGISTER + ('"Visa","","2026-07-20","Bookshop","","","","",'
                           '"$20.00","$0.00","Cleared"\n')
        r = ynabimport.import_ynab(
            self.conn, "chk", text,
            accounts={"Checking": "chk", "Visa": "card"},
            remove_earlier={"Visa": True})
        self.assertEqual(r["skipped_older"], 1)
        self.assertNotIn("Bookshop", [n for n, _ in self._rows("card")])
        self.assertEqual(len(self._rows("card")), 3)


class FolderImportCutoffTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_each_file_reports_what_the_window_left_out(self):
        _choose(self.conn, 3)
        plan = bulk_import.analyze(
            self.conn,
            [("example credit union.csv", _csv(CSV_ROWS).encode())])
        results = submit_and_run(self.conn, plan["token"], plan["files"])
        self.assertTrue(results[0]["ok"], results)
        self.assertEqual(results[0]["imported"], 2)
        self.assertEqual(results[0]["skipped_older"], 2)


class SingleFileDoorCutoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.c = TestClient(self.app)
        self.c.post("/api/signup", data={
            "email": f"window-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = self.c.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
        finally:
            conn.close()

    def test_the_upload_answer_carries_skipped_older(self):
        r = self.c.post("/api/settings", json={"history_months": 3})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.c.post("/api/import", data={"account_id": "chk"},
                        files={"file": ("statement.csv",
                                        _csv(CSV_ROWS).encode(),
                                        "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        result = r.json()["result"]
        self.assertEqual(result["imported"], 2, result)
        self.assertEqual(result["skipped_older"], 2)


if __name__ == "__main__":
    unittest.main()
