"""Spending per day divides by the days the window has actually run.

A listing read on a past day stops counting on that day, a closed window
counts every day, a window that has not begun or has nothing spent carries
no figure.
"""

import datetime as dt
import unittest

from oikonome.web.api import _per_day

MAR1, MAR31 = dt.date(2025, 3, 1), dt.date(2025, 3, 31)


class PerDayWindowTests(unittest.TestCase):
    def test_a_past_day_of_the_month_stops_counting_on_that_day(self):
        self.assertEqual(_per_day(120.0, MAR1, MAR31, dt.date(2025, 3, 12)),
                         (10.0, 12))

    def test_a_closed_window_counts_every_day(self):
        self.assertEqual(_per_day(310.0, MAR1, MAR31, dt.date(2025, 5, 1)),
                         (10.0, 31))

    def test_a_window_that_has_not_begun_has_no_figure(self):
        self.assertEqual(_per_day(50.0, MAR1, MAR31, dt.date(2025, 2, 27)),
                         (None, None))

    def test_nothing_spent_or_no_start_has_no_figure(self):
        self.assertEqual(_per_day(0, MAR1, MAR31, MAR31), (None, None))
        self.assertEqual(_per_day(40.0, None, None, MAR31), (None, None))

    def test_an_open_ended_range_runs_to_today(self):
        self.assertEqual(_per_day(30.0, "2025-03-29", None, MAR31), (10.0, 3))


if __name__ == "__main__":
    unittest.main()
