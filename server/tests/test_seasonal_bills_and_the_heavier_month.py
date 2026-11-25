"""Amounts known to vary (specs/today-figures.md, "A seasonal bill is
estimated from last year", "A heavier month is drawn beside the expected
one"): a seasonal bill's forecast occurrence walks last year's amount for
the month scaled by this year's trend, labelled an estimate, while the
bill, the plan and the still-due figures keep its amount; each card scenario carries the same walk
at a heavier pace — the household's own one-in-five heavier 60 days — and
when only that line dips below $0 a quiet line says so, apart from the
cash notice. Card spending a walk still owes on its last day is named, not
dropped. Figures and names are invented."""

import datetime as dt
import unittest
from unittest import mock

from markupsafe import escape

from oikonome.engine import budget, forecast
from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date


def _history(amounts: dict) -> list:
    """Monthly payments on the 10th, newest first: {(y, m): amount}."""
    return sorted(((D(y, m, 10), a, "chk") for (y, m), a in amounts.items()),
                  reverse=True)


def _months(start: tuple, vals: list) -> dict:
    y, m = start
    out = {}
    for v in vals:
        out[(y, m)] = v
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    return out


MONTHLY = {"amount": 110.0, "recurrence": {"frequency": "MONTHLY",
                                           "interval": 1, "byMonthDay": [10]}}
NOV = D(2026, 11, 20)


def _invented_year():
    # Jul 2025 .. Oct 2026: last December $120; Aug-Oct $90, $95, $100 a
    # year ago and $99, $104, $110 this year
    return _months((2025, 7), [85, 90, 95, 100, 112, 120, 125, 118, 104, 96,
                               92, 90, 88, 99, 104, 110])


def _household(checking: float, days: int = 120, per_day: float = 50.0,
               account: str = "chk"):
    conn = make_db()
    write_config(conn, food_monthly=600, other_monthly=600)
    conn.execute("UPDATE accounts SET balance_available=%s, "
                 "balance_current=%s WHERE id='chk'", (checking, checking))
    conn.execute("UPDATE accounts SET balance_current=0 WHERE id='card'")
    d = TODAY - dt.timedelta(days=days)
    while d < TODAY:
        add_txn(conn, d, per_day, "CORNER MARKET", account=account,
                primary="FOOD_AND_DRINK")
        d += dt.timedelta(days=1)
    return conn


class SeasonalBills(unittest.TestCase):
    amount = staticmethod(forecast.occurrence_amount)

    def test_last_years_month_times_this_years_trend(self):
        got = self.amount(MONTHLY, _history(_invented_year()),
                          D(2026, 12, 10), NOV)
        self.assertEqual(got, (131.79, True))

    def test_the_trend_is_held_between_four_fifths_and_five_fourths(self):
        hot = _invented_year()
        hot.update({(2026, 8): 300, (2026, 9): 300, (2026, 10): 300})
        self.assertEqual(self.amount(MONTHLY, _history(hot), D(2026, 12, 10),
                                     NOV), (round(120 * 1.25, 2), True))
        cold = _invented_year()
        cold.update({(2026, 8): 10, (2026, 9): 10, (2026, 10): 10})
        self.assertEqual(self.amount(MONTHLY, _history(cold),
                                     D(2026, 12, 10), NOV),
                         (round(120 * 0.80, 2), True))

    def test_a_steady_short_hand_set_or_yearly_bill_walks_its_amount(self):
        steady = {k: 110.0 + (k[1] % 2) for k in _invented_year()}
        self.assertEqual(self.amount(MONTHLY, _history(steady),
                                     D(2026, 12, 10), NOV), (110.0, False))
        short = dict(list(sorted(_invented_year().items()))[-14:])
        self.assertEqual(self.amount(MONTHLY, _history(short),
                                     D(2026, 12, 10), NOV), (110.0, False))
        self.assertEqual(self.amount(MONTHLY, _history(_invented_year()),
                                     D(2026, 12, 10), NOV, hand_set=True),
                         (110.0, False))
        yearly = dict(MONTHLY, recurrence={"frequency": "YEARLY",
                                           "interval": 1})
        self.assertEqual(self.amount(yearly, _history(_invented_year()),
                                     D(2026, 12, 10), NOV), (110.0, False))

    def test_generated_histories_stay_inside_the_clamps(self):
        import random
        from . import props
        for seed in props.seeds(30):
            rng = random.Random(seed)
            vals = _months((2025, 7), [rng.randint(40, 260) for _ in range(16)])
            for gone in rng.sample(sorted(vals), rng.randint(0, 2)):
                del vals[gone]
            amt, est = self.amount(MONTHLY, _history(vals), D(2026, 12, 10),
                                   NOV)
            lo, hi = forecast.SEASONAL_AMOUNT_CLAMP
            self.assertTrue(110.0 * lo - 0.005 <= amt <= 110.0 * hi + 0.005)
            if est:
                last = vals[(2025, 12)]
                rlo, rhi = forecast.SEASONAL_RATIO_CLAMP
                self.assertTrue(min(last * rlo, 110 * lo) - 0.01 <= amt
                                <= max(last * rhi, 110 * hi) + 0.01)
            else:
                self.assertEqual(amt, 110.0)

    def test_only_the_forecasts_occurrence_moves(self):
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=600)
            conn.execute("UPDATE accounts SET balance_available=5000, "
                         "balance_current=5000 WHERE id='chk'")
            vals = _months((2025, 2), [100, 96, 92, 90, 95, 100, 106, 112,
                                       118, 122, 120, 112, 104, 98, 94, 92,
                                       96])
            for (y, m), a in vals.items():
                add_txn(conn, D(y, m, 20), float(a), "ACME POWER",
                        account="chk")
            add_bill(conn, "ACME POWER", 104.0, next_due=D(2026, 7, 20),
                     last_seen=D(2026, 6, 20))
            st = report.gather(conn, TODAY)
            with mock.patch.object(forecast, "occurrence_amount",
                                   side_effect=lambda b, *a, **k: (
                                       float(b["amount"]), False)):
                plain = report.gather(conn, TODAY)
        finally:
            conn.close()
        fc = st["forecast"]
        est = [(d, a) for d, a, p in fc["events"]
               if p == "ACME POWER (estimate)"]
        self.assertTrue(est, fc["events"])
        self.assertTrue(all(a != -104.0 for _d, a in est), est)
        self.assertIn("ACME POWER (estimate)", fc["estimated"])
        # the bill, the plan and the month's figures keep the bill's amount
        for k in ("month_budget", "unpaid_due"):
            self.assertEqual(st["buckets"]["fixed"][k],
                             plain["buckets"]["fixed"][k], k)
        self.assertEqual(st.get("runway"), plain.get("runway"))
        ctx = todayview.build_context(st)
        rows = [r for sc in ctx["scenarios"] for r in sc["rows"]
                if r["label"].startswith("ACME POWER")]
        self.assertTrue(rows and all(r["estimated"] for r in rows), rows)


class HeavierMonth(unittest.TestCase):
    def test_the_heavier_line_never_sits_above_the_usual_one(self):
        conn = _household(3300.0)
        try:
            f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        for key, walk in (("usual", "pace_usual"), ("now", "pace_now"),
                          ("due", "pace_due")):
            for (d0, b0), (_d1, b1) in zip(f[walk]["series"],
                                           f["heavy"][key]["series"]):
                self.assertLessEqual(b1, b0 + 0.005, (key, d0))

    def test_a_factor_of_one_is_the_same_walk(self):
        conn = _household(3300.0)
        try:
            with mock.patch.object(forecast, "heavy_month_factor",
                                   return_value={"factor": 1.0, "measured": 0,
                                                 "basis": "default"}):
                f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        self.assertEqual(f["pace_low"]["series"], f["pace_usual"]["series"])

    def test_the_factor_is_the_households_own_and_held_in_bounds(self):
        # four months: under half a year of ledger
        conn = _household(3300.0)
        try:
            h = forecast.heavy_month_factor(conn, TODAY)
        finally:
            conn.close()
        self.assertEqual((h["factor"], h["basis"]),
                         (forecast.HEAVY_DEFAULT, "default"))
        # sixteen steady months: the ratio is 1, held up at the floor
        conn = _household(3300.0, days=490)
        try:
            h = forecast.heavy_month_factor(conn, TODAY)
        finally:
            conn.close()
        self.assertEqual(h["basis"], "household")
        self.assertGreater(h["measured"], 300)
        self.assertEqual(h["factor"], forecast.HEAVY_CLAMP[0])
        # a household whose every other 60 days is four times as heavy:
        # held at the top
        conn = _household(3300.0, days=490)
        try:
            d = TODAY - dt.timedelta(days=490)
            while d < TODAY:
                if (d.toordinal() // 60) % 2:
                    add_txn(conn, d, 150.0, f"CORNER MARKET {d.day}",
                            primary="FOOD_AND_DRINK", account="chk")
                d += dt.timedelta(days=1)
            h = forecast.heavy_month_factor(conn, TODAY)
        finally:
            conn.close()
        self.assertEqual(h["factor"], forecast.HEAVY_CLAMP[1])

    def test_the_line_is_the_forecasts_own_pace_crossed_one_time_in_five(self):
        """On generated households with known variance — ordinary days
        around $60, heavier weeks now and then, repairs, ties at one
        merchant — each ratio is the 30 days' variable spending over
        forecast_pace as of that day (the same rows, the same one-off
        rule), and the 30 days ran above pace x factor in 18-22 % of the
        windows."""
        import random
        from . import props
        for seed in props.seeds(3):
            rng = random.Random(seed)
            conn = make_db()
            try:
                write_config(conn, food_monthly=600, other_monthly=600)
                d = TODAY - dt.timedelta(days=430)
                heavy_until = d
                k = 0
                while d < TODAY:
                    if d >= heavy_until and rng.random() < 0.03:
                        heavy_until = d + dt.timedelta(days=rng.randint(7, 15))
                    amt = rng.uniform(30, 90) * (2.5 if d < heavy_until else 1)
                    add_txn(conn, d, round(amt, 2), "CORNER MARKET",
                            account="chk", primary="FOOD_AND_DRINK")
                    if rng.random() < 0.01:
                        add_txn(conn, d, 900.0, f"REPAIR SHOP {k}",
                                account="chk")
                        k += 1
                    if rng.random() < 0.03:
                        for _ in range(2):
                            add_txn(conn, d, 274.51, "TWIN SHOP",
                                    account="chk")
                    d += dt.timedelta(days=1)
                forecast.forget_learned()
                st = budget.month_status(conn, TODAY)
                st_y = budget.month_status(conn, TODAY - dt.timedelta(days=1))
                rows, since, _n = forecast.variable_rows_since(
                    conn, TODAY, TODAY - dt.timedelta(days=425), st=st_y)
                plan = 1200.0 / 31
                ratios = forecast.heavy_ratios(conn, rows, since, TODAY, plan)
                factor = forecast.heavy_month_factor(conn, TODAY,
                                                     st=st)["factor"]
                # spot days: the ratio is the real forecast_pace as of d
                first = TODAY - dt.timedelta(days=365)
                for off in (0, 97, 211, 300):
                    day = first + dt.timedelta(days=off)
                    fp = forecast.forecast_pace(conn, day)["daily"]
                    ahead = sum(r["amount"] for r in rows
                                if day <= r["date"] < day
                                + dt.timedelta(days=30)) / 30
                    self.assertAlmostEqual(ratios[off], ahead / fp, places=6,
                                           msg=(seed, day))
            finally:
                conn.close()
            with self.subTest(seed=seed, factor=factor):
                self.assertTrue(forecast.HEAVY_CLAMP[0] < factor
                                < forecast.HEAVY_CLAMP[1], factor)
                crossed = sum(1 for r in ratios if r > factor) / len(ratios)
                self.assertTrue(0.18 <= crossed <= 0.22, crossed)

    def test_many_windows_decide_one_offs_as_window_one_offs_does(self):
        import random
        from . import props
        for seed in props.seeds(6):
            rng = random.Random(seed)
            rows, d = [], TODAY - dt.timedelta(days=200)
            while d < TODAY:
                rows.append({"date": d, "amount": round(rng.uniform(5, 90), 2),
                             "txn_id": f"t{len(rows)}", "payee": "SHOP",
                             "merchant_id": None})
                # a weekly deli whose large order is judged apart from
                # its visits, and a diagnostic days before a repair
                if rng.random() < 0.15:
                    rows.append({"date": d, "amount": 14.0,
                                 "txn_id": f"v{len(rows)}",
                                 "payee": "DELI", "merchant_id": None})
                if rng.random() < 0.02:
                    rows.append({"date": d, "amount": rng.choice(
                                     [450.0, 900.0]),
                                 "txn_id": f"o{len(rows)}",
                                 "payee": "DELI", "merchant_id": None})
                if rng.random() < 0.02:
                    for k, amt in ((0, 75.0), (rng.randint(1, 9), 1100.0)):
                        rows.append({"date": d + dt.timedelta(days=k),
                                     "amount": amt,
                                     "txn_id": f"r{len(rows)}",
                                     "payee": "GARAGE", "merchant_id": None})
                if rng.random() < 0.05:
                    amt = rng.choice([274.51, 600.0, 1200.0, 333.33])
                    for _ in range(rng.choice([1, 2])):
                        rows.append({"date": d, "amount": amt,
                                     "txn_id": f"b{len(rows)}",
                                     "payee": rng.choice(["TWIN SHOP",
                                                          "REPAIR SHOP"]),
                                     "merchant_id": None})
                d += dt.timedelta(days=1)

            def habit(lead, on, start, total):
                return (1, 1) if total % 7 == 0 else (0, 0)
            fast = forecast._WindowOneOffs(rows, TODAY - dt.timedelta(
                days=200), TODAY, habit)
            for off in range(0, 140, 3):
                start = TODAY - dt.timedelta(days=200 - off)
                win = [r for r in rows if start <= r["date"]
                       < start + dt.timedelta(days=60)]
                want = sum(round(o["amount"] * 100) for o in
                           forecast.window_one_offs(None, win, start, 60,
                                                    habit_of=habit))
                with self.subTest(seed=seed, start=start):
                    self.assertEqual(fast.cents(start, 60), want)

    def test_only_the_heavier_line_below_zero_is_a_quiet_line(self):
        conn = _household(3300.0)
        try:
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        ctx = todayview.build_context(st)
        fc = st["forecast"]
        self.assertGreaterEqual(fc["pace_usual"]["min"], 0)
        self.assertLess(fc["pace_low"]["min"], 0)
        band = ctx["cash_band"]
        self.assertEqual(band["low"], fc["pace_low"]["min"])
        self.assertTrue(band["text"].startswith(
            "Your usual pattern stays above $0 (low "), band["text"])
        self.assertEqual(band["kind"], "heavier")
        self.assertIn("(25% above your usual pace)", band["text"])
        self.assertIsNone(ctx["cash_notice"]["state"])
        _s, plain, html = report.build(st)
        self.assertIn(band["text"], plain)
        self.assertIn(str(escape(band["text"])), html)

    def test_when_the_usual_walk_goes_below_zero_the_notice_speaks_alone(self):
        conn = _household(2000.0)
        try:
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        ctx = todayview.build_context(st)
        self.assertLess(st["forecast"]["pace_usual"]["min"], 0)
        self.assertIsNone(ctx["cash_band"])
        self.assertIsNotNone(ctx["cash_notice"]["state"])


class SurprisesAreInsideTheHeavierLine(unittest.TestCase):
    """The heavier line is one empirical ratio with one-offs already in
    it: a household whose stretches carry repairs runs a higher factor
    than the same household without them, and the legend and the quiet
    line say the same words."""

    def _household(self, repairs):
        conn = _household(4000.0, days=400)
        if repairs:
            d = TODAY - dt.timedelta(days=390)
            k = 0
            while d < TODAY:
                add_txn(conn, d, 1200.0, f"REPAIR SHOP {k}", account="chk")
                d += dt.timedelta(days=75)
                k += 1
        return conn

    def test_one_offs_raise_the_factor_and_nothing_is_added_beside_it(self):
        plain, rough = self._household(False), self._household(True)
        try:
            forecast.forget_learned()
            a = forecast.heavy_month_factor(plain, TODAY)
            b = forecast.heavy_month_factor(rough, TODAY)
            st = report.gather(rough, TODAY)
        finally:
            plain.close()
            rough.close()
        self.assertGreater(b["factor"], a["factor"])
        self.assertNotIn("allowance", b)
        fc = st["forecast"]
        self.assertAlmostEqual(fc["pace_low"]["rate"] - fc["pace_usual"]["rate"],
                               round(fc["pace_basis"]["daily"]
                                     * (b["factor"] - 1), 2), delta=0.02)
        ctx = todayview.build_context(st)
        words = ctx["scenarios"][0]["heavy_label"]
        pct = round(b["factor"] * 100) - 100
        self.assertEqual(words, f"heavier month — spending {pct}% above the "
                                "forecast's pace, as one 30-day stretch in "
                                "five ran in the last year")
        money = todayview.charts._money
        self.assertTrue(todayview.pace_sentence(fc).endswith(
            " A heavier month spends "
            f"{money(fc['pace_basis']['daily'] * b['factor'])} a day on "
            "everyday purchases."))


class PayingEveryCardNow(unittest.TestCase):
    """The quiet line also reads cash after card balances: when the usual
    walk stays above $0 and checking less what is owed on cards would not,
    it says so, with that walk's own figures."""

    def test_the_pay_now_dip_is_said_beside_the_usual_low(self):
        from oikonome.engine.compat import jsonb
        conn = _household(3600.0, per_day=80.0, account="card")
        try:
            conn.execute("UPDATE accounts SET balance_current=1200 "
                         "WHERE id='card'")
            conn.execute(
                "INSERT INTO liabilities (account_id, raw) VALUES "
                "('card', %s)", (jsonb({
                    "last_statement_balance": 1200.0,
                    "last_statement_issue_date": "2026-07-10",
                    "next_payment_due_date": "2026-08-05"}),))
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        fc = st["forecast"]
        ctx = todayview.build_context(st)
        self.assertGreaterEqual(fc["pace_usual"]["min"], 0)
        self.assertLess(fc["pace_now"]["min"], 0)
        band = ctx["cash_band"]
        self.assertEqual(band["kind"], "after_cards")
        money = todayview.charts._money
        self.assertTrue(band["text"].startswith(
            f"Your usual pattern stays above $0 (low "
            f"{money(fc['pace_stmt']['min'])} on "), band["text"])
        self.assertIn(
            f"On {todayview.fmt_date(fc['pace_now']['min_date'])} your card "
            f"balances would be about {money(-fc['pace_now']['min'])} more "
            "than your checking — you could not clear every card that day.",
            band["text"])
        self.assertEqual((band["low"], band["date"]),
                         (fc["pace_now"]["min"], fc["pace_now"]["min_date"]))
        self.assertIsNone(ctx["cash_notice"]["state"])
        _s, plain, html = report.build(st)
        self.assertIn(band["text"], plain)
        self.assertIn(str(escape(band["text"])), html)


class CardSpendingAfterTheChart(unittest.TestCase):
    def test_what_the_walk_still_owes_is_named(self):
        conn = _household(9000.0, account="card")
        try:
            from oikonome.engine.compat import jsonb
            conn.execute(
                "INSERT INTO liabilities (account_id, raw) VALUES "
                "('card', %s)", (jsonb({
                    "last_statement_issue_date": "2026-07-10",
                    "next_payment_due_date": "2026-08-05"}),))
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        ctx = todayview.build_context(st)
        usual = ctx["scenarios"][0]
        self.assertGreater(usual["cards_after_horizon"], 0.5)
        self.assertEqual(
            usual["cards_note"],
            f"{todayview.charts._money(usual['cards_after_horizon'])} of "
            "card spending is paid after 09/13/26, when the chart ends.")
        # pay all cards today goes on as the usual walk does, so it owes
        # the same later spending at the chart's end
        now = ctx["scenarios"][1]
        self.assertEqual((now["key"], now["cards_note"]),
                         ("now", usual["cards_note"]))
        _s, plain, html = report.build(st)
        self.assertIn(usual["cards_note"], plain)
        self.assertIn(str(escape(usual["cards_note"])), html)


if __name__ == "__main__":
    unittest.main()
