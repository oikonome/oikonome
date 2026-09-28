"""Building the transaction-search indexes must not lock the ledger.

The search migration adds a generated column and three GIN indexes. Built
the ordinary way, in one transaction, those three builds hold ACCESS
EXCLUSIVE on `transactions` for as long as all three take — on a household
with a large ledger, upgrading across the migration is an outage rather
than a pause. So the file carries the `-- oikonome: no-transaction` marker
and builds CONCURRENTLY (SHARE UPDATE EXCLUSIVE), which is legal only
outside a transaction block.

Pinned here: the marker is honoured, the statement split survives dollar
quotes and comments, `skip-unless` still gates the indexes on pg_trgm (a
locked-down Postgres must lose speed, never results), an interrupted
build's INVALID leftover is rebuilt instead of adopted, and applying the
migration to a table that already HAS rows ends with three valid indexes
and nothing left to do.
"""

import unittest
import uuid

import psycopg

from oikonome.db import migrate

from .util import TEST_DB, _admin_dsn

MIG = "111_transaction_search.sql"
INDEXES = ("transactions_search_trgm", "merchant_canonical_canon_trgm",
           "merchants_name_trgm")
NOTX_DB = f"{TEST_DB}_notx"


def _sql() -> str:
    return (migrate.HERE / "migrations" / MIG).read_text()


class MigrationTextTests(unittest.TestCase):
    """No database needed: the file and the splitter."""

    def test_search_migration_is_marked_no_transaction(self):
        self.assertTrue(_sql().lstrip().startswith(migrate.NO_TRANSACTION),
                        "the concurrent index builds are illegal inside a "
                        "transaction — the marker is what keeps them out of one")

    def test_every_index_build_is_concurrent_and_gated_on_pg_trgm(self):
        builds = [s for s in migrate._sql_statements(_sql())
                  if "CREATE INDEX" in s]
        self.assertEqual(len(INDEXES), len(builds))
        for stmt in builds:
            self.assertIn("CONCURRENTLY", stmt)
            self.assertIsNotNone(
                migrate.SKIP_UNLESS.search(stmt),
                "an index build with no pg_trgm guard fails the whole "
                f"migration on a Postgres without the extension:\n{stmt}")

    def test_statement_split_respects_dollar_quotes_and_literals(self):
        sql = """
        -- a leading comment belongs to the statement it introduces
        ALTER TABLE t ADD COLUMN c TEXT;
        DO $$ BEGIN RAISE NOTICE 'one; two'; END $$;
        -- oikonome: skip-unless SELECT 1
        CREATE INDEX CONCURRENTLY i ON t (c);
        -- a trailing comment is not a statement
        """
        stmts = migrate._sql_statements(sql)
        self.assertEqual(3, len(stmts), stmts)
        self.assertIn("ALTER TABLE", stmts[0])
        self.assertIn("RAISE NOTICE", stmts[1])       # the ';' inside $$ held
        self.assertIn("CREATE INDEX", stmts[2])
        self.assertEqual("SELECT 1",
                         migrate.SKIP_UNLESS.search(stmts[2]).group(1).strip())

    def test_ordinary_migrations_are_untouched_by_the_marker(self):
        # only the one file opts in; everything else still goes to the
        # server whole, inside its transaction
        marked = [f.name for f in (migrate.HERE / "migrations").glob("*.sql")
                  if f.read_text().lstrip().startswith(migrate.NO_TRANSACTION)]
        self.assertEqual([MIG], marked)


class ConcurrentIndexBuildTests(unittest.TestCase):
    """The real thing, on its own database so no other suite sees the
    ledger without its search column."""

    @classmethod
    def setUpClass(cls):
        cls.dsn = _admin_dsn(NOTX_DB)
        with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {NOTX_DB} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {NOTX_DB}")
        migrate.run(cls.dsn)

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {NOTX_DB} WITH (FORCE)")

    def _conn(self):
        return psycopg.connect(self.dsn, autocommit=True)

    def _valid(self, conn, name):
        row = conn.execute(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = %s", (name,)).fetchone()
        return None if row is None else row[0]

    def _unapply(self, conn):
        """Put the database back where an instance that never ran the
        migration stands: no column, no indexes, nothing recorded."""
        for name in INDEXES:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute("ALTER TABLE transactions DROP COLUMN IF EXISTS search_text")
        conn.execute("DELETE FROM schema_migrations WHERE name = %s", (MIG,))

    def _fill_ledger(self, conn, rows=400):
        tid = uuid.uuid4()
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)",
                     (tid, "notx"))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO transactions (tenant_id, id, date, amount, name, "
                "merchant_name) VALUES (%s, %s, %s, %s, %s, %s)",
                [(tid, f"t{i}", "2026-01-01", 1.0 + i, f"row {i}", "Bigbox")
                 for i in range(rows)])
        return tid

    def test_applying_to_a_populated_ledger_leaves_valid_indexes(self):
        with self._conn() as conn:
            self._unapply(conn)
            self._fill_ledger(conn)
            applied = migrate.run(self.dsn)
            self.assertIn(MIG, applied)
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'transactions' "
                    "AND column_name = 'search_text'").fetchone())
            # the search column is populated for rows that were already there
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM transactions WHERE search_text LIKE %s LIMIT 1",
                ("%bigbox%",)).fetchone())
            for name in INDEXES:
                self.assertTrue(self._valid(conn, name),
                                f"{name} is missing or INVALID after migrate")

    def test_an_instance_that_already_ran_it_has_nothing_to_do(self):
        # the whole safety of rewriting a shipped migration: the recorded
        # id is the filename, so a migrated instance re-runs nothing
        self.assertNotIn(MIG, migrate.run(self.dsn))
        self.assertNotIn(MIG, migrate.run(self.dsn))

    def test_an_interrupted_build_is_rebuilt_not_adopted(self):
        # a cancelled upgrade leaves an INVALID index; IF NOT EXISTS would
        # otherwise keep it forever — a name that satisfies the migration
        # while no query can use it
        with self._conn() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE name = %s", (MIG,))
            try:
                conn.execute("UPDATE pg_index SET indisvalid = false "
                             "WHERE indexrelid = 'transactions_search_trgm'"
                             "::regclass")
            except psycopg.Error as exc:          # not superuser here
                self.skipTest(f"cannot forge an invalid index: {exc}")
            self.assertFalse(self._valid(conn, "transactions_search_trgm"))
            self.assertIn(MIG, migrate.run(self.dsn))
            self.assertTrue(self._valid(conn, "transactions_search_trgm"))

    def test_skip_unless_runs_a_statement_only_when_its_query_matches(self):
        with self._conn() as conn:
            migrate._apply_no_transaction(conn, """
                -- oikonome: skip-unless SELECT 1 WHERE false
                CREATE TABLE notx_skipped (a int);
                -- oikonome: skip-unless SELECT 1
                CREATE TABLE notx_ran (a int);
            """)
            names = {r[0] for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE 'notx_%'"
            ).fetchall()}
            conn.execute("DROP TABLE IF EXISTS notx_ran")
            self.assertEqual({"notx_ran"}, names)

    def test_the_guard_does_not_swallow_a_real_error(self):
        # skip-unless is deliberately narrower than a try/except: a broken
        # statement must still fail the migration loudly
        with self._conn() as conn, self.assertRaises(psycopg.Error):
            migrate._apply_no_transaction(
                conn, "-- oikonome: skip-unless SELECT 1\n"
                      "CREATE INDEX CONCURRENTLY ON no_such_table (nope);")

    def test_concurrent_build_would_fail_inside_a_transaction(self):
        # why the marker exists at all: applying the whole file in one
        # transaction cannot run this migration
        with self._conn() as conn:
            self._unapply(conn)
            try:
                with self.assertRaises(psycopg.errors.ActiveSqlTransaction), \
                        conn.transaction():
                    conn.execute(_sql())
            finally:
                migrate.run(self.dsn)


if __name__ == "__main__":
    unittest.main()
