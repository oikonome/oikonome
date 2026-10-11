"""Cash-flow income figures judge the same rows, month by month, that the
income total sums.

Income is personal bank deposits: business-entity and shadow rows never
reach the total, so they must not anchor the Income tab's window or its
per-month divisor either, or a few months of personal pay are divided by
years of a business account's history. A month the bank feed covers is
known even when nothing was deposited in it — its income is a measured
$0, and it is often the month that lost the most, so the month series
must keep it. And a month in a year with no bank feed at all has unknown
income, not $0: the flow picture must leave it out rather than set all its
spend against nothing and call the difference overspending."""

import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import entities, reporting

from .util import (_admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts,
                   write_config)

TODAY = dt.date(2026, 10, 9)


def _months(first: dt.date, last: dt.date):
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        yield dt.date(y, m, 10)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class _Tenant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            self.tid = str(tenancy.create_tenant(
                admin, f"inc-{uuid.uuid4().hex[:8]}"))
        finally:
            admin.close()
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            self.seed(conn)
            conn.commit()
        finally:
            conn.close()
        # a fresh connection, so the entity account list is read anew
        self.conn = tenancy.tenant_connect(self.tid)

    def tearDown(self):
        self.conn.close()

    def seed(self, conn):
        raise NotImplementedError

    @staticmethod
    def pay(conn, day, amount, account="chk"):
        add_txn(conn, day, -amount, "NORTHWIND PAYROLL", account=account,
                primary="INCOME", detailed="INCOME_WAGES")

    @staticmethod
    def card(conn, day, amount):
        add_txn(conn, day, amount, "GROCER", account="card",
                primary="FOOD_AND_DRINK")


class _WithBusinessChecking(_Tenant):
    """A business entity's checking with years of rows, beside a personal
    checking linked recently."""

    personal_from = dt.date(2026, 6, 1)

    def seed(self, conn):
        conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,balance_available) VALUES "
            "('bizchk','it1','Biz Checking','depository','checking',"
            "9000,9000)")
        for d in _months(dt.date(2021, 6, 1), dt.date(2026, 9, 1)):
            add_txn(conn, d, -7000.0, "CLIENT INVOICE", account="bizchk",
                    primary="INCOME")
            add_txn(conn, d, 500.0, "BIZ HOSTING", account="bizchk")
        for d in _months(self.personal_from, TODAY):
            # this month's pay lands before today
            self.pay(conn, d if d <= TODAY else TODAY.replace(day=3),
                     6000.0)
        ent = entities.create_entity(conn, name="Fabrikam LLC",
                                     structure="sole_prop")
        entities.assign_account(conn, "bizchk", ent["id"])


class IncomeWindowAnchorsOnPersonalRows(_WithBusinessChecking):
    def test_business_history_does_not_stretch_the_per_month_divisor(self):
        """Personal pay from Jun 2026, $6,000 a month; the business account
        reaches back to 2021. The 1y window totals Jun–Sep and must divide
        by those 4 months — divided by 12, $6,000/mo reads as $2,000/mo."""
        w = reporting.income_window(self.conn, TODAY, "1y")
        self.assertEqual(w["total"], 24000.0)
        self.assertEqual(w["months"], 4)
        self.assertEqual(w["total"] / w["months"], 6000.0)

    def test_all_starts_at_the_first_personal_row(self):
        """'All' opens where the personal rows the total sums begin, not at
        the business account's first row — a 2021 start would label the
        window 'Jun 2021–Sep 2026' and divide 4 months of pay by 64."""
        a = reporting.income_window(self.conn, TODAY, "all")
        self.assertEqual(a["from"], "2026-06-01")
        self.assertEqual(a["months"], 4)
        self.assertEqual(a["total"], 24000.0)


class IncomeLiveMonthIgnoresBusinessHistory(_WithBusinessChecking):
    personal_from = dt.date(2026, 10, 1)

    def test_a_personal_feed_linked_this_month_shows_the_month_so_far(self):
        """Personal checking first linked this month has no complete month:
        the range falls back to the month so far. Older business rows the
        total never counts must not hide that and leave every range at $0."""
        w = reporting.income_window(self.conn, TODAY, "1y")
        self.assertTrue(w["live_month"])
        self.assertEqual(w["total"], 6000.0)


class CoveredMonthWithoutDeposit(_Tenant):
    """Bank feed from Jan 2024; nothing deposited in Jul 2025; $4,000 of
    card spend every month."""

    def seed(self, conn):
        for d in _months(dt.date(2024, 1, 1), dt.date(2026, 9, 1)):
            if (d.year, d.month) != (2025, 7):
                self.pay(conn, d, 5000.0)
            self.card(conn, d, 4000.0)

    def test_the_month_without_pay_is_a_saved_by_month_bar(self):
        """The covered month with spend and no deposit is the month that
        lost the most; dropping it hides the worst month from the series."""
        cf = reporting.compute_cashflow(self.conn, TODAY)
        points = dict(cf["cash_graph"]["points"][
            :cf["cash_graph"]["forecast_from"]])
        self.assertEqual(points.get("2025-07"), -4000.0)
        self.assertEqual(min(points.values()), -4000.0)

    def test_a_years_month_bars_sum_to_its_saved_figure(self):
        """The expanded month bars must add up to the year row they expand;
        leaving out a no-deposit month makes them sum to more."""
        cf = reporting.compute_cashflow(self.conn, TODAY)
        row = {r[0]: r for r in cf["savings_by_year"]}["2025"]
        bars = dict(cf["monthly_by_year"]["2025"])
        self.assertEqual(len(bars), 12)
        self.assertEqual(bars["Jul"], -4000.0)
        self.assertAlmostEqual(sum(bars.values()), row[3], places=2)

    def test_months_still_ahead_get_no_bar(self):
        """The current year shows months through this one, never a $0 bar
        for a month that has not happened."""
        cf = reporting.compute_cashflow(self.conn, TODAY)
        self.assertEqual([m for m, _v in cf["monthly_by_year"]["2026"]][-1],
                         "Oct")
        self.assertNotIn("2026-11", dict(cf["cash_graph"]["points"][
            :cf["cash_graph"]["forecast_from"]]))


class FlowPictureSkipsYearsWithoutABankFeed(_Tenant):
    """Checking rows in 2019 only, then none until Jan 2025; card spend of
    $1,000 every month throughout and pay of $3,000 whenever checking is
    fed."""

    def seed(self, conn):
        for d in _months(dt.date(2019, 1, 1), dt.date(2026, 9, 1)):
            self.card(conn, d, 1000.0)
            if d.year == 2019 or d >= dt.date(2025, 1, 1):
                self.pay(conn, d, 3000.0)

    def test_gap_years_are_left_out_on_both_sides(self):
        """5y is Oct 2021–Sep 2026; 2021–2024 have no bank rows, so their
        spend is not set against $0 of income — only the 21 known months
        are summed, and the label names what was left out."""
        f = reporting.flow_breakdown(self.conn, TODAY, "5y")
        self.assertFalse(f["income_unknown"])
        self.assertEqual(f["in"]["total"], 21 * 3000.0)
        self.assertEqual(f["out"]["total"], 21 * 1000.0)
        self.assertEqual(f["saved"], 21 * 2000.0)
        self.assertEqual(f["income_gap_months"], 39)
        self.assertIn("without Oct 2021–Dec 2024 (no bank feed)", f["label"])

    def test_a_window_entirely_in_a_feedless_stretch_is_unknown(self):
        """A window whose every month lies in a year without bank rows has
        unknown income, as the Month lens says — not $0 and a loss."""
        # 2026 is fed in this fixture, so drop its pay and look at it again
        self.conn.execute(
            "UPDATE transactions SET removed=1 WHERE account_id='chk' "
            "AND date >= '2025-01-01'")
        f = reporting.flow_breakdown(self.conn, TODAY, "1y")
        self.assertTrue(f["income_unknown"])
        self.assertIsNone(f["saved"])
        self.assertEqual(f["out"]["total"], 12000.0)

    def test_a_fully_covered_window_is_unchanged(self):
        f = reporting.flow_breakdown(self.conn, TODAY, "1y")
        self.assertEqual(f["income_gap_months"], 0)
        self.assertEqual(f["label"], "Oct 2025–Sep 2026")
        self.assertEqual(f["saved"], 24000.0)


if __name__ == "__main__":
    unittest.main()
