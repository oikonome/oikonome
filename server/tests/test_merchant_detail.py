"""One merchant, opened: the merchants screen's expandable row. The facts
and the places must come from the same display-merchant grouping the
catalog uses, so the row you open is the row you read about."""
import unittest

from oikonome.engine import entities, merchant_dedup

from .test_llm_categorize import add_raw_txn
from .util import make_db


class MerchantDetailTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        for i, (city, addr, lat, lon, d) in enumerate([
                ("Lakeview", "1 Cedar Blvd", 39.80, -89.65, "2026-08-01"),
                ("Lakeview", "1 Cedar Blvd", 39.80, -89.65, "2026-08-09"),
                ("Ridgeport", "10 Harbor Pkwy", 39.72, -89.51, "2026-07-20")]):
            add_raw_txn(self.conn, f"c{i}", d, 40 + i, "NORTHWIND CLUB WHSE #0999",
                        primary="GENERAL_MERCHANDISE", raw={})
            self.conn.execute(
                "UPDATE transactions SET location_city=%s, location_region='IL', "
                "location_address=%s, location_lat=%s, location_lon=%s WHERE id=%s",
                (city, addr, lat, lon, f"c{i}"))
        add_raw_txn(self.conn, "online", "2026-08-02", 9.99, "NETFLIX.COM",
                    primary="ENTERTAINMENT", raw={})

    def tearDown(self):
        self.conn.close()

    def test_places_are_grouped_busiest_first_with_latest_coordinates(self):
        d = merchant_dedup.detail(self.conn, "NORTHWIND CLUB WHSE #0999")
        self.assertEqual(d["rows"], 3)
        self.assertEqual(d["category"], "GENERAL_MERCHANDISE")
        self.assertEqual([(l["city"], l["n"]) for l in d["locations"]],
                         [("Lakeview", 2), ("Ridgeport", 1)])
        self.assertEqual(d["locations"][0]["address"], "1 Cedar Blvd")
        self.assertAlmostEqual(d["locations"][0]["lat"], 39.80)
        self.assertEqual(str(d["locations"][0]["last"]), "2026-08-09")

    def test_an_online_merchant_has_no_places(self):
        d = merchant_dedup.detail(self.conn, "NETFLIX.COM")
        self.assertEqual(d["locations"], [])
        self.assertEqual(d["variants"], ["NETFLIX.COM"])

    def test_unknown_display_is_none(self):
        self.assertIsNone(merchant_dedup.detail(self.conn, "nobody"))

    def test_rule_is_reported(self):
        from oikonome.web import data
        data.set_merchant_category(self.conn, "NORTHWIND CLUB WHSE #0999", "FOOD_AND_DRINK")
        d = merchant_dedup.detail(self.conn, "NORTHWIND CLUB WHSE #0999")
        self.assertEqual(d["rule"]["category_primary"], "FOOD_AND_DRINK")
        self.assertEqual(d["rule"]["source"], "user")
        self.assertEqual(d["category"], "FOOD_AND_DRINK")

    def test_a_flow_named_rule_is_not_reported(self):
        # A legacy flow-named rule (a rename, a replayed undo, an old row)
        # is one apply() refuses to act on — the card must not claim a rule
        # that categorizes nothing.
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES ('NETFLIX.COM', 'TRANSFER_OUT', 'user')")
        d = merchant_dedup.detail(self.conn, "NETFLIX.COM")
        self.assertIsNone(d["rule"])

    def test_the_pin_and_the_address_come_from_the_same_visit(self):
        # Two outlets in one city: the address that sorts last is not the
        # one visited last. Each must be its own place, and a place's pin
        # must belong to the address shown next to it.
        add_raw_txn(self.conn, "c3", "2026-08-12", 55, "NORTHWIND CLUB WHSE #0999",
                    primary="GENERAL_MERCHANDISE", raw={})
        self.conn.execute(
            "UPDATE transactions SET location_city='Lakeview', "
            "location_region='IL', location_address='9 Zero Way', "
            "location_lat=39.90, location_lon=-89.70 WHERE id='c3'")
        # a newer Cedar Blvd visit, so the latest Lakeview row is NOT Zero Way
        add_raw_txn(self.conn, "c4", "2026-08-15", 12, "NORTHWIND CLUB WHSE #0999",
                    primary="GENERAL_MERCHANDISE", raw={})
        self.conn.execute(
            "UPDATE transactions SET location_city='Lakeview', "
            "location_region='IL', location_address='1 Cedar Blvd', "
            "location_lat=39.80, location_lon=-89.65 WHERE id='c4'")
        d = merchant_dedup.detail(self.conn, "NORTHWIND CLUB WHSE #0999")
        places = {l["address"]: l for l in d["locations"]}
        self.assertEqual(set(places), {"1 Cedar Blvd", "9 Zero Way",
                                       "10 Harbor Pkwy"})
        self.assertEqual(places["1 Cedar Blvd"]["n"], 3)
        self.assertAlmostEqual(places["1 Cedar Blvd"]["lat"], 39.80)
        self.assertAlmostEqual(places["9 Zero Way"]["lat"], 39.90)
        self.assertAlmostEqual(places["9 Zero Way"]["lon"], -89.70)
        self.assertEqual(places["9 Zero Way"]["n"], 1)

    def test_business_rows_stay_out_of_the_category_and_the_places(self):
        # The same store visited through a business account: its rows are
        # that entity's books, not the household's merchant card. The head
        # already kept them out of rows/total; category and places too.
        eid = entities.create_entity(self.conn, name="Side Biz",
                                     structure="sole_prop")["id"]
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('biz','it1','Biz Checking','depository','checking',100)")
        entities.assign_account(self.conn, "biz", eid)
        self.conn.execute(
            "SELECT set_config('app.entity_account_ids', 'biz', false)")
        for i, d in enumerate(["2026-08-10", "2026-08-11", "2026-08-12",
                               "2026-08-13"]):
            add_raw_txn(self.conn, f"b{i}", d, 200, "NORTHWIND CLUB WHSE #0999",
                        primary="GENERAL_SERVICES", raw={}, account="biz")
            self.conn.execute(
                "UPDATE transactions SET location_city='Springfield', "
                "location_region='IL', location_address='1 Biz St', "
                "location_lat=39.78, location_lon=-89.64 WHERE id=%s", (f"b{i}",))
        d = merchant_dedup.detail(self.conn, "NORTHWIND CLUB WHSE #0999")
        self.assertEqual(d["rows"], 3)
        self.assertEqual(d["category"], "GENERAL_MERCHANDISE")
        self.assertEqual([l["city"] for l in d["locations"]],
                         ["Lakeview", "Ridgeport"])

    def test_rule_is_found_through_the_canonical_not_the_spelling(self):
        # The rule lives under the canonical ("Northwind Club"); the merchant ROW
        # the raws resolve to has since taken the aggregator's name
        # ("Northwind Club Wholesale"), which is what the ledger displays. Neither
        # the display name nor any raw string equals the rule's key, yet
        # apply() still reaches every row through the canonical — so the
        # card must report the rule the same way, not by string equality.
        from oikonome.engine import llm_categorize
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method) "
            "VALUES ('NORTHWIND CLUB WHSE #0999', 'Northwind Club', 'manual')")
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES ('Northwind Club', 'FOOD_AND_DRINK', 'user')")
        mid = self.conn.execute(
            "INSERT INTO merchants (name, name_source) "
            "VALUES ('Northwind Club Wholesale', 'plaid') RETURNING id").fetchone()["id"]
        self.conn.execute(
            "UPDATE transactions SET merchant_id=%s WHERE name='NORTHWIND CLUB WHSE #0999'",
            (mid,))
        llm_categorize.apply(self.conn)
        self.assertIsNone(merchant_dedup.detail(self.conn, "NORTHWIND CLUB WHSE #0999"))
        d = merchant_dedup.detail(self.conn, "Northwind Club Wholesale")
        self.assertEqual(d["category"], "FOOD_AND_DRINK")   # the rule applied
        self.assertEqual(d["rule"]["category_primary"], "FOOD_AND_DRINK")
        self.assertEqual(d["rule"]["source"], "user")
