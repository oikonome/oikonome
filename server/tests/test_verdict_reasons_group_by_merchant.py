"""`/api/today`'s reasons name each over-budget bucket's top merchants
the way `/api/today/full`'s Why does: by the merchant the ledger names,
not by the bank's raw descriptor. A merchant whose rows carry two
spellings ("Orchard Mkt #12", "ORCHARD MARKET") was split into two
smaller totals, so an integration reading the verdict saw neither one as
the driver. Figures are invented."""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import TODAY, add_txn, make_db, write_config


class ReasonsByMerchantTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=200, other_monthly=50)
        mid = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Orchard Market', 'manual') RETURNING id").fetchone()["id"]
        for day, name in ((2, "ORCHARD MKT #12"), (5, "ORCHARD MARKET"),
                          (8, "ORCHARD MKT #12"), (11, "ORCHARD MARKET")):
            t = add_txn(self.conn, dt.date(2026, 7, day), 70.0, name,
                        primary="FOOD_AND_DRINK")
            self.conn.execute("UPDATE transactions SET merchant_id = %s "
                              "WHERE id = %s", (mid, t))
        add_txn(self.conn, dt.date(2026, 7, 6), 45.0, "CORNER CAFE",
                primary="FOOD_AND_DRINK")

    def tearDown(self):
        self.conn.close()

    def test_one_merchant_is_one_reason_line(self):
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["verdict"], "OVER BUDGET")
        food = next(r for r in st["reasons"] if r["bucket"] == "food")
        self.assertEqual(food["detail"][0], "Orchard Market $280 (4×)")
        self.assertFalse([d for d in food["detail"] if "ORCHARD" in d])
        why = next(e for e in st["why"]["entries"] if e["name"] == "Food")
        self.assertEqual((why["top"][0]["payee"], why["top"][0]["count"]),
                         ("Orchard Market", 4))


if __name__ == "__main__":
    unittest.main()
