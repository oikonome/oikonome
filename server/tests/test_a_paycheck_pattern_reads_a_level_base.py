"""Last year's check at today's point is the base a paycheck pattern is
read against (specs/today-figures.md, "A paycheck follows last year's
pattern"), so it must be a level check: one bonus check or a one-check
dip there is no level, and every walked payday would be scaled by it.
A base that no neighbour agrees with is read as the median of the checks
around it. Figures and names are invented."""

import datetime as dt
import unittest

from oikonome.engine import forecast

D = dt.date


def biweekly(first: D, last: D, amount_on):
    out, d = [], first
    while d <= last:
        out.append({"date": d, "amount": float(amount_on(d))})
        d += dt.timedelta(days=14)
    return out


class TheBaseIsALevel(unittest.TestCase):
    amount = staticmethod(forecast.paycheck_pattern_amount)

    def test_a_bonus_check_at_todays_point_last_year_scales_nothing(self):
        deps = biweekly(D(2024, 12, 13), D(2026, 12, 11),
                        lambda d: 4500.0 if d == D(2025, 12, 12) else 3000.0)
        today = D(2026, 12, 11)
        for pd in (D(2026, 12, 25), D(2027, 1, 8), D(2027, 1, 22),
                   D(2027, 2, 5), D(2027, 2, 19)):
            self.assertEqual(self.amount(3000.0, deps, pd, today),
                             (3000.0, False), pd)

    def test_a_one_check_dip_at_todays_point_last_year_scales_nothing(self):
        deps = biweekly(D(2024, 12, 13), D(2027, 1, 8),
                        lambda d: 2850.0 if d == D(2026, 1, 9) else 3000.0)
        today = D(2027, 1, 9)
        for pd in (D(2027, 1, 22), D(2027, 2, 5), D(2027, 3, 5),
                   D(2027, 3, 19)):
            self.assertEqual(self.amount(3000.0, deps, pd, today),
                             (3000.0, False), pd)

    def test_a_step_still_reads_when_the_base_is_beside_a_bonus(self):
        # a level base with a bonus check one payday after it: last
        # year's step from $3,000 to $3,300 in March still carries
        deps = biweekly(D(2024, 12, 13), D(2026, 12, 11),
                        lambda d: (4500.0 if d == D(2025, 12, 26) else
                                   3300.0 if D(2026, 3, 1) <= d < D(2026, 12, 1)
                                   else 3000.0))
        self.assertEqual(
            self.amount(3000.0, deps, D(2027, 3, 19), D(2026, 12, 11)),
            (3300.0, True))


if __name__ == "__main__":
    unittest.main()
