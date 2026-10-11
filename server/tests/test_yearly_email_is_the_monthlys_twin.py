"""The year in review is the monthly report card at year scale.

One template, one anatomy: the year's net beside last year's, the twelve
months coloured by their sign, the verdict count; the budget as annual
bars over the budgeted months; the year's spending by category as a ring
with the bills under their categories; the twenty largest variable
transactions. These
tests pin `year_mail_context` against a synthetic Year lens payload and
the rendered mail against a fixture household."""

import unittest

from oikonome.web import lens_email

from .util import TODAY, add_txn, make_db, write_config


def _cell(m, verdict=None, variance=None, income=None, spend=None,
          status="final"):
    net = (income - spend) if income is not None and spend is not None else None
    return {"m": m, "status": status, "verdict": verdict, "variance": variance,
            "variable_actual": None, "variable_budget": None,
            "income": income, "spend": spend, "net": net}


def _year():
    nets = [640, -310, 1120, 85, -540, 960, 410, -120, 1280, -415, 300, 450]
    verdicts = ["UNDER BUDGET", "OVER BUDGET", "UNDER BUDGET", None,
                "OVER BUDGET", "UNDER BUDGET", "ON BUDGET", "OVER BUDGET",
                "UNDER BUDGET", "OVER BUDGET", "UNDER BUDGET", "OVER BUDGET"]
    cells = [_cell(i + 1, v, (-150 if v and v.startswith("UNDER") else 120),
                   9400.0, 9400.0 - n, status="net" if v is None else "final")
             for i, (n, v) in enumerate(zip(nets, verdicts))]
    return {"y": 2026, "cells": cells,
            "totals": {"income": 112800.0, "spend": 108940.0,
                       "saved": 3860.0, "rate": 3.4},
            "prev_totals": {"income": 100000.0, "spend": 101240.0,
                            "saved": -1240.0, "rate": -1.2},
            "categories": [], "saved_ytd": None,
            "buckets_annual": {"fixed": {"actual": 75000.0, "expected": 77000.0,
                                         "month_budget": 77000.0},
                               "food": {"actual": 12744.0, "expected": 12000.0,
                                        "month_budget": 12000.0},
                               "other": {"actual": 14736.0, "expected": 16800.0,
                                         "month_budget": 16800.0}},
            "plan_surplus": 350.0, "first_year": 2024,
            "detail": {"categories_variable": [["GROCERIES", 9800.0, "G"],
                                               ["AUTO AND GAS", 4500.0, "A"],
                                               ["FEES", 90.0, "F"]],
                       "fixed_total": 75000.0, "bills_paid": 253,
                       "children": {}, "months_walked": 12,
                       "budgeted_months": 11,
                       "envelopes": {"Dog walker": {"used": 14400.0,
                                                    "monthly": 18000.0,
                                                    "overflow": 0.0,
                                                    "show_today": True},
                                     "Pharmacy": {"used": 400.0,
                                                  "monthly": 840.0,
                                                  "overflow": 0.0,
                                                  "show_today": False}},
                       "years": [{"y": 2022, "income": None, "spend": 0.0,
                                  "net": None},
                                 {"y": 2023, "income": None, "spend": 61000.0,
                                  "net": None},
                                 {"y": 2024, "income": 98000.0, "spend": 100100.0,
                                  "net": -2100.0},
                                 {"y": 2025, "income": 100000.0, "spend": 101240.0,
                                  "net": -1240.0},
                                 {"y": 2026, "income": 112800.0, "spend": 108940.0,
                                  "net": 3860.0}]}}


class YearMailContextTests(unittest.TestCase):

    def setUp(self):
        self.ctx = lens_email.year_mail_context(_year(), 2026)

    def test_the_two_tiles_are_this_years_net_and_last_years(self):
        tiles = self.ctx["tiles"]
        self.assertEqual(tiles[0]["eyebrow"], "Net for 2026")
        self.assertEqual(tiles[0]["value"], "+$3,860")
        self.assertIn("3.4% of income kept", tiles[0]["sub"])
        self.assertEqual(tiles[1]["eyebrow"], "Net for 2025")
        self.assertEqual(tiles[1]["value"], "−$1,240")
        self.assertEqual(tiles[1]["tone"], "neg")
        self.assertEqual(self.ctx["headline"], "2026: you made money")
        self.assertEqual(self.ctx["status_text"], "final")

    def test_every_month_is_in_the_strip_coloured_by_its_net(self):
        strip = self.ctx["strip"]
        self.assertEqual(len(strip), 12)
        self.assertEqual([c["text"] for c in strip][:3],
                         ["+$640", "−$310", "+$1,120"])
        self.assertEqual(strip[1]["fg"], "#e0695d")
        self.assertEqual(strip[11]["text"], "+$450")
        self.assertFalse(any(c["current"] for c in strip))

    def test_the_variable_line_counts_the_budgeted_months(self):
        line = self.ctx["variable_line"]
        self.assertIn("$27,480", line)
        self.assertIn("$28,800", line)
        self.assertIn("$1,320 under", line)
        # five under, one on, five over, one month with no budget
        self.assertIn("on or under budget <b>6 of 11</b>", line)
        self.assertIn("Feb, May, Aug, Oct, Dec over", line)
        self.assertTrue(any("no budget on record" in n for n in self.ctx["notes"]))

    def test_the_bars_are_annual_totals_with_the_pinned_envelope_apart(self):
        rows = {r["label"]: r for r in self.ctx["rows"]}
        self.assertEqual(list(rows), ["Bills", "Food", "Everything else",
                                      "Dog walker"])
        # 75000 − 14400 of 77000 − 18000
        self.assertEqual((rows["Bills"]["actual_text"], rows["Bills"]["plan_text"]),
                         ("$60,600", "$59,000"))
        self.assertIn("253 paid", rows["Bills"]["note"])
        self.assertIn("without Dog walker", rows["Bills"]["note"])
        self.assertEqual(rows["Bills"]["bar"], "#e0695d")
        self.assertEqual(rows["Food"]["note"], "$744 over")
        self.assertEqual(rows["Everything else"]["note"], "$2,064 under")
        self.assertEqual((rows["Dog walker"]["actual_text"],
                          rows["Dog walker"]["plan_text"]),
                         ("$14,400", "$18,000"))
        self.assertIn("$3,600 of the cap left", rows["Dog walker"]["note"])

    def test_the_pie_is_the_years_everyday_spend_with_the_bills_by_kind(self):
        # no bill categories in the fixture: the bills stay one slice,
        # the envelopes slices of their own
        self.assertEqual([(c["label"], c["pct"]) for c in self.ctx["cats"]],
                         [("Bills", 67), ("Dog walker", 16), ("GROCERIES", 11),
                          ("AUTO AND GAS", 5), ("Other", 1)])
        self.assertEqual(self.ctx["cat_total_text"], "$89,390")
        self.assertIn("over 12 months", self.ctx["pie_sub"])
        self.assertIn("the bills under their categories", self.ctx["pie_sub"])
        self.assertEqual(self.ctx["pie_title"], "Spending by category — 2026")

    def test_the_years_bills_split_by_their_categories(self):
        d = _year()
        # the year detail has already read the mortgage as housing
        # (lenses.ring_label); a car loan keeps its own label
        d["detail"]["categories_bills"] = [["Mortgage, rent and utilities", 50000.0],
                                           ["RENT AND UTILITIES", 6000.0],
                                           ["LOAN PAYMENTS", 2400.0],
                                           ["MEDICAL", 2000.0]]
        d["detail"]["categories_variable"].append(["MEDICAL", 1000.0, "H"])
        ctx = lens_email.year_mail_context(d, 2026)
        got = {c["label"]: c["amount"] for c in ctx["cats"]}
        self.assertEqual(got["Mortgage, rent and utilities"], 56000.0)
        self.assertEqual(got["LOAN PAYMENTS"], 2400.0)
        self.assertEqual(got["Dog walker"], 14400.0)
        self.assertEqual(got["MEDICAL"], 3000.0)
        # 75000 fixed − 60400 labelled − 14800 envelopes = −200: nothing
        # unlabelled, no Bills slice; the pharmacy envelope and the fees
        # are under the 2% floor
        self.assertNotIn("Bills", got)
        self.assertAlmostEqual(got["Other"], 400.0 + 90.0, places=2)
        self.assertEqual(ctx["cat_total_text"], "$90,590")

    def test_the_years_ring_holds_twelve_slices_down_to_two_percent(self):
        # a month's ring stops at eight slices and 3%; a year's has room
        # for twelve, down to 2%, so Other is a tail, not a fifth
        d = _year()
        d["detail"]["categories_variable"] = [[f"CAT {i}", 2500.0, str(i)]
                                              for i in range(14)]
        d["detail"]["fixed_total"] = 14800.0   # the envelopes only
        ctx = lens_email.year_mail_context(d, 2026)
        labels = [c["label"] for c in ctx["cats"]]
        # Dog walker (14400) then eleven of the fourteen 2,500 slices (each 5.0%)
        self.assertEqual(labels[0], "Dog walker")
        self.assertEqual(len(labels), 13)
        self.assertEqual(labels[-1], "Other")
        self.assertEqual([c["amount"] for c in ctx["cats"]][-1], 3 * 2500.0 + 400.0)


    def test_a_year_with_no_budget_on_record_has_no_bars(self):
        y = _year()
        y["detail"]["budgeted_months"] = 0
        for c in y["cells"]:
            c["verdict"] = None
        ctx = lens_email.year_mail_context(y, 2026)
        self.assertEqual(ctx["rows"], [])
        self.assertIn("No budget was on record for 2026", ctx["variable_line"])
        self.assertEqual(ctx["notes"], [])
        self.assertTrue(ctx["cats"], "the categories still tell the story")

    def test_every_year_the_ledger_holds_is_in_the_year_strip(self):
        strip = self.ctx["year_strip"]
        self.assertEqual([c["abbr"] for c in strip],
                         ["2022", "2023", "2024", "2025", "2026"])
        # a gap year reads as a gap, a year without bank coverage as what
        # went out, the rest by their net
        self.assertEqual([c["text"] for c in strip],
                         ["·", "$61,000 out", "−$2,100", "−$1,240", "+$3,860"])
        self.assertEqual(strip[1]["fg"], "#8b98a3", "no bank coverage: grey")
        self.assertEqual(strip[2]["fg"], "#e0695d")
        self.assertEqual(strip[4]["fg"], "#5cb56b")
        self.assertTrue(strip[4]["current"])
        self.assertFalse(any(c["current"] for c in strip[:4]))

    def test_a_losing_year_says_so(self):
        y = _year()
        for c in y["cells"]:
            c["net"] = -1000.0
        ctx = lens_email.year_mail_context(y, 2026)
        self.assertEqual(ctx["headline"], "2026: you lost money")
        self.assertEqual(ctx["tiles"][0]["value"], "−$12,000")

    def test_plain_text_mirrors_the_html_context(self):
        plain = lens_email._plain(self.ctx)
        for s in ("2026: you made money", "Net for 2026: +$3,860",
                  "Net for 2025: −$1,240", "Jan +$640", "Dec +$450",
                  "Yearly budget:", "Dog walker: $14,400 of $18,000",
                  "Spending by category — 2026 ($89,390)",
                  "Net by year: 2022 · · 2023 $61,000 out · 2024 −$2,100 "
                  "· 2025 −$1,240 · 2026 +$3,860"):
            self.assertIn(s, plain)


class YearlyMailRendersTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=800, other_monthly=1000)
        # a charge in the previous calendar year, the year the mail covers
        add_txn(self.conn, TODAY.replace(year=TODAY.year - 1, month=3, day=5),
                500.0, "HARBOR GROCER", primary="FOOD_AND_DRINK")

    def tearDown(self):
        self.conn.close()

    def test_the_mail_renders_the_shared_anatomy(self):
        subject, plain, html = lens_email.build_yearly(self.conn, TODAY)
        year = TODAY.year - 1
        self.assertTrue(subject.startswith(f"{year} in review"))
        for s in (f"Net for {year}", f"Net for {year - 1}",
                  f"Spending by category — {year}", "Net by year",
                  f"{year}<br", 'src="data:image/png'):
            self.assertIn(s, html, s)
        self.assertNotIn("Largest variable", html)
        self.assertIn("Oikonome</span>", html)
        self.assertNotIn("var(--", html)
        # the fixture household had no budget that year: no bars, said so
        self.assertNotIn("Yearly budget", html)
        self.assertNotIn("Yearly budget", plain)
        self.assertIn(f"No budget was on record for {year}", plain)


if __name__ == "__main__":
    unittest.main()
