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

    def test_a_closed_months_cached_fixed_total_follows_a_ledger_edit(self):
        """A closed month's fixed total is the sum of the charges the bills
        matched, so a ledger edit alone moves it — the charge removed, a
        reimbursement netting it out, an account hidden — with no bill or
        setting changing. The cached figure must miss then, or the page
        keeps the pre-edit split while the table beside it has moved."""
        today = dt.date(2026, 8, 31)
        reporting._FIXED_MONTH_CACHE.clear()
        self.addCleanup(reporting._FIXED_MONTH_CACHE.clear)
        _txn(self.conn, self.bank, dt.date(2026, 7, 10), 1500.0, "RENT",
             category_primary="RENT_AND_UTILITIES")
        _txn(self.conn, self.bank, dt.date(2026, 7, 12), 2000.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        add_bill(self.conn, "RENT", 1500.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 9, 10))
        self.assertEqual(
            reporting.flow_breakdown(self.conn, today, "3m")["out"]["fixed"],
            1500.0)
        # the July charge (a closed month) leaves the ledger
        self.conn.execute("UPDATE transactions SET removed = 1 "
                          "WHERE name = 'RENT'")
        m = reporting.flow_breakdown(self.conn, today, "3m")
        self.assertEqual(m["out"]["total"], 2000.0)
        self.assertEqual(m["out"]["fixed"], 0.0,
                         "a cached month kept a charge the ledger dropped")


    def test_a_closed_months_cached_fixed_total_follows_a_bill_raw_edit(self):
        """Which charges a bill claims is decided partly by fields kept in
        the bill's raw payload — its merchant list, an envelope's match
        category and months — and editing those rewrites raw alone. The
        cache fingerprint must move with them, or Cash Flow keeps the
        pre-edit fixed total for a closed month while Budget has moved."""
        add_bill(self.conn, "RENT", 1500.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 9, 10))
        before = reporting._fixed_inputs_stamp(self.conn, {})
        self.conn.execute(
            "UPDATE bills SET raw = COALESCE(raw, '{}'::jsonb) "
            "|| '{\"match_category\": \"HOME_IMPROVEMENT\"}'::jsonb "
            "WHERE payee = 'RENT'")
        self.assertNotEqual(reporting._fixed_inputs_stamp(self.conn, {}),
                            before,
                            "a bill raw edit left the fixed cache stamp as it was")

    def _twin_checking(self, aggregator="test"):
        """A second copy of the checking account from another source — the
        dual-sourced account a link exists to de-duplicate."""
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token) VALUES ('it2', %s, 'Twin Bank', 'tok-2')",
            (aggregator,))
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('chk2','it2','Twin Checking',"
            "'depository','checking',5000)")
        return "chk2"

    def test_a_closed_months_cached_fixed_total_follows_an_account_link(self):
        """Linking two copies of one account changes which rows the fixed
        match reads (the shadow copy drops out) while writing nothing but
        the link table. The cached closed-month figure must miss then —
        otherwise fixed stays at the doubled pre-link value, is clamped to
        the live total, and Cash Flow reads the month as all-fixed, no
        variable, for as long as the cache lives."""
        from oikonome.engine import links
        today = dt.date(2026, 8, 31)
        reporting._FIXED_MONTH_CACHE.clear()
        self.addCleanup(reporting._FIXED_MONTH_CACHE.clear)
        twin = self._twin_checking()
        # the rent charge exists only in the copy the link will shadow
        _txn(self.conn, twin, dt.date(2026, 7, 10), 1500.0, "RENT",
             category_primary="RENT_AND_UTILITIES")
        _txn(self.conn, self.bank, dt.date(2026, 7, 12), 400.0, "GROCER",
             category_primary="FOOD_AND_DRINK")
        add_bill(self.conn, "RENT", 1500.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 9, 10))
        reporting.flow_breakdown(self.conn, today, "3m")     # warm the cache
        links.create(self.conn, [self.bank, twin])
        cached = reporting.flow_breakdown(self.conn, today, "3m")["out"]
        reporting._FIXED_MONTH_CACHE.clear()
        live = reporting.flow_breakdown(self.conn, today, "3m")["out"]
        self.assertEqual(live["fixed"], 0.0)
        self.assertEqual((cached["fixed"], cached["variable"]),
                         (live["fixed"], live["variable"]),
                         "a closed month kept its pre-link fixed total")

    def test_the_fixed_cache_stamp_follows_a_failover_nothing_wrote(self):
        """A linked primary whose feed goes stale stops being primary by
        the clock alone — no row is written — and the backup's rows start
        counting. The stamp must move with the effective shadow set, not
        only with the tables, or a failover leaves Cash Flow on the
        pre-failover split for a day."""
        from unittest import mock
        from oikonome.engine import links
        twin = self._twin_checking(aggregator="plaid")
        self.conn.execute("UPDATE items SET aggregator = 'plaid' "
                          "WHERE id = 'it1'")
        self.conn.execute(
            "UPDATE accounts SET updated_at = now() - interval '47 hours' "
            "WHERE id = %s", (self.bank,))
        self.conn.execute("UPDATE accounts SET updated_at = now() "
                          "WHERE id = %s", (twin,))
        links.create(self.conn, [self.bank, twin])
        before = reporting._fixed_inputs_stamp(self.conn, {})
        # the clock moves between requests, and each request is a fresh
        # checkout with nothing remembered from the last one
        if getattr(self.conn, "_memo", None) is not None:
            self.conn._memo = None
        with mock.patch.object(links, "STALE_HOURS", 46):
            self.assertIn(self.bank, links.shadow_ids(self.conn))
            self.assertNotEqual(
                reporting._fixed_inputs_stamp(self.conn, {}), before,
                "a clock-driven failover left the fixed cache stamp as it was")

    def test_the_fixed_cache_stamp_covers_every_table_the_spend_rows_read(self):
        """The month walk reads its rows through the budget engine's
        ledger query; every table that query depends on must be in the
        stamp, so an input added there cannot silently escape the cache."""
        from oikonome.engine import budget
        self.assertLessEqual(set(budget._LEDGER_TABLES),
                             set(reporting._FIXED_LEDGER_TABLES))

if __name__ == "__main__":
    unittest.main()
