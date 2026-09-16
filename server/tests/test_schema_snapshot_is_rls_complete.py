"""schema.sql must stay a complete, RLS-correct snapshot on its own.

A fresh install runs schema.sql and then every numbered migration, so a
table that exists only in a migration still gets created. But schema.sql is
what a reader treats as "the schema", and the next table copied into it
from a migration without also joining the RLS block would carry tenant_id
with no policy — a cross-tenant leak nothing else would catch until the
inventory test ran against a database that had ALSO run the migration.
So: apply schema.sql ALONE to an empty database and require that every
tenant table it defines has row-level security, then apply the migrations
on top and require that they still succeed (idempotent CREATEs).
"""

import unittest

import psycopg

from oikonome.db import migrate

from .util import TEST_DB, _admin_dsn

SNAP_DB = f"{TEST_DB}_schema_snapshot"


class SchemaSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {SNAP_DB} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {SNAP_DB}")
        with psycopg.connect(_admin_dsn(SNAP_DB), autocommit=True) as c:
            c.execute(migrate.APP_ROLE_SQL.format(pw="apppass"))
            c.execute((migrate.HERE / "schema.sql").read_text())

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {SNAP_DB} WITH (FORCE)")

    def _tenant_tables_without_rls(self):
        with psycopg.connect(_admin_dsn(SNAP_DB)) as c:
            rows = c.execute(
                """SELECT t.tablename
                     FROM pg_tables t
                     JOIN pg_class c ON c.relname = t.tablename
                    WHERE t.schemaname = 'public' AND NOT c.relrowsecurity
                      AND EXISTS (SELECT 1 FROM information_schema.columns col
                                   WHERE col.table_schema = 'public'
                                     AND col.table_name = t.tablename
                                     AND col.column_name = 'tenant_id')
                    ORDER BY 1""").fetchall()
        return {r[0] for r in rows}

    def test_schema_sql_alone_gives_merchant_renames_rls(self):
        with psycopg.connect(_admin_dsn(SNAP_DB)) as c:
            row = c.execute(
                "SELECT relrowsecurity FROM pg_class "
                "WHERE relname = 'merchant_renames'").fetchone()
        self.assertIsNotNone(row, "schema.sql does not define merchant_renames")
        self.assertTrue(row[0], "merchant_renames in schema.sql has no RLS")

    def test_every_tenant_table_in_schema_sql_has_rls(self):
        # the same control-plane exemptions the live inventory test accepts
        from .test_inventory_rls import RLS_EXEMPT
        missing = self._tenant_tables_without_rls() - set(RLS_EXEMPT)
        self.assertEqual(set(), missing,
                         f"schema.sql defines tenant table(s) outside its "
                         f"RLS block: {sorted(missing)}")

    def test_migrations_apply_cleanly_on_top_of_the_snapshot(self):
        migrate.run(_admin_dsn(SNAP_DB))
        with psycopg.connect(_admin_dsn(SNAP_DB)) as c:
            n = c.execute("SELECT count(*) FROM schema_migrations").fetchone()
        self.assertGreater(n[0], 90)


if __name__ == "__main__":
    unittest.main()
