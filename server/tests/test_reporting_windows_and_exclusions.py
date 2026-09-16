"""Reporting windows and account exclusions must agree with the rest of
the engine: the flow picture's spend total honours the same
excluded_accounts the budget engine's fixed split does; compute_spending's
trailing windows follow the household's `today` rather than the DB clock;
and the Spending/Income windows' `months` divisor is the calendar span the
range label promises, not the number of months that happened to have a
row. Disagree on any of the three and two screens quote different totals
for the same range."""

import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import add_bill, make_db, seed_accounts, write_config


def _txn(conn, acct, day, amount, name, **cols):
    keys = ["id", "account_id", "date", "amount", "name", *cols]
    vals = [f"rpt-{name}-{day}", acct, day, amount, name, *cols.values()]
    conn.execute(f"INSERT INTO transactions ({', '.join(keys)}) VALUES "
                 f"({', '.join(['%s'] * len(keys))})", vals)


class FlowBreakdownExcludedAccountsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        seed_accounts(self.conn)
        self.today = dt.date(2026, 8, 31)
        reporting._FIXED_MONTH_CACHE.clear()

    def tearDown(self):
        reporting._FIXED_MONTH_CACHE.clear()
        self.conn.close()

    def test_spend_total_and_fixed_split_share_the_excluded_accounts_filter(self):
        """A bill paid from an account the household excluded from the
        budget is invisible to month_status (so never 'fixed') — it must be
        invisible to the flow picture's spend total too, or it lands in
        'variable' on a page that promises to agree with the Budget page."""
        write_config(self.conn, excluded_accounts=["card"])
        add_bill(self.conn, "RENT", 1500.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 9, 10))
        add_bill(self.conn, "INTERNET", 80.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 9, 3))
        # the rent lands on the EXCLUDED card; internet on the counted bank
        _txn(self.conn, "card", dt.date(2026, 8, 10), 1500.0, "RENT",
             category_primary="RENT_AND_UTILITIES")
        _txn(self.conn, "chk", dt.date(2026, 8, 3), 80.0, "INTERNET",
             category_primary="RENT_AND_UTILITIES")
        _txn(self.conn, "chk", dt.date(2026, 8, 12), 240.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, "card", dt.date(2026, 8, 14), 55.0, "CINEMA",
             category_primary="ENTERTAINMENT")
        m = reporting.flow_breakdown(self.conn, self.today, "3m")
        self.assertEqual(m["out"]["fixed"], 80.0)
        self.assertEqual(m["out"]["variable"], 240.0)
        self.assertEqual(m["out"]["total"], 320.0)

    def test_without_exclusions_every_account_still_counts(self):
        write_config(self.conn)
        add_bill(self.conn, "RENT", 1500.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 9, 10))
        _txn(self.conn, "card", dt.date(2026, 8, 10), 1500.0, "RENT",
             category_primary="RENT_AND_UTILITIES")
        _txn(self.conn, "chk", dt.date(2026, 8, 12), 240.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        m = reporting.flow_breakdown(self.conn, self.today, "3m")
        self.assertEqual(m["out"]["fixed"], 1500.0)
        self.assertEqual(m["out"]["variable"], 240.0)
        self.assertEqual(m["out"]["total"], 1740.0)


class EveryReportSharesTheExclusionTests(unittest.TestCase):
    """Excluding an account must move EVERY report by the same rows.

    The exclusion lived in flow_breakdown alone, so the Overview's "went
    out" dropped the excluded account while the Spending tab's total for
    the same window kept it — two figures on one screen, disagreeing, each
    right by its own rules. Every entry point now shares one predicate
    (reporting.excluded_accounts_sql), so the totals reconcile."""

    def setUp(self):
        self.conn = make_db()
        seed_accounts(self.conn)
        # a SECOND bank account, the one the household excludes: income and
        # spend both land on it, so an entry point that forgets the filter
        # reports a visibly different total rather than an empty one
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('side','it1','Side Checking','depository','checking',900)")
        write_config(self.conn, excluded_accounts=["side"])
        self.today = dt.date(2026, 8, 31)
        reporting._FIXED_MONTH_CACHE.clear()
        # counted: $240 out, $3,000 in — both on the ordinary checking
        _txn(self.conn, "chk", dt.date(2026, 8, 12), 240.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        _txn(self.conn, "chk", dt.date(2026, 8, 5), -3000.0, "EMPLOYER",
             category_primary="INCOME", category_detailed="INCOME_WAGES")
        # excluded: $500 out, $900 in — nothing may see these
        _txn(self.conn, "side", dt.date(2026, 8, 13), 500.0, "BOATYARD",
             category_primary="GENERAL_SERVICES")
        _txn(self.conn, "side", dt.date(2026, 8, 6), -900.0, "SIDE GIG",
             category_primary="INCOME", category_detailed="INCOME_WAGES")

    def tearDown(self):
        reporting._FIXED_MONTH_CACHE.clear()
        self.conn.close()

    def test_spending_window_agrees_with_flow_breakdown_on_the_same_window(self):
        flow = reporting.flow_breakdown(self.conn, self.today, "3m")
        spend = reporting.spending_window(self.conn, self.today, "3m")
        self.assertEqual(spend["total"], 240.0)
        self.assertEqual(spend["total"], flow["out"]["total"])
        self.assertEqual([c[0] for c in spend["categories"]],
                         ["FOOD AND DRINK"])
        self.assertNotIn("BOATYARD", [m[0] for m in spend["merchants"]])

    def test_income_window_agrees_with_flow_breakdown_on_the_same_window(self):
        flow = reporting.flow_breakdown(self.conn, self.today, "3m")
        inc = reporting.income_window(self.conn, self.today, "3m")
        self.assertEqual(inc["total"], 3000.0)
        self.assertEqual(inc["total"], flow["in"]["total"])
        self.assertEqual(inc["kinds"]["paychecks"], 3000.0)
        self.assertNotIn("SIDE GIG", [s[0] for s in inc["sources"]])

    def test_month_flows_omits_the_excluded_accounts_rows(self):
        m = reporting.month_flows(self.conn, 2026, 8)
        self.assertEqual(m["out"], 240.0)
        self.assertEqual(m["in"], 3000.0)
        self.assertEqual(m["net"], 2760.0)

    def test_compute_cashflow_omits_the_excluded_accounts_rows(self):
        cf = reporting.compute_cashflow(self.conn, today=self.today)
        year = {r[0]: r for r in cf["savings_by_year"]}["2026"]
        self.assertEqual(year[1], 3000.0)          # income
        self.assertEqual(year[2], 240.0)           # spend
        self.assertEqual(dict(cf["income_by_month"])["2026-08"], 3000.0)

    def test_compute_spending_omits_the_excluded_accounts_rows(self):
        sp = reporting.compute_spending(self.conn, today=self.today)
        self.assertEqual(dict((r[0], r[1]) for r in sp["by_year"])["2026"],
                         240.0)
        self.assertNotIn("BOATYARD", [m[0] for m in sp["top_merchants"]])


class ComputeSpendingTodayTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        seed_accounts(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_trailing_windows_follow_the_household_today(self):
        """The 12- and 36-month cutoffs are measured from the `today` the
        caller passes (the household's local day), not the DB server's
        CURRENT_DATE — a household west of UTC would otherwise see a day
        drop out of its trailing totals every evening."""
        today = dt.date(2025, 1, 15)
        # inside 12 months of the household's today; long past it on the
        # real clock this test runs under
        _txn(self.conn, "chk", dt.date(2024, 3, 1), 120.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        # just past the 12-month cutoff (2024-01-15): a 36-month row only
        _txn(self.conn, "chk", dt.date(2024, 1, 10), 70.0, "CINEMA",
             category_primary="ENTERTAINMENT")
        # inside 36 months (cutoff 2022-01-15), outside 12
        _txn(self.conn, "chk", dt.date(2022, 6, 1), 30.0, "OLDGROCER",
             category_primary="FOOD_AND_DRINK")
        # past even the 36-month cutoff
        _txn(self.conn, "chk", dt.date(2021, 12, 1), 40.0, "ANCIENT",
             category_primary="GENERAL_SERVICES")
        sp = reporting.compute_spending(self.conn, today=today)
        cats = dict(map(tuple, sp["category_totals"]))
        self.assertEqual(cats, {"FOOD AND DRINK": 120.0})
        merch = {m[0]: m[1] for m in sp["top_merchants"]}
        self.assertEqual(merch, {"GROCER": 120.0})
        self.assertEqual([ym for ym, _ in sp["by_month"]],
                         ["2022-06", "2024-01", "2024-03"])


class WindowMonthsDivisorTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        seed_accounts(self.conn)
        self.today = dt.date(2026, 8, 31)

    def tearDown(self):
        self.conn.close()

    def test_income_months_is_the_calendar_span_not_the_populated_months(self):
        """$300 a quarter over a 1y window is $100/mo, not $300/mo: the
        divisor is the 12 months the label promises even when eight of
        them had no deposit."""
        for day in (dt.date(2025, 9, 15), dt.date(2025, 12, 15),
                    dt.date(2026, 3, 15), dt.date(2026, 6, 15)):
            _txn(self.conn, "chk", day, -300.0, "ROYALTY",
                 category_primary="INCOME",
                 category_detailed="INCOME_OTHER_INCOME")
        w = reporting.income_window(self.conn, self.today, "1y")
        self.assertEqual(w["total"], 1200.0)
        self.assertEqual(w["months"], 12)
        self.assertEqual(len(w["by_month"]), 4)

    def test_spending_months_is_the_calendar_span(self):
        _txn(self.conn, "chk", dt.date(2026, 3, 2), 90.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        # the ledger begins in March, so the 1y divisor is Mar–Aug: six —
        # the range label's twelve would halve the pace of a young ledger
        w = reporting.spending_window(self.conn, self.today, "1y")
        self.assertEqual(w["months"], 6)
        self.assertEqual(reporting.spending_window(
            self.conn, self.today, "3m")["months"], 3)
        # 'all' opens at the earliest bank row's month: Mar–Aug is six
        self.assertEqual(reporting.spending_window(
            self.conn, self.today, "all")["months"], 6)

    def test_an_empty_ledger_still_divides_by_at_least_one(self):
        self.assertEqual(reporting.spending_window(
            self.conn, self.today, "all")["months"], 1)
        self.assertEqual(reporting.income_window(
            self.conn, self.today, "all")["months"], 1)


if __name__ == "__main__":
    unittest.main()
