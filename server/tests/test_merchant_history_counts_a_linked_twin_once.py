"""A merchant's history counts each charge once.

A card linked through two connections holds every charge twice, once per
source, and a hidden account contributes to nothing. The merchant page's
lifetime total, monthly totals and average follow the same rule as every
other total, and so do the facts a merge offer shows for each side.
"""
import datetime as dt
import unittest

from oikonome.engine import bills, links, merchant_merge

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 15)
MONTHS = (dt.date(2026, 4, 9), dt.date(2026, 5, 9), dt.date(2026, 6, 9))


def _account(conn, aid, item):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name) VALUES"
        " (%s, 'test', %s) ON CONFLICT (tenant_id, id) DO NOTHING",
        (item, item))
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, subtype, mask)"
        " VALUES (%s, %s, 'Rewards Card', 'credit', 'credit card', '0042')",
        (aid, item))


class MerchantHistoryTwinTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.mid = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES"
            " ('Lantern Streaming', 'layer1') RETURNING id::text AS id"
        ).fetchone()["id"]
        for aid, item in (("card-a", "src-a"), ("card-b", "src-b")):
            _account(self.conn, aid, item)
            for d in MONTHS:
                self._charge(aid, d)

    def _charge(self, aid, day):
        tid = add_txn(self.conn, day, 15.49, "LANTERN STREAMING",
                      merchant="Lantern Streaming", account=aid,
                      primary="ENTERTAINMENT")
        self.conn.execute("UPDATE transactions SET merchant_id = %s"
                          " WHERE id = %s", (self.mid, tid))

    def _history(self):
        return bills.merchant_history(self.conn, "Lantern Streaming",
                                      today=TODAY)

    def _assert_once(self, h):
        self.assertEqual(h["lifetime"]["total"], 46.47)
        self.assertEqual(h["lifetime"]["count"], 3)
        self.assertEqual(h["lifetime"]["avg_monthly_active"], 15.49)
        self.assertEqual({m["month"]: (m["total"], m["count"])
                          for m in h["monthly"]},
                         {"2026-04": (15.49, 1), "2026-05": (15.49, 1),
                          "2026-06": (15.49, 1)})

    def test_a_linked_twin_counts_once(self):
        links.create(self.conn, ["card-a", "card-b"])
        h = self._history()
        self._assert_once(h)
        # the list still shows both, as a bulk edit from it would touch both
        self.assertEqual(len(h["txns"]), 6)

    def test_a_hidden_account_counts_for_nothing(self):
        self.conn.execute("UPDATE accounts SET user_removed_at = now()"
                          " WHERE id = 'card-b'")
        self._assert_once(self._history())

    def test_unlinked_accounts_both_count(self):
        h = self._history()
        self.assertEqual(h["lifetime"]["total"], 92.94)

    def test_a_merge_offer_states_each_charge_once(self):
        links.create(self.conn, ["card-a", "card-b"])
        f = merchant_merge.side_facts(self.conn, [self.mid])[self.mid]
        self.assertEqual((f["rows"], f["total"]), (3, 46.47))
        self.assertEqual(len(f["recent"]), 3)


if __name__ == "__main__":
    unittest.main()
