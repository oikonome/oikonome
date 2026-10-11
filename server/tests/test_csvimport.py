"""Generic CSV importer: mapping, sign normalization, idempotency,
same-day duplicates, cross-source dedup."""

import datetime as dt
import unittest

from oikonome.sync import csvimport

from .util import add_txn, make_db, write_config

CSV = """Date,Description,Amount,Category
2026-07-10,SAFEWAY STORE,-42.50,Groceries
2026-07-10,SAFEWAY STORE,-42.50,Groceries
07/11/2026,EMPLOYER PAYROLL,"2,000.00",Income
"Jul 12, 2026",COFFEE SHOP,(4.25),Dining
"""

MAPPING = {"date": "Date", "amount": "Amount", "name": "Description",
           "category": "Category"}


class CsvImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_import_signs_and_formats(self):
        r = csvimport.import_csv(self.conn, "chk", CSV, MAPPING)
        self.assertEqual(r["imported"], 4)
        rows = self.conn.execute(
            "SELECT date, amount, name FROM transactions WHERE id LIKE 'csv:%' "
            "ORDER BY date, amount").fetchall()
        # bank sign flipped: -42.50 (out) → +42.50; payroll +2000 → -2000;
        # accounting-parens (4.25) → out → +4.25
        amounts = [r["amount"] for r in rows]
        self.assertIn(42.50, amounts)
        self.assertIn(-2000.0, amounts)
        self.assertIn(4.25, amounts)
        # date formats all parsed
        self.assertEqual({r["date"] for r in rows},
                         {dt.date(2026, 7, 10), dt.date(2026, 7, 11),
                          dt.date(2026, 7, 12)})

    def test_same_day_duplicates_survive_and_reimport_is_idempotent(self):
        csvimport.import_csv(self.conn, "chk", CSV, MAPPING)
        r2 = csvimport.import_csv(self.conn, "chk", CSV, MAPPING)
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE id LIKE 'csv:%'"
        ).fetchone()["n"]
        self.assertEqual(n, 4)          # two identical Safeway rows kept, once
        self.assertEqual(r2["imported"], 4)   # upsert, not duplicate

    def test_cross_source_dedup_is_one_to_one(self):
        """ONE aggregator row at the same amount within ±3 days claims ONE
        csv row — it must not absorb both same-day Safeway charges (the
        old LIMIT-1-never-consumed rule dropped the second, legitimate
        charge)."""
        add_txn(self.conn, dt.date(2026, 7, 9), 42.50, "SAFEWAY", account="chk")
        r = csvimport.import_csv(self.conn, "chk", CSV, MAPPING)
        self.assertEqual(r["skipped_duplicates"], 1)   # one absorbed…
        self.assertEqual(r["imported"], 3)             # …the other imported

    def test_cross_source_dedup_two_existing_claim_two(self):
        """Two aggregator rows → both csv rows are covered."""
        add_txn(self.conn, dt.date(2026, 7, 9), 42.50, "SAFEWAY", account="chk")
        add_txn(self.conn, dt.date(2026, 7, 10), 42.50, "SAFEWAY", account="chk")
        r = csvimport.import_csv(self.conn, "chk", CSV, MAPPING)
        self.assertEqual(r["skipped_duplicates"], 2)
        self.assertEqual(r["imported"], 2)

    def test_dedup_name_mismatch_does_not_suppress(self):
        """$42.50 at an unrelated merchant a day earlier must NOT suppress
        the Safeway charges (the old rule had no name check)."""
        add_txn(self.conn, dt.date(2026, 7, 9), 42.50, "PETCO STORE",
                account="chk", merchant="Petco")
        r = csvimport.import_csv(self.conn, "chk", CSV, MAPPING)
        self.assertEqual(r["skipped_duplicates"], 0)
        self.assertEqual(r["imported"], 4)

    def test_dedup_same_day_exact_amount_needs_no_name(self):
        """Same-day exact-amount matches are trusted when the existing
        row's descriptor is opaque (no substantive name tokens) — this was
        narrowed from "no name check at all": two conflicting real
        merchant names on the same day are two purchases (see
        test_dedup_distinct)."""
        add_txn(self.conn, dt.date(2026, 7, 10), 42.50, "POS DEBIT 1234",
                account="chk")
        r = csvimport.import_csv(self.conn, "chk", CSV, MAPPING)
        self.assertEqual(r["skipped_duplicates"], 1)
        self.assertEqual(r["imported"], 3)

    def test_flow_categories_mapped(self):
        """Card payments / transfers in the category column map to
        spend-excluded categories instead of passing through as vendor
        strings the spend definition counts."""
        text = ("Date,Description,Amount,Category\n"
                "2026-07-10,CHASE EPAY,-300.00,Credit Card Payment\n"
                "2026-07-11,ONLINE XFER TO SAVINGS,-500.00,Transfer\n"
                "2026-07-12,ONLINE XFER BACK,500.00,transfer\n"
                "2026-07-13,SAFEWAY,-42.50,Groceries\n")
        csvimport.import_csv(self.conn, "chk", text, MAPPING)
        rows = {r["name"]: r for r in self.conn.execute(
            "SELECT name, category_primary, category_detailed "
            "FROM transactions WHERE id LIKE 'csv:%'").fetchall()}
        self.assertEqual(rows["CHASE EPAY"]["category_detailed"],
                         "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        self.assertEqual(rows["CHASE EPAY"]["category_primary"],
                         "LOAN_PAYMENTS")
        self.assertEqual(rows["ONLINE XFER TO SAVINGS"]["category_primary"],
                         "TRANSFER_OUT")         # money out
        self.assertEqual(rows["ONLINE XFER BACK"]["category_primary"],
                         "TRANSFER_IN")          # money in, case-insensitive
        self.assertEqual(rows["SAFEWAY"]["category_primary"], "Groceries")

    def test_account_and_routing_columns_never_land_whole_in_raw(self):
        """transactions.raw is plaintext JSONB that rides /export and the
        data-portability archive verbatim, so a full account or routing
        number must not be stored there — banks outside aggregator coverage
        (this importer's whole population) routinely put one on every row.
        Only the last 4 is kept, the same rule ofximport applies to the OFX
        ACCTID; every other column stays whole, because raw is what
        re-categorization and a source audit read."""
        text = ("Date,Description,Amount,Account Number,Routing Number,"
                "Account Name,Reference\n"
                "2026-07-10,SAFEWAY STORE,-42.50,1234567890123456,"
                "021000021,Everyday Checking,REF-99887766\n")
        csvimport.import_csv(self.conn, "chk", text, MAPPING)
        raw = self.conn.execute(
            "SELECT raw FROM transactions WHERE id LIKE 'csv:%'"
        ).fetchone()["raw"]
        self.assertEqual(raw["Account Number"], "3456")
        self.assertEqual(raw["Routing Number"], "0021")
        # kept: a nickname is not an identifier, and an opaque reference
        # number is what the audit trail is FOR — no value-shaped guessing
        self.assertEqual(raw["Account Name"], "Everyday Checking")
        self.assertEqual(raw["Reference"], "REF-99887766")
        self.assertEqual(raw["Description"], "SAFEWAY STORE")

    def test_identifier_header_spellings(self):
        """The same column under the spellings real exports use."""
        for header in ("Acct. No.", "ACCOUNT_NUMBER", "Account #",
                       "Card Number", "aba number"):
            with self.subTest(header=header):
                text = (f"Date,Description,Amount,{header}\n"
                        "2026-07-10,SHOP,-1.00,1234567890123456\n")
                conn = make_db()
                try:
                    write_config(conn)
                    csvimport.import_csv(conn, "chk", text, MAPPING)
                    raw = conn.execute(
                        "SELECT raw FROM transactions "
                        "WHERE id LIKE 'csv:%'").fetchone()["raw"]
                    self.assertEqual(raw[header], "3456")
                finally:
                    conn.close()

    def test_missing_required_mapping(self):
        with self.assertRaises(ValueError):
            csvimport.import_csv(self.conn, "chk", CSV, {"date": "Date"})

    def test_plaid_signed_export(self):
        text = "Date,Description,Amount\n2026-07-10,SHOP,42.50\n"
        csvimport.import_csv(self.conn, "chk", text,
                             {"date": "Date", "amount": "Amount",
                              "name": "Description"}, amount_sign="plaid")
        row = self.conn.execute(
            "SELECT amount FROM transactions WHERE id LIKE 'csv:%'").fetchone()
        self.assertEqual(row["amount"], 42.50)   # kept as-is


class DebitCreditColumnSignTests(unittest.TestCase):
    """With separate Debit and Credit columns the column decides the
    direction; a sign printed inside the column must not flip the row."""

    MAPPING = {"date": "Date", "name": "Description",
               "debit": "Debit", "credit": "Credit"}

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _amounts(self):
        return {r["name"]: r["amount"] for r in self.conn.execute(
            "SELECT name, amount FROM transactions WHERE id LIKE 'csv:%'")}

    def test_negative_credit_is_money_in(self):
        text = ("Date,Description,Debit,Credit\n"
                "2026-07-10,HARDWARE STORE,63.20,\n"
                "2026-07-11,REFUND HARDWARE STORE,,-150.00\n"
                "2026-07-12,ONLINE PAYMENT THANK YOU,,(410.00)\n")
        csvimport.import_csv(self.conn, "chk", text, self.MAPPING)
        rows = self._amounts()
        self.assertEqual(rows["HARDWARE STORE"], 63.20)
        self.assertEqual(rows["REFUND HARDWARE STORE"], -150.00)
        self.assertEqual(rows["ONLINE PAYMENT THANK YOU"], -410.00)

    def test_negative_debit_is_money_out(self):
        text = ("Date,Description,Debit,Credit\n"
                "2026-07-10,CORNER GROCER,-27.35,\n"
                "2026-07-11,PAYROLL DEPOSIT,,1800.00\n")
        csvimport.import_csv(self.conn, "chk", text, self.MAPPING)
        rows = self._amounts()
        self.assertEqual(rows["CORNER GROCER"], 27.35)
        self.assertEqual(rows["PAYROLL DEPOSIT"], -1800.00)


class ReimportAfterTheColumnSignRuleTests(unittest.TestCase):
    """A Debit/Credit file imported while a sign printed inside a column
    still flipped the row (amount = debit - credit) holds rows under ids
    hashed from that sign. Importing it again must correct those rows in
    place, under the new id, carrying what a person put on them — never
    add the corrected row beside the old one, which counts the money once
    each way."""

    MAPPING = {"date": "Date", "name": "Description",
               "debit": "Debit", "credit": "Credit"}
    TEXT = ("Date,Description,Debit,Credit\n"
            "2026-07-10,HARDWARE STORE,60.00,\n"
            "2026-07-11,REFUND HARDWARE STORE,,-150.00\n"
            "2026-07-12,CORNER GROCER,-30.00,\n")
    # the old rule's reading of each line, in file order
    OLD = ((dt.date(2026, 7, 10), 60.00, "HARDWARE STORE"),
           (dt.date(2026, 7, 11), 150.00, "REFUND HARDWARE STORE"),
           (dt.date(2026, 7, 12), -30.00, "CORNER GROCER"))
    NEW = (60.00, -150.00, 30.00)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _ids(self):
        from oikonome.sync.base import FileImportIds
        new, old = FileImportIds("csv", "chk"), FileImportIds("csv", "chk")
        return ([new.id_for(d, a, n) for (d, _, n), a
                 in zip(self.OLD, self.NEW)],
                [old.id_for(d, a, n) for d, a, n in self.OLD])

    def _store_the_old_reading(self, *, keep_new_too=False):
        """Rows as the earlier rule stored them: the refund and the
        grocer under their old-sign ids and amounts."""
        csvimport.import_csv(self.conn, "chk", self.TEXT, self.MAPPING)
        new, old = self._ids()
        for i in (1, 2):
            amount = self.OLD[i][1]
            if keep_new_too:
                self.conn.execute(
                    "INSERT INTO transactions (id, account_id, date, amount,"
                    " name, merchant_name, removed, raw)"
                    " SELECT %s, account_id, date, %s, name, merchant_name,"
                    "        0, raw FROM transactions WHERE id = %s",
                    (old[i], amount, new[i]))
            else:
                self.conn.execute(
                    "UPDATE transactions SET id=%s, amount=%s WHERE id=%s",
                    (old[i], amount, new[i]))
            self.conn.execute(
                "INSERT INTO transaction_notes (txn_id, note) VALUES (%s,%s)",
                (old[i], f"note {i}"))
        return new, old

    def _live(self):
        return sorted((r["name"], float(r["amount"])) for r in
                      self.conn.execute(
                          "SELECT name, amount FROM transactions"
                          " WHERE account_id='chk' AND removed=0"))

    def test_reimport_corrects_a_row_stored_under_the_printed_sign(self):
        new, old = self._store_the_old_reading()
        r = csvimport.import_csv(self.conn, "chk", self.TEXT, self.MAPPING)
        self.assertEqual(self._live(), [("CORNER GROCER", 30.0),
                                        ("HARDWARE STORE", 60.0),
                                        ("REFUND HARDWARE STORE", -150.0)])
        self.assertEqual(r["corrected"], 2)
        have = {x["id"] for x in self.conn.execute(
            "SELECT id FROM transactions WHERE account_id='chk'")}
        self.assertEqual(have, set(new))
        notes = {x["txn_id"]: x["note"] for x in self.conn.execute(
            "SELECT txn_id, note FROM transaction_notes")}
        self.assertEqual(notes, {new[1]: "note 1", new[2]: "note 2"})

    def test_reimport_retires_an_opposite_signed_twin_of_the_same_cells(self):
        new, old = self._store_the_old_reading(keep_new_too=True)
        csvimport.import_csv(self.conn, "chk", self.TEXT, self.MAPPING)
        self.assertEqual(self._live(), [("CORNER GROCER", 30.0),
                                        ("HARDWARE STORE", 60.0),
                                        ("REFUND HARDWARE STORE", -150.0)])
        gone = self.conn.execute(
            "SELECT removed, retired_at FROM transactions WHERE id=%s",
            (old[1],)).fetchone()
        self.assertEqual(gone["removed"], 1)
        self.assertIsNotNone(gone["retired_at"])
        notes = {x["txn_id"]: x["note"] for x in self.conn.execute(
            "SELECT txn_id, note FROM transaction_notes")}
        self.assertEqual(notes.get(new[1]), "note 1")

    def test_corrected_row_frees_its_old_id_for_a_real_charge(self):
        self._store_the_old_reading()
        csvimport.import_csv(self.conn, "chk", self.TEXT, self.MAPPING)
        # a later file holds a real charge of the same amount, the same day
        csvimport.import_csv(
            self.conn, "chk",
            "Date,Description,Debit,Credit\n"
            "2026-07-11,REFUND HARDWARE STORE,150.00,\n", self.MAPPING)
        refunds = sorted(a for n, a in self._live()
                         if n == "REFUND HARDWARE STORE")
        self.assertEqual(refunds, [-150.0, 150.0])

    def test_a_different_line_at_the_old_rule_id_is_never_taken(self):
        # the old-rule id of the refund is the id of a real debit of the
        # same amount, stored from another file's Debit cell
        csvimport.import_csv(
            self.conn, "chk",
            "Date,Description,Debit,Credit\n"
            "2026-07-11,REFUND HARDWARE STORE,150.00,\n", self.MAPPING)
        csvimport.import_csv(self.conn, "chk", self.TEXT, self.MAPPING)
        refunds = sorted(a for n, a in self._live()
                         if n == "REFUND HARDWARE STORE")
        self.assertEqual(refunds, [-150.0, 150.0])

    def test_reimport_of_a_file_the_sign_rule_never_changed_is_idempotent(self):
        csvimport.import_csv(self.conn, "chk", self.TEXT, self.MAPPING)
        r = csvimport.import_csv(self.conn, "chk", self.TEXT, self.MAPPING)
        self.assertEqual(r["corrected"], 0)
        self.assertEqual(len(self._live()), 3)


if __name__ == "__main__":
    unittest.main()
