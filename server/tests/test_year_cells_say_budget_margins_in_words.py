"""On the Year page a sign means money kept or lost, nothing else.

A judged month used to print its margin against the budget as "+$300"
(red, overspent) beside a net month's "+$300" (green, kept), the same
figure meaning opposite things. A judged cell now says "over by $X" or
"under by $X" in words, written by the server; only a net cell is signed.
"""
import datetime as dt
import unittest

from oikonome.web import lenses

from .util import add_txn, freeze_month, make_db, write_config

TODAY = dt.date(2026, 7, 15)


class YearCellMarginWordsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=800, other_monthly=1000)
        for m in (3, 4, 5, 6):
            add_txn(self.conn, dt.date(2026, m, 1), -3000.0, "PAYROLL",
                    primary="INCOME", account="chk")
        add_txn(self.conn, dt.date(2026, 4, 10), 500.0, "HARBOR GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 5, 10), 1500.0, "HARBOR GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 6, 10), 2100.0, "HARBOR GROCER",
                primary="FOOD_AND_DRINK")
        freeze_month(self.conn, 2026, 5)
        freeze_month(self.conn, 2026, 6)
        self.cells = {c["m"]: c for c in lenses.year_summary(
            self.conn, 2026, today=TODAY)["cells"]}

    def test_an_over_month_says_over_by_in_words(self):
        c = self.cells[6]
        self.assertEqual(c["status"], "final")
        self.assertEqual((c["figure"], c["tone"]), ("over by $300", "neg"))

    def test_an_under_month_says_under_by_in_words(self):
        c = self.cells[5]
        self.assertEqual((c["figure"], c["tone"]), ("under by $300", "pos"))

    def test_only_a_net_month_carries_a_sign(self):
        c = self.cells[4]
        self.assertEqual(c["status"], "net")
        self.assertEqual(c["figure"], "+$2,500")
        for m in (5, 6):
            self.assertNotIn("+", self.cells[m]["figure"])
            self.assertNotIn("−", self.cells[m]["figure"])


if __name__ == "__main__":
    unittest.main()
