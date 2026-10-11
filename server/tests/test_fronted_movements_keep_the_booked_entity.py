"""Where a charge carries two fronted movements, the upgrade keeps the one
for the business the charge is booked to now, and says what it removed.

Before the one-per-charge rule, a second pick naming another business
moved the charge to it while the first kept its contribution. Keeping the
earliest movement regardless would credit the first business's capital
for a cost the second one's books carry, and delete the movement that
matched without a word.
"""
import unittest
from pathlib import Path

import psycopg

from oikonome.db import tenancy

from .util import TEST_DB, _admin_dsn, add_txn, make_db

MIGRATIONS = Path(__file__).resolve().parents[1] / "oikonome" / "db" / "migrations"
CHOOSE = MIGRATIONS / "159a_fronted_movement_follows_the_booked_entity.sql"
CONVERGE = MIGRATIONS / "160_one_fronted_movement_everywhere.sql"
INDEX = "equity_movement_one_fronted_per_txn"


class KeepTheBookedEntityTests(unittest.TestCase):
    def setUp(self):
        conn = make_db()
        self.tid = str(conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        self.txn = add_txn(conn, "2026-01-01", 75.0, "PRINTER INK",
                           account="card")
        conn.close()
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(self.admin.close)

    def _ent(self, name):
        return self.admin.execute(
            "INSERT INTO business_entity (tenant_id, name, structure)"
            " VALUES (%s, %s, 'single_member_llc') RETURNING id",
            (self.tid, name)).fetchone()["id"]

    def _move(self, ent, kind, at):
        self.admin.execute(
            "INSERT INTO equity_movement (tenant_id, entity_id, kind, amount,"
            " date, txn_id, created_at, note)"
            " VALUES (%s,%s,%s,75,'2026-01-01',%s,%s,NULL)",
            (self.tid, ent, kind, self.txn, at))

    def _left(self):
        return [dict(r) for r in self.admin.execute(
            "SELECT entity_id, kind, note FROM equity_movement"
            " WHERE txn_id = %s AND tenant_id = %s", (self.txn, self.tid))]

    def test_the_booked_entity_wins_over_the_earliest(self):
        try:
            with self.admin.transaction(force_rollback=True):
                self.admin.execute(f"DROP INDEX IF EXISTS {INDEX}")
                self.admin.execute("DELETE FROM equity_movement")
                a, b = self._ent("Alder Works LLC"), self._ent("Birch Lane LLC")
                self._move(a, "contribution", "2026-01-02")
                self._move(b, "reimbursement", "2026-01-05")
                self.admin.execute(
                    "UPDATE transactions SET entity_id = %s"
                    " WHERE id = %s AND tenant_id = %s", (b, self.txn, self.tid))
                self.admin.execute(CHOOSE.read_text())
                left = self._left()
                self.assertEqual([(r["entity_id"], r["kind"]) for r in left],
                                 [(b, "reimbursement")])
                note = left[0]["note"]
                self.assertIn("contribution of 75.00", note)
                self.assertIn("Alder Works LLC", note)
                self.assertIn("2026-01-02", note)
                # the later migration then has nothing to choose
                self.admin.execute(CONVERGE.read_text())
                self.assertEqual(len(self._left()), 1)
                self.admin.execute(CHOOSE.read_text())        # re-runnable
                self.assertEqual(self._left()[0]["note"], note)
        except psycopg.Rollback:
            pass

    def test_neither_matching_keeps_the_earliest_and_says_so(self):
        try:
            with self.admin.transaction(force_rollback=True):
                self.admin.execute(f"DROP INDEX IF EXISTS {INDEX}")
                self.admin.execute("DELETE FROM equity_movement")
                a, b = self._ent("Alder Works LLC"), self._ent("Birch Lane LLC")
                self._move(b, "reimbursement", "2026-01-05")
                self._move(a, "contribution", "2026-01-02")
                self.admin.execute(CHOOSE.read_text())
                left = self._left()
                self.assertEqual([(r["entity_id"], r["kind"]) for r in left],
                                 [(a, "contribution")])
                self.assertIn("reimbursement of 75.00 for Birch Lane LLC",
                              left[0]["note"])
        except psycopg.Rollback:
            pass


if __name__ == "__main__":
    unittest.main()
