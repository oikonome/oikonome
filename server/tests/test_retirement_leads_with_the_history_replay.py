"""Retirement's headline is the history replay, the flat rate comes second.

What breaks if these fail: the page says "not by 72" on a flat real
return while its own replay of every past market says retiring at 62
lasts in 9 of 10 of them; or the chart under the verdict is a
keep-working curve that never draws a dollar, dated from June of a year
already half gone (specs/retirement-figures.md).
"""

import datetime as dt
import unittest

from oikonome.engine import retirement as R

from .util import make_db

KW = dict(end_age=95, nominal=0.05, inflation=0.03, ss_monthly=0, ss_age=67,
          rental_monthly=0, td_annual=0, taxable_annual=0, resume_age=60,
          retire_max=65)


class HistoryLeadsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()
        cls.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('b1','it1','Brokerage','investment',"
            "'brokerage',900000)")
        cls.r = R.project(cls.conn, age=60, spend=40_000, **KW)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_the_verdict_is_the_first_age_lasting_in_nine_of_ten_past_markets(self):
        v = self.r["verdict"]
        lasting = [row["age"] for row in self.r["rows"]
                   if row["hist_pct"] >= 90]
        self.assertEqual(v["age"], lasting[0])
        self.assertEqual((v["basis"], v["tone"], v["at_age"]),
                         ("history", "good", lasting[0]))
        self.assertEqual(v["in_ten"], v["ok"] * 10 // v["n"])
        self.assertGreaterEqual(v["in_ten"], 9)
        self.assertIsNone(v["catch_up"])

    def test_the_steady_model_is_kept_second(self):
        # the flat 1.9% real return finds no age; the replay does
        self.assertIsNone(self.r["earliest"])
        self.assertIsNone(self.r["verdict"]["steady_age"])
        self.assertIsNotNone(self.r["verdict"]["age"])

    def test_each_row_carries_the_replays_verdict(self):
        for row in self.r["rows"]:
            self.assertEqual(row["hist_lasts"], row["hist_pct"] >= 90)

    def test_no_lasting_age_says_what_saving_would_make_one(self):
        r = R.project(self.conn, age=60, spend=50_000, **KW)
        v = r["verdict"]
        self.assertIsNone(v["age"])
        self.assertEqual((v["tone"], v["at_age"]), ("bad", 65))
        [c] = v["catch_up"]
        self.assertEqual(c["age"], 65)
        self.assertEqual(c["extra_monthly"] % 10, 0)
        # and that saving really reaches nine in ten
        kw = dict(rr=R.real_return(0.05, 0.03), ss_annual=0, ss_age=67,
                  rental_annual=0, td_annual=0, taxable_annual=0,
                  resume_age=60, eff_ord=0.25, eff_cg=0.18, gain_frac=0.5,
                  cash_rr=0.0, rmd_start=R.RMD_START,
                  debt_rr=R.debt_real_return(r["buckets"], 0.03))
        paths = R._hist_paths(36, 0.9)
        ok = sum(R.simulate(r["buckets"], 60, 65, 95, 50_000, rr_path=p,
                            extra_annual=c["extra_monthly"] * 12,
                            **kw)["survived"] for p in paths)
        self.assertGreaterEqual(ok * 10, 9 * len(paths))

    def test_the_chart_is_the_plan_retiring_at_the_verdicts_age(self):
        path = self.r["growth_path"]
        today = dt.date.today()
        self.assertEqual(path[0][0], f"{today.year}-{today.month:02d}")
        self.assertEqual(self.r["growth_path_retire_age"],
                         self.r["verdict"]["age"])
        # one point a year to the plan's end age, not cut off at 80
        self.assertEqual(len(path), 95 - 60 + 2)
        self.assertEqual(path[0][1], self.r["buckets"]["total"])


if __name__ == "__main__":
    unittest.main()
