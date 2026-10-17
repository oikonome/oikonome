"""A matched store record whose items are of several kinds splits the
charge it explains, so a mixed warehouse run counts each kind in its own
budget bucket instead of the whole run in the dominant one.

The parts are the items' categories weighted by their amounts and scaled to
what the card was charged, summing to the row exactly; they are the store's
reading (source = the store), recomputed with every match, and a person's
split or pin always outranks them. A record that reads as one category, or
whose items cannot be read, leaves the row whole as before.
"""
import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import amazon_match, budget, costco_match, splits, store_match
from oikonome.engine import categories as _cats
from oikonome.engine.compat import as_date, jsonb
from oikonome.web import data

from .util import _ensure_db, add_txn, make_db, seed_accounts, write_config

FOOD, HOME, FUEL = "Costco - Food & Drink", "Costco - Home", "Costco - Fuel"


def add_receipt(conn, dedup_key, date, amount, category, items,
                rtype="warehouse", is_refund=0):
    conn.execute(
        """INSERT INTO costco_receipts (dedup_key, account, date, amount,
               receipt_type, category, is_refund, items_json)
           VALUES (%s,'main',%s,%s,%s,%s,%s,%s)
           ON CONFLICT (tenant_id, dedup_key) DO UPDATE SET items_json =
               EXCLUDED.items_json, category = EXCLUDED.category""",
        (dedup_key, as_date(date), amount, rtype, category, is_refund,
         jsonb(items)))


def add_order(conn, dedup_key, date, amount, category, items):
    conn.execute(
        """INSERT INTO amazon_orders (dedup_key, account, date, amount, payee,
               category, is_refund, items_json)
           VALUES (%s,'acct',%s,%s,'Amazon',%s,0,%s)""",
        (dedup_key, as_date(date), amount, category, jsonb(items)))


def parts(conn, txn_id):
    return [(r["category"], r["amount"], r["source"]) for r in conn.execute(
        "SELECT category, amount, source FROM transaction_splits "
        "WHERE txn_id=%s ORDER BY line", (txn_id,))]


def food_counted(conn, txn_id) -> float:
    """What the budget's food bucket reads for this row: the test the
    verdict applies per PART, through the join every rollup uses."""
    return conn.execute(
        f"""SELECT COALESCE(SUM(COALESCE(sp.amount, t.amount)), 0) AS food
             FROM transactions t {_cats.SPLIT_JOIN}
            WHERE t.id = %s AND {budget.PART_FOOD_SQL}""",
        (txn_id,)).fetchone()["food"]


class AllocationTests(unittest.TestCase):
    def test_parts_follow_the_items_and_add_up_to_the_charge(self):
        # items pre-tax 80.00 food + 20.00 home; the card was charged 107.01
        items = [{"category": "Food & Drink", "amount": 30.00},
                 {"category": "Home", "amount": 20.00},
                 {"category": "Food & Drink", "amount": 50.00}]
        got = store_match.record_parts(items, 107.01, "Costco - ")
        self.assertEqual([p["category"] for p in got], [FOOD, HOME])
        self.assertEqual(round(sum(p["amount"] for p in got), 2), 107.01)
        # tax spread pro rata: food is 80% of the items, so of the charge —
        # 85.608 and 21.402, the odd cent going to the larger remainder
        self.assertAlmostEqual(got[0]["amount"], 85.61, places=2)
        self.assertAlmostEqual(got[1]["amount"], 21.40, places=2)

    def test_leftover_cents_land_on_the_largest_remainders(self):
        got = store_match.record_parts(
            [{"category": "A", "amount": 1}, {"category": "B", "amount": 1},
             {"category": "C", "amount": 1}], 1.00, "")
        self.assertEqual(sorted(p["amount"] for p in got), [0.33, 0.33, 0.34])

    def test_a_storewide_coupon_is_netted_into_the_items(self):
        items = [{"category": "Food & Drink", "amount": 60},
                 {"category": "Home", "amount": 40},
                 {"category": "Shopping", "amount": -10}]   # coupon line
        got = store_match.record_parts(items, 90.0, "Costco - ")
        self.assertEqual({p["category"]: p["amount"] for p in got},
                         {FOOD: 54.0, HOME: 36.0})

    def test_one_category_or_unreadable_items_is_no_split(self):
        one = [{"category": "Food & Drink", "amount": 20},
               {"category": "Food & Drink", "amount": 30}]
        self.assertIsNone(store_match.record_parts(one, 50.0, "Costco - "))
        no_cat = [{"category": "Food & Drink", "amount": 20}, {"amount": 30}]
        self.assertIsNone(store_match.record_parts(no_cat, 50.0, "Costco - "))
        no_amt = [{"category": "Food & Drink", "amount": 20},
                  {"category": "Home", "title": "x"}]
        self.assertIsNone(store_match.record_parts(no_amt, 50.0, "Costco - "))
        self.assertIsNone(store_match.record_parts([], 50.0, "Costco - "))
        self.assertIsNone(store_match.record_parts("not json", 50.0, "Costco - "))
        self.assertIsNone(store_match.record_parts({"a": 1}, 50.0, "Costco - "))
        # money in is one row
        two = [{"category": "Food & Drink", "amount": 20},
               {"category": "Home", "amount": 30}]
        self.assertIsNone(store_match.record_parts(two, -50.0, "Costco - "))

    def test_a_part_that_rounds_to_nothing_is_dropped(self):
        items = [{"category": "Food & Drink", "amount": 100},
                 {"category": "Home", "amount": 0.001}]
        self.assertIsNone(store_match.record_parts(items, 100.0, "Costco - "))


class CostcoTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_mixed_receipt_splits_its_charge_and_the_food_bucket_reads_the_part(self):
        # 96.00 of items and 4.00 of tax: food carries 76/96 of the charge
        add_receipt(self.conn, "r1", "2025-07-10", -100.00, "Food & Drink",
                    [{"category": "Home", "amount": 20.00},
                     {"category": "Food & Drink", "amount": 76.00}])
        t = add_txn(self.conn, "2025-07-10", 100.00, "COSTCO WHSE")
        res = costco_match.run_match(self.conn)
        self.assertEqual(res["matched"], 1)
        self.assertEqual(res["split"], 1)
        got = parts(self.conn, t)
        self.assertEqual([c for c, _, _ in got], [FOOD, HOME])
        self.assertEqual({s for _, _, s in got}, {"costco"})
        self.assertEqual(round(sum(a for _, a, _ in got), 2), 100.00)
        self.assertAlmostEqual(food_counted(self.conn, t), 79.17, places=2)
        # the row's own override is untouched: the split is an overlay
        self.assertEqual(self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (t,)).fetchone()["category_override"], FOOD)

    def test_a_single_kind_receipt_leaves_the_row_whole(self):
        add_receipt(self.conn, "r1", "2025-07-10", -40.00, "Food & Drink",
                    [{"category": "Food & Drink", "amount": 18.00},
                     {"category": "Food & Drink", "amount": 20.00}])
        t = add_txn(self.conn, "2025-07-10", 40.00, "COSTCO WHSE")
        res = costco_match.run_match(self.conn)
        self.assertEqual(res["matched"], 1)
        self.assertEqual(res["split"], 0)
        self.assertEqual(parts(self.conn, t), [])
        self.assertAlmostEqual(food_counted(self.conn, t), 40.00, places=2)

    def test_recomputed_with_the_match_so_a_corrected_receipt_is_followed(self):
        add_receipt(self.conn, "r1", "2025-07-10", -100.0, "Food & Drink",
                    [{"category": "Food & Drink", "amount": 60},
                     {"category": "Home", "amount": 40}])
        t = add_txn(self.conn, "2025-07-10", 100.0, "COSTCO WHSE")
        costco_match.run_match(self.conn)
        costco_match.run_match(self.conn)            # idempotent
        self.assertEqual([(c, a) for c, a, _ in parts(self.conn, t)],
                         [(FOOD, 60.0), (HOME, 40.0)])
        # the collector re-pushes the receipt with a better reading
        add_receipt(self.conn, "r1", "2025-07-10", -100.0, "Food & Drink",
                    [{"category": "Food & Drink", "amount": 75},
                     {"category": "Cleaning", "amount": 25}])
        costco_match.run_match(self.conn)
        self.assertEqual([(c, a) for c, a, _ in parts(self.conn, t)],
                         [(FOOD, 75.0), ("Costco - Cleaning", 25.0)])
        # and a receipt that now reads as one kind takes the split away
        add_receipt(self.conn, "r1", "2025-07-10", -100.0, "Food & Drink",
                    [{"category": "Food & Drink", "amount": 100}])
        costco_match.run_match(self.conn)
        self.assertEqual(parts(self.conn, t), [])

    def test_a_hand_split_outranks_the_receipt(self):
        add_receipt(self.conn, "r1", "2025-07-10", -100.0, "Food & Drink",
                    [{"category": "Food & Drink", "amount": 60},
                     {"category": "Home", "amount": 40}])
        t = add_txn(self.conn, "2025-07-10", 100.0, "COSTCO WHSE")
        splits.set_split(self.conn, t, [{"category": "FOOD_AND_DRINK", "amount": 90},
                                        {"category": "Costco - Pets", "amount": 10}])
        costco_match.run_match(self.conn)
        self.assertEqual(parts(self.conn, t),
                         [("FOOD_AND_DRINK", 90.0, "user"),
                          ("Costco - Pets", 10.0, "user")])
        # and a person's parts written OVER the receipt's stay theirs
        t2 = add_txn(self.conn, "2025-07-12", 100.0, "COSTCO WHSE")
        add_receipt(self.conn, "r2", "2025-07-12", -100.0, "Food & Drink",
                    [{"category": "Food & Drink", "amount": 60},
                     {"category": "Home", "amount": 40}])
        costco_match.run_match(self.conn)
        self.assertEqual({s for _, _, s in parts(self.conn, t2)}, {"costco"})
        splits.set_split(self.conn, t2, [{"category": FOOD, "amount": 50},
                                         {"category": HOME, "amount": 50}])
        costco_match.run_match(self.conn)
        self.assertEqual(parts(self.conn, t2),
                         [(FOOD, 50.0, "user"), (HOME, 50.0, "user")])

    def test_a_pinned_row_is_the_persons_whole_answer(self):
        add_receipt(self.conn, "r1", "2025-07-10", -100.0, "Food & Drink",
                    [{"category": "Food & Drink", "amount": 60},
                     {"category": "Home", "amount": 40}])
        t = add_txn(self.conn, "2025-07-10", 100.0, "COSTCO WHSE")
        data.set_category(self.conn, t, "FOOD_AND_DRINK")
        costco_match.run_match(self.conn)
        self.assertEqual(parts(self.conn, t), [])
        self.assertEqual(self.conn.execute(
            "SELECT category_override, override_source FROM transactions "
            "WHERE id=%s", (t,)).fetchone()["override_source"], "user")

    def test_a_refund_is_one_row(self):
        add_receipt(self.conn, "r1", "2025-07-10", 50.0, "Refunds",
                    [{"category": "Food & Drink", "amount": -30},
                     {"category": "Home", "amount": -20}], is_refund=1)
        t = add_txn(self.conn, "2025-07-10", -50.0, "COSTCO WHSE")
        res = costco_match.run_match(self.conn)
        self.assertEqual(res["matched"], 1)
        self.assertEqual(parts(self.conn, t), [])


class AmazonTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_items_the_model_categorized_split_the_charge_by_price(self):
        add_order(self.conn, "o1", "2025-07-08", -54.00, "Beauty & Personal Care",
                  [{"title": "hand soap", "price": 17.0,
                    "category": "Beauty & Personal Care"},
                   {"title": "notebook", "price": 12.0, "category": "Apparel"},
                   {"title": "desk lamp", "price": 25.0,
                    "category": "Beauty & Personal Care"}])
        t = add_txn(self.conn, "2025-07-10", 54.00, "AMZN Mktp US*123")
        res = amazon_match.run_match(self.conn)
        self.assertEqual(res["split"], 1)
        got = {c: a for c, a, _ in parts(self.conn, t)}
        self.assertEqual(set(got), {"Amazon - Beauty & Personal Care",
                                    "Amazon - Apparel"})
        self.assertEqual(round(sum(got.values()), 2), 54.00)
        self.assertAlmostEqual(got["Amazon - Apparel"], 12.0, places=2)

    def test_an_item_without_a_price_or_a_category_leaves_the_row_whole(self):
        add_order(self.conn, "o1", "2025-07-08", -30.0, "Home",
                  [{"title": "lamp", "price": 20.0, "category": "Home"},
                   {"title": "snacks", "category": "Food & Drink"}])
        t = add_txn(self.conn, "2025-07-10", 30.0, "AMZN Mktp US*123")
        amazon_match.run_match(self.conn)
        self.assertEqual(parts(self.conn, t), [])
        # the model answered for the order only: items stamped null
        add_order(self.conn, "o2", "2025-07-08", -31.0, "Home",
                  [{"title": "lamp", "price": 20.0, "category": None},
                   {"title": "snacks", "price": 10.0, "category": None}])
        t2 = add_txn(self.conn, "2025-07-10", 31.0, "AMZN Mktp US*123")
        amazon_match.run_match(self.conn)
        self.assertEqual(parts(self.conn, t2), [])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"rsplit-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.d = dt.date.today().replace(day=10)
            cls.txn = add_txn(conn, cls.d, 100.0, "COSTCO WHSE", account="chk")
            add_receipt(conn, "r1", cls.d, -100.0, "Food & Drink",
                        [{"category": "Food & Drink", "amount": 60},
                         {"category": "Home", "amount": 40}])
            costco_match.run_match(conn)
        finally:
            conn.close()

    def _row(self):
        r = self.client.get("/api/transactions",
                            params={"y": self.d.year, "m": self.d.month}).json()
        return next(x for x in r["rows"] if x["id"] == self.txn)

    def test_the_row_says_the_receipt_split_it_and_removing_it_pins_the_row(self):
        row = self._row()
        self.assertEqual(row["split_source"], "costco")
        self.assertEqual([p["category"] for p in row["split"]], [FOOD, HOME])
        # a person's edit makes the parts theirs
        r = self.client.put(f"/api/transactions/{self.txn}/split", json={
            "parts": [{"category": FOOD, "amount": 70},
                      {"category": HOME, "amount": 30}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._row()["split_source"], "user")
        conn = tenancy.tenant_connect(self.tid)
        try:
            costco_match.run_match(conn)
            self.assertEqual([(c, a) for c, a, _ in parts(conn, self.txn)],
                             [(FOOD, 70.0), (HOME, 30.0)])
            # back to the receipt's parts: drop theirs by hand
            conn.execute("DELETE FROM transaction_splits WHERE txn_id=%s",
                         (self.txn,))
            costco_match.run_match(conn)
        finally:
            conn.close()
        self.assertEqual(self._row()["split_source"], "costco")
        # removing the receipt's parts pins the row whole, so the next
        # match run does not bring them back
        r = self.client.delete(f"/api/transactions/{self.txn}/split")
        self.assertEqual(r.status_code, 200, r.text)
        row = self._row()
        self.assertIsNone(row["split"])
        self.assertIsNone(row["split_source"])
        conn = tenancy.tenant_connect(self.tid)
        try:
            costco_match.run_match(conn)
            self.assertEqual(parts(conn, self.txn), [])
            src = conn.execute(
                "SELECT category_override, override_source FROM transactions "
                "WHERE id=%s", (self.txn,)).fetchone()
            self.assertEqual((src["category_override"], src["override_source"]),
                             (FOOD, "user"))
        finally:
            conn.close()
