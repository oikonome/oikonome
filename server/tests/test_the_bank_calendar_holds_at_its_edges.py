"""The bank calendar (engine/business_days.py) reads only holidays that
existed in the year asked — Juneteenth is a bank holiday from 2021 on —
and never overflows at the ends of the calendar: a restored date at the
last or first representable day rolls to itself, not to an error."""

import datetime as dt
import unittest

from oikonome.engine import business_days

D = dt.date


class Juneteenth(unittest.TestCase):
    def test_a_holiday_from_2021_on(self):
        self.assertIn(D(2022, 6, 20), business_days.bank_holidays(2022))
        self.assertIn(D(2026, 6, 19), business_days.bank_holidays(2026))

    def test_no_holiday_before_2021(self):
        self.assertTrue(business_days.is_business_day(D(2020, 6, 19)))
        self.assertTrue(business_days.is_business_day(D(2019, 6, 19)))


class TheEdges(unittest.TestCase):
    def test_the_last_day_rolls_to_itself(self):
        self.assertEqual(business_days.add_business_days(dt.date.max, 3),
                         dt.date.max)
        self.assertEqual(business_days.next_business_day(dt.date.max),
                         dt.date.max)
        self.assertEqual(
            business_days.next_business_day(D(9999, 12, 25)), D(9999, 12, 27))

    def test_the_first_day_rolls_to_itself(self):
        self.assertEqual(business_days.previous_business_day(dt.date.min),
                         dt.date.min)


if __name__ == "__main__":
    unittest.main()
