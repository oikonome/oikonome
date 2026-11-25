"""Every money-map row on Today reads against where it should be BY
TODAY, the basis the headline's "more than planned by today" uses. A row
past its whole month's budget used to say "over by" the budget while the
row beside it said "more than planned" against the pace, and no row
figure added up to either headline. Now each judged row says "over pace"
or "under pace", the figures printed beside the rows add up to the one
printed beside "Variable spending", and that one is the verdict's own
variance — including a carve-out that has spent more today than its day's
allowance. Bills keep their own words. Figures are invented."""

import datetime as dt
import re
import unittest

from oikonome.web import report, todayview

from .util import TODAY, add_txn, make_db, write_config

PETS = [{"name": "Pets", "parent": "other", "monthly": 100,
         "merchants": ["Chewy"]}]


def _signed(text: str) -> int:
    if text == "on pace":
        return 0
    m = re.fullmatch(r"\$([\d,]+) (over|under) pace", text)
    assert m, text
    n = int(m.group(1).replace(",", ""))
    return n if m.group(2) == "over" else -n


class PaceRowsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=900,
                     custom_buckets=PETS, today_view="detail")
        add_txn(self.conn, dt.date(2026, 7, 3), 120.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 7, 9), 261.4, "HILLTOP MARKET",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 15.55, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
        # Everything else is past its whole month's budget
        add_txn(self.conn, dt.date(2026, 7, 5), 1100.0, "OAKWOOD FURNITURE")
        add_txn(self.conn, dt.date(2026, 7, 14), 30.0, "Chewy")
        # today's spend on Pets is far past its day's allowance
        add_txn(self.conn, TODAY, 90.0, "Chewy")
        self.st = report.gather(self.conn, TODAY)
        self.rows, self.total = todayview.today_map(self.st)

    def tearDown(self):
        self.conn.close()

    def judged(self):
        return [r for r in self.rows if r["judged"]
                and r.get("actual_label") != "—"]

    def test_a_row_past_its_budget_reads_against_the_pace(self):
        other = next(r for r in self.rows if r["label"] == "Everything else")
        self.assertGreater(other["actual"], other["budget"])
        self.assertTrue(other["status"]["text"].endswith(" over pace"),
                        other["status"])
        self.assertNotIn("over by", other["status"]["text"])
        for r in self.judged():
            self.assertAlmostEqual(r["status"]["vs_pace"],
                                   r["actual"] - r["expected"], places=2)

    def test_the_rows_add_up_to_the_total_and_the_total_is_the_verdicts(self):
        judged = self.judged()
        self.assertEqual(len(judged), 3)
        self.assertAlmostEqual(
            sum(r["status"]["vs_pace"] for r in judged),
            self.total["status"]["vs_pace"], delta=0.02)
        self.assertAlmostEqual(self.total["status"]["vs_pace"],
                               self.st["variance"], places=2)
        self.assertEqual(sum(_signed(r["status"]["text"]) for r in judged),
                         _signed(self.total["status"]["text"]))

    def test_the_headline_prints_the_totals_figure(self):
        self.assertEqual(self.st["verdict"], "OVER BUDGET")
        day = todayview.build_context(self.st)["day"]
        n = _signed(self.total["status"]["text"])
        self.assertEqual(day["pace_line"],
                         f"${n:,} more than planned by today")

    def test_bills_keep_their_own_words_and_every_surface_prints_these(self):
        sent = todayview.sent_plan_rows(self.rows)
        for r, s in zip(self.rows, sent):
            self.assertEqual(r["status"], s["status"])
        _s, _plain, html = report.build(self.st)
        for r in self.judged():
            self.assertIn(r["status"]["text"], html)
        self.assertIn(self.total["status"]["text"], html)
        # no bills scheduled: no Bills row, so nothing beside one
        self.assertNotIn("Bills", [r["label"] for r in self.rows])


if __name__ == "__main__":
    unittest.main()
