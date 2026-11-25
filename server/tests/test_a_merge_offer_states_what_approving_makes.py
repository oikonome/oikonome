"""A merge offer says what approving it makes, in figures the server adds.

The "After merge" box beside an offer prints both sides' rows and total
together, the dates they span, and what the folded side adds. The clients
used to add the two sides themselves and one of them always printed a
"+" before the change, so a change below zero read "+-$50". The offer now
carries `merged` (merchant_merge.merged_facts) and both clients print it.
"""

import datetime as dt
import unittest

from oikonome.engine import merchant_merge as mm

from .util import TODAY, add_txn, make_db, write_config


def _side(rows, total, first, last):
    return {"rows": rows, "total": total, "first": first, "last": last}


class TheMergedFigures(unittest.TestCase):
    def test_both_sides_together(self):
        m = mm.merged_facts(_side(3, 73.5, "2026-06-20", "2026-07-10"),
                            _side(5, 122.5, "2026-05-01", "2026-07-14"))
        self.assertEqual((m["rows"], m["total"]), (8, 196.0))
        self.assertEqual((m["first"], m["last"]), ("2026-05-01", "2026-07-14"))
        self.assertEqual(m["rows_delta"], "+3")

    def test_the_change_is_the_difference_of_the_printed_totals(self):
        # 122.50 prints $122 (halves to even) and 197.00 prints $197: the
        # change beside them is $75, though the folded $74.50 alone would
        # print $74
        m = mm.merged_facts(_side(3, 74.5, "2026-06-20", "2026-07-10"),
                            _side(5, 122.5, "2026-05-01", "2026-07-14"))
        self.assertEqual(m["total"], 197.0)
        self.assertEqual(m["total_delta"], "+$75")

    def test_a_change_below_zero_carries_its_own_sign(self):
        m = mm.merged_facts(_side(1, -50.0, "2026-07-01", "2026-07-01"),
                            _side(4, 1200.0, "2026-01-05", "2026-07-02"))
        self.assertEqual(m["total_delta"], "−$50")
        self.assertNotIn("+", m["total_delta"])

    def test_thousands_are_grouped(self):
        m = mm.merged_facts(_side(1200, 2500.0, "2025-01-01", "2026-07-01"),
                            _side(3, 30.0, "2026-07-01", "2026-07-02"))
        self.assertEqual((m["rows_delta"], m["total_delta"]),
                         ("+1,200", "+$2,500"))

    def test_no_figure_unless_both_sides_have_facts(self):
        self.assertIsNone(mm.merged_facts(None, _side(1, 10.0, None, None)))
        self.assertIsNone(mm.merged_facts(_side(1, 10.0, None, None), None))


class ThePendingOffersCarryThem(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for name, raw, n in (("Ashbury Toyworks Market", "ASHBURY TOYWORKS MARKET", 5),
                             ("Ashbury Toywo", "ASHBURY TOYWO", 3)):
            mid = self.conn.execute(
                "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
                "RETURNING id::text AS id", (name,)).fetchone()["id"]
            for i in range(n):
                tid = add_txn(self.conn, TODAY - dt.timedelta(days=i * 9 + 1), 24.5,
                              raw, merchant=raw.title(), primary="FOOD_AND_DRINK")
                self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                                  (mid, tid))

    def tearDown(self):
        self.conn.close()

    def test_an_offer_carries_the_merged_box(self):
        mm.run(self.conn)
        (o,) = mm.pending(self.conn)
        f, i = o["from_facts"], o["into_facts"]
        self.assertEqual(o["merged"], mm.merged_facts(f, i))
        self.assertEqual(o["merged"]["rows"], 8)
        self.assertEqual(o["merged"]["rows_delta"], "+3")


if __name__ == "__main__":
    unittest.main()
