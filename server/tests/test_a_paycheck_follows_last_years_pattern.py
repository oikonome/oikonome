"""A paycheck that changes through the year (lower early in the year while
benefits and taxes reset, higher once a yearly wage maximum is paid) is
walked by the series' own last year (specs/today-figures.md, "A paycheck
follows last year's pattern"): a future payday's estimate is the current
amount times last year's deposit at that point in the year over last
year's deposit at today's point, held between 0.85 and 1.15, only when
last year shows a step that held for two deposits in a row. A raise
carries over as the ratio, not the level; a changed pay cadence or a
series under a year old walks the current amount. Only the forecast's
occurrence moves: the plan's income, the paychecks still due and the
month's income keep their figures. Figures and names are invented."""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import forecast
from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date
EST = "(estimate, from last year's pattern)"


def biweekly(first: D, last: D, amount_on):
    out, d = [], first
    while d <= last:
        out.append({"date": d, "amount": float(amount_on(d))})
        d += dt.timedelta(days=14)
    return out


def last_years_shape(d: D) -> float:
    # lower through February, the usual level, higher from November
    if d.month <= 2:
        return 1900.0
    if d.month >= 11:
        return 2100.0
    return 2000.0


OCT = D(2026, 10, 16)


class ThePattern(unittest.TestCase):
    amount = staticmethod(forecast.paycheck_pattern_amount)

    def test_a_step_last_year_carries_as_a_ratio(self):
        deps = biweekly(D(2025, 1, 2), D(2026, 10, 9), last_years_shape)
        # a raise since: the current check is $2,300
        self.assertEqual(self.amount(2300.0, deps, D(2026, 11, 20), OCT),
                         (round(2300.0 * 2100 / 2000, 2), True))

    def test_no_step_no_estimate(self):
        deps = biweekly(D(2025, 1, 2), D(2026, 10, 9), lambda d: 2000.0)
        self.assertEqual(self.amount(2000.0, deps, D(2026, 11, 20), OCT),
                         (2000.0, False))

    def test_a_single_bonus_check_is_no_step(self):
        deps = biweekly(D(2025, 1, 2), D(2026, 10, 9),
                        lambda d: 3500.0 if d == D(2025, 11, 20) else 2000.0)
        self.assertEqual(self.amount(2000.0, deps, D(2026, 11, 19), OCT),
                         (2000.0, False))

    def test_the_ratio_is_held_between_085_and_115(self):
        deps = biweekly(D(2025, 1, 2), D(2026, 10, 9),
                        lambda d: 2900.0 if d.month >= 11 else 2000.0)
        self.assertEqual(self.amount(2000.0, deps, D(2026, 11, 20), OCT),
                         (2300.0, True))

    def test_a_changed_cadence_or_a_new_series_walks_the_amount(self):
        monthly_then = [{"date": D(2025, m, 1),
                         "amount": last_years_shape(D(2025, m, 1)) * 2}
                        for m in range(1, 13)]
        biweekly_now = biweekly(D(2026, 1, 2), D(2026, 10, 9),
                                last_years_shape)
        self.assertEqual(self.amount(2000.0, monthly_then + biweekly_now,
                                     D(2026, 11, 20), OCT), (2000.0, False))
        young = biweekly(D(2026, 1, 9), D(2026, 10, 9), last_years_shape)
        self.assertEqual(self.amount(2000.0, young, D(2026, 11, 20), OCT),
                         (2000.0, False))


class OnlyTheForecastMoves(unittest.TestCase):
    def test_the_plan_and_the_still_due_figures_keep_their_amounts(self):
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=600)
            conn.execute("UPDATE accounts SET balance_available=4000, "
                         "balance_current=4000 WHERE id='chk'")
            # last year: $2,000 until August, $2,200 from then on; this
            # year $2,100 so far
            d = D(2025, 1, 2)
            while d < TODAY:
                amt = (2100.0 if d.year == 2026
                       else 2200.0 if d >= D(2025, 7, 31) else 2000.0)
                add_txn(conn, d, -amt, "NORTHSTAR PAYROLL", account="chk",
                        primary="INCOME", detailed="INCOME_WAGES")
                last = d
                d += dt.timedelta(days=14)
            add_bill(conn, "NORTHSTAR PAYROLL", 2100.0, income=True,
                     frequency="WEEKLY", interval=2,
                     next_due=last + dt.timedelta(days=14), last_seen=last)
            st = report.gather(conn, TODAY)
            with mock.patch.object(forecast, "paycheck_pattern_amount",
                                   side_effect=lambda cur, *a, **k: (cur,
                                                                     False)):
                plain = report.gather(conn, TODAY)
        finally:
            conn.close()
        fc = st["forecast"]
        est = [(dd, a) for dd, a, p in fc["events"]
               if p == f"NORTHSTAR PAYROLL {EST}"]
        self.assertTrue(est, fc["events"])
        self.assertTrue(all(a == round(2100.0 * 1.1, 2) for _d, a in est
                            if _d >= "2026-08-01"), est)
        for k in ("income", "income_expected", "income_due_total",
                  "plan_surplus"):
            self.assertEqual(st.get(k), plain.get(k), k)
        self.assertIn(f"NORTHSTAR PAYROLL {EST}", fc["estimated"])
        ctx = todayview.build_context(st)
        rows = [r for r in ctx["scenarios"][0]["rows"]
                if r["label"].startswith("NORTHSTAR PAYROLL")]
        self.assertTrue(all(r["estimated"] for r in rows if
                            r["label"].endswith(EST)))


if __name__ == "__main__":
    unittest.main()
