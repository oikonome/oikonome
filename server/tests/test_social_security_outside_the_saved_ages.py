"""Social Security at a claim age outside every saved estimate follows
SSA's own adjustment from the nearest saved age, never that age's figure
held flat.

Benefits move about 6-8% for each year a claim is earlier or later, so a
household that saved only its age-67 estimate and claims at 62 must not be
projected at the full age-67 benefit (SSA pays about 70% of it), and a
claim at 70 is more than the age-67 figure. Between two saved ages the
figure stays the straight line."""

import unittest

from oikonome.engine import retirement as retire


class SocialSecurityOutsideTheSavedAges(unittest.TestCase):
    def test_an_early_claim_is_reduced_from_the_nearest_estimate(self):
        self.assertAlmostEqual(retire.ss_at_age({67: 2000}, 62), 1400.0)
        # 24 months early: 5/9 % a month
        self.assertAlmostEqual(retire.ss_at_age({67: 3000}, 65), 2600.0)

    def test_a_delayed_claim_earns_the_delayed_credit(self):
        self.assertAlmostEqual(retire.ss_at_age({62: 1400, 67: 2000}, 70),
                               2480.0)

    def test_full_retirement_age_follows_the_birth_year(self):
        # born 1957: full retirement age 66 and 6 months
        self.assertAlmostEqual(
            retire.ss_at_age({70: 2400}, 67, birth_year=1957),
            2400 * (1 + 6 * 2 / 300) / (1 + 42 * 2 / 300))

    def test_inside_the_saved_ages_is_the_straight_line(self):
        self.assertAlmostEqual(retire.ss_at_age({62: 1400, 67: 2000}, 64),
                               1640.0)
        self.assertEqual(retire.ss_at_age({}, 62), 0.0)




class SavedAgesOutsideTheClaimRange(unittest.TestCase):
    """A restored config can carry an estimate at an age SSA never pays at;
    it must not divide by a zero factor or print a negative benefit."""

    def test_an_estimate_outside_62_to_70_is_ignored(self):
        from oikonome.engine import retirement as retire
        for sched in ({48: 2000.0}, {30: 2000.0}, {-10**400: 2000.0}):
            self.assertEqual(retire.ss_at_age(sched, 67, 1960), 0.0)
        self.assertEqual(retire.ss_at_age({48: 9.0, 67: 2000.0}, 67, 1960),
                         2000.0)


if __name__ == "__main__":
    unittest.main()
