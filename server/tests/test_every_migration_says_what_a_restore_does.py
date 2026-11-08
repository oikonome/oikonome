"""Every migration says what a restore of an older archive does about it.

A migration that rewrites stored rows rewrites only the rows already here.
An archive written before it brings the old shape back unless the restore
applies the same change on the way in, and nothing used to ask: migration
204 cut card numbers out of merchant names, and a restore planted them
again. So a new migration file fails this test until it is named in
`archive_upgrades.UPGRADES` (with how a restore gets its change) or in
`archive_upgrades.SCHEMA_ONLY` (it changes no restored row), and the
schema-only claim is checked against the file itself.
"""

import re
import unittest

from oikonome.sync import archive_upgrades as up
from oikonome.sync import restore

# a data statement and the table it writes
_WRITES = re.compile(r"\b(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+([a-z_][a-z_0-9]*)",
                     re.I)


def _code(sql: str) -> str:
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


def _writes(name: str) -> set:
    sql = _code((up.MIGRATIONS / name).read_text())
    return {m.group(1).lower() for m in _WRITES.finditer(sql)}


class EveryMigrationIsAccountedForTests(unittest.TestCase):

    def test_every_migration_file_is_in_exactly_one_list(self):
        files = set(up.migration_names())
        upgrades = [u.migration for u in up.UPGRADES]
        self.assertEqual(len(upgrades), len(set(upgrades)),
                         "a migration is listed twice in UPGRADES")
        listed = set(upgrades)
        schema_only = set(up.SCHEMA_ONLY)
        missing = sorted(files - listed - schema_only)
        self.assertEqual(
            missing, [],
            "a migration says nothing about what a restore of an archive "
            "written before it does: add it to archive_upgrades.UPGRADES "
            "(replay / after / inline / structural / gap, with why) or to "
            "SCHEMA_ONLY if it changes no row a restore loads")
        self.assertEqual(sorted(listed & schema_only), [])
        self.assertEqual(sorted((listed | schema_only) - files), [],
                         "a listed migration is no file")

    def test_each_upgrade_says_how_and_why(self):
        for u in up.UPGRADES:
            self.assertIn(u.how, up.HOWS, u.migration)
            self.assertTrue(u.why.strip(), u.migration)
        for name, why in up.SCHEMA_ONLY.items():
            self.assertTrue(why.strip(), name)

    def test_the_upgrades_are_in_the_order_migrate_applies_them(self):
        names = [u.migration for u in up.UPGRADES]
        self.assertEqual(names, sorted(names))

    def test_a_schema_only_migration_writes_no_restored_table(self):
        restored = set(restore.RESTORED_MEMBERS)
        for name in up.SCHEMA_ONLY:
            self.assertEqual(
                sorted(_writes(name) & restored), [],
                f"{name} rewrites rows a restore loads, so an older "
                "archive brings them back in the old shape: it belongs in "
                "UPGRADES, saying what the restore does about it")

    def test_a_migration_that_writes_a_restored_table_is_an_upgrade(self):
        restored = set(restore.RESTORED_MEMBERS)
        listed = {u.migration for u in up.UPGRADES}
        for name in up.migration_names():
            if _writes(name) & restored:
                self.assertIn(name, listed, name)

    def test_no_migration_file_is_run_by_a_restore(self):
        # a restore runs on the household's pooled connection, over text
        # an archive brought: a migration's file there ran PostgreSQL
        # regular expressions over unclamped cells and left temporary
        # functions on the pooled session
        self.assertNotIn("replay", up.HOWS)
        self.assertFalse(hasattr(up, "replay"))
        src = (up.MIGRATIONS.parent.parent / "sync" / "restore.py").read_text()
        self.assertNotIn("pg_temp", src)
        self.assertNotIn("MIGRATIONS", src)

    def test_no_migration_is_left_as_a_gap(self):
        # a gap is an older archive restoring in a shape the migration
        # changed; one is accepted only by name, with why no restore can
        # apply it, and there are none
        gaps = sorted(u.migration for u in up.UPGRADES if u.how == "gap")
        self.assertEqual(gaps, sorted(up.ACCEPTED_GAPS),
                         "a migration's change is not applied to an older "
                         "archive: give it an inline or after step")
        self.assertEqual(up.ACCEPTED_GAPS, {})

    def test_the_cuts_of_card_numbers_are_inline(self):
        inline = {u.migration[:3] for u in up.UPGRADES if u.how == "inline"}
        self.assertTrue({"203", "204", "205", "206"} <= inline, inline)


class ArchiveAgeTests(unittest.TestCase):

    def test_an_archive_without_a_manifest_is_the_oldest(self):
        self.assertTrue(up.older_than(None, up.migration_names()[0]))

    def test_each_floor_column_is_one_its_migration_added(self):
        """An archive without a manifest is dated by these; a column an
        earlier file already had would date an old archive too late."""
        names = up.migration_names()
        for migration, member, column in up.FLOOR_COLUMNS:
            self.assertIn(migration, names)
            table = member.removesuffix(".csv")
            sql = (up.MIGRATIONS / migration).read_text().lower()
            self.assertIn(column, sql, migration)
            self.assertIn(table, sql, migration)
            for earlier in names[:names.index(migration)]:
                text = (up.MIGRATIONS / earlier).read_text().lower()
                self.assertFalse(
                    f"add column if not exists {column} " in text
                    and f"table {table}" in text,
                    f"{earlier} already added {table}.{column}")

    def test_a_name_this_install_does_not_ship_is_placed_by_its_number(self):
        self.assertFalse(up.older_than("999_a_newer_one.sql",
                                       up.migration_names()[-1]))
        self.assertTrue(up.older_than("040_an_add_ons.sql",
                                      "044_llm_rule_canonical_rekey.sql"))
        self.assertTrue(up.older_than("159_x.sql", "159a_y.sql"))

    def test_an_archive_at_the_newest_migration_is_older_than_none(self):
        newest = up.migration_names()[-1]
        self.assertFalse(any(up.older_than(newest, n)
                             for n in up.migration_names()))
