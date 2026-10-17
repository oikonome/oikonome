"""What the Bills line owns is what the live verdict says it owns.

The planner's suggested food/other budgets leave out only the rows that pay
a bill — the verdict's own occurrence match, near the due date and inside
the bill's band — never every row at the bill's merchant. The monthly bill
load counts every accepted cadence over its whole cycle. And the Bills
page's week ahead is what is still owed in it, the rule the forecast and
runway read: paid occurrences are not due, overdue unpaid ones are.
"""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import TODAY, _ensure_db, add_bill, add_txn, make_db, write_config


class _Db(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()


class SuggestionsKeepAMerchantsOtherSpendTests(_Db):
    def test_a_subscription_does_not_hide_the_merchants_other_orders(self):
        """A $14.99 subscription at a store pays one charge a month; the
        store's $400 of other orders are variable spend and must be in the
        suggested "other" budget, or the live month reads over it."""
        add_bill(self.conn, "Amazon Prime", 14.99,
                 next_due=dt.date(2026, 8, 3),
                 last_seen=dt.date(2026, 7, 3))
        for mo in (4, 5, 6):
            add_txn(self.conn, dt.date(2026, mo, 3), 14.99, "AMAZON PRIME",
                    account="chk")
            add_txn(self.conn, dt.date(2026, mo, 12), 250.0, "AMAZON MKTPL",
                    account="chk")
            add_txn(self.conn, dt.date(2026, mo, 22), 150.0, "AMAZON MKTPL",
                    account="chk")
        out = budget.suggest_budgets(self.conn, TODAY)
        self.assertEqual(out["suggestions"]["other_monthly"], 400.0)

    def test_a_yearly_fee_does_not_hide_a_years_groceries(self):
        add_bill(self.conn, "Costco Membership", 65.0, frequency="YEARLY",
                 next_due=dt.date(2027, 5, 2),
                 last_seen=dt.date(2026, 5, 2))
        add_txn(self.conn, dt.date(2026, 5, 2), 65.0, "COSTCO MEMBERSHIP",
                account="chk")
        for mo in (2, 3, 4, 5, 6):
            add_txn(self.conn, dt.date(2026, mo, 9), 600.0, "COSTCO WHSE",
                    primary="FOOD_AND_DRINK", account="chk")
        out = budget.suggest_budgets(self.conn, TODAY)
        self.assertEqual(out["suggestions"]["food_monthly"], 600.0)
        # and the fee itself stays on the Bills line, out of "other"
        self.assertEqual(out["suggestions"]["other_monthly"], 0.0)



class SuggestionsMatchTheLiveMonthsEnvelopeSplitTests(_Db):
    """An envelope owns what the live claimer gives it — its merchant's rows
    and, when it matches by category, its category's rows at any merchant —
    up to its pool; spend past the pool is variable. The suggested "other"
    budget must be the live month's variable spend, or every month reads
    over or under a budget it was never measured against."""

    def _live_other(self, day):
        return budget.month_status(self.conn, day)["buckets"]["other"]["actual"]

    def test_suggest_budgets_leaves_a_category_envelopes_rows_to_it(self):
        add_bill(self.conn, "Doctor", 150.0, bill_type="envelope",
                 frequency=None, interval=1, category="MEDICAL",
                 match_category=True)
        for mo in range(1, 7):
            add_txn(self.conn, dt.date(2026, mo, 10), 120.0, "VALLEY CLINIC",
                    primary="MEDICAL")
            add_txn(self.conn, dt.date(2026, mo, 12), 80.0, "HARDWARE")
        out = budget.suggest_budgets(self.conn, TODAY)
        self.assertEqual(out["suggestions"]["other_monthly"],
                         self._live_other(dt.date(2026, 6, 30)))
        self.assertEqual(out["suggestions"]["other_monthly"], 80.0)
        # the envelope's category is no carve-out: its pool already owns it
        self.assertNotIn("MEDICAL",
                         [c["category"] for c in out["bucket_candidates"]])

    def test_suggest_budgets_counts_spend_past_an_envelopes_pool(self):
        add_bill(self.conn, "Bean Haus", 100.0, bill_type="envelope",
                 frequency=None, interval=1)
        for mo in range(1, 7):
            add_txn(self.conn, dt.date(2026, mo, 10), 300.0, "BEAN HAUS")
        out = budget.suggest_budgets(self.conn, TODAY)
        self.assertEqual(out["suggestions"]["other_monthly"],
                         self._live_other(dt.date(2026, 6, 30)))
        self.assertEqual(out["suggestions"]["other_monthly"], 200.0)

    def test_suggest_budgets_caps_an_annual_envelope_on_its_years_pool(self):
        """A $120/yr pool drawn $100 in January has $20 left in March: the
        March renewal's other $80 is variable, as the live March reads it."""
        add_bill(self.conn, "Namehost", 120.0, bill_type="envelope",
                 frequency=None, interval=12)
        add_txn(self.conn, dt.date(2026, 1, 10), 100.0, "NAMEHOST")
        add_txn(self.conn, dt.date(2026, 3, 10), 100.0, "NAMEHOST")
        out = budget.suggest_budgets(self.conn, TODAY)
        samples = dict(out["samples"]["other"])
        self.assertEqual(samples["2026-01"], 0.0)
        self.assertEqual(samples["2026-03"],
                         self._live_other(dt.date(2026, 3, 31)))
        self.assertEqual(samples["2026-03"], 80.0)

    def test_suggest_budgets_counts_an_annual_pool_drawn_before_the_window(self):
        """The window can open mid-year: what the pool spent in the months
        before it still binds the cap inside it."""
        add_bill(self.conn, "Namehost", 120.0, bill_type="envelope",
                 frequency=None, interval=12)
        add_txn(self.conn, dt.date(2026, 1, 10), 100.0, "NAMEHOST")
        add_txn(self.conn, dt.date(2026, 5, 10), 100.0, "NAMEHOST")
        out = budget.suggest_budgets(self.conn, dt.date(2026, 10, 15))
        samples = dict(out["samples"]["other"])
        self.assertNotIn("2026-01", samples)
        self.assertEqual(samples["2026-05"],
                         self._live_other(dt.date(2026, 5, 31)))
        self.assertEqual(samples["2026-05"], 80.0)

class LongCycleLoadTests(_Db):
    def _load(self):
        return budget.recurring_load(self.conn, TODAY)["bills_monthly"]

    def test_a_fifteen_year_bill_costs_its_rate_wherever_it_falls(self):
        for year in (2030, 2040):
            with self.subTest(first_due=year):
                self.conn.execute("DELETE FROM bills")
                add_bill(self.conn, "Roof Reserve", 1500.0,
                         frequency="YEARLY", interval=15,
                         next_due=dt.date(year, 6, 1), source="manual")
                self.assertAlmostEqual(self._load(), 1500 / 15 / 12,
                                       places=2)

    def test_a_thirteen_month_bill_costs_its_rate(self):
        add_bill(self.conn, "Odd Cycle", 1300.0, interval=13,
                 next_due=dt.date(2026, 9, 1), source="manual")
        self.assertAlmostEqual(self._load(), 100.0, places=2)


class WeekAheadIsWhatIsOwedTests(_Db):
    def test_a_bill_paid_early_is_not_due_this_week(self):
        due = TODAY + dt.timedelta(days=3)
        add_bill(self.conn, "Hillside Rent", 2000.0, next_due=due,
                 last_seen=due - dt.timedelta(days=30))
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 2000.0,
                "HILLSIDE RENT", account="chk")
        t = budget.recurring_load(self.conn, TODAY)
        self.assertEqual(t["week_count"], 0)
        self.assertEqual(t["week_total"], 0.0)

    def test_an_overdue_unpaid_bill_is_still_due_this_week(self):
        due = TODAY - dt.timedelta(days=2)
        add_bill(self.conn, "Valley Water", 80.0, next_due=due,
                 last_seen=due - dt.timedelta(days=30))
        t = budget.recurring_load(self.conn, TODAY)
        self.assertEqual(t["week_count"], 1)
        self.assertAlmostEqual(t["week_total"], 80.0, places=2)


if __name__ == "__main__":
    unittest.main()
