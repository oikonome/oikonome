"""An imported file's name keeps only the last four of a card number.

Banks name exports after the account number, and the name is kept for
years: Recent imports lists it, /export and the portability archive carry
it. A name whose groups an underscore or a comma separates
("Visa_4000_0000_0000_0002.csv") is a number all the same, read the way an
account id's slug reads it, so the record, a restore of it and the record
stored by an earlier build all keep "…0002" and nothing more. Every number
here is an invented test-card shape."""
import unittest

from oikonome.db import migrate
from oikonome.sync import batches, card_number, export, restore

from .util import make_db, write_config

STEP = "209_card_numbers_in_import_file_names_are_cut.sql"

NAMES = {
    "Visa_4000_0000_0000_0002.csv": "Visa_…0002.csv",
    "chase,4000,0000,0000,0002.csv": "chase,…0002.csv",
    "Visa 4000 0000 0000 0002.csv": "Visa …0002.csv",
    "export_4000000000000002.ofx": "export_…0002.ofx",
    # not numbers: dates and periods stay as the bank wrote them
    "statement_2024-01-15.csv": "statement_2024-01-15.csv",
    "Statement 2021-01-01_2021-12-31.csv":
        "Statement 2021-01-01_2021-12-31.csv",
    "spring-statement.csv": "spring-statement.csv",
}


def _stored(conn, bid):
    return conn.execute("SELECT filename FROM import_batches WHERE id = %s",
                        (bid,)).fetchone()["filename"]


class TheRecordIsCut(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def test_create_keeps_the_last_four_whatever_the_separator(self):
        for name, want in NAMES.items():
            bid = batches.create(self.conn, "csv", "chk", name)
            self.assertEqual(_stored(self.conn, bid), want, name)
            self.assertFalse(card_number.holds_number(
                card_number.cut_as_slug_reads(_stored(self.conn, bid))), name)

    def test_a_restore_keeps_the_last_four(self):
        ids = {}
        for i, name in enumerate(NAMES):
            ids[name] = f"b{i:011d}"
            # stored whole, as an earlier build wrote it
            self.conn.execute(
                "INSERT INTO import_batches (id, source, account_id, filename)"
                " VALUES (%s, 'csv', 'chk', %s)", (ids[name], name))
        data = export.build_zip(self.conn)
        dest = make_db()
        self.addCleanup(dest.close)
        restore.restore_zip(dest, data)
        for name, want in NAMES.items():
            self.assertEqual(_stored(dest, ids[name]), want, name)

    def test_the_migration_cuts_what_an_earlier_build_stored(self):
        ids = {}
        for i, name in enumerate(NAMES):
            ids[name] = f"m{i:011d}"
            self.conn.execute(
                "INSERT INTO import_batches (id, source, account_id, filename)"
                " VALUES (%s, 'csv', 'chk', %s)", (ids[name], name))
        migrate.PYTHON_STEPS[STEP](self.conn)
        for name, want in NAMES.items():
            self.assertEqual(_stored(self.conn, ids[name]), want, name)
        # applied twice it changes nothing
        migrate.PYTHON_STEPS[STEP](self.conn)
        for name, want in NAMES.items():
            self.assertEqual(_stored(self.conn, ids[name]), want, name)

    def test_the_migration_file_ships(self):
        self.assertTrue((migrate.HERE / "migrations" / STEP).exists())


if __name__ == "__main__":
    unittest.main()


class TheCutHoldsForNamesWrittenAfterTheStep(unittest.TestCase):
    def test_every_migrate_runs_the_cut_again(self):
        """The step runs once while the old container still serves; a name
        that container stores after the step read would keep its number for
        good unless every migrate cuts again, as 208's pass does."""
        import inspect
        tail = inspect.getsource(migrate.run).split(
            "cut_bill_keys_and_log_names(conn, pw)", 1)[1]
        self.assertIn("_cut_import_file_names(conn)", tail.split("finally:")[0])
