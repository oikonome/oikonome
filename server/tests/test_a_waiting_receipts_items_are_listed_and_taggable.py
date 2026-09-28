"""A receipt waiting for its charge shows its line items, and they tag.

A receipt snapped before its charge reaches the ledger is read at once,
so its lines are real purchases — but the Items page joined every line to
its transaction, so those lines were invisible (and could not be tagged
from there) until a charge arrived, which for a cash purchase is never.
They are listed with the store and date the receipt itself printed; a
line whose charge was removed from the ledger stays hidden. A receipt that
printed no store totals under the name the clients show for one, and a
tagged waiting line reaches the tag's expense report."""

import json
import unittest

import httpx

from oikonome.engine import receipts

from .test_receipts import LLM_JSON, _enable_llm
from .util import TODAY, add_txn, make_db, write_config


def _reply(payload):
    return httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(payload)}}]}))


WAITING = {"merchant": "CORNER HARDWARE", "date": "2026-07-12",
           "total": 41.00, "tax": None, "tip": None,
           "items": [{"description": "WOOD GLUE", "qty": 1, "amount": 9.00},
                     {"description": "CLAMPS", "qty": 2, "amount": 32.00}]}


class WaitingItems(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)
        self.rid = receipts.add(self.conn, None, b"\x89PNG...", "image/png")
        out = receipts.parse_one(self.conn, self.rid,
                                 transport=_reply(WAITING))
        self.assertEqual(out["status"], "parsed")
        self.assertIsNone(self.conn.execute(
            "SELECT txn_id FROM receipts WHERE id=%s::uuid",
            (self.rid,)).fetchone()["txn_id"])

    def tearDown(self):
        self.conn.close()

    def test_the_lines_are_listed_with_the_receipts_store_and_date(self):
        rows = receipts.search_items(self.conn, q="glue")["rows"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["txn_id"])
        self.assertEqual(rows[0]["payee"], "Corner Hardware")
        self.assertEqual(str(rows[0]["date"]), "2026-07-12")
        groups = receipts.search_items(self.conn, group="merchant")["groups"]
        self.assertEqual(groups[0]["key"], "Corner Hardware")
        self.assertEqual(groups[0]["total"], 41.00)
        months = receipts.search_items(self.conn, group="month")["groups"]
        self.assertEqual(months[0]["key"], "2026-07")

    def test_a_waiting_line_can_be_tagged(self):
        self.assertEqual(receipts.set_tag(self.conn, self.rid, 1, "shop"), 1)
        row = receipts.search_items(self.conn, q="clamps")["rows"][0]
        self.assertEqual(row["tag"], "shop")

    def test_a_receipt_without_a_printed_date_uses_the_snap_day(self):
        self.conn.execute(
            "UPDATE receipts SET parsed = parsed - 'date' WHERE id=%s::uuid",
            (self.rid,))
        row = receipts.search_items(self.conn, q="glue")["rows"][0]
        snapped = self.conn.execute(
            "SELECT created_at::date AS d FROM receipts WHERE id=%s::uuid",
            (self.rid,)).fetchone()["d"]
        self.assertEqual(row["date"], snapped)

    def test_a_date_that_is_not_one_does_not_fail_the_page(self):
        self.conn.execute(
            "UPDATE receipts SET parsed = jsonb_set(parsed, '{date}',"
            " '\"2026-02-30\"') WHERE id=%s::uuid", (self.rid,))
        self.assertEqual(
            len(receipts.search_items(self.conn, q="glue")["rows"]), 1)

    def test_lines_of_a_removed_charge_stay_hidden(self):
        txn = add_txn(self.conn, TODAY, 63.00, "BIGBOX WHOLESALE")
        rid = receipts.add(self.conn, txn, b"\x89PNG...", "image/png")
        receipts.parse_one(self.conn, rid, transport=_reply(LLM_JSON))
        self.assertEqual(
            len(receipts.search_items(self.conn, q="paper")["rows"]), 1)
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s",
                          (txn,))
        self.assertEqual(
            receipts.search_items(self.conn, q="paper")["rows"], [])
        # the waiting receipt is unaffected
        self.assertEqual(
            len(receipts.search_items(self.conn, q="glue")["rows"]), 1)

    def test_a_waiting_receipt_with_no_store_groups_as_unknown(self):
        self.conn.execute(
            "UPDATE receipts SET parsed = parsed - 'merchant'"
            " WHERE id=%s::uuid", (self.rid,))
        groups = receipts.search_items(self.conn, group="merchant")["groups"]
        self.assertEqual([g["key"] for g in groups], ["Unknown store"])
        self.assertEqual(groups[0]["total"], 41.00)

    def test_a_tagged_waiting_line_reaches_the_expense_report(self):
        self.assertEqual(receipts.set_tag(self.conn, self.rid, 1, "shop"), 1)
        rep = receipts.report(self.conn, "shop", 2026, 7)
        self.assertEqual(rep["count"], 1)
        self.assertEqual(rep["total"], 32.00)
        row = rep["rows"][0]
        self.assertIsNone(row["txn_id"])
        self.assertEqual(row["payee"], "Corner Hardware")
        self.assertEqual(str(row["date"]), "2026-07-12")
        # its printed month only
        self.assertEqual(receipts.report(self.conn, "shop", 2026, 8)["count"],
                         0)

    def test_a_tagged_line_of_a_removed_charge_stays_off_the_report(self):
        txn = add_txn(self.conn, TODAY, 63.00, "BIGBOX WHOLESALE")
        rid = receipts.add(self.conn, txn, b"\x89PNG...", "image/png")
        receipts.parse_one(self.conn, rid, transport=_reply(LLM_JSON))
        self.assertEqual(receipts.set_tag(self.conn, rid, 0, "shop"), 1)
        y, m = TODAY.year, TODAY.month
        self.assertEqual(receipts.report(self.conn, "shop", y, m)["count"], 1)
        self.conn.execute("UPDATE transactions SET removed=1 WHERE id=%s",
                          (txn,))
        self.assertEqual(receipts.report(self.conn, "shop", y, m)["count"], 0)


if __name__ == "__main__":
    unittest.main()
