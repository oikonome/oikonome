"""The receipt reader takes the object that answers the prompt's schema.

A reply can carry more than one JSON object: a model may echo the
prompt's "unreadable" example ({"items": []}) before answering, or copy a
JSON-looking line printed on the paper itself ({"total": 1.00}). Taking
the first object that named any expected field let such a fragment set
the receipt's total — and a waiting receipt's total is what the matcher
pairs a charge by — or wipe every line item. The object carrying the most
of the prompt's fields wins, the later one on a tie."""

import json
import unittest

import httpx

from oikonome.engine import llm_categorize, receipts

from .test_receipts import LLM_JSON, _enable_llm
from .util import TODAY, add_txn, make_db, write_config


def _reply(content):
    return httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": content}}]}))


ANSWER = json.dumps(LLM_JSON)


class Shapes(unittest.TestCase):
    keys = receipts._RECEIPT_KEYS

    def test_json_printed_on_the_receipt_does_not_set_the_total(self):
        for reply in (
                'The receipt shows {"total": 1.00} in its footer. ' + ANSWER,
                ANSWER + ' (the footer also prints {"total": 1.00})'):
            got = llm_categorize.json_answer(reply, self.keys)
            self.assertEqual(got["total"], 63.00, reply)

    def test_an_echoed_fallback_example_does_not_empty_the_items(self):
        reply = ('If unreadable I would return {"items": []}, but here it '
                 'is: ' + ANSWER)
        got = llm_categorize.json_answer(reply, self.keys)
        self.assertEqual(len(got["items"]), 3)

    def test_the_fallback_alone_is_still_the_answer(self):
        self.assertEqual(
            llm_categorize.json_answer('{"items": []}', self.keys),
            {"items": []})

    def test_on_a_tie_the_later_object_wins(self):
        reply = 'Example: {"year": 1999}. Answer: {"year": 2025}'
        self.assertEqual(llm_categorize.json_answer(reply, ("year",)),
                         {"year": 2025})


class ParsedReceipt(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)
        self.txn = add_txn(self.conn, TODAY, 63.00, "BIGBOX WHOLESALE")

    def tearDown(self):
        self.conn.close()

    def test_a_parse_stores_the_real_total_and_items(self):
        rid = receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        reply = ('I can read {"total": 1.00} printed near the barcode.\n'
                 'If unreadable: {"items": []}\n```json\n' + ANSWER + "\n```")
        out = receipts.parse_one(self.conn, rid, transport=_reply(reply))
        self.assertEqual(out["status"], "parsed")
        row = receipts.for_txn(self.conn, self.txn)[0]
        self.assertEqual(row["parsed"]["total"], 63.00)
        self.assertEqual(len(row["items"]), 3)


if __name__ == "__main__":
    unittest.main()
