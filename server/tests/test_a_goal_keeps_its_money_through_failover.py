"""A savings goal keeps the money it was given when its account fails over.

The goal's account can be one source of a linked (dual-sourced) savings
account. When that source fails over and becomes the group's shadow, every
aggregate drops its rows, so the goal reads the group's effective primary
instead of the pinned id, or it would see nothing while the twin carries
every contribution.
"""
import datetime as dt
import unittest

from oikonome.engine import links, savings

from .util import TODAY, add_txn, make_db, write_config


def _months_ago(n):
    d = TODAY.replace(day=10)
    for _ in range(n):
        d = (d.replace(day=1) - dt.timedelta(days=1)).replace(day=10)
    return d


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.conn = c = make_db()
        c.execute("INSERT INTO items (id, aggregator, institution_name, "
                  "access_token, status) VALUES "
                  "('it-a','test','First Source','tok-a','ok'), "
                  "('it-b','test','Second Source','tok-b','ok')")
        c.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                  "balance_current) VALUES "
                  "('sav-a','it-a','Vacation Savings','depository','savings',0),"
                  "('sav-b','it-b','Vacation Savings','depository','savings',0)")
        links.create(c, ["sav-a", "sav-b"])
        for acc, prefix in (("sav-a", "a"), ("sav-b", "b")):
            for n in (1, 2):
                add_txn(c, _months_ago(n), -300.0, "TRANSFER FROM CHECKING",
                        account=acc, primary="TRANSFER_IN",
                        txn_id=f"{prefix}-in-{n}")
        self.goal = {"name": "Vacation", "account_id": "sav-a",
                     "target": 3000.0}
        write_config(c, savings_goals=[self.goal])
        self.cfg = {"savings_goals": [self.goal]}

    def tearDown(self):
        self.conn.close()

    def _saved(self):
        return savings.progress(self.conn, self.cfg, TODAY)[0]["saved"]

    def test_saved_does_not_change_when_the_pinned_source_fails_over(self):
        self.assertEqual(self._saved(), 600.0)
        self.conn.execute("UPDATE items SET status = 'error' "
                          "WHERE id = 'it-a'")
        links.set_shadow_scope(self.conn)
        self.assertIn("sav-a", links.shadow_ids(self.conn))
        self.assertEqual(links.effective_account(self.conn, "sav-a"), "sav-b")
        self.assertEqual(self._saved(), 600.0)

    def test_a_goal_pinned_to_the_lower_ranked_twin_reads_the_primary(self):
        goal = {**self.goal, "account_id": "sav-b"}
        cfg = {"savings_goals": [goal]}
        self.assertEqual(
            savings.progress(self.conn, cfg, TODAY)[0]["saved"], 600.0)

    def test_an_unlinked_account_resolves_to_itself(self):
        self.assertEqual(links.effective_account(self.conn, "chk"), "chk")
        self.assertIsNone(links.effective_account(self.conn, None))


if __name__ == "__main__":
    unittest.main()
