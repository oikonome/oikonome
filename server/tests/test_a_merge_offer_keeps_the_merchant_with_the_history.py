"""A merge offer folds the smaller, newer merchant into the larger, older
one.

Approving an offer renames one side's rows after the other. Ranked by who
backs the name, a merchant the feed minted last week (two rows, an entity
id) outranked the one the household has used for years, and the offer
asked to rename the whole history after the newcomer. Rows decide first,
then age; a name's quality only breaks a tie. A name that is the other
cut short still never survives.
"""
import datetime as dt
import unittest

from oikonome.engine import merchant_merge as mm
from oikonome.engine.compat import jsonb

from .util import add_txn, make_db, write_config


def _cand(name, *, rows, first=None, entity=None, raws=None):
    return {"id": name, "name": name, "rows": rows, "inflow": 0,
            "raws": raws or [name], "samples": [name.upper()],
            "stems": set(), "entity": entity, "parent": False,
            "manual": False, "category": "", "median": 20.0, "mcc": None,
            "city": None, "p2p": False, "labels": {},
            "first": first, "last": first}


class TheSurvivorIsTheMerchantWithTheHistoryTests(unittest.TestCase):
    def test_rows_outrank_an_entity(self):
        big = _cand("Juniper's Market Hall", rows=300,
                    first=dt.date(2024, 1, 5))
        new = _cand("Juniper's", rows=2, first=dt.date(2026, 4, 2),
                    entity="ent-junipers")
        into, frm = mm.survivor(big, new)
        self.assertEqual((into["name"], frm["name"]),
                         ("Juniper's Market Hall", "Juniper's"))
        self.assertEqual(mm.survivor(new, big)[0]["name"],
                         "Juniper's Market Hall")

    def test_equal_rows_keep_the_older_merchant(self):
        old = _cand("Maplehurst", rows=5, first=dt.date(2023, 6, 1))
        new = _cand("Maplehursts", rows=5, first=dt.date(2026, 2, 1),
                    entity="ent-maple")
        self.assertEqual(mm.survivor(new, old)[0]["name"], "Maplehurst")

    def test_equal_rows_and_age_fall_back_to_the_name(self):
        day = dt.date(2025, 1, 1)
        plain = _cand("Maplehurst", rows=5, first=day)
        backed = _cand("Maplehursts", rows=5, first=day, entity="ent-maple")
        self.assertEqual(mm.survivor(plain, backed)[0]["name"], "Maplehursts")

    def test_the_proposer_stores_the_offer_onto_the_larger_side(self):
        props = mm.propose(
            [_cand("Juniper's Market", rows=300, first=dt.date(2024, 1, 5)),
             _cand("Junipers Market", rows=2, first=dt.date(2026, 4, 2),
                   entity="ent-junipers")], [])
        self.assertEqual([(p["from"]["name"], p["into"]["name"])
                          for p in props],
                         [("Junipers Market", "Juniper's Market")])

    def test_a_cut_name_still_never_survives_its_full_name(self):
        props = mm.propose(
            [_cand("Bluebird Orchard Market", rows=4,
                   raws=["Bluebird's Orchard Market"]),
             _cand("Bluebird Orc", rows=90, raws=["Bluebird's Orc"])], [])
        self.assertEqual([(p["from"]["name"], p["into"]["name"])
                          for p in props],
                         [("Bluebird Orc", "Bluebird Orchard Market")])


class AStoredOfferIsTurnedTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _merchant(self, name, n, start, entity=None):
        mid = self.conn.execute(
            "INSERT INTO merchants (name, name_source, plaid_entity_id)"
            " VALUES (%s, %s, %s) RETURNING id",
            (name, "plaid" if entity else "layer1", entity)).fetchone()["id"]
        for d in range(n):
            t = add_txn(self.conn, start + dt.timedelta(days=7 * d), 20.0,
                        name.upper(), merchant=name)
            self.conn.execute(
                "UPDATE transactions SET merchant_id=%s WHERE id=%s", (mid, t))
        return mid

    def test_the_nightly_turns_a_pending_offer_stored_the_other_way(self):
        big = self._merchant("Juniper's Market", 12, dt.date(2025, 1, 6))
        new = self._merchant("Junipers Market", 2, dt.date(2026, 4, 2),
                             entity="ent-junipers")
        pid = mm._sig(str(big), str(new))
        self.conn.execute(
            "INSERT INTO merchant_merge_proposals (id, from_merchant_id,"
            " into_merchant_id, evidence, status) VALUES (%s,%s,%s,%s,"
            " 'pending')",
            (pid, big, new, jsonb({"signals": ["one name"], "supports": []})))
        stats = mm.run(self.conn)
        row = self.conn.execute(
            "SELECT from_merchant_id, into_merchant_id, status"
            "  FROM merchant_merge_proposals WHERE id=%s", (pid,)).fetchone()
        self.assertEqual((row["from_merchant_id"], row["into_merchant_id"],
                          row["status"]), (new, big, "pending"))
        # turning an offer is not a new offer for the alert to announce
        self.assertEqual(stats["inserted"], 0)
        offer = next(o for o in mm.pending(self.conn) if o["id"] == pid)
        self.assertEqual((offer["from"], offer["into"]),
                         ("Junipers Market", "Juniper's Market"))
        # a second run finds nothing to turn
        self.assertEqual(mm.run(self.conn).get("turned", 0), 0)

    def test_a_rejected_pair_is_not_reopened_by_a_change_of_direction(self):
        big = self._merchant("Juniper's Market", 12, dt.date(2025, 1, 6))
        new = self._merchant("Junipers Market", 2, dt.date(2026, 4, 2),
                             entity="ent-junipers")
        pid = mm._sig(str(big), str(new))
        self.conn.execute(
            "INSERT INTO merchant_merge_proposals (id, from_merchant_id,"
            " into_merchant_id, status) VALUES (%s,%s,%s,'rejected')",
            (pid, big, new))
        mm.run(self.conn)
        row = self.conn.execute(
            "SELECT from_merchant_id, status FROM merchant_merge_proposals"
            " WHERE id=%s", (pid,)).fetchone()
        self.assertEqual((row["from_merchant_id"], row["status"]),
                         (big, "rejected"))


if __name__ == "__main__":
    unittest.main()
