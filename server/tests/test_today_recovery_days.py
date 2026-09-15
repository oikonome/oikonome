"""A reduced daily answers "how long do I stop spending to get it back".

Every spend-at-most tile whose daily has been reduced carries the number
of no-spend days that bring it back to the plan, computed once in the
allowance entry and rendered verbatim on the page context, the email
HTML and the plain text — the same number on every surface.
"""

import datetime as dt
import html as _html
import unittest

from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config


class RecoverDaysArithmeticTests(unittest.TestCase):
    """budget $310 over 31 days = a $10 plan; 10 days left."""

    def test_exact_division_does_not_cost_an_extra_day(self):
        # $50 left over 10 days: five no-spend days leave 50/5 = $10 — the
        # plan exactly, and float noise must not round that up to six
        self.assertEqual(todayview.recover_days(310, 260, 10, 31), 5)

    def test_nothing_to_recover_is_zero(self):
        # $100 left over 10 days is the plan already
        self.assertEqual(todayview.recover_days(310, 210, 10, 31), 0)
        # and being ahead is still nothing to recover
        self.assertEqual(todayview.recover_days(310, 100, 10, 31), 0)

    def test_needing_every_remaining_day_is_unrecoverable(self):
        # $5 left: 10 − 0.5 rounds up to all 10 days — no day left to
        # spend the recovered daily on
        self.assertIsNone(todayview.recover_days(310, 305, 10, 31))

    def test_over_the_month_is_unrecoverable(self):
        self.assertIsNone(todayview.recover_days(310, 310, 10, 31))
        self.assertIsNone(todayview.recover_days(310, 400, 10, 31))

    def test_degenerate_inputs(self):
        self.assertIsNone(todayview.recover_days(0, 0, 10, 31))
        self.assertIsNone(todayview.recover_days(310, 100, 0, 31))


class RecoverDaysOnTheTilesTests(unittest.TestCase):
    """TODAY is the 15th of a 31-day month: 17 days left, $1,000 food and
    other budgets, so the plan daily is $32."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, today_view="detail")
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_txn(self.conn, TODAY, 42.50, "SAFEWAY", primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 19.99, "TARGET")

    def tearDown(self):
        self.conn.close()

    def _tiles(self):
        d = report.gather(self.conn, TODAY)
        return d, dict(todayview.build_context(d)["day"]["allow_tiles"])

    def test_reduced_daily_says_how_many_days_bring_it_back(self):
        # food: $742.50 spent, $257.50 left over 17 days → daily $15;
        # ten no-spend days leave 257.50/7 = $36.79 ≥ $32.26, nine leave
        # 257.50/8 = $32.19 — one short
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 700.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        d, tiles = self._tiles()
        food = tiles["Food"]
        self.assertEqual(food["daily_note"], "daily reduced from $32 to $15")
        self.assertEqual(food["recover_days"], 10)
        self.assertEqual(food["recover_note"],
                         "skip 10 days of spending and the daily is back to $32")
        # other is barely touched: nothing to recover, so no line at all
        self.assertIsNone(tiles["Everything else"]["recover_note"])
        self.assertIsNone(tiles["Everything else"]["recover_days"])
        # the same sentence on both email surfaces
        _, plain, html = report.build(d)
        for surface in (html, plain):
            self.assertIn("skip 10 days of spending and the daily is back "
                          "to $32", surface)

    def test_one_day_reads_as_one_no_spend_day(self):
        # $527.50 left over 17 days → 16.35 plan-days: one skipped day
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 430.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        d, tiles = self._tiles()
        self.assertEqual(tiles["Food"]["daily_note"],
                         "daily reduced from $32 to $31")
        self.assertEqual(tiles["Food"]["recover_days"], 1)
        self.assertEqual(tiles["Food"]["recover_note"],
                         "one no-spend day and the daily is back to $32")
        _, plain, html = report.build(d)
        for surface in (html, plain):
            self.assertIn("one no-spend day and the daily is back to $32",
                          surface)

    def test_over_for_the_month_says_so_instead_of_a_number(self):
        add_txn(self.conn, TODAY, 1180.0, "GADGET BARN",
                primary="GENERAL_MERCHANDISE")
        d, tiles = self._tiles()
        other = tiles["Everything else"]
        self.assertEqual(other["daily_note"], "daily reduced from $32 to $0")
        self.assertIsNone(other["recover_days"])
        self.assertEqual(other["recover_note"],
                         "can't get back to $32 this month")
        _, plain, html = report.build(d)
        # the HTML entity-escapes the apostrophe; the words are the same
        for surface in (_html.unescape(html), plain):
            self.assertIn("can't get back to $32 this month", surface)

    def test_a_raised_daily_carries_no_recovery_line(self):
        d, tiles = self._tiles()
        for label, a in tiles.items():
            self.assertIsNone(a["recover_note"], label)
        _, plain, html = report.build(d)
        for surface in (html, plain):
            self.assertNotIn("no-spend", surface)
            self.assertNotIn("skip ", surface)
            self.assertNotIn("get back to", surface)

    def test_carveout_child_recovers_on_its_own_numbers(self):
        from oikonome.engine import budget as _b
        cfg = _b.load_config(self.conn)
        cfg["custom_buckets"] = [{"name": "Coffee", "parent": "food",
                                  "monthly": 100,
                                  "merchants": ["STARBUCKS"]}]
        _b.save_config(self.conn, cfg)
        self.conn.commit()
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 80.0, "STARBUCKS",
                primary="FOOD_AND_DRINK")
        d, tiles = self._tiles()
        # child: $20 left over 17 days against a $3.23 plan → 6.2 plan-days
        # → 11 no-spend days (20/6 = $3.33 ≥ plan; 20/7 = $2.86 short)
        self.assertEqual(tiles["Coffee"]["recover_days"], 11)
        self.assertEqual(tiles["Coffee"]["recover_note"],
                         "skip 11 days of spending and the daily is back to $3")
        # the parent's carved remainder is ahead → nothing to recover
        self.assertEqual(tiles["Food"]["daily_note_tone"], "pos")
        self.assertIsNone(tiles["Food"]["recover_note"])
        _, plain, html = report.build(d)
        for surface in (html, plain):
            self.assertIn("skip 11 days of spending and the daily is back "
                          "to $3", surface)

    def test_api_payload_carries_the_recovery_fields(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 700.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        d, _ = self._tiles()
        day = todayview.build_context(d)["day"]
        for k in ("food", "other"):
            self.assertIn("recover_days", day["allow"][k])
            self.assertIn("recover_note", day["allow"][k])


if __name__ == "__main__":
    unittest.main()
