"""The database holds one fronted movement per transaction.

A personally-paid business cost is either capitalized (a contribution) or
marked reimbursable, for one entity, never both. The engine refuses a
second pick under a row lock; the migration adds a unique index so the
database refuses one too. Rows written before the rule may already break
it, and which of two movements is the real one is the owner's decision,
so the migration builds the index only when the data allows it, and
otherwise leaves the data untouched rather than failing the upgrade.
"""

import unittest
from pathlib import Path

import psycopg

from oikonome.db import tenancy

from .util import _admin_dsn, TEST_DB, add_txn, make_db

MIGRATION = (Path(__file__).resolve().parents[1] / "oikonome" / "db"
             / "migrations" / "159_one_fronted_movement_per_transaction.sql")
INDEX = "equity_movement_one_fronted_per_txn"


class OneFrontedMovementIndexTests(unittest.TestCase):

    def setUp(self):
        # a tenant (make_db also brings the test database up to date)
        conn = make_db()
        self.tid = str(conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        self.txn = add_txn(conn, "2026-01-01", 10.0, "OFFICE SUPPLIES",
                           account="card")
        conn.close()
        self.sql = MIGRATION.read_text()
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(self.admin.close)

    def _index_exists(self):
        return self.admin.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname=%s",
            (INDEX,)).fetchone() is not None

    def _movement(self, ent, kind, txn):
        self.admin.execute(
            "INSERT INTO equity_movement (tenant_id, entity_id, kind, amount,"
            " date, txn_id) VALUES (%s,%s,%s,10,'2026-01-01',%s)",
            (self.tid, ent, kind, txn))

    def test_built_when_clean_skipped_when_not(self):
        # everything below is rolled back: the test only borrows the table
        try:
            with self.admin.transaction(force_rollback=True):
                self.admin.execute(f"DROP INDEX IF EXISTS {INDEX}")
                self.admin.execute("DELETE FROM equity_movement")
                ent = self.admin.execute(
                    "INSERT INTO business_entity (tenant_id, name, structure)"
                    " VALUES (%s, 'Example LLC', 'single_member_llc')"
                    " RETURNING id",
                    (self.tid,)).fetchone()["id"]
                txn = self.txn
                self._movement(ent, "contribution", txn)
                self._movement(ent, "reimbursement", txn)

                self.admin.execute(self.sql)
                self.assertFalse(self._index_exists(),
                                 "built over rows that break it")
                self.assertEqual(self.admin.execute(
                    "SELECT count(*) AS n FROM equity_movement"
                ).fetchone()["n"], 2, "the migration deleted a movement")

                self.admin.execute(
                    "DELETE FROM equity_movement WHERE kind='reimbursement'")
                self.admin.execute(self.sql)
                self.assertTrue(self._index_exists())
                with self.assertRaises(psycopg.errors.UniqueViolation):
                    with self.admin.transaction():
                        self._movement(ent, "reimbursement", txn)
                # draws and unlinked movements stay unconstrained
                self._movement(ent, "draw", txn)
                self._movement(ent, "contribution", None)
                self._movement(ent, "contribution", None)
        except psycopg.Rollback:
            pass


if __name__ == "__main__":
    unittest.main()
