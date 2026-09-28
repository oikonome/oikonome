"""A model's JSON answer is read through whatever it is wrapped in.

Models are told to answer with bare JSON and many don't: a reasoning
model (qwen3-vl on the bundled Ollama) thinks aloud in <think>…</think>
first, others fence the object in ```json … ``` or put a sentence either
side of it. Read strictly, every one of those replies failed the receipt
as "the AI backend's answer couldn't be read" after minutes of work. The
receipt reader, the categorizer, the summary pass, the Amazon batch and
the tax-document reader all read their answer through one helper, so each
gets one case here; the helper's own cases pin the shapes.

A receipt's date is stored as ISO whatever order the model copied it in,
and a reply with no JSON at all fails with a sentence a person can act on,
without the reply's text reaching any surface but the DEBUG log."""

import json
import logging
import os
import unittest
from unittest import mock

import httpx

from oikonome.engine import llm_categorize, receipts, taxdocs

from .test_llm_categorize import ENV
from .test_receipts import LLM_JSON, _enable_llm
from .util import TODAY, add_txn, make_db, write_config

THINK = ("<think>The user wants JSON like {\"example\": 1}. The total "
         "looks like 63.00 and the store is BIGBOX.</think>\n")


class JsonAnswerShapes(unittest.TestCase):
    def test_a_think_block_before_the_answer_is_skipped(self):
        self.assertEqual(
            llm_categorize.json_answer(THINK + '{"total": 5}', ("total",)),
            {"total": 5})

    def test_an_orphan_closing_think_tag_ends_the_thinking(self):
        # some servers strip the opening tag and leave only the close
        self.assertEqual(llm_categorize.json_answer(
            'thinking about {"x": 1} here</think>{"total": 5}'),
            {"total": 5})

    def test_an_unterminated_think_is_still_scanned(self):
        self.assertEqual(llm_categorize.json_answer(
            '<think>reading... {"total": 7, "merchant": "A"}', ("total",)),
            {"total": 7, "merchant": "A"})

    def test_a_fenced_answer_parses(self):
        self.assertEqual(llm_categorize.json_answer(
            'Here you go:\n```json\n{"total": 5, "items": []}\n```\n'),
            {"total": 5, "items": []})

    def test_prose_around_the_object_is_ignored(self):
        self.assertEqual(llm_categorize.json_answer(
            'Sure! The receipt says {"total": 5} — hope that helps {.'),
            {"total": 5})

    def test_of_two_objects_the_one_with_the_expected_keys_wins(self):
        reply = ('The schema is {"a": {"b": 1}}. Answer: '
                 '{"merchant": "X", "total": 9.5}')
        self.assertEqual(
            llm_categorize.json_answer(reply, ("total", "merchant")),
            {"merchant": "X", "total": 9.5})
        # without expected keys the first object is the answer
        self.assertEqual(llm_categorize.json_answer(reply), {"a": {"b": 1}})

    def test_a_reply_with_no_object_is_none(self):
        for reply in ("", None, "not json", "{never closed", "[1, 2]",
                      "<think>only thinking</think>"):
            self.assertIsNone(llm_categorize.json_answer(reply), reply)


class ReceiptDates(unittest.TestCase):
    def test_dates_are_stored_iso_whatever_order_they_came_in(self):
        cases = {"2031-08-19": "2031-08-19",
                 "08/19/2031": "2031-08-19",
                 "8/19/31": "2031-08-19",
                 "19.08.2031": "2031-08-19",     # first number can't be a month
                 "03/04/2031": "2031-03-04",     # ambiguous: month-first
                 "2031-08-19T14:02:00": "2031-08-19",
                 "Aug 19, 2031": "2031-08-19",
                 "20310819": "2031-08-19",       # separators dropped
                 20310819: "2031-08-19"}         # …or returned as a number
        for raw, want in cases.items():
            self.assertEqual(receipts.iso_date(raw), want, raw)

    def test_an_unreadable_date_is_null(self):
        for raw in (None, "", "yesterday", "13/13/2026", "00/00/0000",
                    99999999, {"d": 1}):
            self.assertIsNone(receipts.iso_date(raw), raw)


def _reply(content):
    return httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": content}}]}))


class ReceiptParse(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)
        self.txn = add_txn(self.conn, TODAY, 63.00, "BIGBOX WHOLESALE")

    def tearDown(self):
        self.conn.close()

    def _parse(self, content):
        rid = receipts.add(self.conn, self.txn, b"\x89PNG...", "image/png")
        out = receipts.parse_one(self.conn, rid, transport=_reply(content))
        return out, receipts.for_txn(self.conn, self.txn)[0]

    def test_a_reasoning_models_answer_parses(self):
        out, row = self._parse(
            THINK + "```json\n" + json.dumps(LLM_JSON) + "\n```")
        self.assertEqual(out["status"], "parsed")
        self.assertEqual(row["parsed"]["total"], 63.0)
        self.assertEqual(len(row["items"]), 3)

    def test_a_us_order_date_is_stored_iso(self):
        _, row = self._parse(json.dumps({**LLM_JSON, "date": "08/19/2031"}))
        self.assertEqual(row["parsed"]["date"], "2031-08-19")

    def test_prose_fails_with_an_actionable_error_and_no_content_leaks(self):
        secret = "PHARMACY LINE ITEM ZZQ"
        with self.assertLogs("oikonome.receipts", logging.DEBUG) as logs:
            out, row = self._parse(f"I can see {secret} on this receipt.")
        self.assertEqual(out["status"], "failed")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], receipts.PROSE_ERROR)
        self.assertIn("Settings → AI", row["error"])
        for rec in logs.records:
            if secret in rec.getMessage():
                self.assertEqual(rec.levelno, logging.DEBUG)
                self.assertTrue(getattr(rec, "no_capture", False))


class OtherReaders(unittest.TestCase):
    def test_the_categorizer_reads_a_thinking_reply(self):
        self.assertEqual(llm_categorize._parse(
            THINK + '```json\n{"1": "FOOD_AND_DRINK"}\n```', 1),
            {1: "FOOD_AND_DRINK"})

    def test_the_summary_pass_reads_a_thinking_reply(self):
        self.assertEqual(llm_categorize._parse_freetext(
            THINK + 'Answer: {"1": "desk lamp"}', 1), {0: "desk lamp"})

    def test_the_amazon_batch_reads_a_thinking_reply(self):
        rows = [{"items_json": [{"title": "lamp"}], "memo": "", "seller": ""}]
        with mock.patch.dict(os.environ, ENV):
            out = llm_categorize._classify_amazon_batch(
                rows, ["Shopping"],
                transport=_reply(THINK + '{"1": "Shopping"}'))
        self.assertEqual(out, {1: "Shopping"})

    def test_the_tax_document_reader_reads_a_thinking_reply(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        payload = {"year": 2024, "employer": "ACME", "ein": None,
                   "boxes": {"1": 50000.0}}
        with mock.patch("oikonome.engine.llm_categorize._chat",
                        return_value=THINK + "```json\n"
                        + json.dumps(payload) + "\n```"), \
             mock.patch("oikonome.engine.llm_categorize._backend",
                        return_value={"url": "http://localhost:11434",
                                      "model": "vl", "api_key": "",
                                      "extra_body": ""}):
            out = taxdocs.analyze(conn, "w2.png", b"\x89PNG...", "image/png")
        self.assertEqual(out["kind"], "w2")
        self.assertEqual(out["rows"][0]["year"], 2024)


if __name__ == "__main__":
    unittest.main()
