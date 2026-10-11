"""The monthly email is the Month lens read in the year it sits in.

Everything it shows is computed once from the two lens payloads
(`month_summary`, `year_summary`) by `month_mail_context`; the HTML and the
plain text both render that dict. These tests pin the context against
synthetic payloads — the arithmetic a reader will check with a pencil —
and the rendered mail against a fixture household."""

import datetime as dt
import unittest

from oikonome.web import lens_email

from .util import TODAY, add_txn, make_db, write_config


def _cell(m, verdict=None, variance=None, income=None, spend=None,
          status="final"):
    net = (income - spend) if income is not None and spend is not None else None
    return {"m": m, "status": status, "verdict": verdict, "variance": variance,
            "variable_actual": None, "variable_budget": None,
            "income": income, "spend": spend, "net": net}


def _month(**over):
    d = {"y": 2026, "m": 9, "status": "final", "as_of": "2026-09-30",
         "days_in_month": 30, "verdict": "UNDER BUDGET", "variance": -153.0,
         "tolerance": 75.0, "variable_actual": 2247.0, "variable_budget": 2400.0,
         "buckets": {"fixed": {"actual": 4790.0, "expected": 4850.0,
                               "month_budget": 4850.0},
                     "food": {"actual": 1062.0, "expected": 1000.0,
                              "month_budget": 1000.0},
                     "other": {"actual": 1185.0, "expected": 1400.0,
                               "month_budget": 1400.0}},
         "bills": {"posted": 4790.0, "planned": 4850.0, "awaiting": 60.0,
                   "paid": [{"date": "2026-09-03", "amount": 2140.0,
                             "payee": "Mortgage"}] * 23,
                   "overdue": [],
                   "envelopes": [{"payee": "Car repairs", "used": 180.0,
                                  "monthly": 150.0, "overflow": 30.0,
                                  "show_today": True},
                                 {"payee": "Pharmacy", "used": 40.0,
                                  "monthly": 70.0, "overflow": 0.0,
                                  "show_today": False}]},
         "by_category": [["GROCERIES", 812.0, "GROCERIES"],
                         ["HOME IMPROVEMENT", 410.0, "HOME_IMPROVEMENT"],
                         ["AUTO AND GAS", 380.0, "AUTO"]],
         "amazon_subs": [], "amazon_total": 0.0,
         "biggest": [{"date": "2026-09-03", "amount": 2140.0,
                      "payee": "Mortgage"}],
         "income": {"actual": 9400.0, "budgeted": 9200.0},
         "spend_total": 8120.0, "variable_scale": None,
         "plan_surplus": 350.0, "saved_month": 800.0,
         "budget_source": "snapshot", "bills_source": "snapshot",
         "first_year": 2024}
    d.update(over)
    return d


def _year(month=9):
    cells = [_cell(1, "UNDER BUDGET", -120.0, 9200.0, 8560.0),
             _cell(2, "OVER BUDGET", 210.0, 9200.0, 9510.0),
             _cell(3, "UNDER BUDGET", -300.0, 9650.0, 8530.0),
             _cell(4, None, None, 9200.0, 9115.0, status="net"),
             _cell(5, "OVER BUDGET", 400.0, 9200.0, 9740.0),
             _cell(6, "UNDER BUDGET", -90.0, 9200.0, 8240.0),
             _cell(7, "ON BUDGET", 20.0, 9200.0, 8790.0),
             _cell(8, "ON BUDGET", 60.0, 9200.0, 9320.0),
             _cell(9, "UNDER BUDGET", -153.0, 9400.0, 8120.0),
             # October is the month the mail is sent in: projected on the
             # Year lens, "not yet" on a report about September
             _cell(10, "ON BUDGET", 5.0, 9200.0, 300.0, status="projected"),
             _cell(11, status="future"), _cell(12, status="future")]
    return {"y": 2026, "cells": cells, "totals": {}, "prev_totals": {},
            "categories": [], "saved_ytd": None, "buckets_annual": {},
            "plan_surplus": 350.0, "first_year": 2024}


class MonthMailContextTests(unittest.TestCase):

    def setUp(self):
        self.ctx = lens_email.month_mail_context(_month(), _year(), 2026, 9)

    def test_the_net_is_income_minus_spending_and_says_which_way(self):
        f = self.ctx["flow"]
        self.assertEqual(f["net_text"], "+$1,280")
        self.assertEqual(f["tone"], "pos")
        self.assertIn("made money", f["sentence"])
        self.assertIn("$1,280 more in than out", f["sentence"])

    def test_a_losing_month_says_so(self):
        ctx = lens_email.month_mail_context(
            _month(income={"actual": 7000.0, "budgeted": None},
                   spend_total=8120.0), _year(), 2026, 9)
        self.assertEqual(ctx["flow"]["net_text"], "−$1,120")
        self.assertEqual(ctx["flow"]["tone"], "neg")
        self.assertIn("lost money", ctx["flow"]["sentence"])

    def test_the_year_strip_stops_at_the_report_month(self):
        strip = self.ctx["strip"]
        self.assertEqual(len(strip), 12)
        self.assertEqual([c["abbr"] for c in strip][:3], ["Jan", "Feb", "Mar"])
        self.assertEqual(strip[8]["text"], "+$1,280")
        self.assertTrue(strip[8]["current"])
        # October has figures on the Year lens but is not yet on a report
        # about September; November and December are future either way
        self.assertEqual([c["text"] for c in strip[9:]], ["·", "·", "·"])
        self.assertFalse(any(c["current"] for c in strip[:8] + strip[9:]))

    def test_the_strip_is_coloured_by_the_sign_of_the_net_not_the_verdict(self):
        strip = self.ctx["strip"]
        green, red = "#5cb56b", "#e0695d"
        # August: over its budget on the Year lens, yet made money — green
        self.assertEqual(strip[7]["text"], "−$120")
        self.assertEqual(strip[7]["fg"], red)
        self.assertEqual(strip[1]["fg"], red)        # Feb lost money
        self.assertEqual(strip[0]["fg"], green)      # Jan made money
        # April had no budget but its net is known: coloured like any other
        self.assertEqual(strip[3]["fg"], green)
        self.assertEqual(strip[3]["text"], "+$85")

    def test_year_to_date_is_the_net_through_the_report_month(self):
        y = self.ctx["ytd"]
        self.assertEqual(y["net"], 3525.0)
        self.assertEqual(y["net_text"], "+$3,525")
        self.assertEqual(y["tone"], "pos")
        # eight judged months (April had no budget); six on or under
        self.assertEqual(y["months_text"],
                         "9 months so far · on or under budget 6 of 8")

    def test_a_year_with_unknown_income_shows_no_net(self):
        year = _year()
        for c in year["cells"]:
            c["income"] = None
            c["net"] = None
        ctx = lens_email.month_mail_context(
            _month(income={"actual": None, "budgeted": None}), year, 2026, 9)
        self.assertEqual(ctx["ytd"]["net_text"], "—")
        self.assertEqual(ctx["flow"]["net_text"], "—")
        self.assertIn("no net", ctx["flow"]["sentence"])
        # a month without income shows what went out, in grey
        self.assertEqual(ctx["strip"][0]["text"], "$8,560 out")
        self.assertEqual(ctx["strip"][0]["fg"], "#8b98a3")
        self.assertTrue(ctx["ytd"]["unknown_months"])

    def test_the_budget_is_the_fixed_bar_then_the_variable_split(self):
        rows = {r["label"]: r for r in self.ctx["rows"]}
        # the pinned envelope is a bar; the unpinned one stays inside Bills
        self.assertEqual(list(rows), ["Bills", "Food", "Everything else",
                                      "Car repairs"])
        car = rows["Car repairs"]
        # the fixture's used (180) already exceeds the cap; whole swipe = 210
        self.assertEqual((car["actual_text"], car["plan_text"]), ("$210", "$150"))
        self.assertEqual(car["bar"], "#e0695d")
        self.assertEqual(car["note"], "envelope · $30 spilled past the cap")
        # Bills without the pinned envelope's share: 4790−180 of 4850−150
        self.assertEqual((rows["Bills"]["actual_text"], rows["Bills"]["plan_text"]),
                         ("$4,610", "$4,700"))
        self.assertIn("23 paid", rows["Bills"]["note"])
        self.assertIn("$60 due, not posted", rows["Bills"]["note"])
        self.assertIn("$90 under the plan", rows["Bills"]["note"])
        self.assertIn("without Car repairs", rows["Bills"]["note"])
        self.assertEqual(rows["Bills"]["bar"], "#5cb56b")
        self.assertEqual(rows["Food"]["note"], "$62 over")
        self.assertEqual(rows["Food"]["bar"], "#e0695d", "red where it ran past")
        self.assertEqual(rows["Everything else"]["note"], "$215 under")
        self.assertEqual(rows["Everything else"]["bar"], "#5cb56b")
        self.assertEqual(rows["Everything else"]["frac"], 84.6)

    def test_a_carve_out_is_its_own_bar_and_the_parent_keeps_the_rest(self):
        d = _month()
        d["buckets"]["other"].update(
            children=[{"name": "Pottery class", "actual": 420.0,
                       "expected": 400.0, "month_budget": 400.0}],
            display_actual=765.0, display_expected=1000.0,
            remaining_budget=1000.0)
        ctx = lens_email.month_mail_context(d, _year(), 2026, 9)
        rows = [(r["label"], r["actual_text"], r["plan_text"], r["bar"])
                for r in ctx["rows"]]
        self.assertEqual(rows[:4], [("Bills", "$4,610", "$4,700", "#5cb56b"),
                                    ("Food", "$1,062", "$1,000", "#e0695d"),
                                    ("Everything else", "$765", "$1,000", "#5cb56b"),
                                    ("Pottery class", "$420", "$400", "#e0695d")])
        self.assertFalse(any(r["child"] for r in ctx["rows"]))

    def test_an_envelope_cap_is_its_own_bar_largest_cap_first(self):
        d = _month()
        d["bills"]["envelopes"] = [
            # used stops at the cap; the spill is carried separately, so the
            # bar's actual is the whole swipe and the bar runs red
            {"payee": "Pottery class", "used": 400.0, "monthly": 400.0,
             "overflow": 160.0, "pool": 400.0, "period_months": 1,
             "period_used": 400.0, "period_left": 0.0, "show_today": True},
            {"payee": "Dog walker", "used": 1200.0, "monthly": 1500.0,
             "overflow": 0.0, "pool": 1500.0, "period_months": 1,
             "period_used": 1200.0, "period_left": 300.0,
             "show_today": True},
            # a $25/mo envelope paid once a year: measured against its
            # 12-month pool, not against one month's share
            {"payee": "Furnace service", "used": 228.0, "monthly": 25.0,
             "overflow": 0.0, "pool": 300.0, "period_months": 12,
             "period_used": 228.0, "period_left": 72.0, "show_today": True},
            # not pinned to Today: stays inside the Bills bar
            {"payee": "Pharmacy", "used": 40.0, "monthly": 70.0,
             "overflow": 0.0, "pool": 70.0, "period_months": 1,
             "period_used": 40.0, "period_left": 30.0, "show_today": False}]
        ctx = lens_email.month_mail_context(d, _year(), 2026, 9)
        env = [(r["label"], r["actual_text"], r["plan_text"], r["bar"], r["note"])
               for r in ctx["rows"][3:]]
        self.assertEqual(env, [
            ("Dog walker", "$1,200", "$1,500", "#5cb56b",
             "envelope · $300 of the cap left"),
            ("Pottery class", "$560", "$400", "#e0695d",
             "envelope · $160 spilled past the cap"),
            ("Furnace service", "$228", "$300", "#5cb56b",
             "12-month envelope · $72 of the cap left")])

    def test_bills_over_their_plan_are_a_red_bar(self):
        d = _month()
        d["bills"].update(posted=5100.0, planned=4850.0, awaiting=0.0)
        ctx = lens_email.month_mail_context(d, _year(), 2026, 9)
        bills = ctx["rows"][0]
        self.assertEqual(bills["bar"], "#e0695d")
        self.assertIn("$220 over the plan", bills["note"])  # 5100−180 vs 4850−150

    def test_categories_are_the_everyday_spend_with_the_bills_by_kind(self):
        # the fixture's paid rows carry no category, so the bills stay one
        # slice — except the envelopes, which are slices of their own
        d = _month(by_category_variable=[["GROCERIES", 812.0, "G"],
                                         ["AUTO AND GAS", 380.0, "A"],
                                         ["FEES", 8.0, "F"]],
                   by_category=[["GROCERIES", 812.0, "G"],
                                ["MORTGAGE", 2140.0, "M"],
                                ["AUTO AND GAS", 380.0, "A"],
                                ["FEES", 8.0, "F"]])
        ctx = lens_email.month_mail_context(d, _year(), 2026, 9)
        self.assertEqual([(c["label"], c["pct"], c["amount_text"])
                          for c in ctx["cats"]],
                         [("Bills", 76, "$4,570"), ("GROCERIES", 14, "$812"),
                          ("AUTO AND GAS", 6, "$380"), ("Car repairs", 3, "$180"),
                          ("Other", 1, "$48")])
        self.assertNotIn("MORTGAGE", [c["label"] for c in ctx["cats"]])
        self.assertEqual(ctx["cat_total_text"], "$5,990")
        self.assertAlmostEqual(sum(sh for _, _, sh in ctx["cat_slices"]), 1.0)
        self.assertIsNone(ctx["cat_pie"], "the pie is drawn by the mail build")
        self.assertIn("the bills under their categories", ctx["pie_sub"])

    def test_bills_split_into_their_categories_and_join_the_everyday_ones(self):
        # the mortgage, the power and the water are one housing slice,
        # the dentist joins the pharmacy run under Medical, and the bill
        # the household tagged Subscriptions shows as that; a charge with
        # no category is still counted, as plain Bills
        d = _month(by_category_variable=[["GROCERIES", 812.0, "G"],
                                         ["MEDICAL", 100.0, "H"]])
        d["bills"]["posted"] = 3220.0
        d["bills"]["paid"] = [
            {"date": "2026-09-01", "amount": 2140.0, "payee": "Mortgage",
             "category": "LOAN PAYMENTS",
             "detailed": "LOAN_PAYMENTS_MORTGAGE_PAYMENT"},
            {"date": "2026-09-02", "amount": 220.0, "payee": "Car loan",
             "category": "LOAN PAYMENTS",
             "detailed": "LOAN_PAYMENTS_CAR_PAYMENT"},
            {"date": "2026-09-05", "amount": 300.0, "payee": "Power",
             "category": "RENT AND UTILITIES"},
            {"date": "2026-09-06", "amount": 90.0, "payee": "Water",
             "category": "RENT AND UTILITIES"},
            {"date": "2026-09-08", "amount": 200.0, "payee": "Dentist",
             "category": "MEDICAL"},
            {"date": "2026-09-09", "amount": 30.0, "payee": "Streaming",
             "category": "Subscriptions"},
            {"date": "2026-09-10", "amount": 20.0, "payee": "Mystery",
             "category": None}]
        d["bills"]["envelopes"] = [{"payee": "Car repairs", "used": 180.0,
                                    "monthly": 150.0, "overflow": 30.0,
                                    "show_today": True}]
        ctx = lens_email.month_mail_context(d, _year(), 2026, 9)
        got = {c["label"]: c["amount"] for c in ctx["cats"]}
        self.assertEqual(got["Mortgage, rent and utilities"], 2140.0 + 390.0)
        self.assertEqual(got["LOAN PAYMENTS"], 220.0)   # the car loan stays
        self.assertEqual(got["GROCERIES"], 812.0)
        self.assertEqual(got["MEDICAL"], 300.0)       # 200 dentist + 100 pharmacy
        self.assertEqual(got["Car repairs"], 180.0)
        self.assertNotIn("RENT AND UTILITIES", got)
        # Subscriptions $30 and the $60 of posted bills no label accounts
        # for (the $20 uncategorised charge and the rest) are under the
        # 3% floor: folded into Other
        self.assertEqual(got["Other"], 30.0 + 60.0)
        self.assertNotIn("Bills", got)
        self.assertEqual(sum(got.values()), 3220.0 + 812.0 + 100.0)
        self.assertEqual(ctx["cat_total_text"], "$4,132")

    def test_everyday_rent_joins_the_housing_slice(self):
        # a household that pays rent by hand, not as a bill: the ring
        # still reads it as housing
        d = _month(by_category_variable=[["RENT AND UTILITIES", 1500.0, "R"],
                                         ["GROCERIES", 812.0, "G"]])
        ctx = lens_email.month_mail_context(d, _year(), 2026, 9)
        got = {c["label"]: c["amount"] for c in ctx["cats"]}
        self.assertEqual(got["Mortgage, rent and utilities"], 1500.0)
        self.assertNotIn("RENT AND UTILITIES", got)

    def test_the_pie_is_a_png_with_a_slice_per_category(self):
        from oikonome.web import emailchart
        png = emailchart.pie([("Bills", 4790.0, .8), ("GROCERIES", 812.0, .14),
                              ("AUTO AND GAS", 380.0, .06)])
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 2000)
        self.assertEqual(emailchart.pie([]), b"")

    def test_the_variable_line_is_the_month_lens_verdict_sentence(self):
        self.assertIn("$153 under", self.ctx["variable_line"])
        ctx = lens_email.month_mail_context(_month(variance=40.0), _year(),
                                            2026, 9)
        self.assertIn("within tolerance ($75)", ctx["variable_line"])

    def test_plain_text_carries_the_same_figures_and_no_totals(self):
        plain = lens_email.month_mail_plain(self.ctx)
        for s in ("Net for September: +$1,280", "Year to date net: +$3,525",
                  "Bills: $4,610 of $4,700",
                  "Food: $1,062 of $1,000 — $62 over", "Sep +$1,280",
                  "GROCERIES"):
            self.assertIn(s, plain)
        self.assertNotIn("<", plain)
        self.assertNotIn("$9,400", plain, "the month's income total is not shown")
        self.assertNotIn("$83,450", plain, "the year's income total is not shown")


class MonthlyMailRendersTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=800, other_monthly=1000)
        last = TODAY.replace(day=1) - dt.timedelta(days=1)
        add_txn(self.conn, last.replace(day=5), 500.0, "HARBOR GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, last.replace(day=9), 120.0, "LANTERN GARAGE",
                primary="TRANSPORTATION")

    def tearDown(self):
        self.conn.close()

    def test_an_envelope_spill_is_listed_as_the_spill_not_the_whole_swipe(self):
        """A purchase an envelope partly absorbed is two rows to the engine:
        the part inside the cap is the bill's, the spill is everyday spend.
        The largest-variable list and the everyday categories must count
        the spill only, or the swipe is charged twice — once as a bill,
        once whole as variable."""
        from .util import add_bill
        last = TODAY.replace(day=1) - dt.timedelta(days=1)
        add_bill(self.conn, "Birchwood Physio", 400.0, bill_type="envelope",
                 frequency=None, interval=1)
        add_txn(self.conn, last.replace(day=9), 150.0, "BIRCHWOOD PHYSIO",
                primary="MEDICAL")
        add_txn(self.conn, last.replace(day=17), 260.0, "BIRCHWOOD PHYSIO",
                primary="MEDICAL")
        from oikonome.web import lenses
        d = lenses.month_summary(self.conn, last.year, last.month, today=TODAY)
        spill = [t for t in d["biggest_variable"] if "Birchwood Physio" in t["payee"]]
        self.assertEqual(len(spill), 1, d["biggest_variable"])
        self.assertAlmostEqual(spill[0]["amount"], 10.0)
        self.assertIn("over envelope", spill[0]["payee"])
        self.assertNotIn(260.0, [t["amount"] for t in d["biggest_variable"]])
        cats = dict((c[0], c[1]) for c in d["by_category_variable"])
        self.assertAlmostEqual(cats.get("envelope overflow", 0), 10.0)
        self.assertNotIn("MEDICAL", cats)

    def test_a_matched_costco_receipt_lists_its_lines(self):
        last = TODAY.replace(day=1) - dt.timedelta(days=1)
        add_txn(self.conn, last.replace(day=12), 88.0, "COSTCO WHSE",
                primary="GENERAL_MERCHANDISE")
        tid = self.conn.execute("SELECT id FROM transactions WHERE name=%s",
                                ("COSTCO WHSE",)).fetchone()["id"]
        self.conn.execute(
            "INSERT INTO costco_receipts (dedup_key, account, date, amount, "
            "receipt_type, summary, items_json) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            ("ck-1", "x", last.replace(day=12), 88.0, "warehouse",
             "groceries $88",
             '[{"description": "BULK OATS", "amount": 12.50}, '
             '{"description": "BANANAS", "amount": 2.49}]'))
        self.conn.execute(
            "INSERT INTO costco_matches (transaction_id, dedup_key) "
            "VALUES (%s, %s)", (tid, "ck-1"))
        from oikonome.web import lenses
        d = lenses.month_summary(self.conn, last.year, last.month, today=TODAY)
        row = next(t for t in d["biggest_variable"] if t["payee"] == "COSTCO WHSE")
        self.assertEqual(row["items"], ["Bulk Oats", "Bananas"])

    def test_the_mail_has_every_section(self):
        subject, plain, html = lens_email.build_monthly(self.conn, TODAY)
        self.assertRegex(subject, r"report card: (ON|OVER|UNDER) budget \(")
        last = TODAY.replace(day=1) - dt.timedelta(days=1)
        for s in ("Year to date net", f"Net for {last:%B}",
                  "Monthly budget", ">Bills<", ">Food<", ">Everything else<",
                  "Spending by category", 'src="data:image/png'):
            self.assertIn(s, html, s)
        self.assertNotIn("Largest variable", html)
        # the daily's shell: brand header and the dark card style baked in
        self.assertIn("Oikonome</span>", html)
        self.assertIn("background:#1a212b", html)
        self.assertNotIn("var(--", html, "mail clients get no CSS variables")
        self.assertNotIn("<script", html)
        self.assertIn("Monthly budget:", plain)
        # one pane: the two nets sit above the month strip inside one card
        i, j = html.index("Year to date net"), html.index(f"{last:%b}<br")
        self.assertLess(i, j)
        self.assertNotIn('class="card"', html[i:j])
        self.assertEqual(html.count("Year to date net"), 1)

    def test_the_strip_marks_the_report_month_and_nothing_after_it(self):
        _, _, html = lens_email.build_monthly(self.conn, TODAY)
        last = TODAY.replace(day=1) - dt.timedelta(days=1)
        abbr = f"{last:%b}"
        i = html.index("outline:2px solid #dbe3ea;")
        self.assertIn(abbr, html[i:i + 200])
        # the month the mail is sent in shows no figure
        j = html.index(f"{TODAY:%b}<br")
        self.assertIn("·</span>", html[j:j + 120])


if __name__ == "__main__":
    unittest.main()
