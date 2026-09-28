"""Personal-only reads on a connection must see entity writes made earlier
on the SAME connection.

tenant_connect stamps which accounts belong to a business entity, and
whether the household combines business and personal money, once per
checkout. A request (or the demo seed) that assigns an account, deletes an
entity or flips the combined toggle and then reads personal money on the
same connection would otherwise use the stale snapshot: a just-assigned
business account still counts as personal, so its bills are proposed as
household bills and its spending lands in the personal verdict.
"""
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import budget, entities

from .util import TODAY, _admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts


def _personal_accounts(conn) -> set[str]:
    return {r["account_id"] for r in conn.execute(
        "SELECT DISTINCT t.account_id FROM transactions t "
        "WHERE t.removed = 0" + budget.PERSONAL_ONLY_SQL).fetchall()}


class EntityScopeFollowsWritesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"escope-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('bizchk','it1','Biz Checking','depository','checking',100)")
        add_txn(self.conn, TODAY, 40.0, "PERSONAL LUNCH", account="card")
        add_txn(self.conn, TODAY, 90.0, "HOSTING CO", account="bizchk")
        self.ent = entities.create_entity(
            self.conn, name="Acme LLC", structure="sole_prop")

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def test_assigned_account_leaves_personal_on_the_same_connection(self):
        self.assertIn("bizchk", _personal_accounts(self.conn))
        entities.assign_account(self.conn, "bizchk", self.ent["id"])
        self.assertEqual(_personal_accounts(self.conn), {"card"})

    def _checked_out_while_assigned(self):
        """A connection whose checkout snapshot already holds the
        assignment, so returning the account to personal is what has to
        refresh it."""
        entities.assign_account(self.conn, "bizchk", self.ent["id"])
        self.conn.close()
        self.conn = tenancy.tenant_connect(self.tid)
        self.assertNotIn("bizchk", _personal_accounts(self.conn))

    def test_unassigned_account_returns_to_personal_on_the_same_connection(self):
        self._checked_out_while_assigned()
        entities.assign_account(self.conn, "bizchk", None)
        self.assertIn("bizchk", _personal_accounts(self.conn))

    def test_deleted_entity_returns_its_account_to_personal(self):
        self._checked_out_while_assigned()
        entities.delete_forever(self.conn, self.ent["id"], "Acme LLC")
        self.assertIn("bizchk", _personal_accounts(self.conn))

    def test_combined_toggle_applies_on_the_same_connection(self):
        entities.assign_account(self.conn, "bizchk", self.ent["id"])
        entities.set_combined(self.conn, True)
        self.assertIn("bizchk", _personal_accounts(self.conn))
        entities.set_combined(self.conn, False)
        self.assertNotIn("bizchk", _personal_accounts(self.conn))


if __name__ == "__main__":
    unittest.main()
