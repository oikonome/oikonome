"""Cash Flow's two short windows: this month so far, and the last complete
month.

Every other range runs from some month's first day to today. "cur" is the
same shape one month long. "1m" is the one window that closes before today
— the last complete month — so a reader on the 13th can see August whole
instead of thirteen days of September. Both are cut server-side on the
household's date, like the rest, and both serve every cash-flow surface:
the flow picture, the spending window and the income window.
"""

import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import TODAY, add_txn, make_db, write_config


class ShortWindows(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # TODAY is mid-month: three months of groceries, one row each
        self.this = add_txn(self.conn, TODAY - dt.timedelta(days=3), 40.0,
                            "GROCER THIS MONTH", account="chk", primary="FOOD_AND_DRINK")
        last_month = (TODAY.replace(day=1) - dt.timedelta(days=1)).replace(day=10)
        self.last = add_txn(self.conn, last_month, 70.0, "GROCER LAST MONTH",
                            account="chk", primary="FOOD_AND_DRINK")
        before = (last_month.replace(day=1) - dt.timedelta(days=1)).replace(day=10)
        self.before = add_txn(self.conn, before, 90.0, "GROCER BEFORE",
                              account="chk", primary="FOOD_AND_DRINK")
        add_txn(self.conn, last_month, -3000.0, "NORTHWIND PAYROLL", account="chk",
                primary="INCOME", detailed="INCOME_WAGES")

    def tearDown(self):
        self.conn.close()

    def test_this_month_is_one_month_open_to_today(self):
        bank = reporting._bank_account_ids(self.conn)
        start, end, prev, label = reporting._range_window(self.conn, TODAY, "cur", bank)
        self.assertEqual(start, TODAY.replace(day=1))
        self.assertEqual(end, TODAY + dt.timedelta(days=1))
        self.assertEqual(prev, (TODAY.replace(day=1) - dt.timedelta(days=1)).replace(day=1))
        self.assertEqual(label, f"{reporting._MON[TODAY.month]} {TODAY.year}")

    def test_last_month_closes_before_today(self):
        bank = reporting._bank_account_ids(self.conn)
        start, end, prev, label = reporting._range_window(self.conn, TODAY, "1m", bank)
        month_start = TODAY.replace(day=1)
        self.assertEqual(end, month_start)
        self.assertEqual(start, (month_start - dt.timedelta(days=1)).replace(day=1))
        self.assertEqual(prev, (start - dt.timedelta(days=1)).replace(day=1))
        self.assertNotIn(reporting._MON[TODAY.month], label)

    def test_spending_window_counts_only_the_windows_rows(self):
        cur = reporting.spending_window(self.conn, TODAY, "cur")
        self.assertAlmostEqual(cur["total"], 40.0, places=2)
        self.assertAlmostEqual(cur["prev_total"], 70.0, places=2)
        last = reporting.spending_window(self.conn, TODAY, "1m")
        self.assertAlmostEqual(last["total"], 70.0, places=2)
        self.assertAlmostEqual(last["prev_total"], 90.0, places=2)

    def test_income_window_and_flow_picture_follow_the_same_window(self):
        inc = reporting.income_window(self.conn, TODAY, "1m")
        self.assertAlmostEqual(inc["total"], 3000.0, places=2)
        self.assertAlmostEqual(reporting.income_window(self.conn, TODAY, "cur")["total"],
                               0.0, places=2)
        flow = reporting.flow_breakdown(self.conn, TODAY, "1m")
        self.assertEqual(flow["to"][:7],
                         (TODAY.replace(day=1) - dt.timedelta(days=1)).isoformat()[:7])
        self.assertAlmostEqual(flow["out"]["total"], 70.0, places=2)

    def test_a_one_month_window_paces_over_one_month(self):
        """The "/mo" divisor counts the WINDOW's months, not the wall
        clock's. "1m" covers exactly one complete calendar month, so its
        pace IS that month's total: $70 of June groceries read on July
        15th is $70/mo. Counted to today instead, the in-progress month
        the window deliberately excludes joins the divisor and halves
        every pace on the Spending and Income tabs."""
        for w in (reporting.spending_window(self.conn, TODAY, "1m"),
                  reporting.income_window(self.conn, TODAY, "1m")):
            self.assertEqual(w["months"], 1)
        self.assertAlmostEqual(
            reporting.spending_window(self.conn, TODAY, "1m")["total"] / 1,
            70.0, places=2)

    def test_this_month_paces_over_one_month(self):
        """"cur" is one month long however far into it the reader is —
        the month counts whole, like every other range's live month."""
        for w in (reporting.spending_window(self.conn, TODAY, "cur"),
                  reporting.income_window(self.conn, TODAY, "cur")):
            self.assertEqual(w["months"], 1)

    def test_a_window_reports_its_own_last_day(self):
        """`to` is the last day the window covers. For "1m" that is the
        last day of last month; reporting today would stamp a window that
        closed in June with a July date, and anything built from the pair
        (a ledger link, an export header) would then name days whose rows
        the figures above it never counted."""
        eom = TODAY.replace(day=1) - dt.timedelta(days=1)
        for w in (reporting.spending_window(self.conn, TODAY, "1m"),
                  reporting.income_window(self.conn, TODAY, "1m")):
            self.assertEqual(w["to"], eom.isoformat())
        for w in (reporting.spending_window(self.conn, TODAY, "cur"),
                  reporting.income_window(self.conn, TODAY, "cur")):
            self.assertEqual(w["to"], TODAY.isoformat())

    def test_ranges_that_run_to_today_still_count_the_live_month(self):
        """The other direction of the same rule: a range whose window is
        open to now ends today and counts the in-progress month whole, as
        its label promises ("May–Jul" is three months, not two-and-a-half)."""
        for w in (reporting.spending_window(self.conn, TODAY, "3m"),
                  reporting.income_window(self.conn, TODAY, "3m")):
            self.assertEqual(w["months"], 3)
            self.assertEqual(w["to"], TODAY.isoformat())

    def test_the_routes_accept_both_keys(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import oikonome.web.app as appmod
        from oikonome.web import reporting_api
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        api_app = FastAPI()
        api_app.include_router(reporting_api.router)
        api_app.dependency_overrides[appmod.current_user] = (
            lambda: {"tenant_id": tid})
        c = TestClient(api_app)
        for key in ("cur", "1m"):
            for path in ("/api/reports/spending/window", "/api/reports/income/window",
                         "/api/reports/cashflow/flow"):
                r = c.get(path, params={"range": key})
                self.assertEqual(r.status_code, 200, (path, key, r.text[:200]))
        self.assertEqual(c.get("/api/reports/income/window",
                               params={"range": "2m"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
