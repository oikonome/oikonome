"""The assistant's charts, tables and the HTML report.

The model writes a fenced JSON spec; the engine validates it into typed
blocks the clients draw, and the same blocks render into a self-contained
HTML page. The model's text is untrusted: a malformed or oversized spec
is never drawn (the reader gets a short notice in its place), markup in a
label is escaped, and a client cannot send back
anything the model could not have produced."""

import json
import os
import pathlib
import shutil
import subprocess
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

    def test_prose_bold_and_bullets_are_drawn_not_shown_as_asterisks(self):
        """The model writes **bold** and "- " bullets; the report draws
        them, exactly as the web and mobile screens do, and a bare
        asterisk, a double underscore or a tag inside the prose is still
        text."""
        page = ab.prose_html("Net worth is **$12,345.67** today.\n\n"
                             "- **Alpha** is most of it\n"
                             "- Beta is small, 2 * 3 = 6, ACH__REF__1\n\n"
                             "1. first\n2. second <b>no</b>\n# Heading")
        self.assertIn("<strong>$12,345.67</strong>", page)
        self.assertNotIn("**", page)
        self.assertEqual(page.count("<li"), 4)
        self.assertIn("<ul><li><strong>Alpha</strong> is most of it</li>", page)
        self.assertIn('<ol><li value="1">first</li>', page)
        self.assertIn("2 * 3 = 6, ACH__REF__1", page)
        self.assertIn("&lt;b&gt;no&lt;/b&gt;", page)
        self.assertIn("<p><strong>Heading</strong></p>", page)

    def test_a_numbered_item_keeps_its_own_number(self):
        """A ranking keeps the model's numbers: a sub-bullet under an item
        does not become the next rank, a blank line between items does not
        restart at 1, and a list that goes on after a chart (its own text
        block) still reads 4, 5."""
        page = ab.prose_html("1. Rent\n   - paid on the 1st\n2. Groceries\n\n3. Dining")
        self.assertIn('<li value="1">Rent</li>', page)
        self.assertIn("<ul><li>paid on the 1st</li></ul>", page)
        self.assertIn('<li value="2">Groceries</li>', page)
        self.assertIn('<li value="3">Dining</li>', page)
        later = ab.prose_html("4. Utilities\n5. Fuel")
        self.assertIn('<li value="4">Utilities</li><li value="5">Fuel</li>', later)

    def test_the_server_parses_prose_exactly_as_the_clients_do(self):
        """The HTML report and the two screens must draw one answer the
        same way: parse_rich and the clients' parseRich (one file, copied)
        agree on every shared fixture, run here under node."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        root = pathlib.Path(__file__).resolve().parents[2]
        web = (root / "webapp/src/richtext.ts").read_text().splitlines()
        mob = (root / "mobile/src/lib/richtext.ts").read_text().splitlines()
        code = lambda lines: [l for l in lines if not l.lstrip().startswith("//")]
        self.assertEqual(code(web), code(mob), "the two client parsers drifted apart")
        fixtures = [
            "Plain **bold** line\nsecond line\n\n- a\n* **b** c\n\n1. one\n2) two\n# Head\ntail",
            "1. Rent\n   - paid\n2. Groceries\n\n3. Dining\n\n   \n4. Fuel\r\n- x",
            "__under__ 2 * 3 ** not closed\n\n\n\n•  dot bullet\n10. ten\n",
            "", "   ", "**only**", "- ", "1. ",
            ("9" * 4400) + ". not a number\n123456789. nine digits\n1234567890. ten",
        ]
        script = ("import('./src/lib/richtext.ts').then(m => "
                  "process.stdout.write(JSON.stringify(JSON.parse(process.argv[1]).map(m.parseRich))))")
        out = subprocess.run([node, "--experimental-strip-types", "-e", script, json.dumps(fixtures)],
                             cwd=root / "mobile", capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [ab.parse_rich(f) for f in fixtures])

    def test_a_whole_conversation_reports_in_order(self):
        turns = [("Net worth?", [{"kind": "text", "text": "A lot."}]),
                 ("And <last> year?", [{"kind": "text", "text": "Less."}])]
        page = ab.report_html("ignored", [], "2026-10-19", turns=turns)
        self.assertIn("<h1>Net worth?</h1>", page)
        self.assertIn('<h2 class="q">And &lt;last&gt; year?</h2>', page)
        self.assertLess(page.index("A lot."), page.index("And &lt;last&gt;"))
        self.assertLess(page.index("And &lt;last&gt;"), page.index("Less."))
        self.assertEqual(page.count('class="q"'), 1)

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

    def test_a_conversation_downloads_as_one_file(self):
        r = self._post({"turns": [
            {"question": "Groceries?", "blocks": [{"kind": "text", "text": "Here."}]},
            {"question": "junk", "blocks": [{"kind": "html", "html": "<b>"}]},
            {"question": "And last year?",
             "blocks": [{"kind": "text", "text": "**Less.**"}]}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("oikonome-conversation-", r.headers["content-disposition"])
        self.assertIn("<h1>Groceries?</h1>", r.text)
        self.assertIn('<h2 class="q">And last year?</h2>', r.text)
        self.assertIn("<strong>Less.</strong>", r.text)
        self.assertNotIn("junk", r.text)
        self.assertNotIn("not included", r.text)

    def test_a_long_conversation_keeps_its_newest_turns_and_says_so(self):
        """The newest answers are the ones a person saving a long
        conversation came for; the page keeps those and names what it
        left out — and a client whose loaded conversation has no start
        says so too."""
        from oikonome.web import api as web_api
        n = web_api.REPORT_TURNS + 7
        turns = [{"question": f"q{i}", "blocks": [{"kind": "text", "text": f"a{i}"}]}
                 for i in range(n)]
        r = self._post({"turns": turns})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(f"a{n - 1}", r.text)
        self.assertNotIn(">a0<", r.text)
        self.assertIn("7 earlier questions in this conversation not included", r.text)
        self.assertIn("<h1>q7</h1>", r.text)
        r = self._post({"turns": turns[:2], "partial": True})
        self.assertIn("earlier questions in this conversation not included (its start was not loaded)",
                      r.text)
        r = self._post({"turns": turns, "partial": True})
        self.assertIn("7 earlier questions in this conversation not included, and the ones "
                      "before them were not loaded", r.text)
        # a digit run no integer parses is prose, never a 500
        r = self._post({"question": "n", "blocks": [{"kind": "text", "text": ("9" * 4400) + ". x"}]})
        self.assertEqual(r.status_code, 200, r.text[:200])
        # many charts are bounded by blocks, not only by turns
        chart = {"kind": "bar", **{k: v for k, v in _BAR.items() if k != "type"}}
        heavy = [{"question": f"q{i}", "blocks": [chart] * 4} for i in range(40)]
        r = self._post({"turns": heavy})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertLessEqual(r.text.count("<svg"), web_api.REPORT_BLOCKS)
        self.assertIn("earlier questions in this conversation not included", r.text)

    def test_the_single_answer_body_still_works_beside_turns(self):
        """Clients send both shapes so a server older than conversations
        renders the first answer; this server prefers `turns`."""
        r = self._post({"question": "Q?", "blocks": [{"kind": "text", "text": "first"}],
                        "turns": [{"question": "Q?", "blocks": [{"kind": "text", "text": "first"}]},
                                  {"question": "More?", "blocks": [{"kind": "text", "text": "second"}]}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("second", r.text)

    def test_a_conversation_with_nothing_valid_is_a_400(self):
        r = self._post({"turns": [{"question": "x",
                                   "blocks": [{"kind": "html", "html": "<b>"}]}]})
        self.assertEqual(r.status_code, 400)

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
