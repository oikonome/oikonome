"""Deleting a business entity rewrites accounts.entity_id and
transactions.entity_id by foreign-key action, which no statement names, so
the per-request memos over those tables must be forgotten all the same."""
import unittest

from oikonome.engine import savings
from tests.util import make_db


class DeletingAnEntityForgetsTheMemosOverItsRows(unittest.TestCase):
    def test_a_goals_owner_follows_the_entity_delete(self):
        conn = make_db()
        try:
            ent = conn.execute(
                "INSERT INTO business_entity (name, structure) "
                "VALUES ('Side LLC', 'single_member_llc') RETURNING id"
            ).fetchone()["id"]
            conn.execute("UPDATE accounts SET entity_id=%s WHERE id='chk'",
                         (ent,))
            goal = {"name": "Trip", "account_id": "chk", "tokens": []}
            self.assertIn(ent, savings._owner_scope(conn, goal)[1])
            conn.execute("DELETE FROM business_entity WHERE id=%s", (ent,))
            self.assertIsNone(conn.execute(
                "SELECT entity_id FROM accounts WHERE id='chk'"
            ).fetchone()["entity_id"])
            self.assertNotIn(ent, savings._owner_scope(conn, goal)[1] or [],
                             "the memo outlived the row it was read from")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
