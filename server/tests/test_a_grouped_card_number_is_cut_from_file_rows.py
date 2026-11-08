"""A file row keeps only the last four of a card number printed in groups.

A description spells a card the way the card does, "AUTOPAY 4111 1111 1111
1111". The stored name and the raw cells are the most travelled text there
is (export, archive, the categorizer's prompt, webhooks, every member's
view), so the grouped number is cut as an unbroken run is.

The row's id is another matter. It is a hash of the text the importer cut
only for unbroken runs, and every re-import finds its row by that hash, so
the id of a line must stay what it has always been: a wider cut there would
land every such line a second time. But that hash holds the whole number and
every other input rides the export beside the row, the last four included, so
such a line is stored under the keyed form of the id from the moment it lands,
and a re-import recomputes the id and finds it there. A row stored before that
is keyed by the migrate pass.
"""
import csv
import datetime as dt
import hashlib
import io
import json
import re
import unittest
import zipfile

from oikonome.db import tenancy
from oikonome.sync import (base, batches, competitors, csvimport, mintimport,
                           ofximport, pdfimport, qifimport, restore,
                           ynabimport)

from .test_pdfimport import make_pdf
from .util import make_db, write_config

CSV_MAP = {"date": "Date", "amount": "Amount", "name": "Description",
           "merchant": "Payee"}
HEADER = "Date,Description,Payee,Memo,Amount\n"
SPACES = "AUTOPAY 4111 1111 1111 1111"
DASHES = "PMT 4111-1111-1111-1111"
LINE_SPACES = f"2026-03-10,{SPACES},{SPACES},card {SPACES},-50.00\n"
LINE_DASHES = f"2026-03-11,{DASHES},{DASHES},card {DASHES},-20.00\n"
FILE = HEADER + LINE_SPACES + LINE_DASHES
WHOLE = re.compile(r"1111[ -]1111")


def _pinned_id(account, day, cents, text, n=1):
    """The id as every release minted it, spelled out here on purpose: the
    masked text a line is hashed over holds a grouped number whole."""
    base_text = f"{account}|{day}|{cents}|{text.lower()}|{n}"
    return "csv:" + hashlib.sha1(base_text.encode()).hexdigest()


class GroupedNumberIsCut(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _import(self, text=FILE):
        return csvimport.import_csv(self.conn, "chk", text, CSV_MAP)

    def _rows(self):
        return self.conn.execute(
            "SELECT id, name, merchant_name, raw FROM transactions"
            " ORDER BY date").fetchall()

    def test_the_stored_name_merchant_and_raw_keep_only_the_last_four(self):
        self._import()
        rows = self._rows()
        self.assertEqual([r["name"] for r in rows],
                         ["AUTOPAY …1111", "PMT …1111"])
        self.assertEqual([r["merchant_name"] for r in rows],
                         ["AUTOPAY …1111", "PMT …1111"])
        for r in rows:
            self.assertIsNone(WHOLE.search(json.dumps(r["raw"])), r["raw"])

    def test_the_mask_helpers_agree(self):
        self.assertEqual(base.mask_account_name(SPACES), "AUTOPAY …1111")
        self.assertEqual(base.mask_account_name(DASHES), "PMT …1111")
        # the hash input keeps its old spelling: see the module docstring
        self.assertEqual(base.mask_long_digits(SPACES), SPACES)

    def _keyed(self, plain):
        return base.keyed_file_id(base.txn_id_secret(self.conn), plain)

    def _as_stored_before_write_time_keying(self, plain):
        self.conn.execute("UPDATE transactions SET id = %s", (plain,))

    def test_a_line_is_keyed_the_moment_it_lands(self):
        """Hashed over the text it always was, and never stored in that
        form: the id would put the number a short search away from anyone
        holding an export taken before the next migrate."""
        self._import()
        ids = [r["id"] for r in self._rows()]
        plain = [_pinned_id("chk", "2026-03-10", 5000, SPACES),
                 _pinned_id("chk", "2026-03-11", 2000, DASHES)]
        self.assertFalse(set(ids) & set(plain))
        self.assertEqual(ids, [self._keyed(p) for p in plain])

    def test_every_importer_keys_such_a_line(self):
        p = SPACES
        for prefix, once in (
            ("csv", lambda: self._import(HEADER + LINE_SPACES)),
            ("ofx", lambda: ofximport._import_into(
                self.conn, "chk",
                [{"fitid": None, "date": dt.date(2026, 1, 5), "amount": -13.0,
                  "name": p, "memo": None, "type": "DEBIT", "acct": None}],
                batch_id=None)),
            ("qif", lambda: qifimport.import_qif(
                self.conn, "chk",
                f"!Type:Bank\nD01/05/2026\nT-13.00\nP{p}\n^\n")),
            ("mint", lambda: mintimport.import_mint(
                self.conn, "chk",
                "Date,Description,Original Description,Amount,"
                "Transaction Type,Category,Account Name\n"
                f"01/05/2026,{p},{p},13.00,debit,Shopping,Chk\n")),
            ("ynab", lambda: ynabimport.import_ynab(
                self.conn, "chk", "Date,Payee,Outflow,Inflow\n"
                f"01/05/2026,{p},13.00,\n")),
            ("monarch", lambda: competitors.import_monarch(
                self.conn, "chk",
                "Date,Merchant,Category,Account,Original Statement,Amount\n"
                f"2026-01-05,{p},Shopping,Chk,{p},-13.00\n")),
            ("pdf", lambda: pdfimport.import_pdf(self.conn, "chk", make_pdf(
                "Statement Period 01/01/2026 - 01/31/2026\n"
                f"01/05 {p} 13.00\n"))),
        ):
            with self.subTest(prefix):
                self.conn.execute("DELETE FROM transactions")
                once()
                rows = self._rows()
                self.assertEqual(len(rows), 1)
                self.assertRegex(rows[0]["id"], rf"^{prefix}:k[0-9a-f]{{40}}$")
                once()
                self.assertEqual([r["id"] for r in self._rows()],
                                 [rows[0]["id"]])

    def test_a_line_cut_only_for_an_unbroken_run_keeps_its_plain_id(self):
        """Its hash input was already cut, so nothing needs a secret."""
        self._import(HEADER + "2026-03-10,AUTOPAY 4111111111111111,,,-50.00\n")
        [row] = self._rows()
        self.assertRegex(row["id"], r"^csv:[0-9a-f]{40}$")
        self.assertIsNone(base.txn_id_secret(self.conn))

    def test_a_new_identical_line_is_keyed_beside_the_one_held(self):
        self._import(HEADER + LINE_SPACES)
        self._import(HEADER + LINE_SPACES + LINE_SPACES)
        rows = self._rows()
        self.assertEqual(len({r["id"] for r in rows}), 2)
        for r in rows:
            self.assertRegex(r["id"], r"^csv:k[0-9a-f]{40}$")

    def test_importing_the_same_file_again_adds_nothing(self):
        self._import()
        before = [r["id"] for r in self._rows()]
        self._import()
        self.assertEqual([r["id"] for r in self._rows()], before)

    def test_a_row_stored_whole_is_found_and_cut_by_a_re_import(self):
        self._import(HEADER + LINE_SPACES)
        plain = _pinned_id("chk", "2026-03-10", 5000, SPACES)
        self._as_stored_before_write_time_keying(plain)
        self.conn.execute(
            "UPDATE transactions SET name = %s, merchant_name = %s",
            (SPACES, SPACES))
        self._import(HEADER + LINE_SPACES)
        [row] = self._rows()
        self.assertEqual(row["name"], "AUTOPAY …1111")
        self.assertEqual(row["id"], plain)

    def test_a_keyed_row_is_found_again_by_its_content(self):
        """A keyed row no secret here explains compares by its line: a file
        spelling the number whole must read as the row stored cut."""
        day = dt.date(2026, 3, 10)
        self.assertEqual(base.line_shape(day, -50, SPACES),
                         base.line_shape(day, -50, "AUTOPAY …1111"))

    def test_a_row_cut_this_way_is_keyed_and_re_import_finds_it(self):
        """A row stored under the plain id before lines were keyed as they
        land: the name no longer proves the id, and the id hashes the
        number, so the keying pass takes it; re-import recomputes the id and
        finds the row under its keyed form."""
        self._import(HEADER + LINE_SPACES)
        plain = _pinned_id("chk", "2026-03-10", 5000, SPACES)
        self._as_stored_before_write_time_keying(plain)
        self.assertEqual(base.key_file_import_ids(self.conn), 1)
        [row] = self._rows()
        self.assertNotEqual(row["id"], plain)
        self.assertTrue(row["id"].startswith("csv:k"))
        self._import(HEADER + LINE_SPACES)
        [again] = self._rows()
        self.assertEqual(again["id"], row["id"])

    def test_a_row_stored_whole_is_corrected_in_place_by_a_csv_re_import(self):
        """The stored cells hold the number whole and the new reading's are
        cut; they are the same printed line, so a corrected sign moves the
        row and never lands a second one beside it."""
        mapping = {"date": "Date", "name": "Description",
                   "debit": "Debit", "credit": "Credit"}
        text = ("Date,Description,Debit,Credit\n"
                f"2026-07-11,REFUND {SPACES},,-150.00\n")
        csvimport.import_csv(self.conn, "chk", text, mapping)
        whole = f"REFUND {SPACES}"
        old = base.FileImportIds("csv", "chk").id_for(
            dt.date(2026, 7, 11), 150.00, whole)
        self.conn.execute(
            "UPDATE transactions SET id = %s, amount = 150, name = %s,"
            " merchant_name = %s, raw = %s::jsonb",
            (old, whole, whole, json.dumps(
                {"Date": "2026-07-11", "Description": whole,
                 "Debit": "", "Credit": "-150.00"})))
        csvimport.import_csv(self.conn, "chk", text, mapping)
        self.assertEqual(
            [(r["name"], float(r["amount"])) for r in self.conn.execute(
                "SELECT name, amount FROM transactions WHERE removed = 0"
                "   AND retired_at IS NULL").fetchall()],
            [("REFUND AUTOPAY …1111", -150.0)])

    def test_a_statement_row_stored_whole_is_corrected_in_place(self):
        """A statement row read with the opposite sign and stored with the
        whole number in its line is that line, not another beside it."""
        pdf = make_pdf("Statement Period 01/01/2026 - 01/31/2026\n"
                       f"01/05 {SPACES} 13.00\n")
        pdfimport.import_pdf(self.conn, "chk", pdf)
        [row] = self.conn.execute(
            "SELECT amount, raw FROM transactions").fetchall()
        amount, line = float(row["amount"]), row["raw"]["line"]
        self.assertIn("…1111", line)
        whole_line = line.replace("…1111", "4111 1111 1111 1111")
        old = base.FileImportIds("pdf", "chk").id_for(
            dt.date(2026, 1, 5), -amount, SPACES)
        self.conn.execute(
            "UPDATE transactions SET id = %s, amount = %s, name = %s,"
            " raw = jsonb_set(raw, '{line}', to_jsonb(%s::text))",
            (old, -amount, SPACES, whole_line))
        pdfimport.import_pdf(self.conn, "chk", pdf)
        self.assertEqual(
            [float(r["amount"]) for r in self.conn.execute(
                "SELECT amount FROM transactions WHERE removed = 0"
                "   AND retired_at IS NULL").fetchall()], [amount])

    def test_an_ofx_line_without_a_fitid_comes_back_as_the_same_row(self):
        rows = [{"fitid": None, "date": dt.date(2026, 5, 1), "amount": -12.0,
                 "name": SPACES, "memo": f"card {DASHES}", "type": "DEBIT",
                 "acct": None}]
        ofximport._import_into(self.conn, "chk", [dict(x) for x in rows],
                               batch_id=None)
        ofximport._import_into(self.conn, "chk", [dict(x) for x in rows],
                               batch_id=None)
        [row] = self._rows()
        self.assertEqual(row["name"], "AUTOPAY …1111")
        self.assertIsNone(WHOLE.search(json.dumps(row["raw"])), row["raw"])

    def test_a_fitid_line_stored_under_another_account_id_is_its_twin(self):
        """The content key an OFX file is matched by reads the row as the
        upsert stored it, whichever way the file spells the number."""
        rows = [{"fitid": "F-1", "date": dt.date(2026, 5, 1), "amount": -12.0,
                 "name": SPACES, "memo": None, "type": "DEBIT", "acct": None}]
        ofximport._import_into(self.conn, "chk", [dict(x) for x in rows],
                               batch_id=None)
        self.conn.execute(
            "UPDATE transactions SET id = 'ofx:manual:old-id:F-1'")
        ofximport._import_into(self.conn, "chk", [dict(x) for x in rows],
                               batch_id=None)
        self.assertEqual(len(self._rows()), 1)

    def test_a_statement_line_is_cut(self):
        self.assertEqual(pdfimport._mask_line(f"03/10 {SPACES} 50.00"),
                         "03/10 AUTOPAY …1111 50.00")
        self.assertEqual(
            restore.scrub_transaction_raw(
                {"line": f"03/10 {DASHES} 20.00"})["line"],
            "03/10 PMT …1111 20.00")

    def test_an_archive_holding_the_number_whole_is_cut_on_the_way_in(self):
        tid = _pinned_id("chk", "2026-03-10", 5000, SPACES)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("accounts.csv", "id,item_id,name,type\n"
                                       "chk,manual,Checking,depository\n")
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["id", "account_id", "date", "amount", "name",
                        "merchant_name", "pending", "removed", "raw"])
            w.writerow([tid, "chk", "2026-03-10", "50.0", SPACES, SPACES,
                        "0", "0", json.dumps({"Description": SPACES})])
            z.writestr("transactions.csv", out.getvalue())
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, "restore-grouped-number")
        finally:
            admin.close()
        other = tenancy.tenant_connect(tid)
        self.addCleanup(other.close)
        restore.restore_zip(other, buf.getvalue())
        [row] = other.execute(
            "SELECT id, name, merchant_name, raw FROM transactions").fetchall()
        self.assertRegex(row["id"], r"^csv:k[0-9a-f]{40}$")
        self.assertEqual(row["name"], "AUTOPAY …1111")
        self.assertEqual(row["merchant_name"], "AUTOPAY …1111")
        self.assertIsNone(WHOLE.search(json.dumps(row["raw"])), row["raw"])

    def test_an_import_batch_filename_keeps_only_the_last_four(self):
        bid = batches.create(self.conn, "csv", "chk",
                             "statement 4111-1111-1111-1111.csv")
        self.assertEqual(self.conn.execute(
            "SELECT filename FROM import_batches WHERE id = %s",
            (bid,)).fetchone()["filename"], "statement …1111.csv")

    def test_a_date_range_and_a_phone_number_are_left_alone(self):
        text = HEADER + ("2026-03-12,Plan 2023-2024 call 617-555-0100,"
                         "Plan,,-5.00\n")
        self._import(text)
        self.assertEqual(self._rows()[0]["name"],
                         "Plan 2023-2024 call 617-555-0100")


if __name__ == "__main__":
    unittest.main()
