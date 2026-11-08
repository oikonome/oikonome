"""The Year page reads income for its own year and the one before, not the
household's whole history; a bounded year's totals are the same figures the
unbounded read gives."""
import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import add_txn, make_db, write_config


class TheYearPageReadsTwoYears(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        for y in (2019, 2022, 2025, 2026):
            for m in (1, 6, 12):
                add_txn(self.conn, dt.date(y, m, 5), -3100.0 - m,
                        "NORTHWIND PAYROLL", primary="INCOME", account="chk")
        self.conn.commit()

    def test_bounded_years_equal_the_whole_history(self):
        all_y, all_m = reporting._income_year_month(self.conn)
        y, m = reporting._income_year_month(
            self.conn, dt.date(2025, 1, 1), dt.date(2027, 1, 1))
        self.assertEqual(set(y), {"2025", "2026"})
        for k in y:
            self.assertEqual(y[k], all_y[k])
        for k in m:
            self.assertEqual(m[k], all_m[k])
        self.assertNotIn("2019", y)


if __name__ == "__main__":
    unittest.main()
