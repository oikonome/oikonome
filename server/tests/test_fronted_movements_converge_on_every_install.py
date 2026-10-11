"""Every install ends with one fronted movement per transaction and the
index that holds it there.

The earlier migration built the index only where the data already obeyed
the rule, so an upgraded install could stay without it while a fresh one
had it. The later one keeps the earliest movement where a transaction has
two (the engine's own rule: the first pick stands) and then builds the
index everywhere.
"""
import unittest
from pathlib import Path

import psycopg

from oikonome.db import tenancy

from .util import TEST_DB, _admin_dsn, add_txn, make_db

MIGRATION = (Path(__file__).resolve().parents[1] / "oikonome" / "db"
             / "migrations" / "160_one_fronted_movement_everywhere.sql")
INDEX = "equity_movement_one_fronted_per_txn"


class ConvergeTests(unittest.TestCase):
    def setUp(self):
        conn = make_db()
        self.tid = str(conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        self.txn = add_txn(conn, "2026-01-01", 10.0, "OFFICE SUPPLIES",
                           account="card")
        conn.close()
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(self.admin.close)

    def test_clashes_keep_the_earliest_then_the_index_is_built(self):
        try:
            with self.admin.transaction(force_rollback=True):
                self.admin.execute(f"DROP INDEX IF EXISTS {INDEX}")
                self.admin.execute("DELETE FROM equity_movement")
                ent = self.admin.execute(
                    "INSERT INTO business_entity (tenant_id, name, structure)"
                    " VALUES (%s, 'Example LLC', 'single_member_llc')"
                    " RETURNING id", (self.tid,)).fetchone()["id"]
                for kind, at in (("reimbursement", "2026-01-03"),
                                 ("contribution", "2026-01-02"),
                                 ("draw", "2026-01-01")):
                    self.admin.execute(
                        "INSERT INTO equity_movement (tenant_id, entity_id,"
                        " kind, amount, date, txn_id, created_at)"
                        " VALUES (%s,%s,%s,10,'2026-01-01',%s,%s)",
                        (self.tid, ent, kind, self.txn, at))
                self.admin.execute(MIGRATION.read_text())
                kinds = sorted(r["kind"] for r in self.admin.execute(
                    "SELECT kind FROM equity_movement WHERE txn_id = %s",
                    (self.txn,)))
                self.assertEqual(kinds, ["contribution", "draw"])
                self.assertIsNotNone(self.admin.execute(
                    "SELECT 1 FROM pg_indexes WHERE indexname = %s",
                    (INDEX,)).fetchone())
                # re-runnable
                self.admin.execute(MIGRATION.read_text())
        except psycopg.Rollback:
            pass


if __name__ == "__main__":
    unittest.main()
