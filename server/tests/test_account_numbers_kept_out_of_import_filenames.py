"""An account number in an import's filename stays out of what is kept.

Banks name their exports after the full account number. A folder import
turns the filename into a new account's name — and from the name, the
account's permanent id — and every import records the filename in
import_batches, which leaves the instance through /export and the
portability archive. Only the last four of a run of eight or more digits
may survive, on the way in, on a restore of an older archive, and (by
migration) on records already stored.
"""

import pathlib
import unittest

from oikonome.sync import base as sync_base
from oikonome.sync import batches, export, restore
from oikonome.web import bulk_import

from .bulk_job_util import submit_and_run
from .util import make_db, write_config

FULL = "4000123456789010"        # invented, not a real account number
MIGRATIONS = pathlib.Path(sync_base.__file__).parent.parent / "db" / "migrations"
MIGRATION = MIGRATIONS / "188_import_batch_filename_number_scrub.sql"
NAME_MIGRATION = MIGRATIONS / "189_manual_account_name_number_scrub.sql"
BANK_CSV = (b"Date,Description,Amount\n"
            b"2021-03-01,COFFEE SHOP,-4.50\n"
            b"2021-03-02,PAYCHECK,1500.00\n")


class AccountNumbersKeptOutOfImportFilenames(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _filenames(self, conn=None):
        return [r["filename"] for r in (conn or self.conn).execute(
            "SELECT filename FROM import_batches").fetchall()]

    def test_a_number_glued_to_a_word_never_reaches_the_account_name(self):
        for fn in (f"Checking{FULL}.csv", f"Checking_{FULL}_2021.csv",
                   f"{FULL}Checking.csv"):
            name = bulk_import._guess_account_name(fn)
            self.assertNotIn(FULL[-8:], name, fn)
            self.assertNotRegex(name, r"\d{8}")
            self.assertIn("Checking", name, fn)

    def test_a_folder_import_keeps_the_number_out_of_the_name_and_id(self):
        fn = f"Vault{FULL}.csv"
        plan = bulk_import.analyze(self.conn, [(fn, BANK_CSV)])
        results = submit_and_run(self.conn, plan["token"], plan["files"])
        self.assertTrue(results[0]["ok"], results)
        acct = self.conn.execute(
            "SELECT id, name FROM accounts WHERE id = %s",
            (results[0]["account_id"],)).fetchone()
        for v in (acct["id"], acct["name"]):
            self.assertNotRegex(v, r"\d{8}")
        for stored in self._filenames():
            self.assertNotIn(FULL, stored)
            self.assertIn("…" + FULL[-4:], stored)

    def test_every_import_records_the_filename_masked(self):
        batches.create(self.conn, "csv", "chk", f"stmt-{FULL}.csv")
        self.assertEqual(self._filenames(), [f"stmt-…{FULL[-4:]}.csv"])

    def test_a_restored_archive_has_its_filenames_masked(self):
        # an archive written before the mask: the record holds it whole
        self.conn.execute(
            "INSERT INTO import_batches (id, source, account_id, filename)"
            " VALUES ('b1', 'csv', 'chk', %s)", (f"stmt-{FULL}.csv",))
        data = export.build_zip(self.conn)
        dest = make_db()
        self.addCleanup(dest.close)
        restore.restore_zip(dest, data)
        self.assertEqual(self._filenames(dest), [f"stmt-…{FULL[-4:]}.csv"])

    def test_the_migration_masks_stored_records_once(self):
        self.conn.execute(
            "INSERT INTO import_batches (id, source, account_id, filename)"
            " VALUES ('b1', 'csv', 'chk', %s), ('b2', 'csv', 'chk',"
            " 'chk_4821_2023.csv'), ('b3', 'csv', 'chk', NULL)",
            (f"a{FULL}b{FULL}.csv",))
        sql = MIGRATION.read_text()
        self.conn.execute(sql)
        got = {r["id"]: r["filename"] for r in self.conn.execute(
            "SELECT id, filename FROM import_batches").fetchall()}
        want = sync_base.mask_long_digits(f"a{FULL}b{FULL}.csv")
        self.assertEqual(got, {"b1": want, "b2": "chk_4821_2023.csv",
                               "b3": None})
        # nothing left to write on a second run
        self.assertEqual(self.conn.execute(sql).rowcount, 0)


    def _manual_account(self, conn, aid, name):
        conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES ('manual', 'manual', 'Manual')"
            " ON CONFLICT (tenant_id, id) DO NOTHING")
        conn.execute("INSERT INTO accounts (id, item_id, name, type)"
                     " VALUES (%s, 'manual', %s, 'depository')", (aid, name))

    def test_the_migration_masks_manual_account_names_once(self):
        self._manual_account(self.conn, "manual:csv-old", f"Vault{FULL}")
        self._manual_account(self.conn, "manual:csv-short", "Vault 2023")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES ('it-linked', 'plaid', 'Example Bank')")
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type) VALUES"
            " ('pl1', 'it-linked', %s, 'depository')", (f"Linked{FULL}",))
        sql = NAME_MIGRATION.read_text()
        self.conn.execute(sql)
        got = {r["id"]: r["name"] for r in self.conn.execute(
            "SELECT id, name FROM accounts").fetchall()}
        self.assertEqual(got["manual:csv-old"],
                         sync_base.mask_long_digits(f"Vault{FULL}"))
        self.assertEqual(got["manual:csv-short"], "Vault 2023")
        # a bank-linked account's name is the bank's, not a file's
        self.assertEqual(got["pl1"], f"Linked{FULL}")
        self.assertEqual(self.conn.execute(sql).rowcount, 0)

    def test_a_restored_manual_account_name_is_masked_and_its_id_named(self):
        aid = f"manual:csv-vault{FULL}"
        self._manual_account(self.conn, aid, f"Vault{FULL}")
        data = export.build_zip(self.conn)
        dest = make_db()
        self.addCleanup(dest.close)
        counts = restore.restore_zip(dest, data)
        row = dest.execute("SELECT id, name FROM accounts WHERE id = %s",
                           (aid,)).fetchone()
        self.assertEqual(row["name"], f"Vault…{FULL[-4:]}")
        notes = " ".join(counts.get("_notes") or [])
        self.assertIn(f"Vault…{FULL[-4:]}", notes)
        self.assertNotIn(FULL, notes)


if __name__ == "__main__":
    unittest.main()
