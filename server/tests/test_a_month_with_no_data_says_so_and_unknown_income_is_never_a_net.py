"""A month with no data says it has none, and a month whose income the
bank feed does not measure never prints a figure under the word "net".

A closed month with no row in it read as a verdict with a row of zeros on
the Month view and as "net $0" in the Year grid. A month with card
spending and no bank feed printed its spend under "net". The server now
says what each is: the month's standing ("no_data"), the words "income
incomplete" on the Month view and on the Year cell, and each cell's
label, figure and tone.
"""
import datetime as dt
import unittest

from oikonome.engine import reporting
from oikonome.web import lenses

from .util import add_txn, freeze_month, make_db, write_config

TODAY = dt.date(2026, 7, 15)


class NoDataAndUnknownIncomeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=800, other_monthly=1000)
        # card spending from March; the bank feed opens in May (a covered
        # month with no deposit) and pays in from June
        for m in (3, 4, 5, 6):
            add_txn(self.conn, dt.date(2026, m, 10), 400.0, "HARBOR GROCER",
                    primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 5, 1), 50.0, "CITY WATER",
                primary="RENT_AND_UTILITIES", account="chk")
        add_txn(self.conn, dt.date(2026, 6, 1), -3000.0, "PAYROLL",
                primary="INCOME", account="chk")
        freeze_month(self.conn, 2026, 6)
        self.cells = {c["m"]: c for c in lenses.year_summary(
            self.conn, 2026, today=TODAY)["cells"]}

    def _month(self, m, y=2026):
        return lenses.month_summary(self.conn, y, m, today=TODAY)

    def test_a_month_with_no_rows_says_no_data(self):
        d = self._month(2)
        self.assertEqual(d["standing"], "no_data")
        self.assertEqual(d["standing_label"], "no data")
        self.assertEqual(d["standing_text"], "No data for this month.")
        self.assertIsNone(d["verdict"])
        self.assertIsNone(d["income_text"])
        self.assertEqual((d["plan_rows"], d["by_category"], d["biggest"]),
                         ([], [], []))

    def test_its_year_cell_says_no_data_and_prints_no_figure(self):
        c = self.cells[2]
        self.assertEqual((c["label"], c["figure"], c["tone"], c["note"]),
                         ("no data", None, "mut", None))
        # a year with no rows at all, the same
        cells = lenses.year_summary(self.conn, 2019, today=TODAY)["cells"]
        self.assertEqual({c["label"] for c in cells}, {"no data"})
        self.assertEqual({c["figure"] for c in cells}, {None})

    def test_unknown_income_reads_spent_never_net(self):
        c = self.cells[4]
        self.assertIsNone(c["net"])
        self.assertEqual((c["label"], c["figure"], c["tone"]),
                         ("spent", "$400", "mut"))
        self.assertEqual(c["note"], reporting.INCOME_INCOMPLETE)

    def test_the_month_view_says_income_incomplete(self):
        d = self._month(4)
        self.assertEqual(d["standing"], "before_budget")
        self.assertIsNone(d["income"]["actual"])
        self.assertIsNone(d["net"])
        self.assertEqual(d["income_text"], reporting.INCOME_INCOMPLETE)

    def test_a_measured_month_keeps_its_signed_net(self):
        c = self.cells[5]            # covered, no deposit, 450 out
        self.assertEqual((c["label"], c["figure"], c["tone"], c["note"]),
                         ("net", "−$450", "neg", None))
        self.assertIsNone(self._month(5)["income_text"])

    def test_a_verdict_cell_carries_its_margin_and_the_note(self):
        june = self.cells[6]
        self.assertEqual(june["status"], "final")
        self.assertEqual((june["label"], june["figure"], june["note"]),
                         (None, "under by $1,400", None))
        # a judged month the feed does not measure: verdict, and the note
        freeze_month(self.conn, 2026, 4)
        april = {c["m"]: c for c in lenses.year_summary(
            self.conn, 2026, today=TODAY)["cells"]}[4]
        self.assertEqual(april["status"], "final")
        self.assertEqual((april["label"], april["note"]),
                         (None, reporting.INCOME_INCOMPLETE))

    def test_a_future_month_has_no_words(self):
        c = self.cells[9]
        self.assertEqual((c["label"], c["figure"], c["note"]),
                         (None, None, None))


class RowsTheFiguresDoNotCountAreNoDataTests(unittest.TestCase):
    """A month whose only rows are ones no figure counts (an account the
    household excluded) has no data, on the Month
    view and in the Year grid, the same as a month with no row at all."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('old','it1','Old Card','credit',"
            "'credit card',0)")
        write_config(self.conn, food_monthly=800, other_monthly=1000,
                     excluded_accounts=["old"])
        add_txn(self.conn, dt.date(2026, 2, 10), 120.0, "HARBOR GROCER",
                primary="FOOD_AND_DRINK", account="old")
        # a month with a counted row still reads as from before the budget
        add_txn(self.conn, dt.date(2026, 4, 10), 70.0, "HARBOR GROCER",
                primary="FOOD_AND_DRINK", account="card")

    def test_the_month_view_and_the_year_cell_say_no_data(self):
        cells = {c["m"]: c for c in lenses.year_summary(
            self.conn, 2026, today=TODAY)["cells"]}
        for m in (2,):
            d = lenses.month_summary(self.conn, 2026, m, today=TODAY)
            self.assertEqual(d["standing"], "no_data", m)
            self.assertEqual(cells[m]["label"], "no data", m)
        self.assertEqual(lenses.month_summary(
            self.conn, 2026, 4, today=TODAY)["standing"], "before_budget")


if __name__ == "__main__":
    unittest.main()
