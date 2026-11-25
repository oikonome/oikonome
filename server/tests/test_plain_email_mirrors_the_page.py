"""The plain-text email is the mirror the HTML email and the page are:
the same money-map rows with the same words beside them (Today's pace
basis, the one bills-still-due figure), the recent rows in the page's
order, the non-monthly bills on a line of their own, and "×" for a count.
It used to print its own table — "Fixed bills (info) posted / awaiting /
month cash" and expected-basis deltas — that stated a different bills
figure and different row figures from the HTML beside it. Figures are
invented."""

import datetime as dt
import unittest

from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config


class PlainMirrorTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=300, other_monthly=400,
                     budgeted_income_monthly=5000)
        for day in (2, 6, 9):
            add_txn(self.conn, dt.date(2026, 7, day), 95.0, "CORNER GROCER",
                    primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 7, 4), 520.0, "OAKWOOD FURNITURE")
        add_txn(self.conn, TODAY, 12.0, "CORNER CAFE",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 80.0, "PINE OUTFITTERS")
        add_bill(self.conn, "VALLEY POWER", 300.0,
                 next_due=dt.date(2026, 7, 25),
                 last_seen=dt.date(2026, 6, 25))
        add_bill(self.conn, "HARBOR INSURANCE", 150.0,
                 next_due=dt.date(2026, 7, 10),
                 last_seen=dt.date(2026, 6, 10))
        add_bill(self.conn, "NORTHWIND INSURANCE", 240.0, frequency="YEARLY",
                 next_due=dt.date(2026, 7, 28),
                 last_seen=dt.date(2025, 7, 28))
        self.st = report.gather(self.conn, TODAY)
        self.ctx = todayview.build_context(self.st)
        _s, self.plain, self.html = report.build(self.st)

    def tearDown(self):
        self.conn.close()

    def test_the_rows_carry_the_htmls_words(self):
        rows = [r for r in self.ctx["map"]["rows"] if r["delta"]]
        self.assertTrue(any(r["label"] == "Bills" for r in rows))
        for r in rows:
            self.assertIn(r["delta"]["text"], self.html)
            line = next(l for l in self.plain.splitlines()
                        if l.strip().startswith(r["label"] + " $"))
            self.assertTrue(line.endswith(" — " + r["delta"]["text"]), line)
        self.assertIn("over pace", self.plain)
        self.assertIn("$690 still due", self.plain)
        self.assertNotIn("awaiting)", self.plain)
        for gone in ("Fixed bills (info)", "month cash", "expected "):
            self.assertNotIn(gone, self.plain)

    def test_recent_rows_come_in_the_pages_order(self):
        # the page lists a day's rows by the larger amount first
        lines = self.plain.split("Recent transactions")[1].splitlines()
        big = next(i for i, l in enumerate(lines) if "PINE OUTFITTERS" in l)
        small = next(i for i, l in enumerate(lines) if "CORNER CAFE" in l)
        self.assertLess(big, small)
        tl = self.html.split("Cash timeline")[1]
        self.assertLess(tl.index("PINE OUTFITTERS"), tl.index("CORNER CAFE"))

    def test_counts_and_the_non_monthly_line(self):
        self.assertEqual(self.st["verdict"], "OVER BUDGET")
        chips = self.ctx["day"]["why_chips"]
        self.assertTrue(any(c["count"] > 1 for c in chips))
        drive = next(l for l in self.plain.splitlines()
                     if l.startswith("Driving it:"))
        self.assertIn("×)", drive)
        self.assertNotIn("x)", drive)
        self.assertIn("Non-monthly bills this month: NORTHWIND INSURANCE",
                      self.plain)
        self.assertIn("Non-monthly bills this month: NORTHWIND INSURANCE",
                      self.html)
        self.assertNotIn("· non-monthly bills:", self.html)


if __name__ == "__main__":
    unittest.main()
