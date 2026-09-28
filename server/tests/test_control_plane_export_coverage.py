"""Every tenant-scoped control-plane table must be a conscious decision for
both export doors.

A table with `tenant_id` and row-level security is DISCOVERED — both the
portable archive and the user-facing ZIP ask Postgres which tables have RLS
on, so a new domain table exports by default and forgetting one is not
possible. A table with `tenant_id` and NO row-level security gets the
opposite treatment on purpose: an unscoped SELECT there would return every
household's rows, so discovery refuses it and the doors name it by hand.

Hand-maintained lists drift. A table like `recipient_invites` — the
addresses a household shares its financial mail with, who invited them, and
the consent stamp that decides whether the mail is sent at all — can be
added long after both lists were written and join neither, so a portability
request returns an archive missing those people entirely and a
self-host→hosted migration carries the recipient list across without the
acceptances behind it.

So the lists stop being trusted: the SCHEMA is the source of truth here, and
this test fails the moment a control-plane table exists that neither door
has classified — exported, or excluded for a stated reason. Same doctrine as
`test_tenant_table_coverage`, one level down.
"""
import unittest

from oikonome import tenant_export
from oikonome.db import tenancy
from oikonome.sync import export

from .util import _ensure_db


def _classified_for_archive() -> set[str]:
    return (set(tenant_export.ARCHIVE_HAND_BUILT)
            | set(tenant_export.ARCHIVE_CONTROL_TABLES)
            | set(tenant_export.ARCHIVE_CONTROL_SKIP))


def _classified_for_zip() -> set[str]:
    return set(export.CONTROL_TABLES) | set(export.CONTROL_SKIP)


class ControlPlaneExportCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect()
        self.addCleanup(self.admin.close)
        self.live = set(tenant_export.control_tenant_tables(self.admin))

    def test_the_portable_archive_classifies_every_control_plane_table(self):
        missing = self.live - _classified_for_archive()
        self.assertEqual(
            missing, set(),
            f"control-plane table(s) {sorted(missing)} carry a tenant_id "
            f"with no RLS, so nothing can discover them. Decide whether a "
            f"data-portability archive must cover them: add to "
            f"tenant_export.ARCHIVE_CONTROL_TABLES, or to "
            f"ARCHIVE_CONTROL_SKIP with the reason it stays out.")

    def test_the_full_export_classifies_every_control_plane_table(self):
        missing = self.live - _classified_for_zip()
        self.assertEqual(
            missing, set(),
            f"control-plane table(s) {sorted(missing)} are in neither "
            f"sync.export.CONTROL_TABLES nor CONTROL_SKIP. Decide whether "
            f"the household's own ZIP should carry them — and if it does, "
            f"whether the restore reads them back.")

    def test_the_lists_do_not_outlive_the_tables_they_name(self):
        for label, named in (("portable archive", _classified_for_archive()),
                             ("full export", _classified_for_zip())):
            stale = named - self.live
            self.assertEqual(stale, set(),
                             f"{label} names table(s) that are no longer in "
                             f"the schema: {sorted(stale)}")

    def test_every_exported_control_table_is_readable_as_named(self):
        """A name in the list has to be a real table with a tenant_id — a
        typo would otherwise sit there looking like coverage."""
        for t in (set(tenant_export.ARCHIVE_CONTROL_TABLES)
                  | set(export.CONTROL_TABLES)):
            self.assertIn(t, self.live, t)

    def test_a_new_control_plane_table_is_caught_before_it_can_drift(self):
        """The guard's own mechanism: a table added with a tenant_id and no
        row-level security shows up as unclassified in BOTH doors, the day
        it lands."""
        class _Rollback(Exception):
            pass

        try:
            with self.admin.transaction():
                self.admin.execute(
                    "CREATE TABLE _control_probe (tenant_id uuid, v text)")
                live = set(tenant_export.control_tenant_tables(self.admin))
                self.assertIn("_control_probe", live)
                self.assertNotIn(
                    "_control_probe",
                    set(tenant_export.rls_tenant_tables(self.admin)),
                    "a table with no RLS must never join the discovered set")
                self.assertIn("_control_probe", live - _classified_for_archive())
                self.assertIn("_control_probe", live - _classified_for_zip())
                raise _Rollback              # the schema stays untouched
        except _Rollback:
            pass


if __name__ == "__main__":
    unittest.main()
