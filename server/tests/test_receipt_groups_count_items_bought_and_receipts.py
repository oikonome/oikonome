"""A Receipts group says how many were bought and on how many receipts.

The group's one count was its number of LINES, printed as "Times": four
bottles on one line read "1", and a store visited twice with several
lines a visit read as many times as it had lines. The server now sends
`bought` (quantity) and `receipts` (slips).
"""
import datetime as dt
import unittest

from oikonome.engine import budget, receipts

from .test_a_receipt_on_a_retired_hold_is_never_lost import PNG, _vision
from .util import add_txn, make_db, write_config

DAY = dt.date.today() - dt.timedelta(days=3)


def _slip(items, total):
    return {"merchant": "Fernhill Grocer", "date": DAY.isoformat(),
            "total": total, "tax": None, "tip": None, "items": items}


class GroupCountsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        cfg = budget.load_config(self.conn)
        cfg["llm_url"] = "http://llm.test"
        cfg["llm_model"] = "vision-model"
        budget.save_config(self.conn, cfg)
        add_txn(self.conn, DAY, 30.00, "FERNHILL GROCER", txn_id="visit-1")
        add_txn(self.conn, DAY, 12.50, "FERNHILL GROCER", txn_id="visit-2")
        self._attach("visit-1", _slip(
            [{"description": "sparkling water", "qty": 4, "amount": 20.00},
             {"description": "rye bread", "qty": None, "amount": 4.00},
             {"description": "apples", "qty": 1.5, "amount": 6.00}], 30.00))
        self._attach("visit-2", _slip(
            [{"description": "sparkling water", "qty": 2, "amount": 10.00},
             {"description": "rye bread", "qty": None, "amount": 2.50}],
            12.50))

    def _attach(self, txn_id, payload):
        rid = receipts.add(self.conn, txn_id, PNG + txn_id.encode(),
                           "image/png")
        out = receipts.parse_one(self.conn, rid, transport=_vision(payload))
        assert out["status"] == "parsed", out

    def _groups(self, group):
        return {g["key"]: g for g in receipts.search_items(
            self.conn, group=group)["groups"]}

    def test_an_item_counts_its_quantity_and_its_receipts(self):
        g = self._groups("item")
        self.assertEqual((g["sparkling water"]["bought"],
                          g["sparkling water"]["receipts"]), (6, 2))
        # no quantity, or a weighed one, is one bought
        self.assertEqual((g["rye bread"]["bought"],
                          g["rye bread"]["receipts"]), (2, 2))
        self.assertEqual((g["apples"]["bought"], g["apples"]["receipts"]),
                         (1, 1))

    def test_a_store_counts_its_trips_as_receipts(self):
        g = self._groups("merchant")
        store = next(iter(g.values()))
        self.assertEqual(store["receipts"], 2)
        self.assertEqual(store["bought"], 6 + 2 + 1)


if __name__ == "__main__":
    unittest.main()
