"""Cash Flow: state the conclusion; charts pan and zoom.

The report page never said what it concluded — you read the answer off a
chart. And its toplists were all-time, which a spending report should not be:
an all-time list is dominated by whatever you spent most on since the ledger
began, and the point of the page is behaviour you can still change.
"""

import datetime as dt
import pathlib
import unittest

import oikonome
from oikonome.engine import reporting

from .util import TODAY, _ensure_db, add_txn, make_db, write_config


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class TrailingTwelveMonthsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_toplists_drop_what_is_older_than_a_year(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=40), 300.0,
                "RECENT STORE", primary="GENERAL_MERCHANDISE")
        add_txn(self.conn, TODAY - dt.timedelta(days=800), 9000.0,
                "ANCIENT MORTGAGE CO", primary="RENT_AND_UTILITIES")
        rep = reporting.compute_spending(self.conn)
        merchants = {m[0] for m in rep["top_merchants"]}
        self.assertIn("RECENT STORE", merchants)
        self.assertNotIn("ANCIENT MORTGAGE CO", merchants,
                         "an all-time toplist buries this year under history")

    def test_the_year_over_year_axis_stays_all_time(self):
        """The displayed toplist narrowed; the MATRIX axis must not, or a
        category that was large for years vanishes from the one table you
        would consult to see exactly that."""
        add_txn(self.conn, TODAY - dt.timedelta(days=800), 9000.0,
                "ANCIENT MORTGAGE CO", primary="RENT_AND_UTILITIES")
        rep = reporting.compute_spending(self.conn)
        self.assertIn("RENT AND UTILITIES", rep["yoy_cats"],
                      "the matrix axis must still carry it")

    def test_by_year_keeps_the_long_view(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=800), 9000.0,
                "ANCIENT MORTGAGE CO", primary="RENT_AND_UTILITIES")
        rep = reporting.compute_spending(self.conn)
        self.assertTrue(any(r[1] >= 9000 for r in rep["by_year"]),
                        "spend-by-year is where the long view lives")


class ChartInteractionTests(unittest.TestCase):
    def setUp(self):
        try:
            self.chart = _src("components/AreaChart.tsx")
            self.page = _src("pages/CashFlow.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_charts_carry_no_zoom_machinery(self):
        """Chart zoom was removed site-wide in favor of the one RangePicker
        vocabulary — 3m/6m/1y/3y/5y/All. A wheel
        listener creeping back would also re-open the passive-listener trap:
        React attaches wheel passively, so preventDefault() is ignored and
        the page scrolls while the chart zooms under it."""
        self.assertNotIn("onWheel", self.chart)
        self.assertNotIn('addEventListener("wheel"', self.chart)
        self.assertNotIn("zoomable", self.chart)
        self.assertNotIn("zoomable", self.page)
        # the hover crosshair survives the removal
        self.assertIn("onMouseMove={onMove}", self.chart)

    def test_the_income_tab_keeps_its_windowed_view(self):
        # The Income tab's living half is the windowed view: the pace line
        # plus WHO paid ("Income by source"), in the flow picture's income
        # blue — not an all-history line.
        self.assertIn("Income by source", self.page)
        self.assertIn("#4f9bd6", self.page)


class HeroAndLegendTests(unittest.TestCase):
    def setUp(self):
        try:
            self.page = _src("pages/CashFlow.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_chart_legend_is_gone(self):
        self.assertNotIn("actual (income − spend, per month)", self.page)
        self.assertNotIn("forecast (bills + income + pace, same engine as Today)",
                         self.page)

    def test_the_table_instruction_line_is_gone(self):
        self.assertNotIn("Click a year to see its month-by-month", self.page,
                         "the chevron is the affordance")

    def test_no_data_is_words_not_a_tooltip_dot(self):
        self.assertNotIn('title="no bank data for this year"', self.page)
        self.assertIn("no data", self.page)

    def test_a_negative_rate_is_red_too(self):
        self.assertIn('rate !== null && rate < 0 ? "neg" : ""', self.page)
