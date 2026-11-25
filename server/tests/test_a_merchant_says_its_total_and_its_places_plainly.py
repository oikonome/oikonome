"""A merchant's panel says its total with its direction, what its category
rule does in words, and one place once however the feed spells it.

The total printed bare ("-$1,200" beside "$5,400"), the rule as "seed rule
→ General Merchandise", and one town spelled two ways was two places.
"""
import unittest

from oikonome.engine import merchant_dedup

from .test_llm_categorize import add_raw_txn
from .util import make_db


class MerchantPanelWordsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        for i, city in enumerate(("Lake O'Brien", "Lake O Brien", "lake obrien",
                                  "Ridgeport")):
            add_raw_txn(self.conn, f"p{i}", f"2026-08-0{i + 1}", 25,
                        "INVENTED HARDWARE CO", primary="GENERAL_MERCHANDISE",
                        raw={})
            self.conn.execute(
                "UPDATE transactions SET location_city=%s, "
                "location_region='ZZ' WHERE id=%s", (city, f"p{i}"))
        add_raw_txn(self.conn, "pay1", "2026-08-03", -600, "INVENTED PAYROLL",
                    primary="INCOME", raw={})

    def tearDown(self):
        self.conn.close()

    def test_one_place_spelled_three_ways_is_one_place(self):
        d = merchant_dedup.detail(self.conn, "INVENTED HARDWARE CO")
        self.assertEqual(sorted(l["n"] for l in d["locations"]), [1, 3])
        town = next(l for l in d["locations"] if l["n"] == 3)
        self.assertIn(town["city"], ("Lake O'Brien", "Lake O Brien",
                                     "lake obrien"))

    def test_the_total_says_which_way_the_money_went(self):
        self.assertEqual(merchant_dedup.total_label(1234.4), "$1,234 spent")
        self.assertEqual(merchant_dedup.total_label(-600), "$600 received")
        d = merchant_dedup.detail(self.conn, "INVENTED HARDWARE CO")
        self.assertEqual(d["total_label"], "$100 spent")

    def test_a_line_of_transfers_and_payments_is_not_called_spent(self):
        # a card payment or a transfer between accounts is money moved,
        # not spent at a merchant nor received from one; the list said
        # "$N received" beside a card's payment credit
        for i in range(3):
            add_raw_txn(self.conn, f"cp{i}", f"2026-08-1{i}", -500,
                        "INVENTED CARD PAYMENT", primary="TRANSFER_IN",
                        raw={})
        rows = {r["display"]: merchant_dedup.listing_words(r)
                for r in merchant_dedup.listing(self.conn)}
        pay = next(v for k, v in rows.items() if "CARD PAYMENT" in k.upper())
        self.assertEqual(pay["total_label"],
                         "$1,500 in transfers and payments")
        hw = next(v for k, v in rows.items() if "HARDWARE" in k.upper())
        self.assertEqual(hw["total_label"], "$100 spent")
        wage = next(v for k, v in rows.items() if "PAYROLL" in k.upper())
        self.assertEqual(wage["total_label"], "$600 received")
        # the panel the line opens says the same words
        for name, row in rows.items():
            self.assertEqual(
                merchant_dedup.detail(self.conn, name)["total_label"],
                row["total_label"], name)

    def test_a_line_mostly_of_payments_or_interest_is_said_so(self):
        """A card issuer's line carries its payments and a fee or two; a
        "Transfer from …" line can carry a row the feed called income; a
        bank's "Interest …" line is often uncategorized. Each was called
        spent or received like a merchant. The list and the panel say
        what most of the line is, by one rule."""
        for i in range(3):
            add_raw_txn(self.conn, f"ip{i}", f"2026-08-1{i}", 400,
                        "INVENTED CARD CO", primary="LOAN_PAYMENTS", raw={})
        add_raw_txn(self.conn, "fee", "2026-08-15", 90, "INVENTED CARD CO",
                    primary="GENERAL_SERVICES", raw={})
        for i in range(2):
            add_raw_txn(self.conn, f"it{i}", f"2026-08-2{i}", -7,
                        "Interest Earned", primary=None, raw={})
        rows = {r["display"].upper(): merchant_dedup.listing_words(r)
                for r in merchant_dedup.listing(self.conn)}
        card = next(v for k, v in rows.items() if "CARD CO" in k)
        self.assertEqual(card["total_label"],
                         "$1,290, mostly transfers and payments")
        intr = next(v for k, v in rows.items() if "INTEREST" in k)
        self.assertEqual(intr["total_label"], "$14 in interest")
        hw = next(v for k, v in rows.items() if "HARDWARE" in k)
        self.assertEqual(hw["total_label"], "$100 spent")
        for name, row in ((card["display"], card), (intr["display"], intr)):
            self.assertEqual(
                merchant_dedup.detail(self.conn, name)["total_label"],
                row["total_label"], name)

    def test_mostly_is_decided_by_the_dollars(self):
        """A lender's line with a few large loan payments and many small
        purchases is mostly payments by its dollars, though most of its
        rows are purchases; a line of many small transfers and one large
        purchase is mostly spending."""
        for i in range(2):
            add_raw_txn(self.conn, f"ln{i}", f"2026-08-1{i}", 900,
                        "INVENTED AUTO FINANCE", primary="LOAN_PAYMENTS",
                        raw={})
        for i in range(5):
            add_raw_txn(self.conn, f"lp{i}", f"2026-08-2{i}", 20,
                        "INVENTED AUTO FINANCE", primary="GENERAL_SERVICES",
                        raw={})
        for i in range(3):
            add_raw_txn(self.conn, f"tx{i}", f"2026-08-1{i}", 10,
                        "INVENTED GADGET BARN", primary="TRANSFER_OUT",
                        raw={})
        add_raw_txn(self.conn, "big", "2026-08-25", 800,
                    "INVENTED GADGET BARN", primary="GENERAL_MERCHANDISE",
                    raw={})
        rows = {r["display"].upper(): merchant_dedup.listing_words(r)
                for r in merchant_dedup.listing(self.conn)}
        lender = next(v for k, v in rows.items() if "AUTO FINANCE" in k)
        self.assertEqual(lender["total_label"],
                         "$1,900, mostly transfers and payments")
        shop = next(v for k, v in rows.items() if "GADGET BARN" in k)
        self.assertEqual(shop["total_label"], "$830 spent")
        for row in (lender, shop):
            self.assertEqual(
                merchant_dedup.detail(self.conn, row["display"])["total_label"],
                row["total_label"], row["display"])
            # the dollars decide the words; the reply keeps its keys
            self.assertNotIn("moved_dollars", row)

    def test_a_line_naming_interest_after_another_word_is_interest(self):
        for i in range(2):
            add_raw_txn(self.conn, f"ci{i}", f"2026-08-2{i}", -6,
                        "Credit Interest", primary=None, raw={})
        rows = {r["display"].upper(): merchant_dedup.listing_words(r)
                for r in merchant_dedup.listing(self.conn)}
        intr = next(v for k, v in rows.items() if "INTEREST" in k)
        self.assertEqual(intr["total_label"], "$12 in interest")

    def test_the_rule_says_what_it_does_for_new_charges(self):
        from oikonome.web import data
        data.set_merchant_category(self.conn, "INVENTED HARDWARE CO",
                                   "FOOD_AND_DRINK")
        d = merchant_dedup.detail(self.conn, "INVENTED HARDWARE CO")
        self.assertEqual(d["rule"]["label"],
                         "new charges go to Food and Drink (your rule)")
        self.assertEqual(merchant_dedup.rule_label(
            {"source": "seed", "category_primary": "GENERAL_MERCHANDISE"}),
            "new charges go to General Merchandise (a built-in rule)")


    def test_a_recent_change_says_what_the_name_was_and_is(self):
        w = merchant_dedup.rename_words(
            {"raw_merchant": "Acme Fuel Co", "from_canonical": "Acme Fuel",
             "to_canonical": "Acme Fuel Co"})
        self.assertEqual(w, {"label": "Acme Fuel → Acme Fuel Co",
                             "line": None})
        w = merchant_dedup.rename_words(
            {"raw_merchant": "ACME FUEL 0042", "from_canonical": None,
             "to_canonical": "Acme Fuel"})
        self.assertEqual(w["label"], "ACME FUEL 0042 → Acme Fuel")
        self.assertIsNone(w["line"])
        w = merchant_dedup.rename_words(
            {"raw_merchant": "ACME FUEL 0042", "from_canonical": "Acme",
             "to_canonical": "Acme Fuel"})
        self.assertEqual(w["line"], "bank line “ACME FUEL 0042”")


if __name__ == "__main__":
    unittest.main()
