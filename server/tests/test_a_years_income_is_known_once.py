"""A partly measured year reads the same on every surface: by the month.

Cash Flow's Left over by year, the Year lens totals, the yearly email's
headline and its year strip all judge income month by month. A year whose
bank feed covers only part of it counts income, saved and the rate over
the months that are measured and says "income incomplete"; only a year
with no measured month is unknown. Judged whole, the lens and the email
called such a year unknown while Cash Flow's month bars and totals
counted it."""

import datetime as dt
import pathlib
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import coverage, reporting
from oikonome.web import lens_email, lenses

from .util import (_admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts,
                   write_config)

TODAY = dt.date(2026, 10, 9)
NOTE = "income incomplete — left over counts 8 of 12 months"


def _months(first: dt.date, last: dt.date):
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        yield dt.date(y, m, 10)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class AYearFedFromApril(unittest.TestCase):
    """The card spends $3,000 every month from Jan 2023; checking (pay of
    $5,000 a month) is on file from Apr 2024. 2023 is not measured at
    all, 2024 in eight months (May on), 2025 wholly."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            self.tid = str(tenancy.create_tenant(
                admin, f"yk-{uuid.uuid4().hex[:8]}"))
        finally:
            admin.close()
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            for d in _months(dt.date(2023, 1, 1), dt.date(2026, 9, 1)):
                add_txn(conn, d, 3000.0, "GROCER", account="card",
                        primary="FOOD_AND_DRINK")
                if d >= dt.date(2024, 4, 1):
                    add_txn(conn, d, -5000.0, "NORTHWIND PAYROLL",
                            account="chk", primary="INCOME",
                            detailed="INCOME_WAGES")
            conn.commit()
        finally:
            conn.close()
        self.conn = tenancy.tenant_connect(self.tid)
        self.addCleanup(self.conn.close)

    def test_cash_flows_year_list_flags_the_partly_fed_year(self):
        cf = reporting.compute_cashflow(self.conn, TODAY)
        rows = {r[0]: r for r in cf["savings_by_year"]}
        self.assertEqual(rows["2024"],
                         ["2024", 45000.0, 36000.0, 16000.0, 40.0])
        self.assertEqual(cf["savings_by_year_notes"]["2024"], NOTE)
        self.assertEqual(rows["2025"],
                         ["2025", 60000.0, 36000.0, 24000.0, 40.0])

    def test_the_year_lens_totals_count_the_measured_months(self):
        t = lenses.year_summary(self.conn, 2024, TODAY)["totals"]
        self.assertEqual(t["spend"], 36000.0)
        # May-December: the same saved and rate as Cash Flow's row
        self.assertEqual(t["income"], 40000.0)
        self.assertEqual(t["saved"], 16000.0)
        self.assertEqual(t["rate"], 40.0)
        self.assertEqual((t["saved_months_known"], t["saved_months_total"]),
                         (8, 12))
        self.assertEqual(t["incomplete_text"], NOTE)
        self.assertIsNone(t["unknown_text"])
        whole = lenses.year_summary(self.conn, 2025, TODAY)["totals"]
        self.assertEqual(whole["saved"], 24000.0)
        self.assertIsNone(whole["incomplete_text"])
        self.assertIsNone(whole["unknown_text"])
        # last year's comparison column follows the same rule
        prev = lenses.year_summary(self.conn, 2025, TODAY)["prev_totals"]
        self.assertEqual(prev["saved"], 16000.0)
        self.assertEqual(prev["incomplete_text"], NOTE)

    def test_a_year_with_no_measured_month_is_still_unknown(self):
        t = lenses.year_summary(self.conn, 2023, TODAY)["totals"]
        self.assertIsNone(t["income"])
        self.assertIsNone(t["saved"])
        self.assertIsNone(t["rate"])
        self.assertIsNone(t["incomplete_text"])
        self.assertEqual(t["unknown_text"],
                         "No month of 2023 had bank data — income "
                         "and what was left over are unknown, not $0.")

    def test_the_yearly_email_says_the_same(self):
        d = lenses.year_summary(self.conn, 2024, TODAY, with_detail=True)
        ctx = lens_email.year_mail_context(d, 2024)
        self.assertEqual(ctx["headline"], "2024: you made money")
        self.assertEqual(ctx["net"], 16000.0)
        self.assertIn(NOTE, ctx["tiles"][0]["sub"])
        self.assertIn("grey = income incomplete", ctx["strip_legend"])
        # the year strip, read from the year after: every year by the month
        d25 = lenses.year_summary(self.conn, 2025, TODAY, with_detail=True)
        ctx25 = lens_email.year_mail_context(d25, 2025)
        years = {y["y"]: y for y in d25["detail"]["years"]}
        self.assertEqual((years[2024]["net"], years[2024]["incomplete"]),
                         (16000.0, True))
        self.assertEqual((years[2025]["net"], years[2025]["incomplete"]),
                         (24000.0, False))
        self.assertIsNone(years[2023]["net"])
        strip = {s["abbr"]: s for s in ctx25["year_strip"]}
        self.assertEqual(strip["2024"]["net"], 16000.0)
        self.assertEqual(strip["2024"]["bg"], lens_email._TONES["net"][0])
        self.assertNotEqual(strip["2025"]["bg"], lens_email._TONES["net"][0])
        self.assertIsNone(strip["2023"]["net"])
        self.assertEqual(ctx25["year_strip_legend"],
                         "grey = income incomplete: the net counts the "
                         "months with bank data")
        # last year's tile is the same figure the strip shows for it
        self.assertIn("16,000", ctx25["tiles"][1]["value"])
        none = lens_email.year_mail_context(
            lenses.year_summary(self.conn, 2023, TODAY, with_detail=True),
            2023)
        self.assertEqual(none["headline"], "2023: income unknown")

    def test_the_cumulative_line_counts_the_measured_months(self):
        line = dict(reporting._cumulative_savings(self.conn))
        self.assertEqual(line["2023"], 0.0)
        self.assertEqual(line["2024"], 16000.0)
        self.assertEqual(line["2025"], 40000.0)


class ThereIsNoWholeYearRule(unittest.TestCase):
    def test_nothing_judges_a_year_whole(self):
        self.assertFalse(hasattr(coverage.MeasuredWindow, "year_known"))
        root = pathlib.Path(__file__).resolve().parents[1] / "oikonome"
        for p in root.rglob("*.py"):
            self.assertNotIn("year_known", p.read_text(), p.name)

    def test_both_year_lenses_print_the_servers_note(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        for rel in ("webapp/src/pages/YearLens.tsx",
                    "mobile/src/app/year.tsx"):
            src = (root / rel).read_text()
            self.assertIn("incomplete_text", src, rel)
            self.assertNotIn("the ones with bank data", src, rel)


if __name__ == "__main__":
    unittest.main()
