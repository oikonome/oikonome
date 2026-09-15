"""the cash-flow report grows `cash_graph` — every historical
month's net (income − spend, the numbers the per-year charts already
render) continued by the 90-day forecast folded into calendar months.
The forward part is DERIVED from engine/forecast's autopay-statement
balance series (balance delta over a month = that month's net flow), so
it reconciles with the Today page by construction."""

import datetime as dt
import unittest

from oikonome.engine import forecast, reporting

from .util import add_txn, make_db, write_config

TODAY = dt.date.today()


def seed_income(conn, date, amount):
    # bank income = a negative-amount (money-in) deposit on a depository
    # account — exactly what reporting._income_by counts
    add_txn(conn, date, -amount, "EMPLOYER PAYROLL", account="chk",
            primary="INCOME")


def _month_add(d: dt.date, n: int) -> dt.date:
    y, m = d.year, d.month + n
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return d.replace(year=y, month=m, day=1)


class CashGraphTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # two fully-covered past months: income + spend
        for back in (2, 1):
            d = _month_add(TODAY, -back).replace(day=5)
            seed_income(self.conn, d, 5000.0)
            add_txn(self.conn, d.replace(day=12), 1200.0, "RENT LLC",
                    account="chk")
        # this month so far
        seed_income(self.conn, TODAY.replace(day=1), 5000.0)

    def tearDown(self):
        self.conn.close()

    def _graph(self):
        return reporting.compute_cashflow(self.conn)["cash_graph"]

    def test_history_months_carry_actual_nets(self):
        g = self._graph()
        keys = [p[0] for p in g["points"]]
        m1 = _month_add(TODAY, -1).strftime("%Y-%m")
        self.assertIn(m1, keys)
        self.assertEqual(dict(g["points"])[m1], 3800.0)   # 5000 − 1200
        # history is strictly before forecast_from
        self.assertEqual(keys.index(m1) < g["forecast_from"] or
                         g["forecast_from"] == 0, True)

    def test_forecast_months_reconcile_with_the_today_forecast(self):
        g = self._graph()
        fc = forecast.build(self.conn, days=forecast.HORIZON_DAYS)
        series = fc["pace_stmt"]["series"]
        # sum of every forecast-month net == (final balance − checking now)
        # + the current month's actual-so-far — pure algebra on the same
        # series the Today page charts
        fwd = [v for k, v in g["points"][g["forecast_from"]:]]
        spend_actual = 0.0                                # no spend this month
        income_actual = 5000.0
        expect = (series[-1][1] - fc["checking"]
                  + (income_actual - spend_actual))
        self.assertAlmostEqual(sum(fwd), expect, places=1)

    def test_forecast_from_points_at_the_current_month(self):
        g = self._graph()
        self.assertEqual(g["points"][g["forecast_from"]][0],
                         TODAY.strftime("%Y-%m"))
        # The forward part spans exactly the calendar months the horizon
        # touches — DERIVED, not a magic range. A hardcoded set of month
        # counts only fails on a 1st, when the horizon from the 1st lands
        # inside the second month — a real off-by-a-month lying in wait for
        # one day in thirty.
        end = TODAY + dt.timedelta(days=forecast.HORIZON_DAYS)
        span = (end.year - TODAY.year) * 12 + (end.month - TODAY.month) + 1
        self.assertEqual(len(g["points"]) - g["forecast_from"], span)


if __name__ == "__main__":
    unittest.main()
