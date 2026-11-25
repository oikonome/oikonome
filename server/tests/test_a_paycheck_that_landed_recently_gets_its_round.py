"""A paycheck that landed within the last few business days, with no round
in the household's history to measure from, still gets its card round
(forecast.landed_paydays): the reach counts back business days from
today, so yesterday's paycheck is inside it and one from two weeks ago is
not. Dates and names are invented."""

import datetime as dt
import unittest

from oikonome.engine import forecast

from .util import make_db

D = dt.date


class TheReachCountsBack(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _landed(self, paid_on, today):
        claims = {"Lantern Works Payroll": [{"date": paid_on,
                                             "amount": 1800.0}]}
        return forecast.landed_paydays(self.conn, claims,
                                       {"last_round": None}, today)

    def test_yesterdays_paycheck_is_inside_the_reach(self):
        # Tuesday, a paycheck on Monday
        self.assertEqual(self._landed(D(2031, 3, 10), D(2031, 3, 11)),
                         {D(2031, 3, 10)})

    def test_one_five_business_days_back_is_inside_it(self):
        # Tuesday: five business days back is the Tuesday before
        self.assertEqual(self._landed(D(2031, 3, 4), D(2031, 3, 11)),
                         {D(2031, 3, 4)})

    def test_one_two_weeks_back_is_outside_it(self):
        self.assertEqual(self._landed(D(2031, 2, 25), D(2031, 3, 11)),
                         set())


if __name__ == "__main__":
    unittest.main()
