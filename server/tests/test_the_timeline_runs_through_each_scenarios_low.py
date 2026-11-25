"""The cash timeline's rows run through each scenario's low point
(specs/today-figures.md, "The timeline runs through the low"): the rows a
client shows by default are the ones up to the selected scenario's low,
the rest sit behind "all N events", and the email's "+N more after the
low point" counts the rows after it. A low that comes after the month
end and the next two weeks used to fall off the end of the list, so the
events that make it were shown nowhere. Figures are invented."""

import datetime as dt
import unittest

from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config


class ALowAfterTheMonth(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=900)
        for day in range(1, 15):
            add_txn(self.conn, dt.date(2026, 7, day), 35.0, "CORNER CAFE",
                    primary="FOOD_AND_DRINK")
        # a large bill well after the month and the next two weeks, with
        # a paycheck after it: the low is that bill's day
        add_bill(self.conn, "HILLSIDE TAX OFFICE", 2400.0,
                 frequency="YEARLY", next_due=dt.date(2026, 8, 24),
                 last_seen=dt.date(2025, 8, 24))
        add_bill(self.conn, "ACME PAYROLL", 2000.0, income=True,
                 next_due=dt.date(2026, 7, 31),
                 last_seen=dt.date(2026, 6, 30))
        self.st = report.gather(self.conn, TODAY)
        self.ctx = todayview.build_context(self.st, email=False)

    def tearDown(self):
        self.conn.close()

    def test_every_scenario_lists_its_rows_through_its_low(self):
        late = (TODAY + dt.timedelta(days=14)).isoformat()
        self.assertTrue(self.ctx["scenarios"])
        for sc in self.ctx["scenarios"]:
            with self.subTest(scenario=sc["key"]):
                self.assertGreater(sc["min_date"], late)
                # the bill that makes the low is listed in every scenario
                self.assertIn("HILLSIDE TAX OFFICE",
                              [r["label"] for r in sc["rows"]
                               if r["date"] == "2026-08-24"])

    def test_the_email_keeps_its_short_list_and_counts_the_rest(self):
        usual = self.ctx["scenarios"][0]
        low = usual["min_date"]
        span = "2026-07-31"      # the month's end, later than two weeks
        pre = self.ctx["tl_pre"]
        future = [r for r in self.ctx["tl_pre"] if r[0] > TODAY.isoformat()]
        self.assertTrue(future)
        # the short span, the month-end row and the low's own day only
        self.assertTrue(all(r[0] <= span or r[0] == low or r[1] is None
                            for r in pre))
        self.assertNotIn("HILLSIDE TAX OFFICE", [r[2] for r in pre])
        hidden = [r for r in usual["rows"] if r["kind"] == "event"
                  and r["date"] > span and r["date"] != low]
        self.assertEqual(self.ctx["tl_rest"], len(hidden))
        # rows before the low are hidden: the count does not say "after"
        k = len(hidden)
        words = f"+ {k} more scheduled event{'' if k == 1 else 's'}"
        self.assertEqual(self.ctx["tl_rest_words"], words)
        st = self.st
        _s, _plain, html = report.build(st)
        self.assertIn(f"{words} — open Oikonome for the full timeline", html)
        self.assertNotIn("after the low point", html)

    def test_the_email_list_is_capped(self):
        from unittest import mock
        with mock.patch.object(todayview, "EMAIL_TIMELINE_MAX", 1):
            ctx = todayview.build_context(self.st)
        shown = [r for r in ctx["tl_pre"] if r[0] > TODAY.isoformat()
                 and r[1] is not None]
        self.assertEqual(len(shown), 1)
        usual = ctx["scenarios"][0]
        n = sum(1 for r in usual["rows"] if r["kind"] == "event")
        self.assertEqual(ctx["tl_rest"], n - 1)


class ALowInsideTheSpan(unittest.TestCase):
    def test_the_rest_is_after_the_low(self):
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=900)
            add_bill(conn, "HILLSIDE TAX OFFICE", 2400.0,
                     frequency="YEARLY", next_due=dt.date(2026, 7, 20),
                     last_seen=dt.date(2025, 7, 20))
            add_bill(conn, "ACME PAYROLL", 3000.0, income=True,
                     next_due=dt.date(2026, 7, 24),
                     last_seen=dt.date(2026, 6, 24))
            add_bill(conn, "BIRCHWOOD GYM", 40.0,
                     next_due=dt.date(2026, 7, 28),
                     last_seen=dt.date(2026, 6, 28))
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        ctx = todayview.build_context(st)
        low = ctx["scenarios"][0]["min_date"]
        self.assertLess(low, "2026-07-28")
        n = ctx["tl_rest"]
        self.assertGreater(n, 0)
        self.assertEqual(ctx["tl_rest_words"],
                         f"+ {n} more scheduled event{'' if n == 1 else 's'}"
                         " after the low point")

if __name__ == "__main__":
    unittest.main()
