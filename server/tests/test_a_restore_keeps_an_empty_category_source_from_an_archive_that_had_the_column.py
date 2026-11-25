"""A restore says who decided a row's category only for an archive that
could not say it.

`transactions.category_source` arrived with migration 098, which derived
it for the rows already stored. A restore applies the same derivation to
an archive written before that migration, whose rows carry no such
column. An archive written since carries the column, and a row whose
source is empty there had an empty source in its household: it lands
exactly so, never relabelled "import" (the file it was imported from).
How old the archive is comes from its own manifest, or, with none, from
the columns its headers show (archive_upgrades.archive_schema).
"""

import csv
import io
import unittest
import zipfile

from oikonome.sync import archive_upgrades, restore

from .util import TODAY, make_db

COLS_NOW = ["id", "account_id", "date", "amount", "name",
            "category_primary", "category_source", "payment_processor"]
COLS_BEFORE_098 = ["id", "account_id", "date", "amount", "name",
                   "category_primary"]


def _zip(cols, manifest_schema=None) -> bytes:
    s = io.StringIO()
    w = csv.DictWriter(s, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerow({"id": "cs-1", "account_id": "chk",
                "date": TODAY.isoformat(), "amount": "41.17",
                "name": "PAPER GOODS STORE",
                "category_primary": "GENERAL_MERCHANDISE",
                "category_source": "", "payment_processor": ""})
    w.writerow({"id": "cs-2", "account_id": "chk",
                "date": TODAY.isoformat(), "amount": "9.03",
                "name": "TEA ROOM", "category_primary": "FOOD_AND_DRINK",
                "category_source": "rule_user", "payment_processor": ""})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("transactions.csv", s.getvalue())
        if manifest_schema:
            z.writestr(archive_upgrades.MANIFEST, archive_upgrades.manifest(
                manifest_schema, "test", ["transactions"]))
    return buf.getvalue()


class ACategorySourceIsDerivedOnlyForAnArchiveWithoutIt(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _sources(self):
        return {r["id"]: r["category_source"] for r in self.conn.execute(
            "SELECT id, category_source FROM transactions "
            "WHERE id LIKE 'cs-%'")}

    def test_a_current_archive_keeps_an_empty_source_empty(self):
        data = _zip(COLS_NOW, archive_upgrades.migration_names()[-1])
        restore.restore_zip(self.conn, data)
        self.assertEqual(self._sources(), {"cs-1": None, "cs-2": "rule_user"})
        # and a second restore of it changes nothing
        restore.restore_zip(self.conn, data)
        self.assertEqual(self._sources(), {"cs-1": None, "cs-2": "rule_user"})

    def test_an_archive_without_a_manifest_that_shows_the_column_keeps_it_empty(self):
        restore.restore_zip(self.conn, _zip(COLS_NOW))
        self.assertEqual(self._sources(), {"cs-1": None, "cs-2": "rule_user"})

    def test_an_archive_from_before_the_column_has_it_derived(self):
        restore.restore_zip(self.conn, _zip(COLS_BEFORE_098))
        self.assertEqual(self._sources(), {"cs-1": "import", "cs-2": "import"})

    def test_a_manifest_older_than_the_column_has_it_derived(self):
        older = [n for n in archive_upgrades.migration_names()
                 if n < "098"][-1]
        restore.restore_zip(self.conn, _zip(COLS_BEFORE_098, older))
        self.assertEqual(self._sources()["cs-1"], "import")


if __name__ == "__main__":
    unittest.main()
