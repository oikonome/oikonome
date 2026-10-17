"""The assistant's charts, tables and the HTML report.

The model writes a fenced JSON spec; the engine validates it into typed
blocks the clients draw, and the same blocks render into a self-contained
HTML page. The model's text is untrusted: a malformed or oversized spec
is never drawn (the reader gets a short notice in its place), markup in a
label is escaped, and a client cannot send back
anything the model could not have produced."""

import json
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.engine import assistant, assistant_blocks as ab

from .test_assistant import _BE, _seq_transport
from .util import _ensure_db, make_db

_BAR = {"type": "bar", "title": "Groceries by month",
        "labels": ["Jan", "Feb", "Mar"],
        "series": [{"name": "Groceries", "values": [412.5, 388, 401.25]}]}


def _fenced(obj, tag="chart"):
    return f"```{tag}\n{json.dumps(obj)}\n```"


class ParseTests(unittest.TestCase):
    def test_prose_and_chart_come_apart_in_order(self):
        text = ("You spent $1,201 on groceries this quarter.\n"
                + _fenced(_BAR) + "\nMarch was the high month.")
        prose, blocks = ab.parse(text)
        self.assertEqual([b["kind"] for b in blocks], ["text", "bar", "text"])
        self.assertEqual(blocks[1]["labels"], ["Jan", "Feb", "Mar"])
        self.assertEqual(blocks[1]["series"][0]["values"], [412.5, 388.0, 401.25])
        # the prose keeps the sentences and loses the fence — an older
        # client reading only `answer` still gets a readable reply
        self.assertNotIn("```", prose)
        self.assertIn("March was the high month.", prose)
        self.assertIn("$1,201", prose)

    def test_a_json_fence_holding_a_chart_is_a_chart(self):
        _, blocks = ab.parse(_fenced(_BAR, "json"))
        self.assertEqual(blocks[0]["kind"], "bar")

    def test_a_bad_spec_becomes_a_notice_and_the_prose_survives(self):
        bad = [
            {**_BAR, "type": "scatter"},
            {**_BAR, "series": [{"name": "x", "values": [1, 2]}]},   # length
            {**_BAR, "series": [{"name": "x", "values": [1, "2", 3]}]},
            {**_BAR, "series": [{"name": "x", "values": [1, True, 3]}]},
            {**_BAR, "series": []},
            {**_BAR, "labels": []},
            {"type": "pie", "title": "t", "labels": ["a"],
             "series": [{"name": "x", "values": [1]},
                        {"name": "y", "values": [2]}]},
            {"type": "table", "title": "t", "columns": ["a", "b"],
             "rows": [["x", 1, 2]]},
            {"type": "table", "title": "t", "columns": [], "rows": [["x"]]},
            {**_BAR, "labels": [{"not": "a label"}, "b", "c"]},
        ]
        for spec in bad:
            prose, blocks = ab.parse("Before.\n" + _fenced(spec) + "\nAfter.")
            self.assertEqual([b["kind"] for b in blocks], ["text"], spec)
            self.assertEqual(
                prose, "Before.\n\n" + ab.DROPPED_NOTICE + "\n\nAfter.")
        # a fence that is not JSON is quoted text, kept without its fence
        prose, blocks = ab.parse("Hi\n```chart\n{not json\n```")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(prose, "Hi\n\n{not json")
        # NaN parses as JSON in Python; it is still not a figure
        prose, blocks = ab.parse(
            "```chart\n" + json.dumps(_BAR).replace("388", "NaN") + "\n```")
        self.assertEqual(prose, ab.DROPPED_NOTICE)
        self.assertEqual([b["kind"] for b in blocks], ["text"])

    def test_a_chart_past_the_limit_becomes_a_notice_not_a_silent_gap(self):
        n = ab.MAX_BLOCKS + 1
        prose, blocks = ab.parse("\n".join(_fenced(_BAR) for _ in range(n)))
        self.assertEqual(sum(b["kind"] == "bar" for b in blocks), ab.MAX_BLOCKS)
        self.assertEqual(blocks[-1], {"kind": "text",
                                      "text": ab.DROPPED_NOTICE})
        self.assertEqual(prose, ab.DROPPED_NOTICE)

    def test_sizes_are_bounded(self):
        too_many = {**_BAR, "labels": [str(i) for i in range(ab.MAX_LABELS + 1)],
                    "series": [{"name": "x",
                                "values": [1] * (ab.MAX_LABELS + 1)}]}
        self.assertIsNone(ab.validate(too_many))
        self.assertIsNone(ab.validate({**_BAR, "series": [
            {"name": str(i), "values": [1, 2, 3]}
            for i in range(ab.MAX_SERIES + 1)]}))
        long_title = ab.validate({**_BAR, "title": "x" * 500})
        self.assertEqual(len(long_title["title"]), ab.MAX_TITLE)
        _, blocks = ab.parse("\n".join(_fenced(_BAR)
                                       for _ in range(ab.MAX_BLOCKS + 3)))
        self.assertEqual(sum(b["kind"] == "bar" for b in blocks), ab.MAX_BLOCKS)
        rows = {"type": "table", "title": "t", "columns": ["a"],
                "rows": [["r"]] * (ab.MAX_TABLE_ROWS + 10)}
        self.assertEqual(len(ab.validate(rows)["rows"]), ab.MAX_TABLE_ROWS)

    def test_table_cells_keep_numbers_as_numbers(self):
        spec = ab.validate({"type": "table", "title": "Top",
                            "columns": ["Merchant", "Spent"],
                            "rows": [["Example Mart", 1204.1],
                                     ["Example Diner", "n/a"]]})
        self.assertEqual(spec["rows"][0], ["Example Mart", 1204.1])
        self.assertEqual(spec["rows"][1], ["Example Diner", "n/a"])

    def test_the_answer_carries_the_blocks(self):
        conn = make_db()
        try:
            t = _seq_transport([{"content": "Here you go.\n" + _fenced(_BAR),
                                 "tool_calls": []}])
            r = assistant.ask(conn, "groceries by month?", transport=t,
                              backend=_BE)
        finally:
            conn.close()
        self.assertEqual(r["answer"], "Here you go.")
        self.assertEqual([b["kind"] for b in r["blocks"]], ["text", "bar"])

    def test_the_prompt_teaches_the_shape(self):
        self.assertIn('"type": "bar"', ab.guide())
        self.assertIn("```chart", ab.guide())


class TooLongANumberTests(unittest.TestCase):
    """A JSON integer of hundreds of digits is valid JSON and no float:
    the block holding it is refused like any other bad spec, and the rest
    of the answer survives."""

    HUGE = 10 ** 400

    def _bar(self, **over):
        return {**_BAR, **over}

    def test_parse_drops_a_chart_whose_value_overflows(self):
        bad = self._bar(series=[{"name": "G", "values": [1, self.HUGE, 2]}])
        prose, blocks = ab.parse("Before.\n" + _fenced(bad) + "\nAfter.")
        self.assertEqual([b["kind"] for b in blocks], ["text"])
        self.assertIn(ab.DROPPED_NOTICE, prose)
        self.assertIn("After.", prose)

    def test_overflowing_title_label_or_cell_is_refused_not_raised(self):
        for spec in (self._bar(title=self.HUGE),
                     self._bar(labels=["Jan", self.HUGE, "Mar"]),
                     {"type": "table", "title": "t", "columns": ["a"],
                      "rows": [[self.HUGE]]},
                     {"type": "table", "title": "t", "columns": [self.HUGE],
                      "rows": [[1]]}):
            with self.subTest(spec=list(spec)):
                self.assertIsNone(ab.validate(spec))

    def test_clean_drops_a_block_whose_value_overflows(self):
        good = {"kind": "text", "text": "kept"}
        bad = {"kind": "bar", **{k: v for k, v in _BAR.items() if k != "type"}}
        bad["series"] = [{"name": "G", "values": [1, self.HUGE, 2]}]
        self.assertEqual(ab.clean([good, bad]), [good])


class CleanTests(unittest.TestCase):
    """What a client sends back for the report is re-validated."""

    def test_round_trip_is_identity(self):
        _, blocks = ab.parse("Hi\n" + _fenced(_BAR))
        self.assertEqual(ab.clean(blocks), blocks)

    def test_foreign_shapes_are_dropped(self):
        self.assertEqual(ab.clean("nope"), [])
        self.assertEqual(ab.clean([{"kind": "html", "html": "<b>x</b>"}]), [])
        self.assertEqual(ab.clean([{"kind": "bar", "title": "t"}]), [])
        self.assertEqual(ab.clean([{"kind": "text", "text": "   "}]), [])


class ReportTests(unittest.TestCase):
    def _page(self, blocks, q="How much on groceries?"):
        return ab.report_html(q, blocks, "2026-10-14")

    def test_self_contained_with_every_chart_kind(self):
        specs = [_BAR, {**_BAR, "type": "line"},
                 {**_BAR, "type": "pie", "title": "Split"},
                 {"type": "table", "title": "Top", "columns": ["M", "S"],
                  "rows": [["Example Mart", 10.0]]}]
        blocks = [{"kind": "text", "text": "Here you go."}] + \
            [ab.validate(s) for s in specs]
        page = self._page(blocks)
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertNotIn("<script", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertEqual(page.count("<svg"), 3)
        self.assertIn("Here you go.", page)
        # every chart's figures are readable as a table as well
        self.assertGreaterEqual(page.count("<table"), 3)
        self.assertIn("$412", page)
        self.assertIn(">10<", page)   # a table number is plain, not money

    def test_markup_in_model_text_is_escaped(self):
        spec = ab.validate({**_BAR, "title": "<img src=x onerror=alert(1)>",
                            "labels": ["<b>Jan</b>", "Feb", "Mar"]})
        page = self._page([{"kind": "text", "text": "<script>evil()</script>"},
                           spec], q="<svg onload=alert(1)>")
        self.assertNotIn("<script>", page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<b>Jan", page)
        self.assertNotIn("<svg onload", page)
        self.assertIn("&lt;script&gt;", page)

    def test_multi_series_bar_and_line_have_a_legend(self):
        two = ab.validate({**_BAR, "series": [
            {"name": "2024", "values": [1, 2, 3]},
            {"name": "2025", "values": [2, 3, 4]}]})
        for kind in ("bar", "line"):
            page = self._page([{**two, "kind": kind}])
            self.assertIn('class="legend"', page)
            self.assertIn("2024", page)
        # one series: the title names it, no legend box
        self.assertNotIn('class="legend"', self._page([ab.validate(_BAR)]))

    def test_negative_bars_hang_below_zero(self):
        spec = ab.validate({**_BAR, "series": [
            {"name": "Saved", "values": [120, -45, 30]}]})
        page = self._page([spec])
        self.assertIn("<rect", page)
        self.assertIn("-$45", page)


class ReportRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        r = cls.client.post("/api/signup", data={
            "email": f"rep-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code in (200, 303), r.text

    def _post(self, body, available=True):
        with mock.patch.object(assistant, "available", return_value=available):
            return self.client.post("/api/assistant/report", json=body)

    def test_report_downloads_as_html(self):
        r = self._post({
            "question": "Groceries?",
            "blocks": [{"kind": "text", "text": "Here."},
                       {"kind": "bar", **{k: v for k, v in _BAR.items()
                                          if k != "type"}}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.headers["content-type"].startswith("text/html"))
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn("<svg", r.text)
        self.assertIn("Groceries?", r.text)

    def test_an_overflowing_number_drops_its_block_not_the_report(self):
        r = self._post({
            "question": "Groceries?",
            "blocks": [{"kind": "text", "text": "Here."},
                       {"kind": "bar", "title": "t", "labels": ["a"],
                        "series": [{"name": "s", "values": [10 ** 400]}]}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Here.", r.text)

    def test_nothing_valid_is_a_400(self):
        r = self._post({"question": "x",
                        "blocks": [{"kind": "html", "html": "<b>"}]})
        self.assertEqual(r.status_code, 400)

    def test_no_llm_no_report(self):
        r = self._post({"question": "x",
                        "blocks": [{"kind": "text", "text": "hi"}]},
                       available=False)
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
