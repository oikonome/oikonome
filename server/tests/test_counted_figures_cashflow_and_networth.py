"""Cash Flow, the lenses, Net Worth and Accounts print figures the server
decided once (specs/counted-figures.md).

- Every Cash Flow tab reads ONE window: Overview, Spending and Income
  cover the same months, and a month the bank feed misses is counted and
  marked `incomplete`, never dropped from one tab and kept on another.
- Saved is income − spending on every bar, the month in progress
  included; the forecast follows as its own points, and the caption's
  horizon is the server's.
- The Year lens's totals are the sum of its twelve cells.
- Net worth sends the headline's change, the property tile's change, each
  ring's net and the Over-time gain, over the same total as the headline.
- The Accounts page's total IS Net Worth's Financial accounts figure; loans
  are listed as owed, not subtracted.
- The Income tab's per-month figure and paycheck share, and interest vs
  interest earned, are server fields.
"""

import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import budget, forecast, reporting
from oikonome.web import lenses

from .util import (_admin_dsn, _ensure_db, TEST_DB, add_bill, add_txn,
                   make_db, seed_accounts, write_config)


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
        reporting._FIXED_MONTH_CACHE.clear()
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            self.tid = str(tenancy.create_tenant(
                admin, f"cf-{uuid.uuid4().hex[:8]}"))
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
        self.conn = tenancy.tenant_connect(self.tid)

    def tearDown(self):
        self.conn.close()
        reporting._FIXED_MONTH_CACHE.clear()

    def seed(self, conn):
        raise NotImplementedError

    @staticmethod
    def pay(conn, day, amount):
        add_txn(conn, day, -amount, "NORTHWIND PAYROLL", account="chk",
                primary="INCOME", detailed="INCOME_WAGES")

    @staticmethod
    def autopay(conn, day, amount):
        add_txn(conn, day, amount, "CARD AUTOPAY", account="chk",
                primary="TRANSFER_OUT")

    @staticmethod
    def card(conn, day, amount):
        add_txn(conn, day, amount, "GROCER", account="card",
                primary="FOOD_AND_DRINK")


class EveryCashFlowTabReadsTheSameWindow(_Tenant):
    """Card spend of $4,000 a month from Jan 2023; the bank feed (pay of
    $5,000 and the card's autopay) from Jan 2024, with a hole in Jul 2025
    where checking has no row at all."""

    TODAY = dt.date(2025, 9, 9)

    def seed(self, conn):
        for d in _months(dt.date(2023, 1, 1), dt.date(2025, 8, 1)):
            self.card(conn, d, 4000.0)
            if d >= dt.date(2024, 1, 1) and (d.year, d.month) != (2025, 7):
                self.pay(conn, d, 5000.0)
                self.autopay(conn, d + dt.timedelta(days=5), 4000.0)

    def _tabs(self, rk):
        return (reporting.flow_breakdown(self.conn, self.TODAY, rk),
                reporting.spending_window(self.conn, self.TODAY, rk),
                reporting.income_window(self.conn, self.TODAY, rk))

    def test_a_month_without_a_bank_feed_is_counted_and_marked(self):
        """3m on Sep 9 is Jun–Aug on every tab. July has no bank row: its
        spend is still in the Overview's total (the Spending tab counts
        it), and every tab names it incomplete."""
        flow, spend, inc = self._tabs("3m")
        for w in (flow, spend, inc):
            self.assertEqual((w["from"], w["to"]),
                             ("2025-06-01", "2025-08-31"))
            self.assertEqual(w["incomplete_months"], ["2025-07"])
        self.assertEqual(spend["total"], 12000.0)
        self.assertEqual(flow["out"]["total"], spend["total"])
        self.assertEqual(flow["in"]["total"], inc["total"])
        self.assertEqual(flow["in"]["total"], 10000.0)
        # saved and the rate only over June and August, the months with
        # bank data: July's spend with no feed is not a loss
        self.assertEqual(flow["saved"], 2000.0)
        self.assertEqual(flow["rate"], 20.0)
        self.assertEqual((flow["saved_months_known"],
                          flow["saved_months_total"]), (2, 3))
        self.assertEqual(flow["dating"], "paid")

    def test_a_years_saved_counts_only_its_months_with_bank_data(self):
        """2025 through August with one month unfed: in and out over
        every month; saved and the rate over the seven fed months, and
        the row says so."""
        cf = reporting.compute_cashflow(self.conn, self.TODAY)
        row = {r[0]: r for r in cf["savings_by_year"]}["2025"]
        self.assertEqual(row[1], 7 * 5000.0)
        self.assertEqual(row[2], 8 * 4000.0)
        self.assertEqual(row[3], 7 * 1000.0)
        self.assertEqual(row[4], 20.0)
        self.assertEqual(cf["savings_by_year_notes"]["2025"],
                         "income incomplete — left over counts 8 of 9 months")
        # September is in progress and measured (nothing posted yet)
        self.assertEqual(cf["savings_by_year_months"]["2025"], [8, 9])
        # 2023 has no bank feed at all: in/out, no saved figure
        row = {r[0]: r for r in cf["savings_by_year"]}["2023"]
        self.assertEqual((row[2], row[3], row[4]), (48000.0, None, None))

    def test_all_opens_on_the_earlier_ledger_on_every_tab(self):
        """The cards begin a year before the bank feed: "All" opens in Jan
        2023 on all three tabs, the feedless months marked."""
        flow, spend, inc = self._tabs("all")
        self.assertEqual({w["from"] for w in (flow, spend, inc)},
                         {"2023-01-01"})
        self.assertEqual(flow["out"]["total"], spend["total"])
        self.assertIn("2023-01", flow["incomplete_months"])
        self.assertIn("2025-07", flow["incomplete_months"])
        self.assertEqual(flow["incomplete_months"],
                         inc["incomplete_months"])

    def test_the_income_tab_sends_its_pace_and_paycheck_share(self):
        _flow, _spend, inc = self._tabs("3m")
        self.assertEqual(inc["per_month"],
                         round(inc["total"] / inc["months"], 2))
        self.assertEqual(inc["paycheck_share"], 100)


class SavedIsIncomeLessSpendingThroughThisMonth(_Tenant):
    """Pay of $5,000 and card spend of $1,200 every month; this month so
    far, $5,000 in and $1,000 out."""

    TODAY = dt.date(2026, 10, 9)

    def seed(self, conn):
        for d in _months(dt.date(2026, 1, 1), dt.date(2026, 9, 1)):
            self.pay(conn, d, 5000.0)
            self.card(conn, d, 1200.0)
            self.autopay(conn, d + dt.timedelta(days=5), 1200.0)
        self.pay(conn, dt.date(2026, 10, 3), 5000.0)
        self.card(conn, dt.date(2026, 10, 5), 1000.0)

    def test_the_month_in_progress_is_a_saved_bar_of_income_less_spending(self):
        cf = reporting.compute_cashflow(self.conn, self.TODAY)
        g = cf["cash_graph"]
        hist = g["points"][:g["forecast_from"]]
        # `points` keeps its meaning for installed apps: history only
        self.assertEqual(hist[-1], ["2026-09", 3800.0])
        self.assertEqual(g["current"], ["2026-10", 4000.0])
        # the bar is the figure the year row expands into
        self.assertEqual(dict(cf["monthly_by_year"]["2026"])["Oct"], 4000.0)
        self.assertEqual(g["forecast_days"], forecast.HORIZON_DAYS)
        self.assertEqual(cf["dating"], "paid")


class YearTotalsAreTheSumOfTheCells(unittest.TestCase):
    """A year of pay and rent, every closed month under a frozen budget.
    The year in progress also holds a charge the feed dated ahead of today:
    the month cell, read through today, does not count it yet, so the
    year's total — the sum of its cells — does not either."""

    TODAY = dt.date(2026, 3, 15)

    def setUp(self):
        reporting._FIXED_MONTH_CACHE.clear()
        self.conn = make_db()
        write_config(self.conn)
        self.bank = "chk"
        add_txn(self.conn, dt.date(2024, 11, 3), 41.0, "Corner Grocer",
                account=self.bank)
        for d in _months(dt.date(2024, 12, 1), dt.date(2026, 2, 1)):
            add_txn(self.conn, d.replace(day=3), -3000.0, "NORTHWIND PAYROLL",
                    account=self.bank, primary="INCOME",
                    detailed="INCOME_WAGES")
        for d in (dt.date(2025, 10, 1), dt.date(2025, 11, 1),
                  dt.date(2025, 12, 1), dt.date(2025, 12, 30),
                  dt.date(2026, 2, 1), dt.date(2026, 3, 1)):
            add_txn(self.conn, d, 1300.0, "Rent Co", account=self.bank,
                    primary="RENT_AND_UTILITIES")
        add_bill(self.conn, "Rent Co", 1300.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 4, 1), last_seen=dt.date(2026, 3, 1))
        for m in range(1, 13):
            budget.snapshot_month(self.conn, dt.date(2025, m, 1))
        add_txn(self.conn, dt.date(2026, 3, 20), 75.0, "Quillsoft",
                account=self.bank)

    def tearDown(self):
        self.conn.close()
        reporting._FIXED_MONTH_CACHE.clear()

    def test_the_year_total_is_its_cells_sum(self):
        for year in (2025, 2026):
            y = lenses.year_summary(self.conn, year, self.TODAY)
            cells = [c for c in y["cells"] if c["status"] != "future"]
            self.assertEqual(y["totals"]["spend"],
                             round(sum(c["spend"] for c in cells), 2))
            self.assertEqual(y["totals"]["income"],
                             round(sum(c["income"] for c in cells), 2))
            self.assertEqual(y["totals"]["saved"],
                             round(y["totals"]["income"]
                                   - y["totals"]["spend"], 2))
        self.assertEqual(y["dating"], "bill_month")

    def test_last_years_comparison_is_the_sum_of_its_cells_too(self):
        """2026 as of Mar 15 is compared with 2025 through Mar 15, summed
        from 2025's own cells read as of that day."""
        y = lenses.year_summary(self.conn, 2026, self.TODAY)
        cells25 = lenses.year_summary(self.conn, 2025,
                                      dt.date(2025, 3, 15))["cells"]
        want = lenses.cell_totals(cells25)
        self.assertEqual(y["prev_totals"], want)
        self.assertEqual(y["prev_totals"]["saved_months_total"], 3)

    def test_the_printed_left_over_is_printed_income_less_printed_spend(self):
        """Rounded apart, income $1,001, spend $500 and left over $500 did
        not subtract; with every month's income known the three whole
        dollars are apportioned so they do. Figures are invented."""
        cells = [{"m": 1, "status": "final", "income": 600.30,
                  "spend": 300.20},
                 {"m": 2, "status": "final", "income": 400.30,
                  "spend": 200.20},
                 {"m": 3, "status": "future"}]
        t = lenses.cell_totals(cells, year=2026)
        d = t["dollars"]
        self.assertEqual(d["saved"], d["income"] - d["spend"])
        # income and spend are their own whole dollars, as the Total spend
        # row and Cash Flow print them; left over is what they leave
        self.assertEqual(d["income"], reporting.whole_dollars(t["income"]))
        self.assertEqual(d["spend"], reporting.whole_dollars(t["spend"]))
        # a month whose income is unknown: left over counts other months
        # than spend, so each figure is its own rounding
        cells[1]["income"] = None
        t = lenses.cell_totals(cells, year=2026)
        self.assertEqual(t["dollars"], {"income": 600, "spend": 500,
                                        "saved": 300})

    def test_the_totals_card_and_the_total_spend_row_print_one_spend(self):
        """The totals card printed the apportioned spend and the category
        table's Total spend row (and its change) the spend rounded alone:
        $501 and $500 for one year. Figures are invented."""
        d = reporting.year_dollars(1000.70, 500.30, 500.40, every_month=True)
        self.assertEqual(d["spend"], reporting.whole_dollars(500.30))
        self.assertEqual(d["income"], reporting.whole_dollars(1000.70))
        self.assertEqual(d["saved"], d["income"] - d["spend"])

    def test_the_yearly_email_prints_the_years_left_over(self):
        """The yearly email's net is the Year lens's left over as printed:
        the printed income less the printed spend. Figures are invented."""
        from oikonome.web import lens_email
        cells = [{"m": 1, "status": "final", "income": 600.30,
                  "spend": 300.20, "net": 300.10},
                 {"m": 2, "status": "final", "income": 400.30,
                  "spend": 200.20, "net": 200.10}]
        cells += [{"m": m, "status": "future"} for m in range(3, 13)]
        t = lenses.cell_totals(cells, year=2026)
        ctx = lens_email.year_mail_context({"cells": cells, "totals": t},
                                           2026)
        self.assertEqual(ctx["tiles"][0]["value"],
                         f"+${t['dollars']['saved']:,}")

    def test_the_yearly_email_prints_each_years_left_over_one_way(self):
        """The headline printed the year's left over as the printed income
        less the printed spend, while the year strip and last year's tile
        rounded the net alone: one year, two figures a dollar apart.
        Figures are invented."""
        from oikonome.web import lens_email
        cells = [{"m": 1, "status": "final", "income": 8000.40,
                  "spend": 3000.90, "net": 4999.50}]
        cells += [{"m": m, "status": "final", "income": 0.0, "spend": 0.0,
                   "net": 0.0} for m in range(2, 13)]
        t = lenses.cell_totals(cells, year=2025)
        years = [{"y": 2024, "income": 7000.40, "spend": 2000.90,
                  "net": 4999.50, "incomplete": False},
                 {"y": 2025, "income": 8000.40, "spend": 3000.90,
                  "net": 4999.50, "incomplete": False}]
        ctx = lens_email.year_mail_context(
            {"cells": cells, "totals": t, "detail": {"years": years}}, 2025)
        want = {y["y"]: reporting.year_dollars(
            y["income"], y["spend"], y["net"], every_month=True)["saved"]
            for y in years}
        # 8,000.40 in and 3,000.90 out print $8,000 and $3,001, so $4,999
        # is left over; the 4,999.50 net alone rounds to $5,000
        self.assertEqual(want[2025], 4999)
        self.assertEqual(ctx["tiles"][0]["value"], f"+${want[2025]:,}")
        strip = {int(c["abbr"]): c["text"] for c in ctx["year_strip"]}
        self.assertEqual(strip[2025], ctx["tiles"][0]["value"])
        self.assertEqual(strip[2024], f"+${want[2024]:,}")
        self.assertEqual(ctx["tiles"][1]["value"], strip[2024])

    def test_one_income_reads_alike_beside_spends_cents_apart(self):
        """Cash Flow dates a bill by the day it was paid and the Year lens
        by its month, so one year's spend can differ by cents between
        them; the same income then printed a dollar apart, because the
        apportioning moved it. Figures are invented."""
        a = reporting.year_dollars(3200.30, 3600.70, -400.40,
                                   every_month=True)
        b = reporting.year_dollars(3200.30, 3600.90, -400.60,
                                   every_month=True)
        self.assertEqual(a["income"], b["income"])
        self.assertEqual(a["spend"], b["spend"])
        self.assertEqual(a["saved"], b["saved"])

    def test_cash_flow_and_the_year_print_a_year_by_one_rule(self):
        """Cash Flow's Left over by year rounded income and left over apart
        and read a dollar off the Year lens's totals for the same year;
        both now print the year in reporting.year_dollars's whole dollars.
        Figures are invented."""
        # cents that round apart: $0.60 in and $0.40 out more
        add_txn(self.conn, dt.date(2026, 1, 9), -0.60, "NORTHWIND PAYROLL",
                account=self.bank, primary="INCOME", detailed="INCOME_WAGES")
        add_txn(self.conn, dt.date(2026, 1, 10), 0.40, "Corner Grocer",
                account=self.bank)
        cf = reporting.compute_cashflow(self.conn, self.TODAY)
        inc, sp, net = ({r[0]: r for r in cf["savings_by_year"]}["2026"]
                        [1:4])
        d = cf["savings_by_year_dollars"]["2026"]
        self.assertEqual(d, reporting.year_dollars(inc, sp, net,
                                                   every_month=True))
        self.assertEqual(d["saved"], d["income"] - d["spend"])

    def test_the_plans_income_is_said_only_when_it_differs(self):
        """"$X in (expected from paychecks $X)" repeated the figure beside
        it; the server leaves the words out when the two are the same
        whole dollars. Figures are invented."""
        say = lenses.income_plan_text
        self.assertIsNone(say({"actual": 3000.2, "budgeted": 2999.8,
                               "source": "paychecks"}))
        self.assertEqual(say({"actual": 2500.0, "budgeted": 3000.0,
                              "source": "paychecks"}),
                         "expected from paychecks $3,000")
        self.assertEqual(say({"actual": None, "budgeted": 3000.0,
                              "source": "typed"}), "budgeted $3,000")
        self.assertIsNone(say({"actual": 10.0, "budgeted": None,
                               "source": None}))
        m = lenses.month_summary(self.conn, 2026, 3, self.TODAY)
        self.assertIn("plan_text", m["income"])

    def test_a_year_with_spend_and_no_bank_feed_has_no_income_or_saved(self):
        """2023 holds only a card charge, no bank row: its income and saved
        are unknown (None), never $0 and a loss of the whole spend."""
        add_txn(self.conn, dt.date(2023, 5, 5), 200.0, "Grocer",
                account="card", primary="FOOD_AND_DRINK")
        y = lenses.year_summary(self.conn, 2023, self.TODAY)
        self.assertIsNone(y["totals"]["income"])
        self.assertIsNone(y["totals"]["saved"])
        self.assertIsNone(y["totals"]["rate"])
        self.assertEqual(y["totals"]["saved_months_known"], 0)
        for c in y["cells"]:
            if c["status"] != "future":
                self.assertIsNone(c.get("net"), c["m"])

    def test_the_month_lens_names_where_its_income_comes_from(self):
        m = lenses.month_summary(self.conn, 2026, 3, self.TODAY)
        self.assertIn("source", m["income"])
        self.assertEqual(m["dating"], "bill_month")


class NetWorthSendsItsFigures(unittest.TestCase):
    """Checking $5,000 and a card owing $250; a manual home of $300,000
    and a mortgage of $200,000; a loan account owing $8,000."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, manual_assets=[
            {"name": "Home", "kind": "real_estate", "value": 300000,
             "as_of": "2026-01-01"},
            {"name": "Home loan", "kind": "mortgage", "value": -200000,
             "as_of": "2026-01-01"}])
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('car','it1','Car Loan','loan','auto',8000)")

    def tearDown(self):
        self.conn.close()

    def test_the_page_figures_come_from_the_server(self):
        nw = reporting.compute_networth(self.conn)
        for k in ("month_change", "financial_month_change",
                  "property_month_change", "trend_windows", "rings",
                  "full_trend_estimated_until", "mortgage_payoff"):
            self.assertIn(k, nw)
        self.assertEqual(nw["rings"]["by_asset_class"]["net"],
                         nw["current_total"])
        self.assertEqual(nw["rings"]["by_institution"]["net"],
                         nw["current_total"])
        self.assertEqual(nw["mortgage_payoff"], 200000.0)
        self.assertEqual(set(nw["trend_windows"]),
                         {"cur", "1m", "3m", "6m", "1y", "3y", "5y", "all"})

    def test_the_month_changes_add_up(self):
        full = [["2026-08", 100.0], ["2026-09", 160.0], ["2026-10", 200.0]]
        fin = [["2026-08", 90.0], ["2026-09", 100.0], ["2026-10", 130.0]]
        f = reporting._networth_figures(fin, full, 1, [], [], [])
        self.assertEqual(f["month_change"], 40.0)
        self.assertEqual(f["financial_month_change"], 30.0)
        self.assertEqual(f["property_month_change"], 10.0)
        w = f["trend_windows"]["all"]
        self.assertEqual((w["start"], w["end"], w["gain"]), (0, 2, 100.0))
        # "1m" is the month before this one: Aug → Sep
        w = f["trend_windows"]["1m"]
        self.assertEqual((w["start"], w["end"], w["gain"]), (0, 1, 60.0))
        w = f["trend_windows"]["cur"]
        self.assertEqual((w["start"], w["end"], w["gain"]), (1, 2, 40.0))

    def test_the_accounts_total_is_net_worths_financial_accounts(self):
        nw = reporting.compute_networth(self.conn, trend=False)
        t = reporting.account_totals(self.conn)
        self.assertEqual(t["financial_total"], nw["current_total"])
        self.assertEqual(t["financial_total"], 4750.0)
        self.assertEqual(t["label"], "Financial accounts")
        # the loan is listed as owed, not taken off the total
        self.assertEqual(t["loans_owed"], 8000.0)
        self.assertEqual(t["sections"]["personal"]["Loans"]["total"], 8000.0)
        self.assertEqual(t["sections"]["personal"]["Bank"]["by_item"],
                         {"it1": 5000.0})

    def _counted_sum(self, t):
        return round(sum(c["counted_total"] for pane in t["sections"].values()
                         for c in pane.values()), 2)

    def test_the_sections_add_up_to_the_headline(self):
        """Whatever the page lists — a loan, a business account, a linked
        backup, a hidden account, an account the budget leaves out — the
        sections' counted parts sum to the headline exactly."""
        from oikonome.engine import entities
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,user_removed_at) VALUES "
            "('gone','it1','Old Savings','depository','savings',700,now())")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('biz','it1','Biz Checking','depository','checking',1000),"
            "('sav','it1','Savings','depository','savings',300)")
        ent = entities.create_entity(self.conn, name="Fabrikam LLC",
                                     structure="sole_prop")
        entities.assign_account(self.conn, "biz", ent["id"])
        cfg = budget.load_config(self.conn)
        cfg["excluded_accounts"] = ["sav"]
        budget.save_config(self.conn, cfg)
        t = reporting.account_totals(self.conn)
        self.assertEqual(self._counted_sum(t), t["financial_total"])
        self.assertEqual(t["sections"]["personal"]["Loans"]["counted_total"],
                         0.0)

    def test_each_section_says_what_of_it_the_headline_counts(self):
        """The sections' printed totals do not add up to the headline — a
        card's is what is owed, a loan is listed and not counted — so a
        section whose counted part differs from its total says so, in the
        words both clients print."""
        t = reporting.account_totals(self.conn)
        sec = t["sections"]["personal"]
        cards = next(k for k in sec if k not in ("Bank", "Loans"))
        self.assertIsNone(sec["Bank"]["shown"]["counted_text"])
        self.assertEqual(sec[cards]["shown"]["counted_text"],
                         "−$250 in the total")
        self.assertEqual(sec["Loans"]["shown"]["counted_text"],
                         "not in the total")

    def test_the_owner_lens_has_no_month_change_rather_than_zero(self):
        nw = reporting.compute_networth(self.conn, owner="someone")
        self.assertIsNone(nw["month_change"])
        self.assertIsNone(nw["financial_month_change"])
        self.assertIsNone(nw["property_month_change"])
        self.assertIsNone(nw["trend_windows"]["1m"]["gain"])

    def test_the_estimate_boundary_rebases_onto_the_full_trend(self):
        """full_trend is the financial trend's tail (cut after the last
        month whose property value is a guess): the boundary moves by the
        months cut, and never below zero."""
        fin = [[f"2026-{m:02d}", 100.0 * m] for m in range(1, 11)]
        f = reporting._networth_figures(fin, fin[4:], 6, [], [], [])
        self.assertEqual(f["full_trend_estimated_until"], 2)
        f = reporting._networth_figures(fin, fin[8:], 6, [], [], [])
        self.assertEqual(f["full_trend_estimated_until"], 0)

    def test_a_combined_business_account_counts_exactly_when_combined(self):
        from oikonome.engine import entities
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('biz','it1','Biz Checking','depository','checking',1000)")
        ent = entities.create_entity(self.conn, name="Fabrikam LLC",
                                     structure="sole_prop")
        entities.assign_account(self.conn, "biz", ent["id"])
        t = reporting.account_totals(self.conn)
        self.assertEqual(t["financial_total"],
                         reporting.compute_networth(
                             self.conn, trend=False)["current_total"])
        self.assertEqual(t["sections"]["business"]["Bank"]["total"], 1000.0)
        self.assertFalse(t["business_counted"])
        self.assertEqual(t["financial_total"], 4750.0)


class CashCostsSendInterestVsEarned(unittest.TestCase):
    TODAY = dt.date(2026, 10, 9)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_txn(self.conn, dt.date(2026, 3, 5), 30.0, "INTEREST CHARGE",
                account="card", primary="BANK_FEES",
                detailed="BANK_FEES_INTEREST_CHARGE")
        add_txn(self.conn, dt.date(2026, 4, 1), -12.0, "INTEREST PAYMENT",
                account="chk", primary="INCOME",
                detailed="INCOME_INTEREST_EARNED")

    def tearDown(self):
        self.conn.close()

    def test_vs_earned_is_earned_less_the_years_net_cost(self):
        r = reporting.compute_cash_costs(self.conn, self.TODAY)
        net = r["ytd"]["net"] if r["ytd"] else 0.0
        self.assertEqual(r["vs_earned_ytd"], round(r["earned_ytd"] - net, 2))


class CountedNoteIsDecidedInCents(unittest.TestCase):
    """A fully counted Accounts section never reads as partly outside the
    total because its share of the headline rounded the other way."""

    def test_a_section_counted_whole_carries_no_note_whatever_the_rounding(self):
        self.assertIsNone(reporting._counted_text(2000.60, 2000.60, 2000))
        self.assertEqual(reporting._counted_text(500.0, 0.0, 0),
                         "not in the total")
        self.assertEqual(reporting._counted_text(250.0, -250.0, -250),
                         "−$250 in the total")


if __name__ == "__main__":
    unittest.main()
