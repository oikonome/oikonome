"""A receipt's lines count once, at what each line cost, and say when
they do not add up to the charge.

What breaks if these fail: the Receipts page totals a store at twice its
charges because one photo was attached twice; "4 packs at $6.25" reads
as $6.25; the card the receipt was paid with is listed as a purchase and
the fee beside it as a refund; and nothing says the lines miss the charge
by a discount the reading dropped (specs/receipts.md).
"""

import json
import unittest

import httpx

from oikonome.engine import receipts

from .test_receipts import _enable_llm
from .util import TODAY, add_txn, make_db, write_config


def _reply(payload):
    return httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(payload)}}]}))


def _paper(items, total, tax=None, merchant="Fernhill Grocer"):
    return {"merchant": merchant, "date": TODAY.isoformat(), "total": total,
            "tax": tax, "tip": None, "items": items}


class ReconcileTests(unittest.TestCase):
    """The rule itself, on stored lines."""

    def _lines(self, items, parsed, charge):
        got = receipts.reconcile(
            [{"line": i, **it} for i, it in enumerate(items)], parsed, charge)
        return [(ln["description"], ln["amount"]) for ln in got["lines"]
                if ln["counted"]], got["gap"]

    def test_a_unit_price_is_multiplied_out_when_the_total_says_so(self):
        lines, gap = self._lines(
            [{"description": "SPARKLING 12PK", "qty": 4, "amount": 6.25},
             {"description": "RYE BREAD", "qty": None, "amount": 3.50}],
            {"total": 30.30, "tax": 1.80}, 30.30)
        self.assertEqual(lines, [("SPARKLING 12PK", 25.0), ("RYE BREAD", 3.5)])
        self.assertIsNone(gap)

    def test_a_line_total_with_its_quantity_stays_as_read(self):
        lines, gap = self._lines(
            [{"description": "LIMES", "qty": 3, "amount": 1.50}],
            {"total": 1.50, "tax": None}, 1.50)
        self.assertEqual(lines, [("LIMES", 1.5)])
        self.assertIsNone(gap)

    def test_a_payment_line_is_not_an_item_and_a_negative_fee_is_a_fee(self):
        lines, gap = self._lines(
            [{"description": "card payment", "qty": None,
              "amount": 75.0},
             {"description": "license renewal", "qty": None,
              "amount": -75.0}],
            {"total": 77.25, "tax": None}, 77.25)
        self.assertEqual(lines, [("license renewal", 75.0)])
        # the convenience fee the reading missed
        self.assertEqual(gap, 2.25)

    def test_lines_short_of_the_charge_say_by_how_much(self):
        _, gap = self._lines(
            [{"description": "OATS", "qty": None, "amount": 70.00},
             {"description": "HONEY", "qty": None, "amount": 45.30}],
            {"total": 121.50, "tax": 6.20}, 117.75)
        self.assertEqual(gap, -3.75)

    def test_a_slip_on_a_refund_counts_negative(self):
        # a return slip printed negative stays negative on the refund
        lines, gap = self._lines(
            [{"description": "HONEY JAR", "qty": None, "amount": -14.50},
             {"description": "EMBER TEA", "qty": None, "amount": -6.00}],
            {"total": -20.50, "tax": None}, -20.50)
        self.assertEqual(lines, [("HONEY JAR", -14.5), ("EMBER TEA", -6.0)])
        self.assertIsNone(gap)
        # one printed positive is negated: it is money coming back
        lines, gap = self._lines(
            [{"description": "HONEY JAR", "qty": None, "amount": 14.50},
             {"description": "EMBER TEA", "qty": None, "amount": 6.00}],
            {"total": 20.50, "tax": None}, -20.50)
        self.assertEqual(lines, [("HONEY JAR", -14.5), ("EMBER TEA", -6.0)])
        self.assertIsNone(gap)

    def test_a_refund_slip_read_with_purchase_signs_and_a_coupon(self):
        # the slip's net sign decides, never its smallest line's: the
        # returned item comes back negative and its coupon positive
        lines, gap = self._lines(
            [{"description": "HONEY JAR", "qty": None, "amount": 16.00},
             {"description": "COUPON", "qty": None, "amount": -1.50}],
            {"total": 14.50, "tax": None}, -14.50)
        self.assertEqual(lines, [("HONEY JAR", -16.0), ("COUPON", 1.5)])
        self.assertIsNone(gap)

    def test_a_coupon_on_a_charge_or_waiting_stays_as_read(self):
        items = [{"description": "HONEY JAR", "qty": None, "amount": 16.00},
                 {"description": "COUPON", "qty": None, "amount": -1.50}]
        for charge in (14.50, None):
            lines, gap = self._lines(items, {"total": 14.50}, charge)
            self.assertEqual(lines, [("HONEY JAR", 16.0), ("COUPON", -1.5)])
            self.assertIsNone(gap)

    def test_lines_short_of_a_refund_say_by_how_much(self):
        # the gap is in the size of the money, as on a charge: positive
        # means the lines come to less than the refund
        _, gap = self._lines(
            [{"description": "HONEY JAR", "qty": None, "amount": -14.50}],
            {"total": -20.50, "tax": None}, -20.50)
        self.assertEqual(gap, 6.0)

    def test_negative_lines_on_a_charge_or_waiting_still_count_positive(self):
        items = [{"description": "HONEY JAR", "qty": None, "amount": -14.50}]
        for charge in (14.50, None):
            lines, gap = self._lines(items, {"total": 14.50}, charge)
            self.assertEqual(lines, [("HONEY JAR", 14.5)])
            self.assertIsNone(gap)


class CountedOnceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def _add(self, txn, payload, image=b"\x89PNG-one-photo"):
        rid = receipts.add(self.conn, txn, image, "image/png")
        out = receipts.parse_one(self.conn, rid, transport=_reply(payload))
        self.assertEqual(out["status"], "parsed")
        return rid

    def test_the_same_photo_twice_on_a_charge_counts_once(self):
        txn = add_txn(self.conn, TODAY, 41.10, "Fernhill Grocer")
        paper = _paper([{"description": "CHEDDAR", "qty": 1, "amount": 20.55},
                        {"description": "SALMON", "qty": 1, "amount": 20.55}],
                       41.10)
        first = self._add(txn, paper)
        second = self._add(txn, paper)
        got = receipts.search_items(self.conn, group="merchant")
        # the copy's lines are listed, said not to count, and not added up
        self.assertEqual([(r["counts"], r["why"]) for r in got["rows"]
                          if r["receipt_id"] == second],
                         [(False, "same_slip")] * 2)
        self.assertEqual(len([r for r in got["rows"] if r["counts"]]), 2)
        [g] = got["groups"]
        self.assertEqual((g["n"], g["total"]), (2, 41.10))
        panel = {r["id"]: r for r in receipts.for_txn(self.conn, txn)}
        self.assertEqual(panel[second]["duplicate_of"], first)
        self.assertEqual(panel[second]["items"], [])
        self.assertEqual(len(panel[second]["listed_items"]), 2)
        self.assertIsNone(panel[first]["duplicate_of"])
        self.assertIsNone(panel[first]["lines_gap"])

    def test_no_stored_image_is_hashed_inside_a_join_or_filter(self):
        # hashing every stored image per request is seconds on a real
        # household: the counting reads the stored slip, never a hash of
        # an image
        txn = add_txn(self.conn, TODAY, 41.10, "Fernhill Grocer")
        paper = _paper([{"description": "CHEDDAR", "qty": 1,
                         "amount": 41.10}], 41.10)
        self._add(txn, paper)
        self._add(txn, paper)
        sql, args, _hit = receipts._counted_lines_query(
            self.conn, "r.txn_id IS NOT NULL", [])
        plan = [r[0] if not isinstance(r, dict) else next(iter(r.values()))
                for r in self.conn.execute("EXPLAIN (VERBOSE) " + sql,
                                           args).fetchall()]
        bad = [ln for ln in plan if ("md5(" in ln or "sha256(" in ln)]
        self.assertEqual(bad, [])
        self.assertEqual(receipts.search_items(
            self.conn, group="merchant")["groups"][0]["total"], 41.10)

    def test_a_different_photo_on_the_same_charge_still_counts(self):
        txn = add_txn(self.conn, TODAY, 30.00, "Fernhill Grocer")
        self._add(txn, _paper([{"description": "TEA", "qty": 1,
                                "amount": 10.0}], 10.0), image=b"\x89PNG-a")
        self._add(txn, _paper([{"description": "JAM", "qty": 1,
                                "amount": 20.0}], 20.0), image=b"\x89PNG-b")
        got = receipts.search_items(self.conn, group="merchant")
        self.assertEqual(got["groups"][0]["total"], 30.0)
        self.assertEqual([r["duplicate_of"]
                          for r in receipts.for_txn(self.conn, txn)],
                         [None, None])

    def test_the_page_totals_what_the_lines_cost(self):
        txn = add_txn(self.conn, TODAY, 30.30, "Fernhill Grocer")
        self._add(txn, _paper(
            [{"description": "SPARKLING 12PK", "qty": 4, "amount": 6.25},
             {"description": "RYE BREAD", "qty": None, "amount": 3.50}],
            30.30, tax=1.80))
        got = receipts.search_items(self.conn, q="sparkling")
        [row] = got["rows"]
        self.assertEqual((row["qty"], row["amount"]), (4, 25.0))
        self.assertEqual(got["groups"][0]["total"], 25.0)

    def test_the_receipt_panel_says_when_lines_miss_the_charge(self):
        txn = add_txn(self.conn, TODAY, 117.75, "Fernhill Grocer")
        self._add(txn, _paper(
            [{"description": "OATS", "qty": None, "amount": 70.00},
             {"description": "HONEY", "qty": None, "amount": 45.30}],
            121.50, tax=6.20))
        [r] = receipts.for_txn(self.conn, txn)
        self.assertEqual(r["lines_gap"], -3.75)

    def test_a_payment_line_never_reaches_the_itemized_report(self):
        txn = add_txn(self.conn, TODAY, 77.25, "County Clerk")
        rid = self._add(txn, _paper(
            [{"description": "card payment", "qty": None,
              "amount": 75.0},
             {"description": "license renewal", "qty": None,
              "amount": -75.0}], 77.25, merchant="County Clerk"))
        for line in (0, 1):
            receipts.set_tag(self.conn, rid, line, "business")
        rep = receipts.report(self.conn, "business", TODAY.year, TODAY.month)
        self.assertEqual([(r["description"], r["amount"]) for r in rep["rows"]],
                         [("license renewal", 75.0)])
        self.assertEqual(rep["total"], 75.0)

    def test_a_returned_item_nets_to_nothing(self):
        bought = add_txn(self.conn, TODAY, 20.50, "Fernhill Grocer")
        refund = add_txn(self.conn, TODAY, -14.50, "Fernhill Grocer")
        first = self._add(bought, _paper(
            [{"description": "HONEY JAR", "qty": 1, "amount": 14.50},
             {"description": "EMBER TEA", "qty": 1, "amount": 6.00}],
            20.50), image=b"\x89PNG-bought")
        back = self._add(refund, _paper(
            [{"description": "HONEY JAR", "qty": 1,
              "amount": -14.50}], -14.50), image=b"\x89PNG-returned")
        receipts.set_tag(self.conn, first, 0, "business")
        receipts.set_tag(self.conn, back, 0, "business")
        got = receipts.search_items(self.conn, q="honey jar", group="merchant")
        self.assertEqual(sorted(r["amount"] for r in got["rows"]),
                         [-14.5, 14.5])
        self.assertEqual(got["groups"][0]["total"], 0.0)
        # bought and brought back: none kept
        self.assertEqual(got["groups"][0]["bought"], 0)
        rep = receipts.report(self.conn, "business", TODAY.year, TODAY.month)
        self.assertEqual(rep["total"], 0.0)
        [panel] = receipts.for_txn(self.conn, refund)
        self.assertEqual([i["amount"] for i in panel["items"]], [-14.5])
        self.assertIsNone(panel["lines_gap"])


if __name__ == "__main__":
    unittest.main()
