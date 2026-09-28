"""A full account or card number printed on a statement line never lands
in a transaction's display name.

Statement importers cut the payee description out of the same line whose
stored copy (raw.line) is masked, and the account number sits inside that
description. The name then leaves the instance through /export, the
portability archive, the LLM categorizer, webhooks and every role's ledger
view, so masking only raw protects nothing. Every file importer's name and
merchant name are held to the same cut: any run of eight or more digits
keeps its last four. Restoring an archive written before the rule applies
the cut too."""

import pathlib
import unittest

from oikonome.sync import csvimport, pdfimport, restore

from .test_pdfimport import make_pdf
from .test_restore_v2 import _zip
from .util import make_db, write_config

_MIGRATIONS = pathlib.Path(restore.__file__).parent.parent / "db" / "migrations"
MIGRATION_FILES = (_MIGRATIONS / "153_file_import_name_number_scrub.sql",
                   _MIGRATIONS / "154_file_import_alias_scrub.sql")


def _migration_sql() -> str:
    return "\n".join(p.read_text() for p in MIGRATION_FILES)

CARD = "4000123456789010"          # invented, not a real card number
ACCT = "000123456789"


class StatementNumbersMaskedInNamesTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _names(self, like):
        return [(r["name"], r["merchant_name"]) for r in self.conn.execute(
            "SELECT name, merchant_name FROM transactions WHERE id LIKE %s",
            (like,)).fetchall()]

    def test_pdf_import_masks_numbers_in_the_name(self):
        pdf = make_pdf(
            "Statement Period 01/01/2026 - 01/31/2026\n"
            f"01/05 PAYMENT CARD {CARD} 13.00\n"
            f"01/06 TRANSFER TO ACCT {ACCT} 50.00 1,000.00\n")
        pdfimport.import_pdf(self.conn, "chk", pdf)
        names = self._names("pdf:%")
        self.assertEqual(len(names), 2)
        for name, merchant in names:
            for v in (name, merchant or ""):
                self.assertNotRegex(v, r"\d{8,}")
        self.assertIn("PAYMENT CARD …9010", [n for n, _ in names])
        self.assertIn("TRANSFER TO ACCT …6789", [n for n, _ in names])

    def test_pdf_id_hash_carries_no_full_number(self):
        """The row id is a hash of the name; hashed over the full number, a
        card number (BIN known, last four shown) is a few hundred thousand
        guesses away from anyone holding an export."""
        import hashlib
        pdf = make_pdf("Statement Period 01/01/2026 - 01/31/2026\n"
                       f"01/05 PAYMENT CARD {CARD} 13.00\n")
        pdfimport.import_pdf(self.conn, "chk", pdf)
        tid = self.conn.execute(
            "SELECT id FROM transactions WHERE id LIKE 'pdf:%'"
        ).fetchone()["id"]
        amount = self.conn.execute(
            "SELECT amount FROM transactions WHERE id=%s", (tid,)
        ).fetchone()["amount"]
        full = (f"chk|2026-01-05|{round(float(amount) * 100)}|"
                f"payment card {CARD}|1")
        self.assertNotEqual(
            tid, "pdf:" + hashlib.sha1(full.encode()).hexdigest())

    def test_reimport_after_the_mask_does_not_duplicate_old_rows(self):
        """A statement imported before names were masked keeps its row when
        the same PDF is imported again: the old id is recognised, the row
        is updated in place (and its name masked), nothing is added."""
        import hashlib
        pdf = make_pdf("Statement Period 01/01/2026 - 01/31/2026\n"
                       f"01/05 PAYMENT CARD {CARD} 13.00\n")
        parsed = pdfimport.parse_statement(pdfimport.extract_text(pdf),
                                           kind="bank")["rows"][0]
        base = (f"chk|{parsed['date'].isoformat()}|"
                f"{round(parsed['amount'] * 100)}|"
                f"payment card {CARD}")
        legacy = "pdf:" + hashlib.sha1(f"{base}|1".encode()).hexdigest()
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " removed, raw) VALUES (%s,'chk',%s,%s,%s,0,'{}')",
            (legacy, parsed["date"], parsed["amount"],
             f"PAYMENT CARD {CARD}"))
        pdfimport.import_pdf(self.conn, "chk", pdf)
        rows = self.conn.execute(
            "SELECT id, name FROM transactions WHERE id LIKE 'pdf:%'"
        ).fetchall()
        self.assertEqual([r["id"] for r in rows], [legacy])
        self.assertEqual(rows[0]["name"], "PAYMENT CARD …9010")

    # --- every file importer: the id hashes the masked name, and a row
    # stored under the id it was given before the mask is found again

    PAYEE = f"PAYMENT CARD {CARD}"

    def _importers(self):
        """(id prefix, import-once callable) for every file importer, each
        fed one row whose name carries the full card number."""
        from oikonome.sync import (competitors, mintimport, ofximport,
                                   qifimport, ynabimport)
        import datetime as dt
        p = self.PAYEE
        return [
            ("csv", lambda: csvimport.import_csv(
                self.conn, "chk", f"Date,Description,Amount\n"
                f"2026-01-05,{p},-13.00\n",
                {"date": "Date", "name": "Description", "amount": "Amount"})),
            ("ofx", lambda: ofximport._import_into(
                self.conn, "chk",
                [{"fitid": None, "date": dt.date(2026, 1, 5),
                  "amount": -13.0, "name": p, "memo": None,
                  "type": "DEBIT", "acct": None}], batch_id=None)),
            ("qif", lambda: qifimport.import_qif(
                self.conn, "chk", f"!Type:Bank\nD01/05/2026\nT-13.00\n"
                f"P{p}\n^\n")),
            ("mint", lambda: mintimport.import_mint(
                self.conn, "chk",
                "Date,Description,Original Description,Amount,"
                "Transaction Type,Category,Account Name\n"
                f"01/05/2026,Card payment,{p},13.00,debit,Shopping,Chk\n")),
            ("ynab", lambda: ynabimport.import_ynab(
                self.conn, "chk", "Date,Payee,Outflow,Inflow\n"
                f"01/05/2026,{p},13.00,\n")),
            ("monarch", lambda: competitors.import_monarch(
                self.conn, "chk",
                "Date,Merchant,Category,Account,Original Statement,Amount\n"
                f"2026-01-05,Card,Shopping,Chk,{p},-13.00\n")),
            ("copilot", lambda: competitors.import_copilot(
                self.conn, "chk",
                "Date,Name,Amount,Status,Category,Account\n"
                f"2026-01-05,{p},13.00,posted,Shopping,Chk\n")),
            ("simplifi", lambda: competitors.import_simplifi(
                self.conn, "chk", "Date,Payee,Amount,Account,Category\n"
                f"2026-01-05,{p},-13.00,Chk,Shopping\n")),
        ]

    def _full_number_id(self, prefix, row):
        import hashlib
        base = (f"{row['account_id']}|{row['date'].isoformat()}|"
                f"{round(float(row['amount']) * 100)}|"
                f"{self.PAYEE.lower()}|1")
        return f"{prefix}:" + hashlib.sha1(base.encode()).hexdigest()

    def _rows(self, prefix):
        return self.conn.execute(
            "SELECT id, account_id, date, amount, name FROM transactions"
            " WHERE id LIKE %s ORDER BY id", (prefix + ":%",)).fetchall()

    def test_every_file_import_id_hash_carries_no_full_number(self):
        for prefix, run in self._importers():
            with self.subTest(importer=prefix):
                # one importer at a time: a row already in the ledger would
                # be claimed by cross-source dedup
                self.conn.execute("DELETE FROM transactions")
                run()
                (row,) = self._rows(prefix)
                self.assertEqual(row["name"], "PAYMENT CARD …9010")
                self.assertNotEqual(row["id"],
                                    self._full_number_id(prefix, row))

    def test_every_file_reimport_updates_a_row_stored_before_the_mask(self):
        """A row an importer stored under its full-number id is updated in
        place by the same file imported again — never duplicated."""
        for prefix, run in self._importers():
            with self.subTest(importer=prefix):
                # one importer at a time: a row already in the ledger would
                # be claimed by cross-source dedup
                self.conn.execute("DELETE FROM transactions")
                run()
                (row,) = self._rows(prefix)
                legacy = self._full_number_id(prefix, row)
                self.conn.execute(
                    "UPDATE transactions SET id=%s, name=%s WHERE id=%s",
                    (legacy, self.PAYEE, row["id"]))
                run()
                rows = self._rows(prefix)
                self.assertEqual([r["id"] for r in rows], [legacy])
                self.assertEqual(rows[0]["name"], "PAYMENT CARD …9010")

    def test_csv_import_masks_numbers_in_name_and_merchant(self):
        text = ("Date,Description,Payee,Amount\n"
                f"2026-01-05,ONLINE TRANSFER TO ACCT {ACCT},"
                f"CARD {CARD},-50.00\n")
        csvimport.import_csv(self.conn, "chk", text,
                             {"date": "Date", "name": "Description",
                              "merchant": "Payee", "amount": "Amount"})
        (name, merchant), = self._names("csv:%")
        self.assertEqual(name, "ONLINE TRANSFER TO ACCT …6789")
        self.assertEqual(merchant, "CARD …9010")

    def test_aggregator_names_are_left_alone(self):
        """Only file imports are cut: an aggregator's name is its own word
        and a re-sync would restate it anyway."""
        from oikonome.sync.base import Transaction, upsert_transactions
        import datetime as dt
        upsert_transactions(self.conn, [Transaction(
            id="agg-1", account_id="chk", date=dt.date(2026, 1, 5),
            amount=5.0, name="REF 1234567890 COFFEE", raw={})])
        self.assertEqual(self._names("agg-1")[0][0], "REF 1234567890 COFFEE")

    def test_restore_masks_numbers_in_file_import_names(self):
        z = _zip({"transactions": (
            ["id", "account_id", "date", "amount", "name", "merchant_name"],
            [{"id": "pdf:abc", "account_id": "chk", "date": "2026-01-05",
              "amount": 13, "name": f"PAYMENT CARD {CARD}",
              "merchant_name": f"PAYMENT CARD {CARD}"},
             {"id": "agg-2", "account_id": "chk", "date": "2026-01-05",
              "amount": 5, "name": "REF 1234567890 COFFEE"}])})
        restore.restore_zip(self.conn, z)
        self.assertEqual(self._names("pdf:abc"),
                         [("PAYMENT CARD …9010", "PAYMENT CARD …9010")])
        self.assertEqual(self._names("agg-2")[0][0], "REF 1234567890 COFFEE")

    def test_migration_masks_names_already_stored(self):
        """Rows imported before the rule are cut once, in place; aggregator
        rows keep their names; a second run changes nothing."""
        for tid, name in (("pdf:old1", f"PAYMENT CARD {CARD}"),
                          ("csv:old2", f"TRANSFER {ACCT}"),
                          ("agg-3", "REF 1234567890 COFFEE")):
            self.conn.execute(
                "INSERT INTO transactions (id, account_id, date, amount,"
                " name, merchant_name, removed, raw)"
                " VALUES (%s,'chk','2026-01-05',1,%s,%s,0,'{}')",
                (tid, name, name))
        sql = _migration_sql()
        self.conn.execute(sql)
        self.conn.execute(sql)
        self.assertEqual(self._names("pdf:old1"),
                         [("PAYMENT CARD …9010", "PAYMENT CARD …9010")])
        self.assertEqual(self._names("csv:old2"),
                         [("TRANSFER …6789", "TRANSFER …6789")])
        self.assertEqual(self._names("agg-3")[0][0], "REF 1234567890 COFFEE")

    # --- the merchant alias and rename journal follow the masked key

    OTHER = "4000999999999010"      # invented; same last four as CARD

    def _alias_of(self, tid):
        from oikonome.engine.merchant_sql import MC_JOIN
        return self.conn.execute(
            f"SELECT mc.canonical FROM transactions t {MC_JOIN}"
            " WHERE t.id = %s", (tid,)).fetchone()["canonical"]

    def _raws(self, table):
        return sorted(r["raw_merchant"] for r in self.conn.execute(
            f"SELECT raw_merchant FROM {table}").fetchall())

    def test_migration_keeps_a_renamed_file_merchant_joined_to_its_rows(self):
        """A file-import merchant the person renamed still shows their name
        after its rows are masked; the full number leaves the alias and the
        rename journal; an aggregator row keyed on its own full string
        keeps its alias; two raws that collapse onto one masked key keep
        the manual alias."""
        rows = (("csv:m1", f"PAYMENT CARD {CARD}"),
                ("pdf:m2", f"PAYMENT CARD {self.OTHER}"),
                ("agg-m3", "REF 1234567890 COFFEE"))
        for tid, name in rows:
            self.conn.execute(
                "INSERT INTO transactions (id, account_id, date, amount,"
                " name, removed, raw)"
                " VALUES (%s,'chk','2026-01-05',1,%s,0,'{}')", (tid, name))
        for raw, canon, method in (
                (f"PAYMENT CARD {CARD}", "My Card", "manual"),
                (f"PAYMENT CARD {self.OTHER}", "Card Pmt", "layer1"),
                ("REF 1234567890 COFFEE", "Corner Coffee", "manual")):
            self.conn.execute(
                "INSERT INTO merchant_canonical (raw_merchant, canonical,"
                " method) VALUES (%s,%s,%s)", (raw, canon, method))
        self.conn.execute(
            "INSERT INTO merchant_renames (raw_merchant, from_canonical,"
            " to_canonical) VALUES (%s,'Card Pmt','My Card')",
            (f"PAYMENT CARD {CARD}",))
        sql = _migration_sql()
        self.conn.execute(sql)
        self.conn.execute(sql)
        self.assertEqual(self._alias_of("csv:m1"), "My Card")
        self.assertEqual(self._alias_of("pdf:m2"), "My Card")
        self.assertEqual(self._alias_of("agg-m3"), "Corner Coffee")
        self.assertEqual(self._raws("merchant_canonical"),
                         ["PAYMENT CARD …9010", "REF 1234567890 COFFEE"])
        self.assertEqual(self._raws("merchant_renames"),
                         ["PAYMENT CARD …9010"])

    def test_restore_keeps_a_renamed_file_merchant_joined_to_its_rows(self):
        """An archive written before names were masked: the file row comes
        back masked, and so do its alias and journal row, which therefore
        still join it; an aggregator alias is restored as written."""
        full = f"PAYMENT CARD {CARD}"
        z = _zip({
            "transactions": (
                ["id", "account_id", "date", "amount", "name"],
                [{"id": "csv:r1", "account_id": "chk",
                  "date": "2026-01-05", "amount": 13, "name": full},
                 {"id": "agg-r2", "account_id": "chk",
                  "date": "2026-01-05", "amount": 5,
                  "name": "REF 1234567890 COFFEE"}]),
            "merchant_canonical": (
                ["raw_merchant", "canonical", "method"],
                [{"raw_merchant": full, "canonical": "My Card",
                  "method": "manual"},
                 {"raw_merchant": "REF 1234567890 COFFEE",
                  "canonical": "Corner Coffee", "method": "manual"}]),
            "merchant_renames": (
                ["id", "raw_merchant", "from_canonical", "to_canonical"],
                [{"id": "7", "raw_merchant": full,
                  "from_canonical": "Card Pmt", "to_canonical": "My Card"}]),
        })
        restore.restore_zip(self.conn, z)
        self.assertEqual(self._alias_of("csv:r1"), "My Card")
        self.assertEqual(self._alias_of("agg-r2"), "Corner Coffee")
        self.assertEqual(self._raws("merchant_canonical"),
                         ["PAYMENT CARD …9010", "REF 1234567890 COFFEE"])
        self.assertEqual(self._raws("merchant_renames"),
                         ["PAYMENT CARD …9010"])

    def test_migration_and_upsert_agree_on_the_file_import_prefixes(self):
        """The backfill and the live rule must cut the same rows, or rows
        stored before and after the rule differ."""
        import re

        from oikonome.sync.base import FILE_IMPORT_ID_PREFIXES
        arrs = re.findall(r"IN \((.*?)\)", _migration_sql(), re.S)
        self.assertEqual(len(arrs), 2)
        for arr in arrs:
            self.assertEqual(
                tuple(p + ":" for p in re.findall(r"'([^']*)'", arr)),
                FILE_IMPORT_ID_PREFIXES)


if __name__ == "__main__":
    unittest.main()
