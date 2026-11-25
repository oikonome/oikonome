"""Cash Flow has one rule for whether a month's income is complete, and
the year list, the month bars and every range's total all read it.

The year list called a year unknown unless every month of it was measured,
while the bars under it and the all-time total counted the year's measured
months: a year read "no data" above eight bars of it. A month the bank
feed does not measure is incomplete everywhere — flagged in the reply,
counted in what came in and went out, and left out of saved and the
savings rate; a year holding one is flagged with the server's words and
saves over its other months."""

import datetime as dt
import pathlib
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import reporting

from .util import (_admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts,
                   write_config)

TODAY = dt.date(2026, 10, 9)


def _months(first: dt.date, last: dt.date):
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        yield dt.date(y, m, 10)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class ACardFromLastYearAndCheckingFromApril(unittest.TestCase):
    """The card spends $3,000 every month from Jan 2023; checking (pay of
    $5,000 a month) is on file from Apr 2024, so May 2024 is the first
    month measured whole. 2023 has no bank data, 2024 eight months of it,
    2025 all twelve."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            self.tid = str(tenancy.create_tenant(
                admin, f"im-{uuid.uuid4().hex[:8]}"))
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
        self.cf = reporting.compute_cashflow(self.conn, TODAY)

    def test_the_reply_flags_the_incomplete_months(self):
        self.assertEqual(self.cf["incomplete_label"], "income incomplete")
        self.assertEqual(
            self.cf["incomplete_months"],
            [f"2023-{m:02d}" for m in range(1, 13)]
            + ["2024-01", "2024-02", "2024-03", "2024-04"])
        # the list every range's total is marked with
        self.assertEqual(
            self.cf["incomplete_months"],
            reporting.flow_breakdown(self.conn, TODAY,
                                     "all")["incomplete_months"])

    def test_a_partly_measured_year_saves_over_its_complete_months(self):
        rows = {r[0]: r for r in self.cf["savings_by_year"]}
        # in and out count every month, as a range's total does; saved
        # and the rate are May-December's
        self.assertEqual(rows["2024"],
                         ["2024", 45000.0, 36000.0, 16000.0, 40.0])
        self.assertEqual(self.cf["savings_by_year_months"]["2024"], [8, 12])
        self.assertEqual(self.cf["savings_by_year_notes"]["2024"],
                         "income incomplete — left over counts 8 of 12 months")

    def test_a_year_with_no_bank_data_keeps_no_saved_figure(self):
        rows = {r[0]: r for r in self.cf["savings_by_year"]}
        self.assertEqual(rows["2023"], ["2023", None, 36000.0, None, None])
        self.assertEqual(self.cf["savings_by_year_notes"]["2023"],
                         "income incomplete — no month has bank data")

    def test_a_wholly_measured_year_is_not_flagged(self):
        rows = {r[0]: r for r in self.cf["savings_by_year"]}
        self.assertEqual(rows["2025"],
                         ["2025", 60000.0, 36000.0, 24000.0, 40.0])
        self.assertEqual(set(self.cf["savings_by_year_notes"]),
                         {"2023", "2024"})

    def test_the_month_bars_keep_a_slot_for_each_incomplete_month(self):
        bars = self.cf["months_by_year"]["2024"]
        self.assertEqual([m for m, _v in bars],
                         ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
                          "Aug", "Sep", "Oct", "Nov", "Dec"])
        self.assertEqual([v for _m, v in bars], [None] * 4 + [2000.0] * 8)
        # a year with no complete month is all slots; the year in
        # progress stops at this month
        self.assertEqual([v for _m, v in self.cf["months_by_year"]["2023"]],
                         [None] * 12)
        self.assertEqual(self.cf["months_by_year"]["2026"][-1][0], "Oct")
        # the complete months only, as installed apps read them
        self.assertEqual(len(self.cf["monthly_by_year"]["2024"]), 8)

    def test_the_bars_and_the_years_add_up_to_the_all_time_saved(self):
        rows = {r[0]: r for r in self.cf["savings_by_year"]}
        for y, bars in self.cf["months_by_year"].items():
            drawn = [v for _m, v in bars if v is not None]
            self.assertEqual(round(sum(drawn), 2), rows[y][3] or 0.0, y)
        flow = reporting.flow_breakdown(self.conn, TODAY, "all")
        self.assertEqual(flow["saved"],
                         sum(r[3] or 0.0 for r in rows.values()))
        self.assertEqual(flow["in"]["total"],
                         sum(r[1] or 0.0 for r in rows.values()))
        graph = self.cf["cash_graph"]
        self.assertEqual(
            [k for k, _v in graph["points"][:graph["forecast_from"]]][0],
            "2024-05")


class BothClientsPrintTheServersWords(unittest.TestCase):
    """The web and the phone print the year's note and name an incomplete
    month with the server's label; neither builds the words or decides
    which month is incomplete."""

    def test_the_note_and_the_label_are_read_from_the_reply(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        for rel in ("webapp/src/pages/CashFlow.tsx",
                    "mobile/src/app/cashflow.tsx"):
            src = (root / rel).read_text()
            for field in ("savings_by_year_notes", "months_by_year",
                          "incomplete_label"):
                self.assertIn(field, src, (rel, field))


if __name__ == "__main__":
    unittest.main()
