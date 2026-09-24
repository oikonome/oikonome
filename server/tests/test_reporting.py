"""Reporting engine (engine/reporting.py) and the /api/reports router, on
synthetic fixtures.

The numeric cases pin the semantics every report is built on: the spend
definition, the EFF_CAT underscore merge, fiat-anchored Coinbase P/L, the
investment-basis mask override, workplace-plan contribution basis,
bank-side investment funding, and the fee word-boundary match."""

import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import reporting, entities
from oikonome.engine.compat import jsonb

from .util import TODAY, add_txn, make_db, write_config

_n = [0]


def _tid(prefix="r"):
    _n[0] += 1
    return f"{prefix}{_n[0]:04d}-{uuid.uuid4().hex[:6]}"


def add_account(conn, aid, name, typ, subtype=None, balance=None,
                item="it1", mask=None):
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype,
               balance_current, mask) VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (tenant_id, id) DO UPDATE
           SET balance_current=EXCLUDED.balance_current""",
        (aid, item, name, typ, subtype, balance, mask))


def add_item(conn, iid, inst, aggregator="test"):
    conn.execute(
        """INSERT INTO items (id, aggregator, institution_name) VALUES
           (%s,%s,%s) ON CONFLICT (tenant_id, id) DO NOTHING""",
        (iid, aggregator, inst))


def add_raw_txn(conn, date, amount, name, account, raw, txn_id=None):
    txn_id = txn_id or _tid("raw")
    conn.execute(
        """INSERT INTO transactions (id, account_id, date, amount, name,
               removed, raw) VALUES (%s,%s,%s,%s,%s,0,%s)
           ON CONFLICT (tenant_id, id) DO NOTHING""",
        (txn_id, account, date, amount, name, jsonb(raw)))
    return txn_id


def add_holding(conn, account, symbol, qty, price, value, raw=None):
    conn.execute(
        """INSERT INTO holdings (account_id, symbol, quantity, price, value, raw)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (tenant_id, account_id, symbol) DO UPDATE
           SET quantity=EXCLUDED.quantity, price=EXCLUDED.price,
               value=EXCLUDED.value, raw=EXCLUDED.raw""",
        (account, symbol, qty, price, value, jsonb(raw or {})))


def months_ago(n: int) -> dt.date:
    """The 15th of the month n months before this one (safe day-of-month)."""
    t = dt.date.today()
    y, m = t.year, t.month - n
    while m < 1:
        y, m = y - 1, m + 12
    return dt.date(y, m, 15)


class AssetClassTests(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(reporting.asset_class("depository", "checking"), "Cash")
        self.assertEqual(reporting.asset_class("credit", None), "Card debt")
        self.assertEqual(reporting.asset_class("investment", "crypto"), "Crypto")
        for st in ("401a", "403b", "457b", "401k", "457", "ira",
                   "roth", "Roth IRA", "roth 401k", "retirement", "pension",
                   "sep ira", "simple ira", "thrift savings plan"):
            self.assertEqual(reporting.asset_class("investment", st), "Retirement")
        self.assertEqual(reporting.asset_class("investment", "brokerage"),
                         "Taxable investments")
        # tax-advantaged wrappers are never labelled taxable money
        self.assertEqual(reporting.asset_class("investment", "hsa"),
                         "Health savings")
        self.assertEqual(reporting.asset_class("investment", "529"),
                         "Education savings")
        self.assertEqual(reporting.asset_class("loan", "mortgage"), "Other")


class NetWorthTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, manual_assets=[
            {"name": "House", "kind": "property", "value": 500000,
             "as_of": "2026-01"},
            {"name": "Old Mortgage", "kind": "mortgage", "value": -210000},
        ])
        add_account(self.conn, "inv1", "Roth IRA", "investment", "ira", 100000)
        add_raw_txn(self.conn, months_ago(3), 2000, "BUY VTI", "inv1",
                    {"units": 10, "ticker": "VTI", "unitprice": 100,
                     "type": "buy"})
        add_holding(self.conn, "inv1", "VTI", 10, 200, 2000)
        add_item(self.conn, "pm", "Northwind")
        add_account(self.conn, "pm-loan", "Mortgage", "loan", "mortgage",
                    200000, item="pm")

    def tearDown(self):
        self.conn.close()

    def test_current_totals_classes_property(self):
        nw = reporting.compute_networth(self.conn)
        # loan excluded from financial total: 5000 − 250 + 100000
        self.assertEqual(nw["current_total"], 104750.0)
        by_class = dict(map(tuple, nw["by_asset_class"]))
        self.assertEqual(by_class,
                         {"Cash": 5000.0, "Card debt": -250.0,
                          "Retirement": 100000.0})
        # live loan supersedes the manual mortgage entry
        kinds = [(p["name"], p["kind"], p["value"]) for p in nw["property_items"]]
        self.assertIn(("House", "property", 500000.0), kinds)
        self.assertIn(("Northwind Mortgage", "mortgage", -200000.0), kinds)
        self.assertNotIn("Old Mortgage", [p["name"] for p in nw["property_items"]])
        self.assertEqual(nw["property_net"], 300000.0)
        self.assertEqual(nw["full_total"], 404750.0)

    def test_unrelated_loan_does_not_delete_the_manual_mortgage(self):
        """Only a live MORTGAGE supersedes a manual mortgage entry.

        Testing the loans as a whole would drop the manual mortgage from
        net worth entirely and relabel the unrelated loan 'mortgage' — a
        $210k home loan disappearing because a $20k car loan arrived,
        overstating net worth by the whole mortgage and putting a wrong
        number behind the page's 'incl. mortgage payoff' caption."""
        conn = make_db()
        write_config(conn, manual_assets=[
            {"name": "Home", "kind": "mortgage", "value": -300000},
        ])
        add_item(conn, "auto", "CreditUnion")
        add_account(conn, "auto-loan", "Car Loan", "loan", "auto",
                    20000, item="auto")
        try:
            nw = reporting.compute_networth(conn)
            kinds = {(p["name"], p["kind"], p["value"])
                     for p in nw["property_items"]}
            # the manual mortgage survives — the car loan is not a mortgage
            self.assertIn(("Home", "mortgage", -300000.0), kinds)
            # and the car loan is labelled for what it is
            self.assertIn(("CreditUnion Car Loan", "auto", -20000.0), kinds)
            self.assertEqual(nw["property_net"], -320000.0)
            # what the SPA's "incl. mortgage payoff" caption reads
            mortgages = sum(p["value"] for p in nw["property_items"]
                            if p["kind"] == "mortgage")
            self.assertEqual(mortgages, -300000.0)
        finally:
            conn.close()

    def test_live_mortgage_still_supersedes_the_manual_entry(self):
        """The other half of the rule: a real linked mortgage still replaces
        the hand-entered one, so it is not counted twice."""
        nw = reporting.compute_networth(self.conn)
        names = [p["name"] for p in nw["property_items"]]
        self.assertNotIn("Old Mortgage", names)
        self.assertIn("Northwind Mortgage", names)

    def test_trend_pinned_exact_today(self):
        nw = reporting.compute_networth(self.conn)
        trend = nw["trend"]
        self.assertGreaterEqual(len(trend), 3)      # inception 3 months back
        self.assertEqual(trend[0][0], months_ago(3).isoformat()[:7])
        self.assertEqual(trend[-1],
                         [dt.date.today().isoformat()[:7], 104750.0])
        # no snapshots / recorded rows → whole line estimated
        self.assertEqual(nw["trend_estimated_until"], len(trend) - 1)
        self.assertEqual(nw["snapshot_trend"], [])

    def test_reconstruction_carries_the_balance_the_holdings_do_not_explain(self):
        """An investment account's balance is rarely all in listed
        holdings (cash sweep, an unpriced fund, a plan that reports only a
        total). That residual is part of net worth in every month the
        account existed, not only in the pinned last point — otherwise the
        line sits at cash + holdings for the whole history and then jumps by
        the residual in the final month, a fake move the size of the
        account."""
        nw = reporting.compute_networth(self.conn)
        trend = nw["trend"]
        first_month_end = dt.date.fromisoformat(trend[0][0] + "-01")
        first_month_end = (first_month_end.replace(day=28)
                           + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        bought, today = months_ago(3), dt.date.today()
        price = 100 + (200 - 100) * ((first_month_end - bought).days
                                     / (today - bought).days)
        residual = 100000 - 10 * 200                 # balance − held value
        expected = 4750 + 10 * price + residual
        self.assertAlmostEqual(trend[0][1], expected, delta=0.01)
        # every point in between carries it too — no step in the last month
        self.assertTrue(all(v > 100000 for _, v in trend), trend)

    def test_a_market_drop_is_not_flattened_into_a_straight_line(self):
        """The reconstruction reports what the ledger's prices say for
        every month. No calendar window is replaced by a line between
        year-ends — a real 2022 drawdown must stay visible."""
        conn = make_db()
        try:
            write_config(conn)
            add_account(conn, "old", "Brokerage", "investment", "brokerage",
                        2000)
            add_raw_txn(conn, dt.date(2020, 6, 15), 1000, "BUY VTI", "old",
                        {"units": 10, "ticker": "VTI", "unitprice": 100,
                         "type": "buy"})
            for d, px in ((dt.date(2021, 12, 15), 100),
                          (dt.date(2022, 6, 15), 50),
                          (dt.date(2022, 12, 15), 100)):
                add_raw_txn(conn, d, 0, "PRICE VTI", "old",
                            {"units": 0, "ticker": "VTI", "unitprice": px,
                             "type": "reinvest"})
            add_holding(conn, "old", "VTI", 10, 200, 2000)
            trend = dict(map(tuple, reporting.compute_networth(conn)["trend"]))
            # the mid-2022 point reflects the $50 print, not the year-end line
            self.assertLess(trend["2022-06"], trend["2021-12"] - 400)
            self.assertLess(trend["2022-06"], trend["2022-12"] - 400)
        finally:
            conn.close()

    def test_recorded_month_pins_reconstruction(self):
        month = months_ago(2).isoformat()[:7]
        self.conn.execute(
            "INSERT INTO networth_recorded (month, total, source) "
            "VALUES (%s,%s,'import') "
            "ON CONFLICT (tenant_id, month) DO UPDATE SET total=EXCLUDED.total",
            (month, 50000.0))
        nw = reporting.compute_networth(self.conn)
        trend = dict(map(tuple, nw["trend"]))
        self.assertEqual(trend[month], 50000.0)
        months = [m for m, _ in nw["trend"]]
        self.assertEqual(nw["trend_estimated_until"], months.index(month))

    def test_historical_source_not_recorded_boundary(self):
        self.conn.execute(
            "INSERT INTO networth_recorded (month, total, source) VALUES "
            "(%s,%s,%s) ON CONFLICT (tenant_id, month) DO NOTHING",
            ("2005-01", -53000.0, "historical (imported register)"))
        months = ["2005-01", dt.date.today().isoformat()[:7]]
        # historical anchors stay "estimated": boundary does NOT move to 2005
        self.assertEqual(
            reporting._trend_estimated_until(self.conn, months), 1)

    def test_investment_holdings_without_txns_do_not_500(self):
        # investment accounts + holdings with zero ledger rows on those
        # accounts → min(inception) empty → ValueError → 500 networth API
        conn = make_db()
        try:
            write_config(conn)
            add_item(conn, "nb", "Northwind Brokerage")
            add_account(conn, "nb-inv", "Investing", "investment", "brokerage",
                        10000, item="nb")
            add_holding(conn, "nb-inv", "VTI", 50, 200, 10000)
            nw = reporting.compute_networth(conn)
            self.assertIn("current_total", nw)
            self.assertEqual(len(nw["trend"]), 1)
            self.assertEqual(nw["trend"][0][0],
                             dt.date.today().strftime("%Y-%m"))
        finally:
            conn.close()


class SnapshotTests(unittest.TestCase):
    def test_snapshot_upserts_and_series(self):
        conn = make_db()
        try:
            reporting.snapshot_networth(conn)
            reporting.snapshot_networth(conn)      # same day → one row
            series = reporting._snapshot_series(conn)
            self.assertEqual(series,
                             [[dt.date.today().isoformat(), 4750.0]])
            row = conn.execute(
                "SELECT by_class FROM networth_snapshot").fetchone()
            self.assertEqual(row["by_class"],
                             {"Cash": 5000, "Card debt": -250})
        finally:
            conn.close()


class SpendingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_txn(self.conn, dt.date(2024, 5, 10), 100, "SAFEWAY",
                primary="FOOD_AND_DRINK", txn_id=_tid("sp"))
        add_txn(self.conn, dt.date(2024, 6, 15), 40, "CITY POWER",
                primary="RENT_AND_UTILITIES", txn_id=_tid("sp"))
        add_txn(self.conn, dt.date(2024, 7, 1), 60, "CITY WATER",
                override="RENT_AND_UTILITIES", txn_id=_tid("sp"))
        add_txn(self.conn, dt.date(2024, 8, 1), 20, "SQ *COFFEE SHOP",
                merchant="SQ *COFFEE SHOP", txn_id=_tid("sp"))
        add_txn(self.conn, dt.date(2025, 1, 5), 30, "AMZN",
                override="Amazon - Food & Drink", txn_id=_tid("sp"))
        # exclusions: refund, transfer, card payment, loan account
        add_txn(self.conn, dt.date(2024, 5, 20), -50, "REFUND", txn_id=_tid("sp"))
        add_txn(self.conn, dt.date(2024, 5, 21), 500, "TO SAVINGS",
                override="TRANSFER_OUT", txn_id=_tid("sp"))
        add_txn(self.conn, dt.date(2024, 5, 22), 200, "CARD PAYMENT",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", txn_id=_tid("sp"))
        add_account(self.conn, "ln1", "Car Loan", "loan", "auto", 9000)
        add_txn(self.conn, dt.date(2024, 5, 23), 75, "LOAN PMT",
                account="ln1", txn_id=_tid("sp"))
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method) "
            "VALUES ('SQ *COFFEE SHOP','Coffee Shop','layer1') "
            "ON CONFLICT (tenant_id, raw_merchant) DO NOTHING")

    def tearDown(self):
        self.conn.close()

    def test_by_year_and_exclusions(self):
        sp = reporting.compute_spending(self.conn)
        by_year = {y: (amt, n) for y, amt, n in sp["by_year"]}
        # spend-by-year keeps the LONG view — that is its job, and the reason
        # the toplists could be narrowed without losing history
        self.assertEqual(by_year["2024"], (220.0, 4))
        self.assertEqual(by_year["2025"], (30.0, 1))

    def test_eff_cat_underscore_merge(self):
        # the displayed toplist is trailing-12-month, so this needs
        # rows inside the window; the merge itself is a property of the
        # category mapping, not of the date range
        add_txn(self.conn, TODAY - dt.timedelta(days=30), 60, "CITY WATER",
                override="RENT_AND_UTILITIES", txn_id=_tid("sp"))
        add_txn(self.conn, TODAY - dt.timedelta(days=25), 40, "CITY POWER",
                primary="RENT_AND_UTILITIES", txn_id=_tid("sp"))
        sp = reporting.compute_spending(self.conn)
        cats = dict(map(tuple, sp["category_totals"]))
        # native primary + underscore override display-merge
        self.assertEqual(cats["RENT AND UTILITIES"], 100.0)
        self.assertNotIn("RENT_AND_UTILITIES", cats)

    def test_top_merchants_use_canonical_map(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=20), 20, "SQ *COFFEE SHOP",
                merchant="SQ *COFFEE SHOP", txn_id=_tid("sp"))
        sp = reporting.compute_spending(self.conn)
        merch = {m: (a, n) for m, a, n, *_ in sp["top_merchants"]}
        self.assertEqual(merch["Coffee Shop"], (20.0, 1))
        self.assertNotIn("SQ *COFFEE SHOP", merch)

    def test_amazon_and_yoy_matrix(self):
        sp = reporting.compute_spending(self.conn)
        self.assertEqual(sp["amazon"], [["Amazon - Food & Drink", 30.0, 1]])
        self.assertEqual(sp["yoy_years"], ["2024", "2025"])
        row = next(r for r in sp["yoy_matrix"]
                   if r[0] == "RENT AND UTILITIES")
        self.assertEqual(row, ["RENT AND UTILITIES", 100.0, 0])


class CashflowTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        c, tid = self.conn, _tid
        # real bank income (negative = money in, chk is depository)
        add_txn(c, dt.date(2025, 3, 1), -6000, "ACME PAYROLL DIRECT DEP",
                account="chk", primary="INCOME", txn_id=tid("cf"))
        add_txn(c, dt.date(2025, 9, 1), -4000, "ACME PAYROLL",
                account="chk", primary="INCOME", txn_id=tid("cf"))
        # own-money inflows that must NOT count as income
        add_txn(c, dt.date(2025, 4, 1), -2000, "BETTERMENT TRANSFER",
                account="chk", primary="INCOME", txn_id=tid("cf"))
        add_txn(c, dt.date(2025, 4, 2), -500, "ONLINE EXT TRANS FROM BANK",
                account="chk", primary="INCOME", txn_id=tid("cf"))
        add_txn(c, dt.date(2025, 4, 3), -300, "MOVE",
                account="chk", override="TRANSFER_IN", txn_id=tid("cf"))
        # spend
        add_txn(c, dt.date(2025, 3, 10), 100, "SAFEWAY",
                primary="FOOD_AND_DRINK", txn_id=tid("cf"))
        # a card-only year: spend exists, no bank rows → income unknowable
        add_txn(c, dt.date(2023, 6, 1), 50, "OLD CARD SPEND", txn_id=tid("cf"))
        # bank-side investment funding (+out to provider, −back from it)
        add_txn(c, dt.date(2025, 4, 2), 1000, "ACH BETTERMENT INVEST",
                account="chk", primary="TRANSFER_OUT", txn_id=tid("cf"))
        # workplace-plan payroll contributions (bypass the bank)
        add_item(c, "plan", "Northwind Plan Services", aggregator="plan_csv")
        add_account(c, "plan1", "401(a)", "investment", "401a", 20000,
                    item="plan")
        for i, d in enumerate((dt.date(2024, 5, 15), dt.date(2024, 6, 15))):
            add_raw_txn(c, d, -500, "Contribution", "plan1",
                        {"type": "Contribution", "amount": 500},
                        txn_id=f"plan:cf{_n[0]}{i}")
        # income spine
        c.execute("INSERT INTO income_annual (year, medicare_earnings) "
                  "VALUES (1990, 20000) ON CONFLICT (tenant_id, year) DO NOTHING")
        c.execute("""INSERT INTO income_annual (year, total_income, wages,
                       investment_income, tax_paid, joint, medicare_earnings)
                     VALUES (2020, 80000, 70000, 500, 9000, 1, 75000)
                     ON CONFLICT (tenant_id, year) DO NOTHING""")

    def tearDown(self):
        self.conn.close()

    def test_savings_by_year(self):
        cf = reporting.compute_cashflow(self.conn)
        rows = {r[0]: r for r in cf["savings_by_year"]}
        # 2023: card spend but no bank data → income unknown, not $0
        self.assertEqual(rows["2023"], ["2023", None, 50.0, None, None])
        # 2025: income 10000 (brokerage/external/TRANSFER_IN excluded), spend 100
        self.assertEqual(rows["2025"], ["2025", 10000.0, 100.0, 9900.0, 99.0])

    def test_a_reclassified_transfer_in_is_not_income(self):
        # Someone is paid through a payment app (TRANSFER_OUT to the bank)
        # and once sends $50 back (TRANSFER_IN). A merchant-wide rule makes
        # the merchant "GENERAL_SERVICES", and a user rule reaches the
        # inflow row too — it stops being a transfer
        # to the effective-category test, but it is still not income.
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, category_primary, category_plaid,
                   category_source, pending, removed, raw)
               VALUES ('venmo-back', 'chk', '2025-03-15', -50, 'PAYAPP REFUND',
                       'PAYAPP REFUND', 'GENERAL_SERVICES', 'TRANSFER_IN',
                       'rule_user', 0, 0, '{}')""")
        cf = reporting.compute_cashflow(self.conn)
        rows = {r[0]: r for r in cf["savings_by_year"]}
        self.assertEqual(rows["2025"][1], 10000.0)
        # an explicit per-row INCOME override is the user's word: it counts
        self.conn.execute("UPDATE transactions SET category_override='INCOME' "
                          "WHERE id='venmo-back'")
        cf = reporting.compute_cashflow(self.conn)
        rows = {r[0]: r for r in cf["savings_by_year"]}
        self.assertEqual(rows["2025"][1], 10050.0)

    def test_monthly_by_year_only_income_months(self):
        cf = reporting.compute_cashflow(self.conn)
        self.assertNotIn("2023", cf["monthly_by_year"])
        pts = dict(cf["monthly_by_year"]["2025"])
        self.assertEqual(pts, {"Mar": 5900.0, "Sep": 4000.0})

    def test_investment_funding_bank_side_plus_plan_contributions(self):
        cf = reporting.compute_cashflow(self.conn)
        fund = dict(map(tuple, cf["investment_funding"]))
        # 2024 = two $500 payroll contributions
        self.assertEqual(fund["2024"], 1000.0)
        # 2025 = +1000 to the brokerage from checking − 2000 back
        self.assertEqual(fund["2025"], -1000.0)

    def test_todays_investment_funding_counts_month_to_date(self):
        """A pull from an investment institution that landed TODAY is the
        very event the Today page and the email flag this morning; the
        Cash Flow month-to-date figure must show the same dollars, not
        wait for tomorrow."""
        add_txn(self.conn, TODAY, -1200, "NORTHWIND PLAN TRANSFER",
                account="chk", primary="TRANSFER_IN", txn_id=_tid("cf"))
        cf = reporting.compute_cashflow(self.conn, today=TODAY)
        self.assertEqual(cf["brokerage_funding_mtd"], 1200.0)

    def test_income_spine(self):
        cf = reporting.compute_cashflow(self.conn)
        self.assertEqual(cf["reported_income"],
                         [[2020, 80000.0, 70000.0, 500.0, 9000.0, 1]])
        self.assertEqual(cf["career_income"],
                         [[1990, 20000.0], [2020, 75000.0]])
        # cash_events runs on the CURRENT month/year; this fixture's funding
        # flows are all 2024/2025, so both figures are zero today
        self.assertEqual(cf["brokerage_funding_mtd"], 0)
        self.assertEqual(cf["brokerage_funding_ytd"], 0)


class FeesTests(unittest.TestCase):
    @staticmethod
    def _brokerage(conn):
        add_item(conn, "brokit", "Evergreen Brokerage")
        add_account(conn, "brok", "Taxable Brokerage", "investment",
                    "brokerage", 1000, item="brokit")

    def test_word_boundary_fee_match(self):
        conn = make_db()
        try:
            write_config(conn)
            self._brokerage(conn)
            add_txn(conn, dt.date(2025, 2, 1), 5, "Monthly Maintenance fee",
                    account="brok", txn_id=_tid("fee"))
            add_txn(conn, dt.date(2025, 3, 1), 12,
                    "ANNUAL MEMBERSHIP FEES CHARGED", account="brok",
                    txn_id=_tid("fee"))
            add_txn(conn, dt.date(2025, 4, 1), 3, "ACCOUNT SERVICING",
                    account="brok", detailed="BANK_FEES_OTHER_BANK_FEES",
                    txn_id=_tid("fee"))
            # coffee is not a fee, though a bare '%fee%' matches it
            add_txn(conn, dt.date(2025, 5, 1), 4, "STARBUCKS COFFEE",
                    account="brok", detailed="FOOD_AND_DRINK_COFFEE",
                    txn_id=_tid("fee"))
            f = reporting.compute_fees(conn)
            self.assertEqual(f["by_platform"], [["Evergreen Brokerage", 20.0, 3]])
            self.assertEqual(f["by_year"], [["2025", 20.0]])
            self.assertEqual(f["total"], 20.0)
        finally:
            conn.close()

    def test_a_refunded_fee_nets_off_the_drag(self):
        """A reversed fee is money coming BACK (negative, house sign
        convention). Summing magnitudes counts the refund as a second
        charge, so a platform that took $40 and gave it back would report
        $80 of drag."""
        conn = make_db()
        try:
            write_config(conn)
            self._brokerage(conn)
            add_txn(conn, dt.date(2025, 2, 1), 40, "QUARTERLY ADVISORY FEE",
                    account="brok", txn_id=_tid("fee"))
            add_txn(conn, dt.date(2025, 3, 1), -15, "ADVISORY FEE REVERSAL",
                    account="brok", txn_id=_tid("fee"))
            f = reporting.compute_fees(conn)
            self.assertEqual(f["by_platform"],
                             [["Evergreen Brokerage", 25.0, 2]])
            self.assertEqual(f["by_year"], [["2025", 25.0]])
            self.assertEqual(f["total"], 25.0)
        finally:
            conn.close()

    def test_investment_accounts_only(self):
        """The report is investment drag, not every fee ever paid.
        Bank/card/ATM fees are ordinary spending and stay on Spending."""
        conn = make_db()
        try:
            write_config(conn)
            self._brokerage(conn)
            add_txn(conn, dt.date(2025, 2, 1), 40, "QUARTERLY ADVISORY FEE",
                    account="brok", txn_id=_tid("fee"))
            add_txn(conn, dt.date(2025, 3, 1), 3, "NON-NETWORK ATM FEE",
                    account="chk", detailed="BANK_FEES_ATM_FEES",
                    txn_id=_tid("fee"))
            add_txn(conn, dt.date(2025, 4, 1), 95, "ANNUAL MEMBERSHIP FEE",
                    account="card", detailed="BANK_FEES_OTHER_BANK_FEES",
                    txn_id=_tid("fee"))
            f = reporting.compute_fees(conn)
            self.assertEqual(f["by_platform"], [["Evergreen Brokerage", 40.0, 1]])
            self.assertEqual(f["by_year"], [["2025", 40.0]])
            self.assertEqual(f["total"], 40.0)
        finally:
            conn.close()

    def test_advisory_fee_estimate_is_configured_per_robo_advisor(self):
        conn = make_db()
        try:
            write_config(conn, advisory_fees={"evergreen": 0.0025},
                         advisory_fee_exempt_masks=["1111"])
            add_item(conn, "rb", "Evergreen Robo")
            add_account(conn, "rba", "Managed Portfolio", "investment",
                        "brokerage", 110000, item="rb")
            # self-directed account at the same robo: exempt by mask
            add_account(conn, "rbs", "Stock Picking", "investment",
                        "brokerage", 50000, item="rb", mask="1111")
            add_raw_txn(conn, months_ago(13), 100000, "BUY VTI", "rba",
                        {"units": 1000, "ticker": "VTI", "unitprice": 100,
                         "type": "buy"})
            add_holding(conn, "rba", "VTI", 1000, 110, 110000)
            add_raw_txn(conn, months_ago(13), 50000, "BUY QQQ", "rbs",
                        {"units": 100, "ticker": "QQQ", "unitprice": 500,
                         "type": "buy"})
            add_holding(conn, "rbs", "QQQ", 100, 500, 50000)
            f = reporting.compute_fees(conn)
            names = [p[0] for p in f["by_platform"]]
            self.assertIn("Evergreen Robo advisory (estimated 0.25%)", names)
            adv = next(p for p in f["by_platform"]
                       if p[0].startswith("Evergreen Robo advisory"))
            self.assertGreater(adv[1], 0)
            # 0.25% of ~$105k over a year, not of $155k: the exempt account
            # pays no advisory fee
            self.assertLess(adv[1], 0.0025 * 130000)
            self.assertEqual(f["total"], adv[1])
        finally:
            conn.close()

    def test_no_configured_fee_estimates_nothing(self):
        conn = make_db()
        try:
            write_config(conn)
            add_item(conn, "rb", "Evergreen Robo")
            add_account(conn, "rba", "Managed Portfolio", "investment",
                        "brokerage", 110000, item="rb")
            add_holding(conn, "rba", "VTI", 1000, 110, 110000)
            names = [p[0] for p in reporting.compute_fees(conn)["by_platform"]]
            self.assertFalse(any("advisory" in n for n in names))
        finally:
            conn.close()


class CoinbasePLTests(unittest.TestCase):
    def test_fiat_flows_only(self):
        conn = make_db()
        try:
            add_item(conn, "coinbase", "Coinbase")
            add_account(conn, "cbw", "BTC Wallet", "investment", "crypto",
                        4000, item="coinbase")
            add_raw_txn(conn, dt.date(2021, 1, 5), 1000, "buy", "cbw",
                        {"type": "buy",
                         "amount": {"amount": "0.5", "currency": "BTC"},
                         "native_amount": {"amount": "1000.00",
                                           "currency": "USD"}})
            add_raw_txn(conn, dt.date(2022, 3, 5), -300, "sell", "cbw",
                        {"type": "sell",
                         "amount": {"amount": "-0.1", "currency": "BTC"},
                         "native_amount": {"amount": "-300.00",
                                           "currency": "USD"}})
            # own money moving between Coinbase venues — NOT a fiat flow
            add_raw_txn(conn, dt.date(2021, 6, 1), -2000, "pro", "cbw",
                        {"type": "pro_withdrawal",
                         "amount": {"amount": "-0.2", "currency": "BTC"},
                         "native_amount": {"amount": "-2000.00",
                                           "currency": "USD"}})
            conn.execute(
                "INSERT INTO crypto_holdings (account_id, currency, quantity, "
                "native_usd) VALUES ('cbw','BTC',0.4,4000) "
                "ON CONFLICT (tenant_id, account_id, currency) DO NOTHING")
            pl = reporting._coinbase_pl(conn)
            self.assertEqual(pl, [{"name": "BTC Wallet", "cash_in": 1000.0,
                                   "cash_out": 300.0, "value": 4000.0,
                                   "pl": 3300.0, "pct": 330.0}])
        finally:
            conn.close()


class BrokeragePLTests(unittest.TestCase):
    def test_basis_override_by_mask(self):
        conn = make_db()
        try:
            write_config(conn, investment_basis_override={"1234": 400})
            add_item(conn, "nb", "Northwind Brokerage")
            add_account(conn, "roth", "Roth IRA", "investment", "roth",
                        1000, item="nb", mask="1234")
            add_account(conn, "auto", "Managed Portfolio", "investment",
                        "brokerage", 2000, item="nb", mask="5678")
            # a source basis known to be wrong ($6) — override must win
            add_holding(conn, "roth", "VEA", 100, 10, 1000,
                        raw={"cost_basis": 6})
            add_holding(conn, "auto", "VTI", 10, 200, 2000,
                        raw={"cost_basis": 1500})
            pl = reporting._brokerage_pl(conn)
            self.assertEqual(pl, [
                {"name": "Managed Portfolio", "institution": "Northwind Brokerage",
                 "basis": 1500.0, "value": 2000.0, "pl": 500.0, "pct": 33.3},
                {"name": "Roth IRA", "institution": "Northwind Brokerage",
                 "basis": 400.0, "value": 1000.0, "pl": 600.0, "pct": 150.0},
                {"name": "Total", "basis": 1900.0, "value": 3000.0,
                 "pl": 1100.0, "pct": 57.9},
            ])
        finally:
            conn.close()


class RetirementPLTests(unittest.TestCase):
    def test_contribution_basis_and_combined(self):
        conn = make_db()
        try:
            write_config(conn)
            add_item(conn, "plan", "Northwind Plan Services", aggregator="plan_csv")
            add_account(conn, "l1", "401(a)", "investment", "401a", 10000,
                        item="plan")
            add_account(conn, "l2", "403(b)", "investment", "403b", 5000,
                        item="plan")
            for i in range(2):
                add_raw_txn(conn, dt.date(2025, 1 + i, 15), -3000,
                            "Contribution", "l1",
                            {"type": "Contribution", "amount": 3000})
            add_raw_txn(conn, dt.date(2025, 1, 15), -2500, "Contribution",
                        "l2", {"type": "Contribution", "amount": 2500})
            pl = reporting._retirement_pl(conn)
            self.assertEqual(pl, [
                {"name": "401(a)", "institution": "Northwind Plan Services",
                 "contributed": 6000.0, "value": 10000.0,
                 "pl": 4000.0, "pct": 66.7},
                {"name": "403(b)", "institution": "Northwind Plan Services",
                 "contributed": 2500.0, "value": 5000.0,
                 "pl": 2500.0, "pct": 100.0},
                {"name": "Total", "contributed": 8500.0, "value": 15000.0,
                 "pl": 6500.0, "pct": 76.5},
            ])
            comb = reporting._retirement_combined(conn)
            self.assertEqual([g["provider"] for g in comb["groups"]],
                             ["Northwind Plan Services"])
            self.assertEqual(comb["total"],
                             {"invested": 8500.0, "value": 15000.0,
                              "pl": 6500.0, "pct": 76.5})
        finally:
            conn.close()


class ReportingApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db
        _ensure_db()                       # redirect DSNs BEFORE app import
        conn = make_db()
        try:
            write_config(conn)
            add_txn(conn, dt.date(2025, 3, 10), 100, "SAFEWAY",
                    primary="FOOD_AND_DRINK", txn_id=_tid("api"))
            cls.tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        finally:
            conn.close()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import oikonome.web.app as appmod
        from oikonome.web import reporting_api
        api_app = FastAPI()
        api_app.include_router(reporting_api.router)
        api_app.dependency_overrides[appmod.current_user] = (
            lambda: {"tenant_id": cls.tid})
        cls.client = TestClient(api_app)

    def test_route_shapes(self):
        r = self.client.get("/api/reports/networth").json()
        for k in ("current_total", "trend", "by_asset_class", "by_institution",
                  "property_items", "snapshot_trend", "savings_trend",
                  "coinbase_pl", "retirement_combined",
                  "trend_estimated_until", "_built_at"):
            self.assertIn(k, r)
        r = self.client.get("/api/reports/spending").json()
        for k in ("by_year", "by_month", "top_merchants", "amazon",
                  "category_totals", "yoy_years", "yoy_cats", "yoy_matrix"):
            self.assertIn(k, r)
        self.assertEqual(r["by_year"], [["2025", 100.0, 1]])
        r = self.client.get("/api/reports/cashflow").json()
        for k in ("savings_by_year", "monthly_by_year", "reported_income",
                  "career_income", "income_by_month", "investment_funding",
                  "brokerage_funding_mtd", "brokerage_funding_ytd"):
            self.assertIn(k, r)
        r = self.client.get("/api/reports/fees").json()
        for k in ("by_platform", "by_year", "total"):
            self.assertIn(k, r)

    def test_snapshot_tenant_job(self):
        from oikonome.web import reporting_api
        out = reporting_api.snapshot_tenant(self.tid)
        self.assertEqual(out, {"snapshot": True})
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT total FROM networth_snapshot WHERE date=%s",
                (dt.date.today(),)).fetchone()
            self.assertEqual(row["total"], 4750.0)
            hb = conn.execute(
                "SELECT job FROM job_runs WHERE job='networth-snapshot'"
            ).fetchone()
            self.assertIsNotNone(hb)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()


class NetWorthBusinessSeparationTests(unittest.TestCase):
    """The reconstructed trend must not count business money the CURRENT
    total excludes.

    compute_networth's `_live_accounts` filters `a.entity_id IS NULL` (unless
    combine_entities), and `_reconstruct_trend` has to apply the same filter.
    Without it today's anchor is personal-only while every historical month
    carries the business balance too — the line steps DOWN at today by the
    size of the entity, purely as an artifact.
    """

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # personal investment account so the trend actually reconstructs
        add_account(self.conn, "inv1", "Roth IRA", "investment", "ira", 100000)
        add_raw_txn(self.conn, months_ago(3), 2000, "BUY VTI", "inv1",
                    {"units": 10, "ticker": "VTI", "unitprice": 100,
                     "type": "buy"})
        add_holding(self.conn, "inv1", "VTI", 10, 200, 2000)
        # business checking, assigned to an entity
        add_account(self.conn, "bizchk", "Biz Checking", "depository",
                    "checking", 40000)
        ent = entities.create_entity(self.conn, name="Acme LLC",
                                     structure="sole_prop")
        entities.assign_account(self.conn, "bizchk", ent["id"])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_trend_excludes_business_like_the_current_total_does(self):
        """The business account is 40,000. Unfiltered, the historical cash
        baseline carries it while today's anchor does not, so the line sits
        ~40k high for every past month and then steps to the real number at
        today — a fabricated jump the size of the entity."""
        nw = reporting.compute_networth(self.conn)
        trend = nw["trend"]
        self.assertGreaterEqual(len(trend), 2)
        self.assertEqual(trend[-1][1], nw["current_total"])
        # the personal line moves only with VTI's price (10 shares) — a
        # past month carrying the 40,000 entity balance stands far above
        # today's personal total
        for month, value in trend[:-1]:
            self.assertLess(
                value, nw["current_total"] + 20000.0,
                f"trend point {month}={value} includes the 40,000 business "
                "account that current_total excludes — the personal "
                "net-worth history and today's total disagree")

    def test_business_money_does_not_move_the_personal_trend(self):
        """Strongest form: assigning an account to an entity must leave the
        personal trend byte-identical to not having the account at all."""
        with_biz = reporting.compute_networth(self.conn)["trend"]
        self.conn.execute("DELETE FROM accounts WHERE id='bizchk'")
        self.conn.commit()
        without = reporting.compute_networth(self.conn)["trend"]
        self.assertEqual(without, with_biz)


class ForecastHorizonConsistencyTests(unittest.TestCase):
    """The cash-flow chart must not invent its own horizon.

    Asking forecast.build for more days than HORIZON_DAYS (what every other
    surface serves — Today runway, headroom) renders days no measurement
    covers. The walk's optimism grows with the horizon, so those furthest
    days are the least trustworthy part of the picture — drawn exactly as
    confidently as day 1.
    """

    def test_cash_graph_uses_the_shared_horizon(self):
        import inspect

        from oikonome.engine import forecast
        src = inspect.getsource(reporting._cash_graph)
        self.assertIn("forecast.HORIZON_DAYS", src,
                      "the cash graph hard-codes its own horizon again")
        self.assertNotIn("days=90", src)
        self.assertEqual(60, forecast.HORIZON_DAYS)

    def test_spa_label_matches_the_horizon(self):
        """A chart labelled 90-day while serving 60 is its own bug.

        The current design carries no horizon in the chart title at all —
        the dashed tail names itself at the boundary instead of the heading
        announcing a number the reader then has to trust. So the invariant
        is not "the label exists": it is that ANY horizon the SPA states
        must be the horizon actually served. A design with no label states
        nothing and can contradict nothing; a design that grows one back is
        checked here.
        """
        import pathlib
        import re

        from oikonome.engine import forecast
        spa = (pathlib.Path(__file__).resolve().parents[2]
               / "webapp" / "src" / "pages" / "CashFlow.tsx")
        if not spa.exists():
            self.skipTest("SPA source not present")
        text = spa.read_text(encoding="utf-8")
        stated = {int(n) for n in re.findall(r"(\d+)-day forecast", text)}
        self.assertTrue(
            stated <= {forecast.HORIZON_DAYS},
            f"the page states {sorted(stated - {forecast.HORIZON_DAYS})}-day "
            f"forecast(s) while serving {forecast.HORIZON_DAYS}")
