"""The Cash Flow page's flow picture splits the same money the page's
table counts: income by kind on the left, fixed bills / variable spend /
saved on the right, for any of the site-wide timeframes (3m/6m/1y/3y/5y/
all) — and the two sides balance."""

import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import add_bill, make_db, seed_accounts, write_config


def _txn(conn, acct, day, amount, name, **cols):
    keys = ["id", "account_id", "date", "amount", "name", *cols]
    vals = [f"fl-{name}-{day}", acct, day, amount, name, *cols.values()]
    conn.execute(f"INSERT INTO transactions ({', '.join(keys)}) VALUES "
                 f"({', '.join(['%s'] * len(keys))})", vals)


class FlowBreakdownTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        seed_accounts(self.conn)
        self.bank = self.conn.execute(
            "SELECT id FROM accounts WHERE type='depository' LIMIT 1"
        ).fetchone()["id"]

    def tearDown(self):
        self.conn.close()

    def test_sides_balance_and_kinds_are_told_apart(self):
        today = dt.date(2026, 8, 31)
        # the paycheck is recognised by the household's INCOME bill, not by
        # an aggregator tag — imported and legacy deposits carry none
        add_bill(self.conn, "ACME PAYROLL", 5000.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 9, 5), income=True)
        _txn(self.conn, self.bank, dt.date(2026, 8, 5), -5000.0, "ACME PAYROLL",
             category_primary="INCOME")
        _txn(self.conn, self.bank, dt.date(2026, 8, 6), -20.0, "INTEREST",
             category_primary="INCOME", category_detailed="INCOME_INTEREST_EARNED")
        _txn(self.conn, self.bank, dt.date(2026, 8, 7), -300.0, "VENMO FROM SAM",
             category_primary="INCOME", category_detailed="INCOME_OTHER_INCOME")
        _txn(self.conn, self.bank, dt.date(2026, 8, 10), 1500.0, "RENT",
             category_primary="RENT_AND_UTILITIES")
        _txn(self.conn, self.bank, dt.date(2026, 8, 12), 240.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        # the rent row is what the budget engine's recurring bill matches —
        # that, not a category tag, is what makes a charge "fixed"
        add_bill(self.conn, "RENT", 1500.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 9, 10))
        # a year-old row sits outside 3m and 6m, inside 1y and wider
        _txn(self.conn, self.bank, dt.date(2025, 10, 1), 999.0, "OLD",
             category_primary="GENERAL_SERVICES")
        m = reporting.flow_breakdown(self.conn, today, "3m")
        self.assertEqual(m["in"]["paychecks"], 5000.0)
        self.assertEqual(m["in"]["interest"], 20.0)
        self.assertEqual(m["in"]["other"], 300.0)
        self.assertEqual(m["out"]["fixed"], 1500.0)
        self.assertEqual(m["out"]["variable"], 240.0)
        self.assertEqual(m["saved"], 5320.0 - 1740.0)
        self.assertEqual(m["rate"], round(100 * 3580 / 5320, 1))
        self.assertEqual(m["label"], "Jun–Aug 2026")
        self.assertEqual(m["from"], "2026-06-01")
        six = reporting.flow_breakdown(self.conn, today, "6m")
        self.assertEqual(six["label"], "Mar–Aug 2026")
        self.assertEqual(six["out"]["total"], 1740.0)
        year = reporting.flow_breakdown(self.conn, today, "1y")
        self.assertEqual(year["from"], "2025-09-01")
        self.assertEqual(year["label"], "Sep 2025–Aug 2026")
        self.assertEqual(year["out"]["total"], 2739.0)   # + the OLD row
        # 'all' opens at the earliest transaction's month
        both = reporting.flow_breakdown(self.conn, today, "all")
        self.assertEqual(both["from"], "2025-10-01")
        self.assertEqual(both["out"]["total"], 2739.0)

    def test_an_empty_period_reports_zero_not_none(self):
        flow = reporting.flow_breakdown(self.conn, dt.date(2026, 2, 3), "3m")
        self.assertEqual(flow["in"]["total"], 0.0)
        self.assertEqual(flow["saved"], 0.0)
        self.assertIsNone(flow["rate"])

    def test_unknown_range_is_the_callers_bug(self):
        with self.assertRaises(KeyError):
            reporting.flow_breakdown(self.conn, dt.date(2026, 8, 31), "2w")

    def test_closed_months_are_cached_and_the_live_month_is_not(self):
        """A wide window re-fetched moments later must not re-walk every
        closed month (that walk is seconds on a long ledger — the reason
        the All toggle used to look dead) — but the CURRENT month's fixed
        total must always be computed live."""
        from unittest import mock
        from oikonome.engine import budget
        today = dt.date(2026, 8, 31)
        reporting._FIXED_MONTH_CACHE.clear()
        real = budget.month_status
        calls: list[str] = []

        def counting(conn, day, **kw):
            calls.append(day.isoformat())
            return real(conn, day, **kw)

        with mock.patch.object(budget, "month_status", counting):
            reporting.flow_breakdown(self.conn, today, "3m")
            first = len(calls)
            self.assertEqual(first, 3)              # Jun, Jul, Aug
            reporting.flow_breakdown(self.conn, today, "3m")
            # only the live month (Aug) recomputes; Jun and Jul come from
            # the cache
            self.assertEqual(calls[first:], ["2026-08-31"])
        reporting._FIXED_MONTH_CACHE.clear()


if __name__ == "__main__":
    unittest.main()
