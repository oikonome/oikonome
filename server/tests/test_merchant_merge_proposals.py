"""Merchants that look like one business are OFFERED, never merged.

A business Plaid has no entity for reaches the ledger under every spelling
its terminals produce; the string clean is deliberately exact, so each
spelling is its own merchant until a person merges them. The proposer
finds the pairs a person would merge on sight, says why in words, and
puts them to the person. Approving goes through the journalled rename
(undo works unchanged); rejecting keeps them apart for good.
"""

import datetime as dt
import unittest

from oikonome.engine import merchant_merge as mm

from .util import TODAY, add_txn, make_db, write_config


def _cand(name, raws=None, *, rows=6, entity=None, parent=False, category="",
          median=20.0, mcc=None, city=None, manual=False, stems=None,
          descriptors=None, inflow=0):
    return {"id": name, "name": name, "rows": rows, "inflow": inflow,
            "raws": raws or [name], "samples": descriptors or [name.upper()],
            "stems": set(stems or []), "entity": entity, "parent": parent,
            "manual": manual, "category": category, "median": median,
            "mcc": mcc, "city": city, "p2p": " — " in name, "labels": {}}


def _pairs(props):
    return {(p["from"]["name"], p["into"]["name"]) for p in props}


CITIES = ["Brookhollow", "Fernvale", "Ashbury"]


class TheProposerOffersWhatAPersonWouldMerge(unittest.TestCase):
    def test_a_terminal_truncation_family_is_one_merchant(self):
        c = [_cand("Bluebird Orchard Market", ["Bluebird's Orchard Market"], rows=80),
             _cand("Bluebird Orc", ["Bluebird's Orc"], rows=120),
             _cand("Bluebird Orchard", ["Bluebird's Orchard Mar"], rows=50),
             _cand("Bluebird Orcha", ["Bluebird's Orcha"], rows=10)]
        props = mm.propose(c, CITIES)
        # every short spelling is offered onto the full name (the survivor)
        self.assertIn(("Bluebird Orc", "Bluebird Orchard Market"), _pairs(props))
        self.assertIn(("Bluebird Orchard", "Bluebird Orchard Market"), _pairs(props))
        self.assertIn(("Bluebird Orcha", "Bluebird Orchard Market"), _pairs(props))
        why = next(p for p in props if p["from"]["name"] == "Bluebird Orc")["signals"][0]
        self.assertIn("cut", why)

    def test_a_city_glued_on_is_the_same_name(self):
        c = [_cand("Bluebird Orchard Market", rows=80),
             _cand("Bluebird Orchard Market Brookhollowil", rows=50)]
        props = mm.propose(c, CITIES)
        self.assertEqual(len(props), 1)
        self.assertIn("Brookhollow", props[0]["signals"][0])

    def test_an_apostrophe_or_an_s_is_not_a_different_business(self):
        c = [_cand("Menard"), _cand("Menards"), _cand("Jimmy John"), _cand("Jimmy Johns")]
        self.assertEqual(_pairs(mm.propose(c, CITIES)),
                         {("Menard", "Menards"), ("Jimmy John", "Jimmy Johns")})

    def test_spacing_and_a_bank_prefix_are_not_the_name(self):
        c = [_cand("Radio Shack"), _cand("Radioshack"), _cand("Kroger"),
             _cand("Withdrawal Pos Kroger")]
        # the survivor is the fuller name only when it IS the name: the
        # bank-prefixed spelling is longer but the person sees "Kroger"
        self.assertEqual(_pairs(mm.propose(c, CITIES)),
                         {("Radioshack", "Radio Shack"), ("Withdrawal Pos Kroger", "Kroger")})

    def test_a_kind_of_business_word_and_initials(self):
        # initials alone fit too many names: they count only with the same
        # category, similar charges and enough rows on both sides
        c = [_cand("Cvs"), _cand("Cvs Pharmacy"),
             _cand("Mta", category="TRANSPORTATION", rows=9, median=2.9),
             _cand("Metropolitan Transportation Authority",
                   category="TRANSPORTATION", rows=12, median=2.9)]
        pairs = _pairs(mm.propose(c, CITIES))
        self.assertIn(("Cvs", "Cvs Pharmacy"), pairs)
        self.assertIn(("Mta", "Metropolitan Transportation Authority"), pairs)


class TheVetoesMakeTheKnownMistakesImpossible(unittest.TestCase):
    def test_a_brand_and_its_fuel_arm_are_never_offered(self):
        c = [_cand("Northwind Club", entity="ent-c"), _cand("Northwind Club Gas", parent=True)]
        self.assertEqual(mm.propose(c, CITIES), [])

    def test_two_plaid_entities_are_two_businesses(self):
        c = [_cand("Example Labs", entity="e1"), _cand("Example Labs Inc", entity="e2")]
        self.assertEqual(mm.propose(c, CITIES), [])

    def test_a_generic_leading_word_shares_nothing(self):
        c = [_cand("Google One"), _cand("Google Store"), _cand("City Water"),
             _cand("City Parking"), _cand("Corner"), _cand("Corner Bakery")]
        self.assertEqual(mm.propose(c, CITIES), [])

    def test_categories_that_disagree_are_two_businesses(self):
        c = [_cand("Volt Roasters", category="FOOD AND DRINK", rows=9),
             _cand("Volt Roasters Cafe", category="RENT AND UTILITIES", rows=9)]
        self.assertEqual(mm.propose(c, CITIES), [])
        # a generic bucket on one side says nothing, so it is not a disagreement
        c[1]["category"] = "GENERAL MERCHANDISE"
        self.assertEqual(len(mm.propose(c, CITIES)), 1)

    def test_a_lone_short_word_is_not_a_cut_of_a_run_together_string(self):
        c = [_cand("Ashbury", rows=30), _cand("Ashburyparks", rows=4),
             _cand("Ashburywater", rows=4)]
        self.assertEqual(mm.propose(c, CITIES), [])

    def test_flow_and_person_to_person_merchants_are_left_alone(self):
        c = [_cand("Northwind Bank", category="TRANSFER OUT"),
             _cand("Northwind Bank Transfer", category="TRANSFER OUT"),
             _cand("Zelle — Casey Example"), _cand("Zelle — Casey Examples")]
        self.assertEqual(mm.propose(c, CITIES), [])

    def test_a_two_word_sibling_is_not_a_cut(self):
        c = [_cand("Amazon Prime", rows=20), _cand("Amazon Prime Video", rows=20)]
        self.assertEqual(mm.propose(c, CITIES), [])

    def test_a_junk_or_city_tail_never_survives_the_name_most_rows_wear(self):
        c = [_cand("Burger Stop", rows=200, descriptors=["BURGER STOP 0001 LOT42"]),
             _cand("Burger Stop Lot Fernval", rows=1, descriptors=["BURGER STOP 0001 LOT42"],
                   stems=["burger stop"]),
             _cand("Greenleaf", rows=90), _cand("Greenleaf Brookhollow", rows=1),
             _cand("5loaf Bakery", rows=10), _cand("Bakery", rows=1)]
        c[0]["stems"] = {"burger stop"}
        pairs = _pairs(mm.propose(c, CITIES))
        self.assertIn(("Burger Stop Lot Fernval", "Burger Stop"), pairs)
        self.assertIn(("Greenleaf Brookhollow", "Greenleaf"), pairs)
        self.assertIn(("Bakery", "5loaf Bakery"), pairs)
        # the fuller name still wins when it carries its share of the rows
        c = [_cand("Bluebird Orc", rows=120), _cand("Bluebird Orchard Market", rows=80)]
        self.assertEqual(_pairs(mm.propose(c, CITIES)),
                         {("Bluebird Orc", "Bluebird Orchard Market")})

    def test_the_same_trade_in_the_same_town_is_not_the_same_business(self):
        # two MCC-8398 places both led by the town's name: the pool is not
        # the library
        c = [_cand("Brookhollow Aquat", rows=4, mcc="8398", city="Brookhollow"),
             _cand("Brookhollow Library", rows=13, mcc="8398", city="Brookhollow"),
             _cand("Brookhollow Aquatic", rows=1, mcc="8398", city="Brookhollow")]
        pairs = _pairs(mm.propose(c, CITIES))
        self.assertNotIn(("Brookhollow Aquat", "Brookhollow Library"), pairs)
        self.assertNotIn(("Brookhollow Aquatic", "Brookhollow Library"), pairs)
        # the pool's own cut spelling still joins it
        self.assertTrue({("Brookhollow Aquat", "Brookhollow Aquatic"),
                         ("Brookhollow Aquatic", "Brookhollow Aquat")} & pairs)
        # and the same trade in one town with a shared name word still counts
        c = [_cand("Ashbury Cove Health", rows=6, mcc="8011", city="Ashbury"),
             _cand("Ashbury Cove Hlth Ctr", rows=3, mcc="8011", city="Ashbury")]
        self.assertEqual(len(mm.propose(c, ["Ashbury"])), 1)

    def test_money_in_never_merges_with_money_out(self):
        c = [_cand("Ebay", rows=14), _cand("Ebay Payouts", rows=1, inflow=30)]
        self.assertEqual(mm.propose(c, CITIES), [])

    def test_outlets_of_one_chain_are_offered_the_brand_not_one_station(self):
        cities = ["Glenharbor", "Oakmere", "Stonefield"]
        c = [_cand("Petrox Svc Station Glenharbo", rows=1),
             _cand("Petrox Svc Station Oakmere", rows=1),
             _cand("Petrox Svc Station Stonefiel", rows=1),
             _cand("Petrox Svc Station", rows=1)]
        props = mm.propose(c, cities)
        self.assertEqual(len(props), 3)
        self.assertTrue(all(p["rename_to"] == "Petrox" for p in props))
        self.assertTrue(all("outlets of" in p["signals"][-1] for p in props))
        # a family one member already names as the brand keeps that member
        c.append(_cand("Petrox", rows=9))
        self.assertTrue(all("rename_to" not in p for p in mm.propose(c, cities)))

    def test_a_name_that_is_a_word_and_a_code_is_not_the_bare_word(self):
        c = [_cand("Pier:9", rows=8, category="FOOD AND DRINK"),
             _cand("Pier Kitchen", rows=2, category="FOOD AND DRINK"),
             _cand("7-Eleven", rows=3), _cand("Eleven Restaurant", rows=3)]
        self.assertEqual(mm.propose(c, CITIES), [])
        # it still meets a spelling of itself
        c = [_cand("Pier:9", rows=8), _cand("Pier 9 Springfield", rows=1)]
        self.assertEqual(len(mm.propose(c, ["Springfield"])), 1)

    def test_kind_words_alone_do_not_make_a_chain(self):
        c = [_cand("Gull Bar Grill", rows=2), _cand("Gull Bar Restaurant", rows=1)]
        props = mm.propose(c, CITIES)
        self.assertEqual(len(props), 1)
        self.assertNotIn("rename_to", props[0])
        self.assertIn(props[0]["into"]["name"], ("Gull Bar Grill", "Gull Bar Restaurant"))

    def test_a_clean_hand_over_in_time_is_cited(self):
        d = dt.date
        a = _cand("Ashbury Harv", rows=20)
        a.update(first=d(2021, 1, 4), last=d(2022, 6, 30))
        b = _cand("Ashbury Harvest Market", rows=20)
        b.update(first=d(2022, 8, 2), last=d(2024, 1, 5))
        why = mm.supports(a, b)
        self.assertTrue(any("stops 06/22" in w and "starts 08/22" in w for w in why), why)
        # spellings that interleave say nothing either way
        b.update(first=d(2021, 6, 1))
        self.assertFalse(any("stops" in w for w in mm.supports(a, b)))

    def test_a_person_who_named_one_has_spoken(self):
        c = [_cand("Bluebird Orc", manual=True), _cand("Bluebird Orchard Market")]
        self.assertEqual(mm.propose(c, CITIES), [])


class OffersAreDecidedByThePerson(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # one business under three spellings, each its own merchant row
        self.ids = {}
        for name, raw, n in (("Ashbury Harvest Market", "ASHBURY HARVEST MARKET", 5),
                             ("Ashbury Harv", "ASHBURY HARV", 8),
                             ("Ashbury Harve", "ASHBURY HARVE", 3)):
            mid = self.conn.execute(
                "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
                "RETURNING id::text AS id", (name,)).fetchone()["id"]
            self.ids[name] = mid
            for i in range(n):
                tid = add_txn(self.conn, TODAY - dt.timedelta(days=i * 9 + 1), 24.5,
                              raw, merchant=raw.title(), primary="FOOD_AND_DRINK")
                self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                                  (mid, tid))

    def tearDown(self):
        self.conn.close()

    def test_the_nightly_offers_and_a_rerun_offers_nothing_new(self):
        st = mm.run(self.conn)
        self.assertEqual(st["inserted"], 2)
        offers = mm.pending(self.conn)
        self.assertEqual({o["from"] for o in offers}, {"Ashbury Harv", "Ashbury Harve"})
        self.assertTrue(all(o["into"] == "Ashbury Harvest Market" for o in offers))
        self.assertTrue(all(o["signals"] for o in offers))
        self.assertEqual(mm.run(self.conn)["inserted"], 0)

    def test_an_offer_carries_each_sides_facts_so_they_can_be_told_apart(self):
        """Each side of an offer says what the ledger holds under it today —
        rows, total, dates, the usual charge, and its latest bank lines with
        the account — and a side's facts never borrow the other side's rows."""
        mm.run(self.conn)
        o = next(o for o in mm.pending(self.conn) if o["from"] == "Ashbury Harve")
        f, i = o["from_facts"], o["into_facts"]
        self.assertEqual((f["rows"], i["rows"]), (3, 5))
        self.assertEqual((f["total"], i["total"]), (73.5, 122.5))
        self.assertEqual(f["typical"], 24.5)
        self.assertEqual(f["category"], "FOOD AND DRINK")
        self.assertEqual(f["last"], str(TODAY - dt.timedelta(days=1)))
        self.assertEqual(f["first"], str(TODAY - dt.timedelta(days=19)))
        self.assertEqual([r["line"] for r in f["recent"]], ["ASHBURY HARVE"] * 3)
        self.assertEqual(len(i["recent"]), mm.RECENT_SHOWN)
        self.assertEqual(f["recent"][0]["date"], f["last"])
        self.assertTrue(f["recent"][0]["account"])
        self.assertEqual(f["accounts"], ["Test Card"])
        self.assertEqual(o["into_name"], "Ashbury Harvest Market")

    def test_approving_merges_through_the_journal_and_is_undoable(self):
        from oikonome.engine import merchant_dedup
        mm.run(self.conn)
        o = next(o for o in mm.pending(self.conn) if o["from"] == "Ashbury Harv")
        r = mm.decide(self.conn, o["id"], "approve")
        self.assertEqual(r.get("into"), "Ashbury Harvest Market")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions t JOIN merchants m ON m.id=t.merchant_id "
            "WHERE m.name='Ashbury Harvest Market' AND t.removed=0").fetchone()["n"], 13)
        self.assertNotIn("Ashbury Harv", {o["from"] for o in mm.pending(self.conn)})
        # the merge is a journal entry the Merchants page can undo
        change = self.conn.execute(
            "SELECT id FROM merchant_renames WHERE raw_merchant='Ashbury Harv' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(change)
        merchant_dedup.undo(self.conn, change["id"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions t JOIN merchants m ON m.id=t.merchant_id "
            "WHERE m.name='Ashbury Harvest Market' AND t.removed=0").fetchone()["n"], 5)

    def test_a_rejected_pair_is_never_offered_again(self):
        mm.run(self.conn)
        o = next(o for o in mm.pending(self.conn) if o["from"] == "Ashbury Harve")
        mm.decide(self.conn, o["id"], "reject")
        self.assertEqual(mm.run(self.conn)["inserted"], 0)
        self.assertNotIn("Ashbury Harve", {o["from"] for o in mm.pending(self.conn)})
        self.assertEqual(mm.decide(self.conn, o["id"], "approve").get("error"),
                         "unknown or already-decided proposal")

    def test_only_the_merchants_a_sync_minted_are_offered_on_ingest(self):
        st = mm.run(self.conn, only_ids=[self.ids["Ashbury Harve"]], limit=10)
        self.assertEqual(st["inserted"], 1)
        self.assertEqual([o["from"] for o in mm.pending(self.conn)], ["Ashbury Harve"])

    def test_the_alert_names_the_batch_and_goes_quiet_as_the_queue_is_worked(self):
        """The strip line is worded from the night's batch, not the live
        count: an alert is identified by its words, so a count that fell
        with each decision would come back un-dismissed every time — the
        noise the person asked not to have. It clears when nothing waits."""
        from oikonome.engine import alerts
        self.assertIsNone(mm.notice(self.conn))
        self.assertEqual(alerts.build({"merge_offers": None}), [])
        mm.run(self.conn)
        first = mm.notice(self.conn)
        self.assertEqual((first["pending"], first["batch"]), (2, 2))
        line = alerts.build({"merge_offers": first})[0]
        self.assertEqual((line["kind"], line["severity"], line["link"]),
                         ("merges", "info", "/merchants"))
        self.assertIn("2 merchant pairs", line["message"])
        o = mm.pending(self.conn)[0]
        mm.decide(self.conn, o["id"], "reject")
        after = mm.notice(self.conn)
        self.assertEqual(after["pending"], 1)
        # same words as before the decision — the dismissal holds
        self.assertEqual(alerts.build({"merge_offers": after})[0]["message"],
                         line["message"])
        mm.decide(self.conn, mm.pending(self.conn)[0]["id"], "approve")
        self.assertIsNone(mm.notice(self.conn))

    def test_working_the_queue_retires_the_logged_alert_at_once(self):
        """The alert row in the log is a snapshot the Today build writes.
        Emptying the queue from the alert's own link — approving or
        rejecting the last offer — must resolve that row then and there,
        or the person comes back to an alert that still says the pairs
        are waiting."""
        from oikonome.engine import alerts
        mm.run(self.conn)
        alerts.log(self.conn, alerts.build({"merge_offers": mm.notice(self.conn)}), TODAY)
        row = lambda: [r for r in alerts.history(self.conn) if r["kind"] == "merges"][0]
        self.assertEqual(row()["active"], 1)
        offers = mm.pending(self.conn)
        mm.decide(self.conn, offers[0]["id"], "reject")
        self.assertEqual(row()["active"], 1)          # one offer still waits
        mm.decide(self.conn, offers[1]["id"], "approve")
        self.assertEqual(row()["active"], 0)          # queue empty: resolved

    def test_approving_a_chain_offer_puts_every_outlet_under_the_brand(self):
        from oikonome.engine.compat import jsonb
        ids = {}
        for name, n in (("Petrox Svc Station Oakmere", 2), ("Petrox Svc Station Stonefiel", 3)):
            mid = self.conn.execute(
                "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
                "RETURNING id::text AS id", (name,)).fetchone()["id"]
            ids[name] = mid
            for i in range(n):
                tid = add_txn(self.conn, TODAY - dt.timedelta(days=i * 5 + 1), 40.0,
                              name.upper(), merchant=name, primary="TRANSPORTATION")
                self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                                  (mid, tid))
        into = ids["Petrox Svc Station Stonefiel"]
        self.conn.execute(
            "INSERT INTO merchant_merge_proposals (id, from_merchant_id, into_merchant_id, "
            "evidence) VALUES ('mm:a', %s, %s, %s)",
            (ids["Petrox Svc Station Oakmere"], into,
             jsonb({"rename_to": "Petrox", "signals": ["all are outlets of \"Petrox\""]})))
        # a sibling offer onto the same survivor, still pending afterwards
        self.conn.execute(
            "INSERT INTO merchant_merge_proposals (id, from_merchant_id, into_merchant_id, "
            "evidence) VALUES ('mm:b', %s, %s, %s)",
            (self.ids["Ashbury Harve"], into, jsonb({"rename_to": "Petrox"})))
        self.assertEqual(next(o for o in mm.pending(self.conn) if o["id"] == "mm:a")["into"],
                         "Petrox")
        r = mm.decide(self.conn, "mm:a", "approve")
        self.assertEqual(r.get("into"), "Petrox")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions t JOIN merchants m ON m.id=t.merchant_id "
            "WHERE m.name='Petrox' AND t.removed=0").fetchone()["n"], 5)
        # the sibling followed the survivor's row and is still on offer
        self.assertIn("mm:b", {o["id"] for o in mm.pending(self.conn)})

    def test_the_ingest_pass_reads_only_the_fresh_merchants_family(self):
        """An hourly sync must not pay a whole-ledger dedup pass. With
        only_ids the run loads the fresh merchant's FAMILY — the merchants
        it could possibly pair with — and offers exactly what the nightly
        full run would offer for it."""
        from unittest import mock

        from oikonome.engine.compat import as_dict

        # catalog noise: merchants with rows that share no name block with
        # the family, so a bounded run must never aggregate their rows
        for name in ("Blue Harbor Hardware", "Cedar Lane Cleaners",
                     "Delta Print Works", "Ember Tea House"):
            mid = self.conn.execute(
                "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
                "RETURNING id::text AS id", (name,)).fetchone()["id"]
            tid = add_txn(self.conn, TODAY - dt.timedelta(days=3), 31.0,
                          name.upper(), merchant=name, primary="GENERAL_MERCHANDISE")
            self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                              (mid, tid))

        loaded = []
        real = mm.load_live

        def counting(conn, ids=None):
            out = real(conn, ids=ids)
            loaded.append(len(out))
            return out

        with mock.patch.object(mm, "load_live", counting):
            st = mm.run(self.conn, only_ids=[self.ids["Ashbury Harve"]], limit=10)
        # the three spellings of the one business, not the seven merchants
        self.assertEqual(loaded, [3])
        self.assertEqual(st["inserted"], 1)

        # ...and the offer is the one a full scan would have produced
        full = {mm._sig(p["from"]["id"], p["into"]["id"]): p
                for p in mm.propose(mm.load_live(self.conn),
                                    mm.cities_known(self.conn))}
        row = self.conn.execute(
            "SELECT id, from_merchant_id::text AS f, into_merchant_id::text AS i, "
            "evidence FROM merchant_merge_proposals").fetchone()
        want = full[row["id"]]
        ev = as_dict(row["evidence"])
        self.assertEqual(
            (row["f"], row["i"], ev["signals"], ev["supports"]),
            (want["from"]["id"], want["into"]["id"], want["signals"],
             want["supports"]))

    def test_the_alert_counts_only_offers_the_person_can_still_act_on(self):
        """The person merges one of the pairs on the Merchants page instead
        of approving the offer; the nightly then prunes the merchant the
        merge emptied. pending() hides the offer from that moment on, so an
        alert still counting it announces a queue the person opens to find
        empty — re-worded, and so undismissed, every night."""
        from oikonome.engine import merchant_dedup, merchant_identity
        mm.run(self.conn)
        self.assertEqual(mm.notice(self.conn)["pending"], 2)
        merchant_dedup.rename(self.conn, "Ashbury Harv", "Ashbury Harvest Market")
        merchant_identity.prune_empty(self.conn)
        self.assertEqual(len(mm.pending(self.conn)), 1)
        self.assertEqual(mm.notice(self.conn)["pending"], 1)

    def test_the_nightly_retires_offers_whose_merchants_are_no_longer_two(self):
        """An offer needs two live merchants to be an offer. One that has
        lost one is unreachable — it can never be approved or rejected —
        so the nightly retires it instead of leaving it pending forever."""
        from oikonome.engine import merchant_dedup, merchant_identity
        mm.run(self.conn)
        merchant_dedup.rename(self.conn, "Ashbury Harv", "Ashbury Harvest Market")
        merchant_identity.prune_empty(self.conn)
        self.assertEqual(mm.run(self.conn)["stale"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM merchant_merge_proposals "
            "WHERE from_merchant_id=%s", (self.ids["Ashbury Harv"],)
        ).fetchone()["status"], "stale")
        self.assertEqual(mm.run(self.conn)["stale"], 0)
        # the one real offer left is still on the strip
        self.assertEqual(mm.notice(self.conn)["pending"], 1)

    def test_approving_never_folds_into_a_third_merchant_nobody_was_shown(self):
        """evidence.rename_to is a NAME, and a rename onto a name that
        already exists is a MERGE. A third live merchant wearing the brand
        would swallow the survivor — two businesses joined without the
        person ever seeing the pair. The approve is refused and names it."""
        from oikonome.engine.compat import jsonb
        ids = {}
        for name, n in (("Petrox Svc Station Oakmere", 2),
                        ("Petrox Svc Station Stonefiel", 3),
                        ("Petrox", 4)):
            mid = self.conn.execute(
                "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
                "RETURNING id::text AS id", (name,)).fetchone()["id"]
            ids[name] = mid
            for i in range(n):
                tid = add_txn(self.conn, TODAY - dt.timedelta(days=i * 5 + 1), 40.0,
                              name.upper(), merchant=name, primary="TRANSPORTATION")
                self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                                  (mid, tid))
        self.conn.execute(
            "INSERT INTO merchant_merge_proposals (id, from_merchant_id, "
            "into_merchant_id, evidence) VALUES ('mm:brand', %s, %s, %s)",
            (ids["Petrox Svc Station Oakmere"], ids["Petrox Svc Station Stonefiel"],
             jsonb({"rename_to": "Petrox"})))
        r = mm.decide(self.conn, "mm:brand", "approve")
        self.assertIn("Petrox", r.get("error", ""))
        self.assertIn("Merchants page", r.get("error", ""))
        # nothing moved, and the offer is still the person's to decide
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE merchant_id=%s "
            "AND removed=0", (ids["Petrox"],)).fetchone()["n"], 4)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM merchant_merge_proposals WHERE id='mm:brand'"
        ).fetchone()["status"], "pending")
        self.assertIn("mm:brand", {o["id"] for o in mm.pending(self.conn)})

    def test_the_rehearsal_scores_the_proposer_against_the_ledgers_own_aliases(self):
        # the three spellings are already one merchant in the labels once the
        # person merged them; the rehearsal must find both pieces
        from oikonome.engine import merchant_dedup
        merchant_dedup.rename(self.conn, "Ashbury Harv", "Ashbury Harvest Market")
        merchant_dedup.rename(self.conn, "Ashbury Harve", "Ashbury Harvest Market")
        r = mm.rehearse(self.conn)
        self.assertGreaterEqual(r["true"], 2)
        self.assertEqual(r["false"], 0)
        self.assertEqual(r["precision"], 1.0)


class _CannedRows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _SplitReadConn:
    """A connection whose two reads disagree: the second answer names a
    merchant the first did not. That is what READ COMMITTED gives when a
    charge is ingested between the two statements behind an offer's side
    facts — each statement takes its own snapshot."""

    def __init__(self, *answers):
        self._answers = list(answers)

    def execute(self, sql, params=None):
        return _CannedRows(self._answers.pop(0))


class SideFactsSurviveALedgerThatMovesBetweenTheReads(unittest.TestCase):
    """The Merchants page must load even when a charge lands mid-read: a
    merchant only the second statement saw is left factless, never fatal."""

    KNOWN = "11111111-1111-1111-1111-111111111111"
    FRESH = "22222222-2222-2222-2222-222222222222"

    def test_a_merchant_only_the_second_read_saw_does_not_break_the_page(self):
        conn = _SplitReadConn(
            [{"id": self.KNOWN, "n": 2, "total": 40.0,
              "first": TODAY - dt.timedelta(days=9), "last": TODAY,
              "typical": 20.0, "category": "FOOD AND DRINK",
              "city": "Ashbury", "accounts": ["Test Card"]}],
            [{"id": self.KNOWN, "date": TODAY, "amount": 20.0,
              "line": "ASHBURY HARVE", "account": "Test Card"},
             {"id": self.FRESH, "date": TODAY, "amount": 5.0,
              "line": "ASHBURY HARVE", "account": "Test Card"}])
        facts = mm.side_facts(conn, [self.KNOWN, self.FRESH])
        self.assertEqual([r["amount"] for r in facts[self.KNOWN]["recent"]], [20.0])
        # no half-stated side: without totals there is nothing honest to show
        self.assertNotIn(self.FRESH, facts)


if __name__ == "__main__":
    unittest.main()
