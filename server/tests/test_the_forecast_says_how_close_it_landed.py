"""What the household sees of the forecast's own record
(specs/forecast-grading.md, "What the household sees"): the track-record
sentence once enough nights are graded, the lean only when the misses
lean, the "checks itself" note in the first weeks, nothing at all for a
household with no kept night; the band around every scenario once the
30-day misses qualify, read by the quiet warning; the panel and the drill,
whose parts add up to the miss to the cent. Figures and names are
invented."""

import datetime as dt
import json
import random
import unittest
from unittest import mock

from oikonome.engine import forecast, forecast_score as fs
from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date
H = fs.HORIZONS


class TheSentences(unittest.TestCase):
    def test_words(self):
        sc = {"horizon": 14, "within": 250.0, "window": "3 months",
              "typical_horizon": 30, "typical": 600.0, "typical_weeks": 13,
              "transfers_out": False, "bias": None,
              "band": {"z": [-50.0, 40.0], "power": 0.5, "n": 300},
              "first_night": "2026-01-01"}
        out = todayview.score_sentences(sc, TODAY)
        self.assertEqual(out["text"], "Over the last 3 months, the 14-day "
                         "forecast landed within $250 of your real balance "
                         "4 times in 5. Over your last 13 weeks the 30-day "
                         "forecast was typically within $600.")
        self.assertIsNone(out["bias_text"])
        self.assertIsNone(out["note"])
        self.assertEqual(out["basis"], "own")
        sc2 = dict(sc, horizon=None, transfers_out=True,
                   bias={"median": -231.0, "share_same_sign": 0.8},
                   band=None)
        out = todayview.score_sentences(sc2, TODAY)
        self.assertEqual(out["text"], "Over your last 13 weeks the 30-day "
                         "forecast was typically within $600, not counting "
                         "money you moved between your own accounts.")
        # real − predicted below zero: the forecast came out above
        self.assertEqual(out["bias_text"], "Lately the 30-day forecast has "
                         "come out about $250 above your real balance.")
        self.assertEqual(out["basis"], "none")
        out = todayview.score_sentences(
            dict(sc, bias={"median": 120.0, "share_same_sign": 0.9}), TODAY)
        self.assertIn("about $120 below your real balance", out["bias_text"])
        # a frequency with no longer horizon beside it
        out = todayview.score_sentences(dict(sc, typical_horizon=None), TODAY)
        self.assertEqual(out["text"], "Over the last 3 months, the 14-day "
                         "forecast landed within $250 of your real balance "
                         "4 times in 5.")

    def test_the_first_weeks_and_a_new_household(self):
        self.assertIsNone(todayview.score_sentences(None, TODAY))
        sc = {"horizon": None, "first_night": (TODAY - dt.timedelta(
            days=10)).isoformat(), "quantiles": {}}
        out = todayview.score_sentences(sc, TODAY)
        self.assertIsNone(out["text"])
        self.assertEqual(out["note"], "The forecast now checks itself "
                         "against your real balance each night; the first "
                         "results show here in about 6 weeks.")
        sc["first_night"] = (TODAY - dt.timedelta(days=50)).isoformat()
        self.assertIn("in about 1 week.",
                      todayview.score_sentences(sc, TODAY)["note"])
        # after 60 days with still nothing graded: nothing
        sc["first_night"] = (TODAY - dt.timedelta(days=61)).isoformat()
        self.assertIsNone(todayview.score_sentences(sc, TODAY)["note"])


def _household(checking=5000.0):
    conn = make_db()
    write_config(conn, food_monthly=600, other_monthly=600)
    conn.execute("UPDATE accounts SET balance_available=%s, "
                 "balance_current=%s, updated_at=now() WHERE id='chk'",
                 (checking, checking))
    d = TODAY - dt.timedelta(days=120)
    n = 0
    while d < TODAY:
        n += 1
        add_txn(conn, d, 12.0 + n % 17, f"SHOP {n % 5}",
                primary="FOOD_AND_DRINK", account="chk")
        d += dt.timedelta(days=1)
    add_bill(conn, "RIVERBEND POWER", 85.0, next_due=D(2026, 7, 21),
             last_seen=D(2026, 6, 21))
    return conn


def _plant_nights(conn, days, spread, *, miss=0.0, seed=1):
    """Kept nights whose 30-day (and every other) prediction landed within
    `spread` of the next nights' balances."""
    rng = random.Random(seed)
    first = TODAY - dt.timedelta(days=days)
    bal = {first + dt.timedelta(days=k): 5000.0 for k in range(days + 61)}
    for k in range(days):
        d = first + dt.timedelta(days=k)
        at = [round((bal[d + dt.timedelta(days=h)] - miss
                     + rng.uniform(-spread, spread)) * 100) for h in H]
        walks = {s: {"at": at, "cards_out": [0] * 5} for s in
                 ("usual", "now", "due", "low")}
        conn.execute(
            "INSERT INTO forecast_nights (as_of, model, checking_id, "
            "start_checking, fresh, walks, pace) VALUES "
            "(%s, %s, 'chk', %s, true, %s::jsonb, '{}')",
            (d, forecast.FORECAST_MODEL, bal[d], json.dumps(walks)))


def _context(conn, email=False):
    forecast.forget_learned()
    conn._memo = None
    st = report.gather(conn, TODAY, live=True)
    return todayview.build_context(st, email=email)


class WhatTheTodayPageSays(unittest.TestCase):
    def test_a_household_with_no_kept_night_sees_nothing_new(self):
        conn = _household()
        try:
            ctx = _context(conn)
        finally:
            conn.close()
        self.assertIsNone(ctx["forecast_score"])
        for sc in ctx["scenarios"]:
            self.assertIsNone(sc["band"])
            self.assertIsNone(sc["band_text"])
            self.assertIsNotNone(sc["heavy"])

    def test_a_graded_household_gets_its_line_and_its_band(self):
        conn = _household()
        try:
            _plant_nights(conn, 130, 150.0)
            ctx = _context(conn)
        finally:
            conn.close()
        sc = ctx["forecast_score"]
        # ninety days of 30-day misses are four separate stretches: the
        # frequency is the 14-day forecast's, the 30-day one its typical
        self.assertRegex(sc["text"], r"^Over the last 3 months, the 14-day "
                         r"forecast landed within \$[\d,]+ of your real "
                         r"balance 4 times in 5\. Over your last \d+ weeks "
                         r"the 30-day forecast was typically within \$")
        self.assertEqual(sc["basis"], "own")
        self.assertIsNone(sc["note"])
        for s in ctx["scenarios"]:
            self.assertEqual(len(s["band"]["lo"]), len(s["series"]))
            self.assertEqual([d for d, _ in s["band"]["lo"]],
                             [d for d, _ in s["series"]])
            for (_d, lo), (_d2, v), (_d3, hi) in zip(
                    s["band"]["lo"], s["series"], s["band"]["hi"]):
                self.assertLessEqual(lo, v + 1e-6)
                self.assertGreaterEqual(hi, v - 1e-6)
            self.assertEqual(s["band_text"], todayview.OWN_BAND_TEXT)

    def test_the_quiet_warning_reads_the_bands_low_edge(self):
        conn = _household(checking=2500.0)
        try:
            # wide misses: the band's low edge dips under $0 while the
            # usual walk stays above it
            _plant_nights(conn, 130, 2500.0)
            ctx = _context(conn)
        finally:
            conn.close()
        usual = ctx["scenarios"][0]
        if usual["min"] < 0:
            self.skipTest("the usual walk itself dips in this fixture")
        cb = ctx["cash_band"]
        self.assertIsNotNone(cb)
        self.assertIn(cb["kind"], ("band", "now"))
        self.assertIn("At the low end of the forecast's past misses it "
                      "would dip to -$", cb["text"])

    def test_the_first_weeks_say_the_forecast_checks_itself(self):
        conn = _household()
        try:
            _plant_nights(conn, 9, 50.0)
            ctx = _context(conn)
        finally:
            conn.close()
        sc = ctx["forecast_score"]
        self.assertIsNone(sc["text"])
        self.assertIn("checks itself", sc["note"])
        self.assertTrue(all(s["band"] is None for s in ctx["scenarios"]))

    def test_the_emails_print_the_line_and_never_the_note(self):
        conn = _household()
        try:
            _plant_nights(conn, 130, 150.0, miss=400.0)
            forecast.forget_learned()
            conn._memo = None
            st = report.gather(conn, TODAY, live=True)
            ctx = todayview.build_context(st)
            _subject, plain, html = report.build(st)
            conn.execute("DELETE FROM forecast_nights WHERE as_of "
                         "< %s", (TODAY - dt.timedelta(days=9),))
            conn._memo = None
            st2 = report.gather(conn, TODAY, live=True)
            early = todayview.build_context(st2)["forecast_score"]
            _s2, plain2, html2 = report.build(st2)
        finally:
            conn.close()
        sc = ctx["forecast_score"]
        for line in (sc["text"], sc["bias_text"]):
            self.assertTrue(line)
            self.assertIn(line, plain)
            self.assertIn(line.replace("'", "&#39;"), html)
        self.assertTrue(early["note"])
        self.assertNotIn("checks itself", plain2)
        self.assertNotIn("checks itself", html2)


class TheDoors(unittest.TestCase):
    def _call(self, conn, fn, **kw):
        from oikonome.web import api_today

        class Kept:
            def __init__(self, c):
                self._c = c

            def __getattr__(self, k):
                return getattr(self._c, k)

            def close(self):
                pass
        with mock.patch.object(api_today, "_conn",
                               return_value=Kept(conn)), \
             mock.patch.object(api_today, "_today", return_value=TODAY):
            return getattr(api_today, fn)({"tenant_id": "x"}, **kw)

    def test_the_panel(self):
        conn = _household()
        try:
            _plant_nights(conn, 130, 150.0)
            conn.execute("UPDATE forecast_nights SET fresh = false "
                         "WHERE as_of = %s", (TODAY - dt.timedelta(days=40),))
            out = self._call(conn, "forecast_score_api")
        finally:
            conn.close()
        self.assertGreaterEqual(len(out["history"]), 30)
        self.assertTrue(all(h["error"] == round(h["actual"] - h["predicted"],
                                                2) for h in out["history"]))
        # measured walk-forward: each check against the band as it stood
        # the night that forecast was made
        # at a horizon whose checks cover six separate stretches (14 days
        # here: ninety days of 30-day checks are about four)
        self.assertRegex(out["coverage_text"],
                         r"^Checked against later days, the real balance "
                         r"(7|14) days ahead landed inside the shaded band "
                         r"\d+ times in 10 \(\d+ checks\)\.$")
        self.assertTrue(any(t.endswith("not graded: your checking balance "
                                       "had not been updated.")
                            for t in out["skips_text"]), out["skips_text"])

    def test_the_drill_adds_up_to_the_cent(self):
        from fastapi import HTTPException
        conn = _household()
        try:
            forecast.forget_learned()
            self.assertTrue(fs.record(conn, TODAY))
            # a week later: spending, a bill, money to savings, a refund,
            # and a pending charge the balance already holds
            for k, (amt, name, prim) in enumerate((
                    (64.10, "SHOP 2", "FOOD_AND_DRINK"),
                    (88.40, "RIVERBEND POWER", "RENT_AND_UTILITIES"),
                    (300.0, "TO SAVINGS", "TRANSFER_OUT"),
                    (-12.25, "SHOP 3 REFUND", "GENERAL_MERCHANDISE"),
                    (412.0, "LAKEFRONT FURNITURE", "GENERAL_MERCHANDISE"))):
                add_txn(conn, TODAY + dt.timedelta(days=1 + k), amt, name,
                        primary=prim, account="chk", txn_id=f"wk{k}")
            conn.execute("UPDATE accounts SET balance_available = 4071.33, "
                         "balance_current = 4071.33, updated_at = now() "
                         "WHERE id = 'chk'")
            later = TODAY + dt.timedelta(days=7)
            forecast.forget_learned()
            conn._memo = None
            self.assertTrue(fs.record(conn, later))
            out = fs.miss(conn, later, 7, later + dt.timedelta(days=1))
            # a horizon whose forecast was never kept: 404
            from oikonome.web import api_today
            with mock.patch.object(api_today, "_today",
                                   return_value=later + dt.timedelta(days=1)):
                with self.assertRaises(HTTPException) as e:
                    self._call_at(conn, later, horizon=14)
            self.assertEqual(e.exception.status_code, 404)
        finally:
            conn.close()
        self.assertIsNotNone(out)
        cents = round(sum(p["diff"] for p in out["parts"]) * 100)
        self.assertEqual(cents, round(out["error"] * 100))
        self.assertEqual(round(out["error"], 2),
                         round(out["actual"] - out["predicted"], 2))
        kinds = {p["kind"]: p for p in out["parts"]}
        self.assertEqual(kinds["transfers"]["actual"], -300.0)
        self.assertEqual(kinds["other"]["actual"], 12.25)
        self.assertEqual(kinds["bills"]["actual"], -88.4)
        texts = " ".join(i["text"] for i in out["items"])
        self.assertIn("LAKEFRONT FURNITURE $412 on", texts)
        self.assertIn("moved out", texts)

    def test_a_payee_twice_in_the_window_is_one_cause(self):
        """Two paychecks of one payer inside the horizon are one line:
        what arrived against what was expected for that payer."""
        conn = _household()
        try:
            add_bill(conn, "OAKFIELD PAYROLL", 1500.0, income=True,
                     frequency="WEEKLY", interval=1,
                     next_due=TODAY + dt.timedelta(days=2),
                     last_seen=TODAY - dt.timedelta(days=5))
            for back in (19, 12, 5):
                add_txn(conn, TODAY - dt.timedelta(days=back), -1500.0,
                        "OAKFIELD PAYROLL", account="chk", primary="INCOME",
                        detailed="INCOME_WAGES")
            forecast.forget_learned()
            conn._memo = None
            self.assertTrue(fs.record(conn, TODAY))
            for k in (2, 9, 16, 23, 30):
                add_txn(conn, TODAY + dt.timedelta(days=k), -1500.0,
                        "OAKFIELD PAYROLL", account="chk", primary="INCOME",
                        detailed="INCOME_WAGES", txn_id=f"pay{k}")
            later = TODAY + dt.timedelta(days=30)
            forecast.forget_learned()
            conn._memo = None
            self.assertTrue(fs.record(conn, later))
            out = fs.miss(conn, later, 30, later + dt.timedelta(days=1))
        finally:
            conn.close()
        pay = [i for i in out["items"] if "OAKFIELD PAYROLL" in i["text"]]
        self.assertLessEqual(len(pay), 1, pay)
        part = next(p for p in out["parts"] if p["kind"] == "paychecks")
        for i in pay:
            self.assertAlmostEqual(i["amount"], part["diff"], places=2)

    def test_a_row_lands_with_the_bill_the_matcher_gave_it(self):
        """Two bills sharing a word: the payment of one is that bill's,
        as the bill matcher decides, never the other's because a word
        overlaps; a deposit is the paycheck series' that claims it."""
        conn = _household()
        try:
            for m in (4, 5, 6):
                add_txn(conn, D(2026, m, 18), 45.0, "CITY WATER",
                        account="chk")
                add_txn(conn, D(2026, m, 19), 60.0, "CITY GAS",
                        account="chk")
            add_bill(conn, "CITY WATER", 45.0, next_due=D(2026, 7, 18),
                     last_seen=D(2026, 6, 18))
            add_bill(conn, "CITY GAS", 60.0, next_due=D(2026, 7, 19),
                     last_seen=D(2026, 6, 19))
            forecast.forget_learned()
            conn._memo = None
            self.assertTrue(fs.record(conn, TODAY))
            # gas is paid, water is not; a deposit no paycheck claims
            add_txn(conn, D(2026, 7, 19), 60.0, "CITY GAS", account="chk",
                    txn_id="gas-paid")
            add_txn(conn, D(2026, 7, 17), -500.0, "SIDE GIG LLC",
                    account="chk", primary="INCOME", txn_id="side-gig")
            later = TODAY + dt.timedelta(days=7)
            forecast.forget_learned()
            conn._memo = None
            self.assertTrue(fs.record(conn, later))
            out = fs.miss(conn, later, 7, later + dt.timedelta(days=1))
        finally:
            conn.close()
        texts = [i["text"] for i in out["items"]]
        self.assertIn("CITY WATER had not been paid by 07/22.", texts)
        self.assertIn("SIDE GIG LLC $500 on 07/17 — income the forecast "
                      "did not expect.", texts)
        self.assertFalse([t for t in texts if t.startswith("CITY GAS")],
                         texts)

    def _call_at(self, conn, day, horizon):
        from oikonome.web import api_today

        class Kept:
            def __init__(self, c):
                self._c = c

            def __getattr__(self, k):
                return getattr(self._c, k)

            def close(self):
                pass
        with mock.patch.object(api_today, "_conn",
                               return_value=Kept(conn)):
            return api_today.forecast_miss_api(
                {"tenant_id": "x"}, day=day.isoformat(), horizon=horizon)


if __name__ == "__main__":
    unittest.main()
