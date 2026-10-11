"""A file row's raw never keeps the full number its stored name was cut from.

A file importer masks a run of eight or more digits in the stored name
(an account or card number a statement prints into the description), but
the whole source row also lands in transactions.raw, which is plaintext and
leaves the instance verbatim through /export and the portability archive.
If the cell the name came from, or a memo or payee cell beside it, kept the
full digits, the masking of the name would protect nothing. The same holds
for an archive written before raw was masked: a restore masks it on the
way in. Cells that carry no such text are kept as they were.
"""

import csv
import io
import json
import unittest
import zipfile

from oikonome.db import tenancy
from oikonome.sync import competitors, csvimport, restore, ynabimport
from oikonome.sync.base import mask_file_raw

from .util import make_db, write_config

FULL = "1234567890123"


def _raw(conn, like):
    return [r["raw"] for r in conn.execute(
        "SELECT raw FROM transactions WHERE id LIKE %s", (like,)).fetchall()]


class FileRowRawIsMasked(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_csv_description_cell_is_masked(self):
        csvimport.import_csv(
            self.conn, "chk",
            "Date,Details,Amount,Ref\n"
            f"2026-03-10,ONLINE TRANSFER TO CHK {FULL},-50.00,R-17\n",
            {"date": "Date", "amount": "Amount", "name": "Details"})
        [raw] = _raw(self.conn, "csv:%")
        self.assertNotIn(FULL, json.dumps(raw))
        self.assertEqual(raw["Details"], "ONLINE TRANSFER TO CHK …0123")
        self.assertEqual(raw["Ref"], "R-17")
        self.assertEqual(raw["Date"], "2026-03-10")

    def test_a_monarch_original_statement_cell_is_masked(self):
        competitors.import_monarch(
            self.conn, "chk",
            "Date,Merchant,Category,Account,Original Statement,Notes,"
            "Amount,Tags\n"
            f"2026-03-10,Bank,Transfer,Checking,XFER {FULL},,-50.00,\n")
        [raw] = _raw(self.conn, "monarch:%")
        self.assertNotIn(FULL, json.dumps(raw))

    def test_a_ynab_memo_and_payee_are_masked(self):
        ynabimport.import_ynab(
            self.conn, "chk",
            "Account,Flag,Date,Payee,Category Group/Category,"
            "Category Group,Category,Memo,Outflow,Inflow,Cleared\n"
            f"Checking,,03/10/2026,PAY {FULL},,,,card {FULL},$50.00,"
            "$0.00,Cleared\n")
        [raw] = _raw(self.conn, "ynab:%")
        self.assertNotIn(FULL, json.dumps(raw))

    def test_an_old_archive_file_row_is_masked_on_the_way_in(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=[
                "id", "account_id", "date", "amount", "name", "raw"])
            w.writeheader()
            w.writerow({"id": "csv:abc", "account_id": "chk",
                        "date": "2026-03-10", "amount": "50",
                        "name": "ONLINE TRANSFER TO CHK …0123",
                        "raw": json.dumps({
                            "Details": f"ONLINE TRANSFER TO CHK {FULL}",
                            "Ref": "R-17"})})
            z.writestr("transactions.csv", s.getvalue())
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=["id", "item_id", "name",
                                              "type"])
            w.writeheader()
            w.writerow({"id": "chk", "item_id": "manual", "name": "Chk",
                        "type": "depository"})
            z.writestr("accounts.csv", s.getvalue())
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, "restore-raw-mask")
        finally:
            admin.close()
        b = tenancy.tenant_connect(tid)
        self.addCleanup(b.close)
        restore.restore_zip(b, buf.getvalue())
        [raw] = _raw(b, "csv:%")
        self.assertEqual(raw, {"Details": "ONLINE TRANSFER TO CHK …0123",
                               "Ref": "R-17"})

    def test_cells_without_the_rows_text_are_kept(self):
        raw = {"Date": "20260310", "Amount": "-50.00",
               "Check": "12345678", "Details": f"X {FULL}"}
        self.assertEqual(mask_file_raw(raw, f"X {FULL}"), {
            "Date": "20260310", "Amount": "-50.00",
            "Check": "12345678", "Details": "X …0123"})


if __name__ == "__main__":
    unittest.main()
