"""An assistant ring prints the server's figures: the centre total and
each drawn part's whole dollars and share, decided once by
assistant_blocks.ring_figures. The dollars add up to the centre and the
shares to 100 (largest remainder), on a fresh answer, on an answer saved
before rings carried the figures, and on the saved page."""

import json
import unittest

from oikonome.engine import assistant, assistant_blocks as ab

from .test_assistant import _BE, _seq_transport
from .util import make_db


def _pie(values, labels=None):
    labels = labels or [f"part {i}" for i in range(len(values))]
    return {"type": "pie", "title": "Split", "labels": labels,
            "series": [{"name": "s", "values": values}]}


class RingFiguresTests(unittest.TestCase):
    def assertAddsUp(self, shown):
        rows = shown["rows"]
        self.assertEqual(sum(r["dollars"] for r in rows), shown["total"])
        if rows:
            self.assertEqual(sum(r["share"] for r in rows), 100)

    def test_three_equal_parts_read_34_33_33(self):
        shown = ab.ring_figures(["a", "b", "c"], [500, 500, 500])
        self.assertEqual(shown["total"], 1500)
        self.assertEqual([(r["label"], r["dollars"], r["share"])
                          for r in shown["rows"]],
                         [("a", 500, 34), ("b", 500, 33), ("c", 500, 33)])

    def test_dollar_parts_add_up_to_the_centre(self):
        # three parts of $100.40: each rounds to $100 on its own, and the
        # centre is $301 — the earliest part takes the missing dollar
        shown = ab.ring_figures(["a", "b", "c"], [100.40, 100.40, 100.40])
        self.assertEqual(shown["total"], 301)
        self.assertEqual([r["dollars"] for r in shown["rows"]], [101, 100, 100])
        self.assertAddsUp(shown)

    def test_a_part_that_is_not_positive_is_not_drawn_or_listed(self):
        shown = ab.ring_figures(["a", "b", "c", "d"], [60, 0, -15, 40])
        self.assertEqual([r["label"] for r in shown["rows"]], ["a", "d"])
        self.assertEqual([r["share"] for r in shown["rows"]], [60, 40])
        self.assertEqual(shown["total"], 100)
        self.assertEqual(ab.ring_figures(["a"], [0]),
                         {"total": 0, "rows": []})

    def test_many_shapes_add_up(self):
        cases = [[1, 1, 1, 1, 1, 1, 1], [0.01, 0.01, 999.98],
                 [33.335, 33.335, 33.33], [250.5, 250.5, 250.5, 248.5],
                 [7, 13, 19, 23, 29, 31], [1e9, 0.01]]
        for vals in cases:
            with self.subTest(vals=vals):
                self.assertAddsUp(ab.ring_figures(
                    [str(i) for i in range(len(vals))], vals))

    def test_validate_attaches_the_figures_to_a_pie_only(self):
        pie = ab.validate(_pie([500, 500, 500]))
        self.assertEqual([r["share"] for r in pie["shown"]["rows"]],
                         [34, 33, 33])
        bar = ab.validate({**_pie([1, 2]), "type": "bar"})
        self.assertNotIn("shown", bar)

    def test_figures_a_client_sends_back_are_decided_again(self):
        sent = {**ab.validate(_pie([500, 500, 500])), "shown": {
            "total": 9, "rows": [{"label": "a", "dollars": 9, "share": 99}]}}
        (back,) = ab.clean([sent])
        self.assertEqual(back["shown"]["total"], 1500)
        self.assertEqual([r["share"] for r in back["shown"]["rows"]],
                         [34, 33, 33])

    def test_the_saved_page_prints_the_same_figures(self):
        page = ab.render_block(ab.validate(_pie([500, 500, 500])))
        self.assertIn("34%", page)
        self.assertEqual(page.count("33%"), 2)
        self.assertIn("$1,500", page)
        page = ab.render_block(ab.validate(_pie([100.40, 100.40, 100.40])))
        self.assertIn("$301", page)
        self.assertIn("<b>$101</b>", page)


class StoredAnswerTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_an_answer_saved_before_rings_carried_figures_gains_them(self):
        text = "Split.\n```chart\n" + json.dumps(_pie([500, 500, 500])) + "\n```"
        out = assistant.ask(self.conn, "split?", backend=_BE,
                            transport=_seq_transport(
                                [{"content": text, "tool_calls": []}]))
        self.assertIn("shown", out["blocks"][1])
        # stored as an older server stored it: the values, no figures
        old = [{k: v for k, v in b.items() if k != "shown"}
               for b in out["blocks"]]
        tid = assistant.record_turn(self.conn, {"user_id": None,
                                                "email": "a@example.dev"},
                                    "split?", {**out, "blocks": old})
        row = self.conn.execute("SELECT blocks FROM assistant_turns "
                                "WHERE id = %s", (tid,)).fetchone()
        self.assertNotIn("shown", row["blocks"][1])
        (turn,) = assistant.history(self.conn)["turns"]
        self.assertEqual([r["share"] for r in turn["blocks"][1]["shown"]["rows"]],
                         [34, 33, 33])
        self.assertNotIn("shown", turn["blocks"][0])


if __name__ == "__main__":
    unittest.main()
