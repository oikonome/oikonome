"""The Month lens's budget rows add up to the hero's budget.

A ledger that begins inside the month may spend only the budget's share
from its first day to month end (budget.pace_window,
specs/short-ledger-pacing.md). The Month lens hero judges that share; a
Food or Everything-else row, or a carve-out beneath them, that printed
the whole month's budget would show money the hero says the month never
had — on the page, in the monthly report card built from the same
payload, and in the Year lens's annual bars that sum the months.
"""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import lens_email, lenses

from .util import add_txn, make_db, write_config

FIRST = dt.date(2026, 10, 10)           # the ledger's first day
TODAY = dt.date(2026, 10, 16)           # in progress: day 16 of 31
LATER = dt.date(2026, 11, 3)            # October closed
SHARE = (31 - FIRST.day + 1) / 31       # the days the ledger covers
CARVE = 100.0                           # the carve-out's monthly budget
PETS = [{"name": "Pets", "parent": "other", "monthly": CARVE,
         "merchants": ["Fabrikam Pets"]}]


def _household():
    conn = make_db()
    write_config(conn, food_monthly=600, other_monthly=900,
                 custom_buckets=PETS)
    d = FIRST
    while d < TODAY:
        add_txn(conn, d, 25.0, "CORNER GROCER", primary="FOOD_AND_DRINK")
        add_txn(conn, d, 20.0, "HARDWARE BARN")
        d += dt.timedelta(days=1)
    add_txn(conn, FIRST, 30.0, "Fabrikam Pets", merchant="Fabrikam Pets")
    return conn


def _variable_plan(buckets: dict) -> float:
    """What the rows say the month may spend: each bucket's own budget,
    or, with carve-outs, its remainder plus every carve-out's."""
    total = 0.0
    for k in ("food", "other"):
        b = buckets[k]
        if b.get("children"):
            total += b["remaining_budget"] + sum(
                c["month_budget"] for c in b["children"])
        else:
            total += b["month_budget"]
    return total


class MonthLensPacedRowsTests(unittest.TestCase):

    def setUp(self):
        self.conn = _household()

    def tearDown(self):
        self.conn.close()

    def test_an_in_progress_first_months_rows_sum_to_the_hero(self):
        d = lenses.month_summary(self.conn, 2026, 10, today=TODAY)
        self.assertTrue(d["budget_set"])
        self.assertAlmostEqual(d["variable_budget"], 1500 * SHARE, delta=0.02)
        self.assertAlmostEqual(_variable_plan(d["buckets"]),
                               d["variable_budget"], delta=0.05)
        pets = d["buckets"]["other"]["children"][0]
        self.assertAlmostEqual(pets["month_budget"], CARVE * SHARE,
                               delta=0.01)

    def test_a_closed_first_months_report_card_bars_sum_to_its_verdict(self):
        budget.snapshot_month(self.conn, TODAY)
        d = lenses.month_summary(self.conn, 2026, 10, today=LATER)
        self.assertEqual(d["status"], "final")
        self.assertEqual(d["budget_source"], "snapshot")
        self.assertAlmostEqual(_variable_plan(d["buckets"]),
                               d["variable_budget"], delta=0.05)
        year = lenses.year_summary(self.conn, 2026, today=LATER)
        ctx = lens_email.month_mail_context(d, year, 2026, 10)
        bars = sum(r["plan"] for r in ctx["rows"]
                   if r["label"] in ("Food", "Everything else", "Pets"))
        self.assertAlmostEqual(bars, d["variable_budget"], delta=0.05)

    def test_the_years_annual_bars_sum_the_months_paced_budgets(self):
        budget.snapshot_month(self.conn, TODAY)
        year = lenses.year_summary(self.conn, 2026, today=LATER,
                                   with_detail=True)
        judged = sum(c["variable_budget"] for c in year["cells"]
                     if c["status"] in ("final", "projected"))
        annual = year["buckets_annual"]
        self.assertAlmostEqual(annual["food"]["month_budget"]
                               + annual["other"]["month_budget"],
                               judged, delta=0.05)
        # the first month at its share, the month in progress whole
        self.assertAlmostEqual(
            year["detail"]["children"]["Pets"]["month_budget"],
            CARVE * SHARE + CARVE, delta=0.02)


if __name__ == "__main__":
    unittest.main()
