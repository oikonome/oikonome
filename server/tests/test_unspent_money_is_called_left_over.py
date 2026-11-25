"""Cash Flow's Overview calls income less spending "Left over" in the
picture, the sentence and the chart, and says what moved to investments on
a line of its own (specs/counted-figures.md). The picture called the figure
"Invested & saved" beside a page that reports a different amount as
invested.

The same figure keeps that one name below the chart and off the page: the
year table and its column, the words under a year with incomplete income,
the Year view's totals on web and phone, and the short yearly message. The
percentage is still the savings rate.
"""
import datetime as dt
import pathlib
import unittest

from oikonome.engine import reporting

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)
_ROOT = pathlib.Path(__file__).resolve().parents[2]


class MovedToInvestmentsIsItsOwnLine(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for month in (8, 9, 10):
            add_txn(self.conn, dt.date(2026, month, 2), -3200.0,
                    "CLIENT DEPOSIT", account="chk", primary="INCOME")
            add_txn(self.conn, dt.date(2026, month, 9), 1000.0, "Grocer",
                    account="chk", primary="FOOD_AND_DRINK")

    def tearDown(self):
        self.conn.close()

    def _move(self, day, amount):
        add_txn(self.conn, day, amount, "VANGUARD BUY INVESTMENT",
                account="chk", primary="TRANSFER_OUT")

    def test_the_windows_net_transfers_are_the_line(self):
        self._move(dt.date(2026, 8, 15), 800.0)
        self._move(dt.date(2026, 9, 15), 800.0)
        self._move(dt.date(2026, 10, 15), 800.0)
        # outside the window: not this window's money
        self._move(dt.date(2026, 5, 15), 5000.0)
        flow = reporting.flow_breakdown(self.conn, TODAY, "3m")
        self.assertEqual(flow["saved"], 6600.0)
        self.assertEqual(flow["invested"], 2400.0)
        self.assertEqual(flow["invested_text"],
                         "moved to investments $2,400")

    def test_it_is_the_records_figure_for_the_same_days(self):
        self._move(dt.date(2026, 8, 15), 800.0)
        self._move(dt.date(2026, 10, 15), -300.0)
        year = dict(map(tuple, reporting._investment_funding(self.conn)))
        self.assertEqual(
            reporting.investment_funding_between(
                self.conn, dt.date(2026, 1, 1), dt.date(2027, 1, 1)),
            year["2026"])

    def test_a_net_withdrawal_says_so_and_nothing_moved_says_nothing(self):
        self.assertIsNone(
            reporting.flow_breakdown(self.conn, TODAY, "3m")["invested_text"])
        self._move(dt.date(2026, 9, 15), -900.0)
        self.assertEqual(
            reporting.flow_breakdown(self.conn, TODAY, "3m")["invested_text"],
            "took out of investments $900")


class TheFigureHasOneName(unittest.TestCase):
    def test_band_sentence_and_chart_say_left_over(self):
        web_page = (_ROOT / "webapp/src/pages/CashFlow.tsx").read_text()
        web_band = (_ROOT / "webapp/src/components/CashFlowCharts.tsx"
                    ).read_text()
        mob_page = (_ROOT / "mobile/src/app/cashflow.tsx").read_text()
        mob_band = (_ROOT / "mobile/src/lib/flowlayout.ts").read_text()
        for band in (web_band, mob_band):
            self.assertTrue('label: "Left over"' in band)
            self.assertFalse("Invested & saved" in band)
        for page in (web_page, mob_page):
            self.assertTrue('"left over" : "overspent"' in page)
            self.assertFalse('"stayed"' in page)
            self.assertTrue("Left over, by month" in page)
            self.assertFalse("Saved, by month" in page)
            self.assertTrue("flow.invested_text" in page)

    def test_the_year_table_says_left_over(self):
        for rel in ("webapp/src/pages/CashFlow.tsx",
                    "mobile/src/app/cashflow.tsx"):
            page = (_ROOT / rel).read_text()
            self.assertIn("Left over by year", page, rel)
            self.assertNotIn("Savings by year", page, rel)
        web = (_ROOT / "webapp/src/pages/CashFlow.tsx").read_text()
        self.assertIn('<th className="num">Left over</th>', web)
        self.assertNotIn('<th className="num">Saved</th>', web)
        mob = (_ROOT / "mobile/src/app/cashflow.tsx").read_text()
        self.assertIn("<Text style={s.tHead}>Left over</Text>", mob)
        self.assertNotIn("<Text style={s.tHead}>Saved</Text>", mob)

    def test_the_year_view_says_left_over_and_keeps_the_savings_rate(self):
        web = (_ROOT / "webapp/src/pages/YearLens.tsx").read_text()
        self.assertIn('<div className="sub">left over</div>', web)
        self.assertNotIn('<div className="sub">saved</div>', web)
        self.assertIn('<div className="sub">savings rate</div>', web)
        mob = (_ROOT / "mobile/src/app/year.tsx").read_text()
        self.assertIn('<KV k="Left over"', mob)
        self.assertNotIn('<KV k="Saved"', mob)
        self.assertIn('<KV k="Savings rate"', mob)

    def test_the_servers_words_say_left_over(self):
        from oikonome.notify import render
        from oikonome.web import lenses
        self.assertEqual(
            reporting.income_incomplete_note(8, 12),
            "income incomplete — left over counts 8 of 12 months")
        self.assertEqual(
            lenses.year_unknown_text(2026),
            "No month of 2026 so far has bank data — income and what was "
            "left over are unknown, not $0.")
        # a closed year is not "so far"
        self.assertEqual(
            lenses.year_unknown_text(2008, closed=True),
            "No month of 2008 had bank data — income and what was "
            "left over are unknown, not $0.")
        cells = [{"m": m, "status": "net", "income": None, "spend": 10.0}
                 for m in range(1, 13)]
        self.assertNotIn("so far", lenses.cell_totals(
            cells, year=2008, closed=True)["unknown_text"])
        self.assertEqual(
            render.short_yearly(
                {"cells": [], "totals": {"spend": 30000.0, "income": 36000.0,
                                         "saved": 6000.0, "rate": 16.7}},
                2026).splitlines()[1],
            "Spent $30,000 on $36,000 income — $6,000 left over (17%).")


if __name__ == "__main__":
    unittest.main()
