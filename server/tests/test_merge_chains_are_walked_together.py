"""Many merge chains are walked in one batch, with the per-id walk's answers.

A pass that asks for the survivors of a bank line's merchants used to walk
each id's merged-into chain on its own: one query per hop per id. The batch
walk must give exactly the answer the single walk gives for every id — the
end of a chain, the stopping point of a cycle (which depends on where the
walk started), None for an id that names no merchant — while its query
count grows with the longest chain, not with the number of ids.
"""

import unittest
import uuid

from oikonome.engine import merchant_identity

from .util import make_db


class _Counting:
    """The connection, counting the statements sent through it."""

    def __init__(self, conn):
        self._conn = conn
        self.queries = 0

    def execute(self, *a, **kw):
        self.queries += 1
        return self._conn.execute(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class MergeChainsWalkedTogether(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.ids = {}
        for n in ("A", "B", "C", "D", "P", "Q", "R", "S", "T", "Lone"):
            self.ids[n] = self.conn.execute(
                "INSERT INTO merchants (name) VALUES (%s) RETURNING id",
                (n,)).fetchone()["id"]
        # A -> B -> C -> D, a three-hop chain
        self._point("A", "B")
        self._point("B", "C")
        self._point("C", "D")
        # P -> Q -> R -> P, a cycle; S -> P walks into it
        self._point("P", "Q")
        self._point("Q", "R")
        self._point("R", "P")
        self._point("S", "P")
        # T points at a merchant that is not there
        self.conn.execute("UPDATE merchants SET merged_into=%s WHERE id=%s",
                          (uuid.uuid4(), self.ids["T"]))

    def _point(self, a, b):
        self.conn.execute("UPDATE merchants SET merged_into=%s WHERE id=%s",
                          (self.ids[b], self.ids[a]))

    def _asked(self):
        return list(self.ids.values()) + [uuid.uuid4(), None]

    def test_every_id_gets_the_single_walks_answer(self):
        asked = self._asked()
        want = {m: merchant_identity._survivor(self.conn, m) for m in asked}
        self.assertEqual(merchant_identity._survivors(self.conn, asked), want)
        # the shapes the single walk is known for, spelled out
        self.assertEqual(want[self.ids["A"]], self.ids["D"])
        self.assertEqual(want[self.ids["Lone"]], self.ids["Lone"])
        self.assertIsNone(want[self.ids["T"]])
        self.assertEqual(want[self.ids["P"]], self.ids["R"])
        self.assertEqual(want[self.ids["Q"]], self.ids["P"])

    def test_a_chain_longer_than_the_hop_bound_stops_where_the_single_walk_stops(self):
        names = [f"Link {i}" for i in range(merchant_identity._MERGE_HOPS + 4)]
        ids = [self.conn.execute(
            "INSERT INTO merchants (name) VALUES (%s) RETURNING id",
            (n,)).fetchone()["id"] for n in names]
        for a, b in zip(ids, ids[1:]):
            self.conn.execute("UPDATE merchants SET merged_into=%s WHERE id=%s",
                              (b, a))
        want = {m: merchant_identity._survivor(self.conn, m) for m in ids}
        self.assertEqual(merchant_identity._survivors(self.conn, ids), want)

    def test_queries_follow_the_longest_chain_not_the_number_of_ids(self):
        counted = _Counting(self.conn)
        merchant_identity._survivors(counted, self._asked())
        # the longest walk here is A -> B -> C -> D: four reads at most,
        # where the single walk spends one read per hop per id
        self.assertLessEqual(counted.queries, 4)

    def test_nothing_asked_reads_nothing(self):
        counted = _Counting(self.conn)
        self.assertEqual(merchant_identity._survivors(counted, []), {})
        self.assertEqual(counted.queries, 0)


if __name__ == "__main__":
    unittest.main()
