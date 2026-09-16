"""After a restore, no two members of a link group share a home_rank.

The failover read picks the lowest healthy rank; on a tie it is whichever
row the planner returns first, so the household's chosen primary could
swap between reads. An archive can carry the tie (a link edited on one
side, two archives merged), and the write side never checked.
"""

import unittest
import uuid

from oikonome.sync import restore

from .util import make_db, write_config


class HomeRankTests(unittest.TestCase):
    def test_ties_are_renumbered_in_order(self):
        conn = make_db()
        try:
            write_config(conn)
            g1, g2 = uuid.uuid4(), uuid.uuid4()
            conn.execute(
                "INSERT INTO items (id, aggregator) VALUES ('it', 'manual') "
                "ON CONFLICT (tenant_id, id) DO NOTHING")
            for aid in ("a1", "a2", "a3", "b1", "b2"):
                conn.execute(
                    "INSERT INTO accounts (id, item_id, name, type) "
                    "VALUES (%s, 'it', %s, 'depository') "
                    "ON CONFLICT (tenant_id, id) DO NOTHING", (aid, aid))
            for gid, aid, rank in ((g1, "a1", 0), (g1, "a2", 0),
                                   (g1, "a3", 5), (g2, "b1", 1),
                                   (g2, "b2", 1)):
                conn.execute(
                    "INSERT INTO account_links (group_id, account_id, "
                    "home_rank) VALUES (%s, %s, %s)", (gid, aid, rank))
            changed = restore._renumber_home_ranks(conn, [str(g1), str(g2)])
            self.assertGreater(changed, 0)
            rows = {r["account_id"]: r["home_rank"] for r in conn.execute(
                "SELECT account_id, home_rank FROM account_links")}
            self.assertEqual(rows["a1"], 0)
            self.assertEqual(rows["a2"], 1)
            self.assertEqual(rows["a3"], 2)        # order kept, gap closed
            self.assertEqual({rows["b1"], rows["b2"]}, {0, 1})
            # a second pass is a no-op; an untouched group is left alone
            self.assertEqual(restore._renumber_home_ranks(conn,
                                                          [str(g1), str(g2)]), 0)
            g3 = uuid.uuid4()
            conn.execute("INSERT INTO accounts (id, item_id, name, type) "
                         "VALUES ('c1','it','c1','depository') "
                         "ON CONFLICT (tenant_id, id) DO NOTHING")
            conn.execute("INSERT INTO accounts (id, item_id, name, type) "
                         "VALUES ('c2','it','c2','depository') "
                         "ON CONFLICT (tenant_id, id) DO NOTHING")
            for aid in ("c1", "c2"):
                conn.execute("INSERT INTO account_links (group_id, account_id, "
                             "home_rank) VALUES (%s, %s, 7)", (g3, aid))
            self.assertEqual(restore._renumber_home_ranks(conn, [str(g1)]), 0)
            self.assertEqual(restore._renumber_home_ranks(conn, [str(g3)]), 2)
        finally:
            conn.close()
