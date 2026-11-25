""""Merge all" must not join two different shops. It covers only the offers
where one name is the other cut short by a terminal (or one name spelled
differently); two names that merely share a word ("Ember" and "Ember
Restaurant", "Fig Bank" and "Fig Kitchen") are offered one at a time with a
caution.

And a merge keeps the longer, full name — never the cut-off one, even when the
cut spelling is the one the aggregator backs with an entity, and even for an
offer stored the other way round before this rule."""
import datetime as dt
import unittest

from oikonome.engine import merchant_merge as mm

from .util import TODAY, add_txn, make_db, write_config


def _cand(name, *, rows=6, entity=None, category=""):
    return {"id": name, "name": name, "rows": rows, "inflow": 0,
            "raws": [name], "samples": [name.upper()], "stems": set(),
            "entity": entity, "parent": False, "manual": False,
            "category": category, "median": 20.0, "mcc": None, "city": None,
            "p2p": False, "labels": {}}


FULL = "Port d'Orvane Toyshop"
CUT = "Port Orvane Toysho"


class WhichNamesAreCutShort(unittest.TestCase):
    def test_a_name_cut_mid_word_is_a_cut(self):
        self.assertTrue(mm.cut_of(CUT, FULL))
        self.assertTrue(mm.cut_of("Bluebird Orc", "Bluebird Orchard Market"))

    def test_a_long_name_cut_after_whole_words_is_a_cut(self):
        self.assertTrue(mm.cut_of("Riverton Family Health",
                                  "Riverton Family Health Care"))

    def test_sharing_a_word_is_not_a_cut(self):
        self.assertFalse(mm.cut_of("Ember", "Ember Restaurant"))
        self.assertFalse(mm.cut_of("Fig Bank", "Fig Kitchen"))
        self.assertFalse(mm.cut_of("Quill", "The Quill Cellars"))
        self.assertFalse(mm.cut_of("Tidal", "Tidal Market"))

    def test_a_name_is_not_a_cut_of_itself_or_of_a_shorter_one(self):
        self.assertFalse(mm.cut_of("Bluebird Orchard", "Bluebird Orchard"))
        self.assertFalse(mm.cut_of("Bluebird Orchard Market", "Bluebird Orc"))

    def test_only_cut_off_and_same_name_offers_go_together(self):
        self.assertEqual(mm.offer_shape("Bluebird Orc", "Bluebird Orchard Market"),
                         {"bulk": True, "caution": None, "turn": False})
        self.assertTrue(mm.offer_shape("Maplehursts", "Maplehurst")["bulk"])
        shared = mm.offer_shape("Ember", "Ember Restaurant")
        self.assertFalse(shared["bulk"])
        self.assertEqual(shared["caution"], mm.SHARE_A_WORD)
        # the offer that keeps the cut name is turned around
        self.assertTrue(mm.offer_shape(FULL, CUT)["turn"])


class TheFullNameSurvives(unittest.TestCase):
    def test_the_cut_spelling_never_survives_even_with_an_entity(self):
        full, cut = "Lantern Hollow Bakeshop", "Lantern Hollow Bakesh"
        c = [_cand(full, rows=6), _cand(cut, rows=1, entity="ent_1")]
        props = mm.propose(c, [])
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["into"]["name"], full)
        self.assertEqual(props[0]["from"]["name"], cut)


class StoredOffers(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.ids = {}
        for name, n in (("Ashbury Toyworks Market", 5), ("Ashbury Toyw", 8),
                        ("Ember", 3), ("Ember Restaurant", 4)):
            mid = self.conn.execute(
                "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
                "RETURNING id::text AS id", (name,)).fetchone()["id"]
            self.ids[name] = mid
            for i in range(n):
                tid = add_txn(self.conn, TODAY - dt.timedelta(days=i * 9 + 1),
                              24.5, name.upper(), merchant=name,
                              primary="FOOD_AND_DRINK")
                self.conn.execute(
                    "UPDATE transactions SET merchant_id=%s WHERE id=%s",
                    (mid, tid))

    def tearDown(self):
        self.conn.close()

    def _offer(self, pid, frm, into):
        self.conn.execute(
            """INSERT INTO merchant_merge_proposals
                   (id, from_merchant_id, into_merchant_id, evidence)
               VALUES (%s, %s, %s, '{"signals": ["a reason"]}'::jsonb)""",
            (pid, self.ids[frm], self.ids[into]))

    def test_the_listing_marks_what_merge_all_may_take(self):
        self._offer("p1", "Ashbury Toyw", "Ashbury Toyworks Market")
        self._offer("p2", "Ember", "Ember Restaurant")
        by = {o["id"]: o for o in mm.pending(self.conn)}
        self.assertTrue(by["p1"]["bulk"])
        self.assertIsNone(by["p1"]["caution"])
        self.assertFalse(by["p2"]["bulk"])
        self.assertEqual(by["p2"]["caution"], mm.SHARE_A_WORD)

    def test_an_offer_stored_onto_the_cut_name_is_turned_around(self):
        self._offer("p1", "Ashbury Toyworks Market", "Ashbury Toyw")
        o = mm.pending(self.conn)[0]
        self.assertEqual((o["from"], o["into"]),
                         ("Ashbury Toyw", "Ashbury Toyworks Market"))
        self.assertEqual(o["from_id"], self.ids["Ashbury Toyw"])
        self.assertEqual(o["from_facts"]["rows"], 8)
        r = mm.decide(self.conn, "p1", "approve")
        self.assertEqual(r.get("into"), "Ashbury Toyworks Market")
        n = self.conn.execute(
            "SELECT count(*) AS n FROM transactions t JOIN merchants m "
            "ON m.id = t.merchant_id WHERE m.name = 'Ashbury Toyworks Market' "
            "AND t.removed = 0").fetchone()["n"]
        self.assertEqual(n, 13)


class ATurnedApproveKeepsItsFamilyLive(StoredOffers):
    """A family of three whose offers were all stored onto the cut
    spelling: approving the first keeps the full name and folds the cut
    one away, so the second offer must follow the survivor or "Merge all"
    stops on a merchant that is gone."""

    def setUp(self):
        super().setUp()
        mid = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
            "RETURNING id::text AS id", ("Ashbury Toywor",)).fetchone()["id"]
        self.ids["Ashbury Toywor"] = mid
        for i in range(2):
            tid = add_txn(self.conn, TODAY - dt.timedelta(days=i * 7 + 2),
                          11.0, "ASHBURY TOYWOR", merchant="Ashbury Toywor",
                          primary="FOOD_AND_DRINK")
            self.conn.execute(
                "UPDATE transactions SET merchant_id=%s WHERE id=%s",
                (mid, tid))

    def test_both_merges_land_in_sequence(self):
        self._offer("p1", "Ashbury Toyworks Market", "Ashbury Toyw")
        self._offer("p2", "Ashbury Toywor", "Ashbury Toyw")
        r1 = mm.decide(self.conn, "p1", "approve")
        self.assertEqual(r1.get("into"), "Ashbury Toyworks Market", r1)
        r2 = mm.decide(self.conn, "p2", "approve")
        self.assertNotIn("error", r2)
        self.assertEqual(r2.get("into"), "Ashbury Toyworks Market")
        n = self.conn.execute(
            "SELECT count(*) AS n FROM transactions t JOIN merchants m "
            "ON m.id = t.merchant_id WHERE m.name = 'Ashbury Toyworks Market' "
            "AND t.removed = 0").fetchone()["n"]
        self.assertEqual(n, 15)

    def test_an_offer_left_pairing_the_survivor_with_itself_is_dropped(self):
        self._offer("p1", "Ashbury Toyworks Market", "Ashbury Toyw")
        self._offer("p3", "Ashbury Toyw", "Ashbury Toyworks Market")
        mm.decide(self.conn, "p1", "approve")
        st = self.conn.execute(
            "SELECT status FROM merchant_merge_proposals WHERE id='p3'"
        ).fetchone()["status"]
        self.assertEqual(st, "stale")
        self.assertEqual(mm.pending(self.conn), [])


class MergeAllTakesOnlyWhatItShowed(StoredOffers):
    """"Merge all" sends each offer it listed as cut-off with bulk set. An
    earlier approve in the same batch can re-point a later offer onto the
    survivor, turning a cut-off pair into two names that only share a
    word; that offer is refused as a conflict (the client skips it and
    refetches), never merged. An offer the batch itself settled is a
    conflict too, so the batch goes on past it."""

    def setUp(self):
        super().setUp()
        for name in ("Ember Market", "Ember Marina", "Ember Mar"):
            self.ids[name] = self.conn.execute(
                "INSERT INTO merchants (name, name_source) VALUES (%s,"
                "'layer1') RETURNING id::text AS id", (name,)).fetchone()["id"]
            for i in range(3):
                tid = add_txn(self.conn, TODAY - dt.timedelta(days=i * 8 + 1),
                              18.0, name.upper(), merchant=name,
                              primary="FOOD_AND_DRINK")
                self.conn.execute(
                    "UPDATE transactions SET merchant_id=%s WHERE id=%s",
                    (self.ids[name], tid))

    def test_an_offer_an_earlier_approve_turned_into_a_shared_word_is_refused(self):
        self._offer("p1", "Ember Market", "Ember Mar")
        self._offer("p2", "Ember Marina", "Ember Mar")
        shown = [o["id"] for o in mm.pending(self.conn) if o["bulk"]]
        self.assertEqual(sorted(shown), ["p1", "p2"])
        self.assertNotIn("error", mm.decide(self.conn, "p1", "approve",
                                            bulk=True))
        r = mm.decide(self.conn, "p2", "approve", bulk=True)
        self.assertTrue(r.get("conflict"), r)
        # the marina's rows are still the marina's
        n = self.conn.execute(
            "SELECT count(*) AS n FROM transactions t JOIN merchants m "
            "ON m.id = t.merchant_id WHERE m.name = 'Ember Marina' "
            "AND t.removed = 0").fetchone()["n"]
        self.assertEqual(n, 3)
        st = self.conn.execute("SELECT status FROM merchant_merge_proposals "
                               "WHERE id='p2'").fetchone()["status"]
        self.assertEqual(st, "pending")

    def test_an_offer_the_batch_already_settled_is_a_conflict_not_a_stop(self):
        self._offer("p1", "Ashbury Toyworks Market", "Ashbury Toyw")
        self._offer("p3", "Ashbury Toyw", "Ashbury Toyworks Market")
        self.assertNotIn("error", mm.decide(self.conn, "p1", "approve",
                                            bulk=True))
        r = mm.decide(self.conn, "p3", "approve", bulk=True)
        self.assertTrue(r.get("conflict"), r)


if __name__ == "__main__":
    unittest.main()
