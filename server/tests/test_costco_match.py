"""Costco receipt↔transaction matcher: cent-exact matching, the tighter
warehouse date window, sign correspondence, the receipt's dominant category
carried as the override, NO stamp on the store's unmatched charges (fuel
and membership already carry a real category), and the LOAD-BEARING
ordering — manual_categories re-applied LAST. Plus the push door's
refresh semantics: a re-push with better categories reaches stored rows."""

import unittest

from oikonome.engine import costco_match
from oikonome.engine.compat import as_date
from oikonome.sync import costco_receipts

from .util import add_txn, make_db


def add_receipt(conn, dedup_key, date, amount, category, summary="",
                payment_method="", is_refund=0, rtype="warehouse"):
    conn.execute(
        """INSERT INTO costco_receipts (dedup_key, account, date, amount,
               receipt_type, category, summary, is_refund, payment_method)
           VALUES (%s,'main',%s,%s,%s,%s,%s,%s,%s)""",
        (dedup_key, as_date(date), amount, rtype, category, summary,
         is_refund, payment_method))


def override(conn, txn_id):
    return conn.execute(
        "SELECT category_override FROM transactions WHERE id=%s",
        (txn_id,)).fetchone()["category_override"]


class CostcoMatchTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_same_day_receipt_matches_and_carries_its_category(self):
        add_receipt(self.conn, "r1", "2025-07-10", -312.44, "Food & Drink",
                    summary="groceries $210 · household $102")
        t = add_txn(self.conn, "2025-07-10", 312.44, "COSTCO WHSE #0123")
        res = costco_match.run_match(self.conn)
        self.assertEqual(res["matched"], 1)
        self.assertEqual(override(self.conn, t), "Costco - Food & Drink")
        m = self.conn.execute(
            "SELECT dedup_key FROM costco_matches WHERE transaction_id=%s",
            (t,)).fetchone()
        self.assertEqual(m["dedup_key"], "r1")

    def test_unmatched_costco_charge_keeps_its_bank_category(self):
        # fuel and the membership fee are described well enough by the
        # aggregator; an "Unmatched" stamp would only hide that
        t = add_txn(self.conn, "2025-07-10", 61.20, "COSTCO GAS #0123",
                    primary="TRANSPORTATION")
        res = costco_match.run_match(self.conn)
        self.assertEqual(res["unmatched"], 1)
        self.assertIsNone(override(self.conn, t))

    def test_non_costco_rows_untouched(self):
        t = add_txn(self.conn, "2025-07-10", 23.45, "SAFEWAY STORE")
        add_receipt(self.conn, "r1", "2025-07-08", -23.45, "Food & Drink")
        costco_match.run_match(self.conn)
        self.assertIsNone(override(self.conn, t))

    def test_posting_may_trail_the_receipt_by_a_few_days_only(self):
        add_receipt(self.conn, "r1", "2025-07-01", -50.00, "Home")
        late = add_txn(self.conn, "2025-07-09", 50.00, "COSTCO WHSE")  # +8
        ok = add_txn(self.conn, "2025-07-04", 50.00, "COSTCO WHSE")    # +3
        costco_match.run_match(self.conn)
        self.assertEqual(override(self.conn, ok), "Costco - Home")
        self.assertIsNone(override(self.conn, late))

    def test_refund_sign_correspondence(self):
        add_receipt(self.conn, "r1", "2025-07-10", 40.00, "Refunds",
                    is_refund=1)
        charge = add_txn(self.conn, "2025-07-10", 40.00, "COSTCO WHSE")
        refund = add_txn(self.conn, "2025-07-11", -40.00, "COSTCO WHSE")
        costco_match.run_match(self.conn)
        self.assertIsNone(override(self.conn, charge))
        self.assertEqual(override(self.conn, refund), "Costco - Refunds")

    def test_manual_category_wins_over_a_match(self):
        add_receipt(self.conn, "r1", "2025-07-10", -80.00, "Apparel")
        t = add_txn(self.conn, "2025-07-10", 80.00, "COSTCO WHSE")
        self.conn.execute(
            "INSERT INTO manual_categories (transaction_id, category) "
            "VALUES (%s, 'GENERAL_MERCHANDISE')", (t,))
        costco_match.run_match(self.conn)
        self.assertEqual(override(self.conn, t), "GENERAL_MERCHANDISE")

    def test_rerun_is_idempotent_and_drops_a_removed_receipt(self):
        add_receipt(self.conn, "r1", "2025-07-10", -80.00, "Apparel")
        t = add_txn(self.conn, "2025-07-10", 80.00, "COSTCO WHSE")
        costco_match.run_match(self.conn)
        costco_match.run_match(self.conn)
        self.assertEqual(override(self.conn, t), "Costco - Apparel")
        self.conn.execute("DELETE FROM costco_receipts WHERE dedup_key='r1'")
        costco_match.run_match(self.conn)
        self.assertIsNone(override(self.conn, t))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM costco_matches").fetchone()["n"], 0)

    def test_amazon_override_survives_a_costco_run(self):
        # each store clears only ITS OWN prefix
        t = add_txn(self.conn, "2025-07-10", 20.00, "AMAZON.COM",
                    override="Amazon - Apparel")
        costco_match.run_match(self.conn)
        self.assertEqual(override(self.conn, t), "Amazon - Apparel")


class CostcoImportDoorTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _push(self, rows):
        return costco_receipts.import_receipts(self.conn, rows)

    def test_push_matches_and_a_repush_refreshes_categories(self):
        t = add_txn(self.conn, "2025-07-10", 99.10, "COSTCO WHSE")
        row = {"dedup_key": "main|w|2025-07-10|C|99.10", "date": "2025-07-10",
               "amount": -99.10, "category": "Shopping",
               "summary": "shopping $99",
               "items": [{"description": "KS ORG EGGS", "amount": 9.10,
                          "department": 17, "category": "Shopping"}]}
        out = self._push([row])
        self.assertEqual(out["new"], 1)
        self.assertEqual(out["costco_match"]["matched"], 1)
        self.assertEqual(override(self.conn, t), "Costco - Shopping")
        # the host's department map improved: the same receipt comes back
        # with a better category and reaches the stored row
        row2 = dict(row, category="Food & Drink", summary="groceries $99")
        row2["items"][0]["category"] = "Food & Drink"
        out = self._push([row2])
        self.assertEqual(out["new"], 0)
        self.assertEqual(out["refreshed"], 1)
        self.assertEqual(override(self.conn, t), "Costco - Food & Drink")
        # an identical re-push changes nothing and skips the matcher
        out = self._push([row2])
        self.assertEqual(out["refreshed"], 0)
        self.assertIn("skipped", out["costco_match"])

    def test_bad_rows_are_skipped_not_fatal(self):
        out = self._push([
            {"dedup_key": "bad-date", "date": "not-a-date", "amount": -1},
            {"dedup_key": "bad-amount", "date": "2025-07-10", "amount": True},
            {"dedup_key": "ok", "date": "2025-07-10", "amount": -12.5,
             "receipt_type": "made-up"}])
        self.assertEqual(out["new"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT receipt_type FROM costco_receipts WHERE dedup_key='ok'"
        ).fetchone()["receipt_type"], "warehouse")

    def test_empty_payload_refused(self):
        with self.assertRaises(ValueError):
            self._push([])


if __name__ == "__main__":
    unittest.main()
